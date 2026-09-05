"""Run with GTK 3/PyGObject: xvfb-run -a python3 -m unittest discover -s tests -v."""

import builtins
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


# Load the addon with real GTK widgets, without needing a running KlipperScreen.
screen_panel = ModuleType("ks_includes.screen_panel")
screen_panel.ScreenPanel = object
autogrid = ModuleType("ks_includes.widgets.autogrid")
autogrid.AutoGrid = Gtk.Grid
spec = importlib.util.spec_from_file_location(
    "afc_panel", Path(__file__).resolve().parents[1] / "KlipperScreen" / "AFC.py"
)
afc = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {
    "ks_includes.screen_panel": screen_panel,
    "ks_includes.widgets.autogrid": autogrid,
}):
    spec.loader.exec_module(afc)


class SpoolmanTests(unittest.TestCase):
    def setUp(self):
        self.translation = patch.object(builtins, "_", lambda text: text, create=True)
        self.translation.start()
        self.addCleanup(self.translation.stop)
        self.panel = panel = afc.Panel.__new__(afc.Panel)
        panel._screen = Mock(width=800)
        panel._screen._ws.send_method.return_value = True
        panel._printer = SimpleNamespace(state="ready")
        panel._gtk = SimpleNamespace(
            Button=lambda icon, label, *args, **kwargs: Gtk.Button(label=label),
            ScrolledWindow=Gtk.ScrolledWindow,
        )
        panel.labels = {}
        panel.buttons = {}
        panel.screen_stack = Gtk.Stack()
        panel.screen_stack.add_named(Gtk.Grid(), "main_grid")
        panel.spoolman_request = 0
        panel.spoolman_pending = False
        panel.status_pending = False
        panel.spoolman = "http://spoolman:7912"
        panel.afc_lane_data = [SimpleNamespace(name="lane1", spool_id=7),
                               SimpleNamespace(name="lane2", spool_id=8)]
        panel.show_selector_grid(None, panel.afc_lane_data[0])

    def reply(self, payload):
        self.panel.spoolman_spools_received(payload, None, None, self.panel.spoolman_request)

    def load(self):
        self.reply({"result": {"response": [
            {"id": 7, "filament": {"name": "<Blue & white>", "material": "PLA",
                                   "vendor": {"name": "Vendor"}}, "remaining_weight": 0},
            {"id": 8, "filament": {"vendor": None}},
            {"id": 9, "filament": None},
            {"id": 10, "archived": True},
            {"id": "bad"}, None,
        ], "error": None}})

    def test_proxy_contract_and_real_widgets(self):
        method, params, _, request = self.panel._screen._ws.send_method.call_args.args
        self.assertEqual(method, "server.spoolman.proxy")
        self.assertEqual(params, {"request_method": "GET", "path": "/v1/spool",
                                  "query": "allow_archived=false", "use_v2_response": True})
        self.assertEqual(request, self.panel.spoolman_request)
        self.assertFalse(self.panel.buttons["spoolman_assign"].get_sensitive())
        self.load()
        self.assertEqual([row[0] for row in self.panel.spoolman_model], [7, 8, 9])
        self.assertIn("<Blue & white>", self.panel.spoolman_model[0][1])
        self.assertIn("0 g", self.panel.spoolman_model[0][1])
        self.assertIn("lane2", self.panel.spoolman_model[1][1])

    def test_empty_and_error_responses(self):
        self.reply({"result": {"response": [], "error": None}})
        self.assertEqual(self.panel.labels["spoolman_status"].get_text(), "No spools found.")
        for response in (None, {}, {"error": {}}, {"result": {"error": {"status_code": 503}}},
                         {"result": {"response": {}}}):
            self.reply(response)
            self.assertIn("Unable to load", self.panel.labels["spoolman_status"].get_text())
            self.assertTrue(self.panel.buttons["spoolman_refresh"].get_sensitive())

    def test_disconnected_fetch_can_retry(self):
        self.panel._screen._ws.send_method.return_value = False
        self.panel.load_spoolman_spools()
        self.assertFalse(self.panel.spoolman_pending)
        self.assertIn("Unable to load", self.panel.labels["spoolman_status"].get_text())

    def test_assignment_only_sends_afc_command(self):
        self.load()
        self.panel.spoolman_tree.get_selection().select_path(2)
        self.assertTrue(self.panel.buttons["spoolman_assign"].get_sensitive())
        self.panel._screen._ws.send_method.reset_mock()
        self.panel.assign_spoolman_lane(None)
        self.assertEqual(self.panel._screen._ws.send_method.call_args.args[:2], (
            "printer.gcode.script", {"script": "SET_SPOOL_ID LANE=lane1 SPOOL_ID=9"}))
        self.panel.assign_spoolman_lane(None)
        self.assertEqual(self.panel._screen._ws.send_method.call_count, 1)
        self.assertEqual(self.panel.selected_lane.spool_id, 7)
        self.panel.request_afc_status = Mock()
        self.panel.spoolman_lane_updated({"result": "ok"}, None, None, self.panel.spoolman_request)
        self.panel.request_afc_status.assert_called_once_with(self.panel.refresh_afc_status)
        self.assertEqual(self.panel.selected_lane.spool_id, 7)

    def test_clear_and_command_error(self):
        self.load()
        self.panel.clear_spoolman_lane(None)
        self.assertEqual(self.panel._screen._ws.send_method.call_args.args[1],
                         {"script": 'SET_SPOOL_ID LANE=lane1 SPOOL_ID=""'})
        self.panel.spoolman_lane_updated({"error": {"message": "offline"}}, None, None,
                                        self.panel.spoolman_request)
        self.assertFalse(self.panel.spoolman_pending)
        self.assertIn("Unable to update", self.panel.labels["spoolman_status"].get_text())

    def test_printing_blocks_mutations(self):
        self.load()
        self.panel.spoolman_tree.get_selection().select_path(0)
        self.panel._printer.state = "printing"
        self.panel.enable_buttons(False)
        self.assertFalse(self.panel.buttons["spoolman_assign"].get_sensitive())
        self.assertFalse(self.panel.buttons["spoolman_clear"].get_sensitive())
        self.panel._screen._ws.send_method.reset_mock()
        self.panel.assign_spoolman_lane(None)
        self.panel.clear_spoolman_lane(None)
        self.panel._screen._ws.send_method.assert_not_called()

    def test_old_reply_cannot_populate_another_lane(self):
        request = self.panel.spoolman_request
        self.panel.show_main_grid(None)
        self.panel.show_selector_grid(None, self.panel.afc_lane_data[1])
        self.panel.spoolman_spools_received({"result": {"response": [{"id": 99}]}}, None, None, request)
        self.assertEqual(len(self.panel.spoolman_model), 0)
        self.assertTrue(self.panel.spoolman_pending)
        self.assertEqual(self.panel.selected_lane.name, "lane2")
        request = self.panel.spoolman_request
        self.panel.deactivate()
        self.panel.spoolman_spools_received(None, None, None, request)
        self.assertTrue(self.panel.spoolman_pending)

    def test_disconnected_assignment_can_retry(self):
        self.load()
        self.panel._screen._ws.send_method.return_value = False
        self.panel.clear_spoolman_lane(None)
        self.assertFalse(self.panel.spoolman_pending)
        self.assertTrue(self.panel.buttons["spoolman_clear"].get_sensitive())
        self.assertIn("Unable to update", self.panel.labels["spoolman_status"].get_text())

    def test_selector_fits_small_landscape_screen(self):
        self.load()
        self.panel.spoolman_tree.get_column(0).get_cells()[0].set_property("wrap-width", 400)
        window = Gtk.Window()
        self.addCleanup(window.destroy)
        window.set_default_size(480, 320)
        window.add(self.panel.screen_stack)
        window.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration()
        self.assertEqual(tuple(window.get_size()), (480, 320))

    def test_manual_editor_without_spoolman(self):
        self.panel.spoolman = None
        self.panel.selector_grid = Gtk.Grid()
        self.panel.screen_stack.add_named(self.panel.selector_grid, "selector_grid")
        self.panel.create_spool_layout()
        lane = SimpleNamespace(name="lane1", color=None, material="PLA", weight=500)
        self.panel.show_selector_grid(None, lane)
        self.assertEqual(self.panel.labels["type_input"].get_text(), "PLA")
        self.assertEqual(self.panel.labels["weight_input"].get_text(), "500")


if __name__ == "__main__":
    unittest.main()
