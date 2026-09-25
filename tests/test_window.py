"""Main window tests, offscreen: the layout switch and what it carries.

The sensor worker is started and stopped as it would be for real, but the
window is fed a synthetic sample directly rather than waiting on it.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings                        # noqa: E402
from PyQt6.QtWidgets import QApplication                  # noqa: E402

from ksyssupervisor import window as window_mod           # noqa: E402
from ksyssupervisor.providers import Sample               # noqa: E402
from ksyssupervisor.providers.base import Reading         # noqa: E402
from ksyssupervisor.treeview import SensorTree            # noqa: E402
from ksyssupervisor.tileboard import TileBoard            # noqa: E402
from ksyssupervisor.widgets import Backdrop               # noqa: E402


def sample(temp=61.5):
    return Sample(backend="sysfs", readings=[
        Reading("cpu", "Temperatures", "Tctl", "Package", temp, "temp"),
        Reading("cpu", "Utilization", "usage_total", "Processor", 42.0,
                "utilization"),
    ])


class WindowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # The offscreen screen is 800x800, which would clamp every default.
        # The window binds the name at import, so it is stubbed there.
        original = window_mod.fit_on_screen
        window_mod.fit_on_screen = lambda w, a, b, fraction=0.9: (a, b)
        self.addCleanup(setattr, window_mod, "fit_on_screen", original)
        self.settings = QSettings("KSysSupervisorTest", "Window")
        self.settings.clear()

    def window(self):
        window = window_mod.KSysSupervisor(settings=self.settings)
        self.addCleanup(window._stop_worker)
        return window


class LayoutTest(WindowTestCase):
    def test_the_tree_is_the_default(self):
        window = self.window()
        self.assertEqual(window.layout_name, window_mod.LAYOUT_TREE)
        self.assertIsInstance(window.view, SensorTree)
        self.assertTrue(window.layout_actions["tree"].isChecked())
        self.assertEqual(window.size().width(), 500)

    def test_switching_shows_the_board_and_remembers(self):
        window = self.window()
        window._apply_sample(sample())
        self.assertGreater(window.tree.topLevelItemCount(), 0)
        self.assertEqual(len(window.board._tiles), 0)

        window.set_layout("tiles")
        self.assertIsInstance(window.view, TileBoard)
        self.assertIsInstance(window._stack.currentWidget(), Backdrop)
        self.assertTrue(window.layout_actions["tiles"].isChecked())
        self.assertFalse(window.layout_actions["tree"].isChecked())
        self.assertEqual(self.settings.value("layout"), "tiles")
        # Filled from the readings in hand, not left empty until the next tick.
        self.assertGreater(len(window.board._tiles), 0)
        self.assertEqual(window.size().width(), 1080)

    def test_a_saved_choice_is_honoured_and_a_bad_one_is_not(self):
        self.settings.setValue("layout", "tiles")
        self.assertEqual(self.window().layout_name, "tiles")
        self.settings.setValue("layout", "spreadsheet")
        self.assertEqual(self.window().layout_name, "tree")

    def test_each_layout_keeps_its_own_size(self):
        # Both under 800: restoreGeometry clamps to the offscreen screen.
        window = self.window()
        window.resize(640, 480)
        window.set_layout("tiles")
        window.resize(700, 600)
        window.set_layout("tree")
        self.assertEqual((window.width(), window.height()), (640, 480))
        window.set_layout("tiles")
        self.assertEqual((window.width(), window.height()), (700, 600))

    def test_the_tree_widens_to_its_columns_once(self):
        window = self.window()
        window.show()
        window._apply_sample(sample())
        self.app.processEvents()
        # Whatever it grew to, a second sample does not grow it again and a
        # size the user then chooses is left alone.
        grown = window.width()
        window.resize(400, 300)
        window._apply_sample(sample(temp=70.0))
        self.assertEqual(window.width(), 400)
        self.assertGreaterEqual(grown, 500)


class LayoutNamesTest(WindowTestCase):
    def test_the_menu_offers_the_two_layouts(self):
        window = self.window()
        self.assertEqual([a.text() for a in window.layout_actions.values()],
                         ["Classic Tree", "Tiles"])

    def test_the_short_lived_scrolling_value_means_tiles(self):
        self.settings.setValue("layout", "tiles-scroll")
        self.assertEqual(self.window().layout_name, "tiles")


class OrderTest(WindowTestCase):
    def test_either_view_persists_the_order_through_the_window(self):
        window = self.window()
        window.tree.order_changed.emit(["b", "a"])
        self.assertEqual(self.settings.value("category_order", type=list),
                         ["b", "a"])
        window.board.order_changed.emit(["a", "b"])
        self.assertEqual(self.settings.value("category_order", type=list),
                         ["a", "b"])


class FanControlMenuTest(WindowTestCase):
    """Tools > Fan Control is always clickable, and fixes what it can."""

    def fake_box(self, press_install):
        from PyQt6.QtWidgets import QMessageBox

        shown = self.shown = []

        class Box:
            Icon = QMessageBox.Icon
            ButtonRole = QMessageBox.ButtonRole
            StandardButton = QMessageBox.StandardButton

            def __init__(self, _parent=None):
                self._buttons = []

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

            def addButton(self, *args):
                button = object()
                self._buttons.append(button)
                return button

            def exec(self):
                shown.append("question")

            def clickedButton(self):
                return self._buttons[0] if press_install else None

            @staticmethod
            def information(_parent, title, _text):
                shown.append("information: " + title)

            @staticmethod
            def warning(_parent, title, _text):
                shown.append("warning: " + title)

        return Box

    def patched(self, state, fixable, install_result=(True, "done")):
        from unittest import mock

        fanctl = window_mod.fanctl
        states = iter([(state, "why"), (fanctl.READY, None)])
        return [mock.patch.object(fanctl, "availability",
                                  side_effect=lambda: next(states)),
                mock.patch.object(fanctl, "can_install_helper",
                                  return_value=fixable),
                mock.patch.object(window_mod.KSysSupervisor, "_install_helper",
                                  staticmethod(lambda: (install_result, None)))]

    def run_click(self, state, fixable, press_install, install_result=(True, "")):
        from contextlib import ExitStack
        from unittest import mock

        window = self.window()
        self.assertTrue(window.fan_action.isEnabled())
        with ExitStack() as stack:
            for patch in self.patched(state, fixable, install_result):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                window_mod, "QMessageBox", self.fake_box(press_install)))
            return window._make_fan_control_ready(
                *window_mod.fanctl.availability())

    def test_a_missing_helper_is_installed_on_request(self):
        fanctl = window_mod.fanctl
        self.assertTrue(self.run_click(fanctl.NO_HELPER, True, True))
        self.assertEqual(self.shown, ["question"])

    def test_declining_installs_nothing(self):
        fanctl = window_mod.fanctl
        self.assertFalse(self.run_click(fanctl.NO_HELPER, True, False))

    def test_a_failed_install_says_why(self):
        fanctl = window_mod.fanctl
        self.assertFalse(self.run_click(fanctl.OUTDATED_HELPER, True, True,
                                        install_result=(False, "no")))
        self.assertIn("warning: Fan control helper", self.shown)

    def test_what_cannot_be_fixed_is_explained(self):
        fanctl = window_mod.fanctl
        self.assertFalse(self.run_click(fanctl.NO_CHANNELS, False, True))
        self.assertEqual(self.shown, ["information: Fan control unavailable"])


class TileConfigWindowTest(WindowTestCase):
    def test_a_tile_layout_is_saved_and_comes_back(self):
        from ksyssupervisor import display

        window = self.window()
        config = display.TileConfig(custom=True, rings=("cpu/usage_total",),
                                    chips=("cpu/Tctl",))
        window.set_tile_config("cpu", config)
        window._stop_worker()
        again = self.window()
        self.assertEqual(again.builder.configs["cpu"], config)

    def test_a_reset_tile_leaves_nothing_behind(self):
        from ksyssupervisor import display

        window = self.window()
        window.set_tile_config("cpu", display.TileConfig(
            custom=True, chips=("cpu/Tctl",)))
        window.set_tile_config("cpu", display.TileConfig())
        self.settings.beginGroup("tile_config")
        self.assertEqual(self.settings.childKeys(), [])
        self.settings.endGroup()


class TooltipTest(WindowTestCase):
    def test_every_menu_entry_explains_itself(self):
        window = self.window()
        missing = []

        def walk(menu):
            self.assertTrue(menu.toolTipsVisible(), menu.title())
            for action in menu.actions():
                if action.isSeparator():
                    continue
                text = action.text().replace("&", "")
                if not action.toolTip() or action.toolTip() == text:
                    missing.append(text)
                if action.menu() is not None:
                    walk(action.menu())

        for action in window.menuBar().actions():
            walk(action.menu())
        self.assertEqual(missing, [])

    def test_a_tools_tooltip_is_titled_in_its_colour(self):
        from ksyssupervisor.widgets import TOOL_COLOURS
        window = self.window()
        self.assertIn(TOOL_COLOURS["fans"], window.fan_action.toolTip())
        self.assertIn(TOOL_COLOURS["stress"], window.stress_action.toolTip())
        self.assertIn(TOOL_COLOURS["graphs"], window.graph_action.toolTip())


class DeviceNameTest(WindowTestCase):
    def test_a_name_typed_on_a_tile_is_kept_and_an_empty_one_resets(self):
        window = self.window()
        key = next(iter(window.registry.devices()))
        window.board.name_changed.emit(key, "  Radeon 6800 ")
        self.assertEqual(window.custom_names[key], "Radeon 6800")
        self.assertEqual(self.settings.value("custom_name_%s" % key),
                         "Radeon 6800")
        window.board.name_changed.emit(key, "")
        self.assertNotIn(key, window.custom_names)
        self.assertIsNone(self.settings.value("custom_name_%s" % key))

    def test_typing_the_hardware_name_back_is_no_custom_name(self):
        window = self.window()
        key = next(iter(window.registry.devices()))
        window.set_device_name(key, "Mine")
        window.set_device_name(key, window._default_name(key))
        self.assertNotIn(key, window.custom_names)



class UpdateMenuTest(WindowTestCase):
    """The Help menu's update entries and what the startup check does."""

    def _release(self, version="99.0.0"):
        from ksyssupervisor import updater
        return updater.Release(version=version, tag="v" + version)

    def test_the_toggles_default_on_and_are_remembered(self):
        window = self.window()
        self.assertTrue(window.update_on_start_action.isChecked())
        window.update_on_start_action.setChecked(False)
        self.assertFalse(self.settings.value("update_check_on_start", True, type=bool))
        self.assertFalse(self.window().update_on_start_action.isChecked())

    def test_a_checkout_cannot_install_itself(self):
        from ksyssupervisor import installation
        from ksyssupervisor.updatedialog import UpdateManager
        window = self.window()
        manager = UpdateManager(window, self.settings,
                                install_kind=installation.SOURCE)
        self.assertFalse(manager.auto_install)

    def test_startup_with_the_check_off_asks_nothing(self):
        from unittest import mock
        window = self.window()
        window.updates.set_check_on_start(False)
        with mock.patch.object(window.updates, "check_in_background") as check:
            window.updates.startup()
        check.assert_not_called()

    def test_a_found_release_is_installed_when_automatic(self):
        from unittest import mock
        from ksyssupervisor import installation
        from ksyssupervisor.updatedialog import UpdateManager
        window = self.window()
        manager = UpdateManager(window, self.settings,
                                install_kind=installation.USER)
        with mock.patch.object(manager, "install") as install, \
                mock.patch.object(manager, "offer") as offer:
            manager._background_result(self._release(), None)
        install.assert_called_once()
        offer.assert_not_called()

    def test_a_found_release_is_offered_when_not_automatic(self):
        from unittest import mock
        from ksyssupervisor import installation
        from ksyssupervisor.updatedialog import UpdateManager
        window = self.window()
        manager = UpdateManager(window, self.settings,
                                install_kind=installation.USER)
        manager.set_auto_install(False)
        with mock.patch.object(manager, "install") as install, \
                mock.patch.object(manager, "offer") as offer:
            manager._background_result(self._release(), None)
        offer.assert_called_once()
        install.assert_not_called()

    def test_a_skipped_version_is_not_offered_again_at_startup(self):
        from unittest import mock
        window = self.window()
        self.settings.setValue("update_skip_version", "99.0.0")
        with mock.patch.object(window.updates, "install") as install, \
                mock.patch.object(window.updates, "offer") as offer:
            window.updates._background_result(self._release(), None)
        install.assert_not_called()
        offer.assert_not_called()

    def test_an_offline_check_stays_quiet(self):
        from unittest import mock
        window = self.window()
        with mock.patch.object(window.updates, "offer") as offer:
            window.updates._background_result(None, "Could not reach GitHub")
        offer.assert_not_called()

    def test_close_tools_with_nothing_open_is_a_yes(self):
        self.assertTrue(self.window().close_tools())
