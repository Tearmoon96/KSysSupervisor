"""Live graphs: the recorded history, the axis scaling, and the docked panel."""

import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QSettings, Qt           # noqa: E402
from PyQt6.QtGui import QColor, QMouseEvent, QPalette    # noqa: E402
from PyQt6.QtWidgets import QApplication                  # noqa: E402

from ksyssupervisor import series                         # noqa: E402
from ksyssupervisor import window as window_mod           # noqa: E402
from ksyssupervisor.display import TreeModel              # noqa: E402
from ksyssupervisor.livegraph import (DARK_PLOT,          # noqa: E402
                                      LIGHT_LINE_COLOURS, LINE_COLOURS,
                                      GraphStrip, LiveGraphPanel,
                                      device_labels, plot_colours,
                                      strip_full_titles, strip_titles,
                                      width_after_panel)
from ksyssupervisor.providers import Sample               # noqa: E402
from ksyssupervisor.providers.base import Reading         # noqa: E402


def temp(value, uid_key="Tctl"):
    return Reading("cpu", "Temperatures", uid_key, "Package", value, "temp")


class SeriesStoreTest(unittest.TestCase):
    def test_every_reading_is_kept_in_time_order(self):
        store = series.SeriesStore()
        for i, value in enumerate((40.0, 41.0, 43.0)):
            store.record([temp(value)], now=100.0 + i)
        self.assertEqual(store.points("cpu/Tctl", 60, now=102.0),
                         [(100.0, 40.0), (101.0, 41.0), (102.0, 43.0)])
        self.assertEqual(store.kind("cpu/Tctl"), "temp")

    def test_the_span_limits_what_comes_back(self):
        store = series.SeriesStore()
        for i in range(10):
            store.record([temp(40.0 + i)], now=float(i))
        self.assertEqual([t for t, _v in store.points("cpu/Tctl", 3, now=9.0)],
                         [6.0, 7.0, 8.0, 9.0])

    def test_a_missed_tick_is_a_gap_not_a_bridge(self):
        store = series.SeriesStore()
        store.record([temp(40.0)], now=0.0)
        store.record([], now=1.0)                  # the sensor went quiet
        store.record([temp(42.0)], now=2.0)
        values = [v for _t, v in store.points("cpu/Tctl", 60, now=2.0)]
        self.assertEqual(values[0], 40.0)
        self.assertTrue(math.isnan(values[1]))
        self.assertEqual(values[2], 42.0)

    def test_a_late_reading_is_aligned_with_its_own_ticks(self):
        store = series.SeriesStore()
        store.record([temp(40.0)], now=0.0)
        store.record([temp(41.0), temp(900.0, "fan1")], now=1.0)
        self.assertEqual(store.points("cpu/fan1", 60, now=1.0), [(1.0, 900.0)])

    def test_old_ticks_are_dropped(self):
        store = series.SeriesStore(max_age=5)
        for i in range(20):
            store.record([temp(40.0)], now=float(i))
        self.assertEqual(len(store.points("cpu/Tctl", 1000, now=19.0)), 6)

    def test_a_reading_gone_for_the_whole_window_is_forgotten(self):
        store = series.SeriesStore(max_age=2)
        store.record([temp(40.0)], now=0.0)
        for i in range(1, 6):
            store.record([], now=float(i))
        self.assertEqual(store.points("cpu/Tctl", 60, now=5.0), [])
        self.assertIsNone(store.kind("cpu/Tctl"))

    def test_nearest_picks_the_closest_tick(self):
        points = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
        self.assertEqual(series.nearest(points, 1.4), 1)
        self.assertEqual(series.nearest(points, 1.6), 2)
        self.assertEqual(series.nearest(points, -5.0), 0)
        self.assertEqual(series.nearest(points, 50.0), 2)
        self.assertIsNone(series.nearest([], 1.0))


class AxisTest(unittest.TestCase):
    def test_percentages_are_always_zero_to_a_hundred(self):
        self.assertEqual(series.axis_range([3.0, 5.0], "utilization")[:2],
                         (0.0, 100.0))

    def test_an_amount_with_a_total_is_scaled_to_it(self):
        self.assertEqual(series.axis_range([7.0], "memory_gb", total=32.0)[:2],
                         (0.0, 32.0))

    def test_a_steady_reading_is_not_magnified(self):
        """45.0 held flat would otherwise fill the strip with rounding."""
        low, high, _step = series.axis_range([45.0, 45.0], "temp")
        self.assertLessEqual(low, 40.0)
        self.assertGreaterEqual(high, 50.0)

    def test_the_range_covers_the_data_on_round_lines(self):
        low, high, step = series.axis_range([612.0, 1840.0], "fan")
        self.assertLessEqual(low, 612.0)
        self.assertGreaterEqual(high, 1840.0)
        self.assertAlmostEqual(low / step, round(low / step))
        self.assertAlmostEqual(high / step, round(high / step))

    def test_non_negative_kinds_do_not_pad_below_zero(self):
        self.assertEqual(series.axis_range([0.0, 30.0], "power")[0], 0.0)

    def test_no_data_still_gives_an_axis(self):
        self.assertEqual(series.axis_range([], "temp"), (0.0, 1.0, 0.25))

    def test_steps_are_round(self):
        for raw, step in ((0.3, 0.5), (7.0, 10.0), (130.0, 200.0),
                          (2.2, 2.5)):
            self.assertEqual(series.nice_step(raw), step)


class WidgetTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class GraphStripTest(WidgetTestCase):
    def strip(self):
        strip = GraphStrip("cpu/Tctl")
        strip.resize(400, 128)
        points = [(100.0 + i, 40.0 + i) for i in range(61)]
        strip.set_data("CPU · Package", "temp", None, points, 60, 160.0)
        return strip

    def hover(self, strip, x):
        event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(x, 60),
                            QPointF(x, 60), Qt.MouseButton.NoButton,
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        strip.mouseMoveEvent(event)

    def test_hovering_finds_the_value_under_the_cursor(self):
        strip = self.strip()
        plot = strip.plot_rect()
        self.hover(strip, plot.left() + plot.width() / 2)   # 30 s into 60
        when, value = strip.hovered_point()
        self.assertAlmostEqual(when, 130.0, delta=1.0)
        self.assertAlmostEqual(value, 70.0, delta=1.0)

    def test_the_right_edge_is_the_latest_reading(self):
        strip = self.strip()
        self.hover(strip, strip.plot_rect().right())
        self.assertEqual(strip.hovered_point(), (160.0, 100.0))

    def test_outside_the_plot_there_is_no_readout(self):
        strip = self.strip()
        self.hover(strip, 5)                     # over the axis labels
        self.assertIsNone(strip.hovered_point())
        strip.leaveEvent(None)
        self.assertIsNone(strip.hovered_point())

    def test_hovering_shows_the_full_name_in_the_title(self):
        strip = self.strip()
        strip.set_data("CPU · Package (°C)", "temp", None, strip.points, 60,
                       160.0, "AMD Ryzen 7 9700X · Package (°C)")
        self.assertEqual(strip.shown_title(), "CPU · Package (°C)")
        self.hover(strip, 200)
        self.assertEqual(strip.shown_title(),
                         "AMD Ryzen 7 9700X · Package (°C)")
        strip.leaveEvent(None)
        self.assertEqual(strip.shown_title(), "CPU · Package (°C)")

    def hover_at(self, strip, x, y):
        event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(x, y),
                            QPointF(x, y), Qt.MouseButton.NoButton,
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        strip.mouseMoveEvent(event)

    def test_the_readout_keeps_clear_of_the_pointer(self):
        strip = self.strip()
        plot = strip.plot_rect()
        gap_x, gap_y = GraphStrip.READOUT_GAP
        x, mouse = plot.left() + 40, plot.bottom() - 10
        self.hover_at(strip, x, mouse)
        box = strip.readout_rect(plot, x, plot.top() + 5, 80, 18)
        self.assertEqual(box.left(), x + gap_x)
        self.assertEqual(box.bottom(), mouse - gap_y)      # above the mouse,
        # not above the point, which is well above the mouse here

    def test_with_no_room_the_readout_goes_the_other_way(self):
        strip = self.strip()
        plot = strip.plot_rect()
        gap_x, _gap_y = GraphStrip.READOUT_GAP
        x, mouse = plot.right() - 10, plot.top() + 4
        self.hover_at(strip, x, mouse)
        box = strip.readout_rect(plot, x, mouse, 80, 18)
        self.assertEqual(box.right(), x - gap_x)            # to the left
        self.assertGreaterEqual(box.top(),
                                mouse + GraphStrip.POINTER_HEIGHT)  # below

    def test_the_cursor_follows_the_mouse_where_nothing_was_recorded(self):
        """Ten seconds of history in a sixty-second span: the left of the
        plot has no readings, and the cursor must not jump to the first."""
        strip = GraphStrip("cpu/Tctl")
        strip.resize(400, 128)
        points = [(150.0 + i, 40.0) for i in range(11)]
        strip.set_data("CPU · Package", "temp", None, points, 60, 160.0)
        plot = strip.plot_rect()
        self.hover(strip, plot.left() + plot.width() / 4)     # 15 s in
        when, value = strip.hovered_point()
        self.assertAlmostEqual(when, 115.0, delta=0.5)
        self.assertTrue(math.isnan(value))

    def test_an_empty_strip_still_has_a_cursor(self):
        strip = GraphStrip("x")
        strip.resize(400, 128)
        strip.set_data("x", "temp", None, [], 60, 160.0)
        self.hover(strip, 200)
        self.assertIsNotNone(strip.hovered_point())
        self.assertFalse(strip.grab().isNull())

    def test_it_paints_with_and_without_data(self):
        """Look-at-the-pixels smoke check: nothing raises either way."""
        strip = self.strip()
        self.hover(strip, 200)
        self.assertFalse(strip.grab().isNull())
        empty = GraphStrip("x")
        empty.resize(300, 128)
        self.assertFalse(empty.grab().isNull())


def themed_palette(window, base, text):
    palette = QPalette()
    for role, colour in ((QPalette.ColorRole.Window, window),
                         (QPalette.ColorRole.Base, base),
                         (QPalette.ColorRole.Text, text),
                         (QPalette.ColorRole.WindowText, text)):
        palette.setColor(role, QColor(colour))
    return palette


def contrast(a, b):
    def luminance(colour):
        def channel(value):
            value /= 255.0
            return (value / 12.92 if value <= 0.03928
                    else ((value + 0.055) / 1.055) ** 2.4)
        return (0.2126 * channel(colour.red())
                + 0.7152 * channel(colour.green())
                + 0.0722 * channel(colour.blue()))
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class PlotThemeTest(WidgetTestCase):
    LIGHT = ("#eff0f1", "#fcfcfc", "#232629")      # Breeze Light
    DARK = ("#202326", "#141618", "#fcfcfc")       # Breeze Dark

    def test_a_dark_theme_keeps_the_black_plot(self):
        colours = plot_colours(themed_palette(*self.DARK))
        self.assertIs(colours, DARK_PLOT)
        self.assertEqual(colours.background, QColor("#000000"))

    def test_a_light_theme_plots_on_its_own_base(self):
        colours = plot_colours(themed_palette(*self.LIGHT))
        self.assertEqual(colours.background, QColor("#fcfcfc"))
        self.assertEqual(colours.title_text, QColor("#232629"))
        # The grid is there, but faint: much closer to the plot than the text.
        self.assertLess(contrast(colours.grid, colours.background), 1.5)

    def test_every_light_line_reads_on_the_light_plot(self):
        background = plot_colours(themed_palette(*self.LIGHT)).background
        self.assertEqual(set(LIGHT_LINE_COLOURS), set(LINE_COLOURS))
        for kind, colour in LIGHT_LINE_COLOURS.items():
            self.assertGreater(contrast(QColor(colour), background), 4.0,
                               kind)

    def test_a_strip_follows_a_theme_switch(self):
        strip = GraphStrip("cpu/Tctl")
        strip.resize(300, 128)
        strip.set_data("CPU · Package", "temp", None, [], 60, 160.0)
        strip.setPalette(themed_palette(*self.DARK))
        image = strip.grab().toImage()
        self.assertEqual(image.pixelColor(2, 2), QColor("#000000"))
        strip.setPalette(themed_palette(*self.LIGHT))
        image = strip.grab().toImage()
        self.assertEqual(image.pixelColor(2, 2), QColor("#fcfcfc"))
        self.assertEqual(strip.colour(), QColor(LIGHT_LINE_COLOURS["temp"]))


class PanelTest(WidgetTestCase):
    def test_selection_adds_and_removes_strips(self):
        panel = LiveGraphPanel()
        changes = []
        panel.selection_changed.connect(changes.append)
        panel.add("cpu/Tctl")
        panel.add("cpu/Tctl")                    # no duplicate strip
        panel.add("gpu/edge")
        self.assertEqual(panel.selection(), ["cpu/Tctl", "gpu/edge"])
        panel.remove("cpu/Tctl")
        self.assertEqual(panel.selection(), ["gpu/edge"])
        self.assertEqual(changes, [["cpu/Tctl"], ["cpu/Tctl", "gpu/edge"],
                                   ["gpu/edge"]])

    def test_a_strip_can_remove_itself(self):
        panel = LiveGraphPanel(["cpu/Tctl"])
        panel._strips["cpu/Tctl"].remove_requested.emit("cpu/Tctl")
        self.assertEqual(panel.selection(), [])

    def test_the_graphs_never_take_focus(self):
        """A focused scroll area gets Breeze's blue frame."""
        self.assertEqual(LiveGraphPanel().scroll.focusPolicy(),
                         Qt.FocusPolicy.NoFocus)

    def test_an_unknown_span_falls_back_to_the_default(self):
        self.assertEqual(LiveGraphPanel(span=17).span, series.DEFAULT_SPAN)


class WidthAfterPanelTest(unittest.TestCase):
    def test_an_untouched_window_goes_back_exactly(self):
        """Even when the panel's minimum grew it by more than was asked."""
        self.assertEqual(width_after_panel(826, 304, (560, 826)), 560)

    def test_a_resized_window_only_loses_the_panels_share(self):
        self.assertEqual(width_after_panel(1100, 440, (560, 1000)), 660)

    def test_a_restored_window_only_loses_the_panels_share(self):
        self.assertEqual(width_after_panel(1000, 440, (None, None)), 560)


class TitleTest(unittest.TestCase):
    def metric(self, uid, label, kind="fan"):
        class Metric:
            pass

        metric = Metric()
        metric.uid, metric.label, metric.kind = uid, label, kind
        return metric

    def model(self, key, name, groups, device_name=None):
        return TreeModel(key=key, name=name, order=10, groups=groups,
                         device_name=name if device_name is None
                         else device_name)

    def test_the_device_comes_first_by_its_short_name(self):
        model = self.model("gpu:0000:03:00.0", "Navi 21 [Radeon RX 6800]",
                           (("Fans", (self.metric("gpu/fan1", "Fan"),)),))
        self.assertEqual(strip_titles([model]), {"gpu/fan1": "GPU · Fan"})

    def test_a_temperature_carries_its_unit(self):
        model = self.model("cpu", "Ryzen", (
            ("Temperatures", (self.metric("cpu/Tctl", "Package", "temp"),)),))
        self.assertEqual(strip_titles([model]),
                         {"cpu/Tctl": "CPU · Package (°C)"})

    def test_a_label_used_twice_is_told_apart_by_its_unit(self):
        model = self.model("cpu", "Ryzen", (
            ("Temperatures", (self.metric("cpu/Tctl", "Package", "temp"),)),
            ("Powers", (self.metric("cpu/rapl_Package", "Package", "power"),
                        self.metric("cpu/rapl_Cores", "Cores", "power")))))
        titles = strip_titles([model])
        self.assertEqual(titles["cpu/Tctl"], "CPU · Package (°C)")
        self.assertEqual(titles["cpu/rapl_Package"], "CPU · Package (W)")
        self.assertEqual(titles["cpu/rapl_Cores"], "CPU · Cores")

    def test_readings_of_one_kind_fall_back_to_their_group(self):
        """Same label, same unit: only the group can tell them apart."""
        model = self.model("gpu:0000:03:00.0", "RX 6800", (
            ("Clocks", (self.metric("gpu/sclk", "Current", "clock"),)),
            ("Memory clocks", (self.metric("gpu/mclk", "Current", "clock"),))))
        titles = strip_titles([model])
        self.assertEqual(titles["gpu/sclk"], "GPU · Current (Clocks)")
        self.assertEqual(titles["gpu/mclk"], "GPU · Current (Memory clocks)")

    def test_short_names(self):
        models = [self.model(key, "whatever", ())
                  for key in ("cpu", "ram", "mobo", "disk:nvme0n1",
                              "battery:BAT0", "odd")]
        self.assertEqual(device_labels(models), {
            "cpu": "CPU", "ram": "RAM", "mobo": "Board",
            "disk:nvme0n1": "Drive nvme0n1", "battery:BAT0": "Battery",
            "odd": "whatever"})

    def test_two_cards_are_numbered(self):
        models = [self.model("gpu:0000:03:00.0", "RX 6800", ()),
                  self.model("gpu:0000:0a:00.0", "Arc A380", ())]
        self.assertEqual(device_labels(models),
                         {"gpu:0000:03:00.0": "GPU 1",
                          "gpu:0000:0a:00.0": "GPU 2"})

    def test_a_renamed_device_goes_by_its_new_name(self):
        model = self.model("gpu:0000:03:00.0", "Radeon 6800", (
            ("Temperatures", (self.metric("gpu/j", "Hot Spot", "temp"),)),),
            device_name="Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]")
        self.assertEqual(strip_titles([model]),
                         {"gpu/j": "Radeon 6800 · Hot Spot (°C)"})
        # Hovered, it says which hardware that is.
        self.assertEqual(strip_full_titles([model]),
                         {"gpu/j": "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"
                                   " · Hot Spot (°C)"})

    def test_the_full_title_has_the_devices_own_name(self):
        model = self.model("cpu", "AMD Ryzen 7 9700X", (
            ("Temperatures", (self.metric("cpu/Tctl", "Package", "temp"),)),))
        self.assertEqual(strip_full_titles([model]),
                         {"cpu/Tctl": "AMD Ryzen 7 9700X · Package (°C)"})


class WindowGraphTest(WidgetTestCase):
    def setUp(self):
        for name in ("fit_on_screen", "screen_limits"):
            original = getattr(window_mod, name)
            self.addCleanup(setattr, window_mod, name, original)
        # The offscreen screen is 800x800, which would clamp every size.
        window_mod.fit_on_screen = lambda w, a, b, fraction=0.9: (a, b)
        window_mod.screen_limits = lambda widget=None, fraction=0.9: None
        self.settings = QSettings("KSysSupervisorTest", "Graphs")
        self.settings.clear()

    def window(self):
        window = window_mod.KSysSupervisor(settings=self.settings)
        self.addCleanup(self.dispose, window)
        window.show()
        return window

    def dispose(self, window):
        """Closed and deleted by Qt, not left to garbage collection.

        A shown window that Python collects is torn down in the middle of
        whatever event comes next, and a later test paints into its tree.
        """
        window._stop_worker()
        window.close()
        window.deleteLater()
        QApplication.processEvents()

    def test_opening_widens_the_window_and_closing_gives_it_back(self):
        window = self.window()
        before = window.width()
        window.graph_action.setChecked(True)
        self.assertTrue(window._graph_dock.isVisible())
        self.assertGreater(window.width(), before)
        window.graph_action.setChecked(False)
        self.assertEqual(window.width(), before)
        self.assertFalse(self.settings.value("live_graphs/open", type=bool))

    def test_the_dock_is_on_the_right(self):
        window = self.window()
        window.graph_action.setChecked(True)
        self.assertEqual(window.dockWidgetArea(window._graph_dock),
                         Qt.DockWidgetArea.RightDockWidgetArea)

    def test_the_selection_and_open_state_are_remembered(self):
        window = self.window()
        window.graph_reading("cpu/Tctl")
        self.assertTrue(window.graph_action.isChecked())
        self.assertEqual(self.settings.value("live_graphs/metrics", type=list),
                         ["cpu/Tctl"])
        window._stop_worker()

        again = window_mod.KSysSupervisor(settings=self.settings)
        self.addCleanup(self.dispose, again)
        self.assertTrue(again.graph_action.isChecked())
        self.assertEqual(again._graph_panel.selection(), ["cpu/Tctl"])

    def test_samples_reach_the_graphs(self):
        window = self.window()
        window.graph_reading("cpu/Tctl")
        for value in (50.0, 55.0):
            window._apply_sample(Sample(backend="sysfs",
                                        readings=[temp(value)]))
        strip = window._graph_panel._strips["cpu/Tctl"]
        self.assertEqual([v for _t, v in strip.points], [50.0, 55.0])
        self.assertIn("Package", strip.title)

    def test_a_renamed_device_is_renamed_in_the_graphs(self):
        window = self.window()
        window.graph_reading("cpu/Tctl")
        window._apply_sample(Sample(backend="sysfs", readings=[temp(50.0)]))
        strip = window._graph_panel._strips["cpu/Tctl"]
        self.assertEqual(strip.title, "CPU · Package (°C)")
        window.set_device_name("cpu", "My CPU")
        window._apply_sample(Sample(backend="sysfs", readings=[temp(51.0)]))
        self.assertEqual(strip.title, "My CPU · Package (°C)")

    def test_a_tile_offers_a_reading_to_the_graphs(self):
        window = self.window()
        window._apply_sample(Sample(backend="sysfs", readings=[temp(61.0)]))
        window.board.graph_requested.emit("cpu/Tctl")
        self.assertTrue(window.graph_action.isChecked())
        self.assertEqual(window._graph_panel.selection(), ["cpu/Tctl"])

    def test_history_is_recorded_before_anything_is_graphed(self):
        """So a graph opened after something happened can still show it."""
        window = self.window()
        window._apply_sample(Sample(backend="sysfs", readings=[temp(70.0)]))
        window.graph_reading("cpu/Tctl")
        strip = window._graph_panel._strips["cpu/Tctl"]
        self.assertEqual([v for _t, v in strip.points], [70.0])

    def test_the_tree_offers_a_reading_to_the_graphs(self):
        window = self.window()
        window._apply_sample(Sample(backend="sysfs", readings=[temp(61.0)]))
        item = window.tree._rows["cpu/Tctl"]
        menu = window.tree.menu_for_reading(item)
        [action] = [a for a in menu.actions() if "Live Graphs" in a.text()]
        action.trigger()
        self.assertEqual(window._graph_panel.selection(), ["cpu/Tctl"])


class AddMetricMenuTest(WidgetTestCase):
    """Add Metric > device > group, the three levels of submenu."""

    def open_levels(self):
        """Pop the menu up and walk into RAM > Temperatures by keyboard, the
        way QMenu opens a submenu itself; returns the three menus."""
        from PyQt6.QtCore import QEvent, QPoint
        from PyQt6.QtGui import QKeyEvent
        from ksyssupervisor.display import Metric
        import inspect
        params = inspect.signature(Metric).parameters
        needed = {k: None for k in params
                  if params[k].default is inspect.Parameter.empty}

        def metric(uid, label):
            return Metric(**dict(needed, uid=uid, label=label, text=""))

        panel = LiveGraphPanel()
        panel.set_models([
            TreeModel("cpu", "AMD Ryzen 7 9700X", 10, (
                ("Temperatures", (metric("cpu/t", "Package"),)),)),
            TreeModel("ram", "RAM", 20, (
                ("Temperatures", (metric("ram/2", "DIMM 2"),
                                  metric("ram/4", "DIMM 4"))),
                ("Utilization", (metric("ram/u", "Memory Used"),))))])
        top = panel.add_menu
        top.popup(QPoint(50, 50))
        self.addCleanup(top.close)
        self.app.processEvents()

        def enter(menu, text):
            action = next(a for a in menu.actions() if a.text() == text)
            menu.setActiveAction(action)
            self.app.sendEvent(menu, QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Right,
                Qt.KeyboardModifier.NoModifier))
            self.app.processEvents()
            return action.menu()

        device = enter(top, "RAM")
        group = enter(device, "Temperatures")
        return top, device, group

    def test_a_menu_with_submenus_leaves_room_for_the_arrows(self):
        """Breeze ran 'Temperatures' and a long GPU name into their arrows."""
        from PyQt6.QtWidgets import QMenu
        from ksyssupervisor.widgets import SUBMENU_ARROW_SLACK
        top, device, group = self.open_levels()

        def natural(menu):
            plain = QMenu()
            plain.addActions(menu.actions())
            return plain.sizeHint().width()

        widths = {}
        for menu in (top, device):
            widths[menu.title()] = menu.width()
            self.assertEqual(menu.width(),
                             natural(menu) + SUBMENU_ARROW_SLACK)
        # A menu of plain items has no arrows to make room for.
        self.assertEqual(group.width(), natural(group))

        # Opened again, the room is not added a second time.
        device.close()
        device.popup(device.pos())
        self.app.processEvents()
        self.assertEqual(device.width(), widths["RAM"])

    def test_on_wayland_a_submenu_is_anchored_level_with_its_item(self):
        """Qt anchors a Wayland submenu to the top of its item, so each
        level sat 4px lower than the last. The anchor is lifted by the
        submenu's own first-item offset, which is what QMenu does on X11."""
        from unittest import mock
        from ksyssupervisor import widgets
        with mock.patch.object(widgets, "on_wayland", return_value=True):
            top, device, group = self.open_levels()
        for parent, menu, item in ((top, device, "RAM"),
                                   (device, group, "Temperatures")):
            action = next(a for a in parent.actions() if a.text() == item)
            item_rect = parent.actionGeometry(action)
            first = menu.actionGeometry(menu.actions()[0])
            anchor = menu.windowHandle().property(widgets.WAYLAND_ANCHOR)
            self.assertEqual(anchor.top(), item_rect.top() - first.top())
            self.assertEqual(anchor.height(), item_rect.height())
            # On the parent's edge, one border over it: anchored to the item
            # alone the submenu covered the parent's border, and anchored
            # to the edge itself the two borders drew a double line.
            overlap = widgets.SUBMENU_BORDER_OVERLAP
            self.assertEqual(overlap, 1)
            self.assertEqual(anchor.left(), overlap)
            self.assertEqual(anchor.left() + anchor.width(),
                             parent.width() - overlap)
        # The top-level menu is not a submenu and keeps Qt's own placement.
        self.assertIsNone(top.windowHandle().property(widgets.WAYLAND_ANCHOR))


if __name__ == "__main__":
    unittest.main()
