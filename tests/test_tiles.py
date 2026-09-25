"""Tile widget tests, offscreen.

These drive real widgets, so they need a QApplication - but never a visible
window and never real hardware: the models come from display.TileBuilder fed
synthetic readings.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt                       # noqa: E402
from PyQt6.QtWidgets import QApplication                  # noqa: E402

from ksyssupervisor import tiles                          # noqa: E402
from ksyssupervisor.widgets import blend, light_tint      # noqa: E402
from ksyssupervisor.display import TileBuilder            # noqa: E402
from ksyssupervisor.providers.base import Device, Reading  # noqa: E402
from ksyssupervisor.tileboard import TileBoard            # noqa: E402


CPU = Device("cpu", "AMD Ryzen 7 9700X", order=10)
RAM = Device("ram", "RAM", order=20)
GPU = Device("gpu:0000:03:00.0", "Radeon RX 6800", order=30)


def sample(load=42.0, temp=61.5, cores=0):
    out = [Reading("cpu", "Utilization", "usage_total", "Processor", load,
                   "utilization"),
           Reading("cpu", "Temperatures", "Tctl", "Package", temp, "temp"),
           Reading("cpu", "Powers", "rapl_Package", "Package", 88.0, "power"),
           Reading("ram", "Utilization", "usage", "Memory Used", 12.0,
                   "memory_gb", total=32.0),
           Reading(GPU.key, "Utilization", "usage_total", "GPU", 7.0,
                   "utilization")]
    for i in range(cores):
        out.append(Reading("cpu", "Clocks", "clock_%d" % i, "Core %d" % i,
                           4000.0 + i, "clock"))
    return out


def builder():
    return TileBuilder({d.key: d for d in (CPU, RAM, GPU)})


class TileTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def box(widget, ancestor):
        """A widget's rectangle in an ancestor's coordinates."""
        from PyQt6.QtCore import QRect
        return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


class MetricRowTest(TileTestCase):
    def test_a_row_reports_whether_it_had_anything_to_do(self):
        """The refresh runs once a second per row; unchanged must be cheap."""
        build = builder()
        metric = build.build(sample(cores=4))[0].rows[0]
        row = tiles.MetricRow()
        self.assertTrue(row.set_metric(metric))
        self.assertFalse(row.set_metric(metric))

    @staticmethod
    def _package(model):
        return next(m for m in model.rows if m.uid == "cpu/Tctl")

    def test_a_changed_reading_is_written_through(self):
        build = builder()
        first = self._package(build.build(sample(temp=50.0))[0])
        second = self._package(build.build(sample(temp=70.0))[0])
        row = tiles.MetricRow()
        row.set_metric(first)
        self.assertTrue(row.set_metric(second))
        self.assertIn("70.0", row.value.text())

    def test_a_row_shows_the_value_and_not_its_range(self):
        """The lowest and highest are the tree's; a tile shows what is now."""
        build = builder()
        build.build(sample(temp=50.0))
        metric = self._package(build.build(sample(temp=70.0))[0])
        self.assertEqual((metric.lo, metric.hi), ("50.0 °C", "70.0 °C"))
        row = tiles.MetricRow()
        row.set_metric(metric)
        self.assertEqual(row.value.text(), "70.0 °C")
        texts = [label.text() for label in row.findChildren(tiles.QLabel)]
        self.assertFalse([t for t in texts if "50.0" in t])


class DeviceTileTest(TileTestCase):
    def test_a_tile_shows_its_name_and_headline(self):
        model = builder().build(sample())[0]
        tile = tiles.DeviceTile(model)
        self.assertEqual(tile.name_label.text(), "AMD Ryzen 7 9700X")
        self.assertEqual(len(tile._headlines), 3)
        self.assertEqual(tile.headline_row.chips[0].value.text(), "61.5 °C")

    def test_refreshing_reuses_the_rows_rather_than_rebuilding_them(self):
        build = builder()
        tile = tiles.DeviceTile(build.build(sample(cores=4))[0])
        before = dict(tile._rows)
        tile.sync(build.build(sample(load=50.0, cores=4))[0])
        self.assertEqual(set(before), set(tile._rows))
        for uid, row in before.items():
            self.assertIs(row, tile._rows[uid])

    def test_the_rest_starts_folded_and_opens(self):
        model = builder().build(sample(cores=8))[0]
        tile = tiles.DeviceTile(model)
        more = tile.more
        self.assertFalse(more.expanded())
        self.assertIn("(8)", more.heading.text())

        more.set_expanded(True)
        self.assertTrue(more.expanded())
        self.assertEqual(len(more._rows), 8)

    def test_nothing_folded_means_no_fold(self):
        tile = tiles.DeviceTile(builder().build(sample())[0])
        self.assertFalse(tile.more.isVisibleTo(tile))

    def test_the_grip_sits_in_the_top_right_corner(self):
        model = builder().build(sample())[0]
        tile = tiles.DeviceTile(model)
        tile.resize(320, 200)
        tile.show()
        grip = tile.grip.geometry()
        header = tile.grip.parentWidget().geometry()
        self.assertGreater(grip.center().x(), header.width() * 0.6)
        self.assertLess(grip.top(), 40)

    def test_the_edit_button_lines_up_with_the_grip(self):
        tile = tiles.DeviceTile(builder().build(sample())[0])
        tile.resize(320, 200)
        tile.show()
        self.app.processEvents()
        edit, grip = tile.edit_button.geometry(), tile.grip.geometry()
        self.assertEqual(edit.center().y(), grip.center().y())
        self.assertEqual(edit.size(), grip.size())

    def test_dragging_the_grip_drives_the_tile(self):
        from PyQt6.QtCore import QPoint
        model = builder().build(sample())[0]
        tile = tiles.DeviceTile(model)
        seen = []
        tile.drag_started.connect(lambda t: seen.append("start"))
        tile.drag_moved.connect(lambda t, pos: seen.append("move"))
        tile.drag_finished.connect(lambda t: seen.append("end"))

        tile.grip.pressed.emit()
        tile.grip.moved.emit(QPoint(10, 10))
        tile.grip.released.emit()
        self.assertEqual(seen, ["start", "move", "end"])

    def test_the_grip_is_white_on_dark_and_dark_on_light(self):
        """Asked for white; a light theme cannot have it without vanishing."""
        from PyQt6.QtGui import QPalette, QColor
        grip = tiles.MoveGrip()

        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor(32, 35, 38))
        grip.setPalette(dark)
        self.assertEqual(grip._colour().lightness(), 255)

        light = QPalette()
        light.setColor(QPalette.ColorRole.Window, QColor(246, 245, 244))
        grip.setPalette(light)
        self.assertLess(grip._colour().lightness(), 128)

    def test_both_width_hints_are_stated(self):
        """Ignored would keep both out of the layout; Preferred plus two
        overrides is what lets the board open wide and still shrink."""
        model = builder().build(sample())[0]
        tile = tiles.DeviceTile(model)
        self.assertEqual(tile.sizeHint().width(), tiles.TILE_WIDTH)
        self.assertEqual(tile.minimumSizeHint().width(), tiles.TILE_MIN_WIDTH)
        self.assertLess(tile.minimumSizeHint().width(), tile.sizeHint().width())


class ColourTest(TileTestCase):
    def test_each_kind_of_device_gets_its_own_accent(self):
        models = builder().build(sample())
        accents = {}
        for model in models:
            tile = tiles.DeviceTile(model)
            accents[model.key] = tile.accent().name()
        self.assertEqual(len(set(accents.values())), len(accents))

    def test_heat_takes_the_card_over(self):
        build = builder()
        tile = tiles.DeviceTile(build.build(sample(temp=45.0))[0])
        cool = tile.accent().name()

        tile.sync(build.build(sample(temp=95.0))[0])
        self.assertEqual(tile.accent().name(),
                         tiles.LEVEL_COLOURS["hot"])
        self.assertNotEqual(tile.accent().name(), cool)

        tile.sync(build.build(sample(temp=45.0))[0])
        self.assertEqual(tile.accent().name(), cool)

    def test_a_hot_value_is_tinted_without_a_stylesheet(self):
        build = builder()
        tile = tiles.DeviceTile(build.build(sample(temp=95.0))[0])
        text = tile.headline_row.chips[0].value.text()
        self.assertIn(tiles.LEVEL_COLOURS["hot"], text)
        self.assertEqual(tile.styleSheet(), "")

    def test_a_cool_value_is_plain_text(self):
        build = builder()
        tile = tiles.DeviceTile(build.build(sample(temp=45.0))[0])
        self.assertEqual(tile.headline_row.chips[0].value.text(), "45.0 °C")

    def test_a_headline_is_captioned_with_its_reading(self):
        build = builder()
        tile = tiles.DeviceTile(build.build(sample())[0])
        headline = tile.headline_row.chips[0]
        self.assertTrue(headline.caption.isVisibleTo(tile))
        self.assertEqual(headline.caption.text(), "Hot Spot")


class BackdropTest(TileTestCase):
    """The ground the cards sit on, and the cards' contrast against it."""

    @staticmethod
    def _palette(window):
        from PyQt6.QtGui import QPalette, QColor
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(*window))
        return palette

    def test_a_dark_theme_is_left_alone(self):
        from ksyssupervisor.widgets import backdrop_colour
        colour = backdrop_colour(self._palette((32, 35, 38)))
        self.assertEqual((colour.red(), colour.green(), colour.blue()),
                         (32, 35, 38))

    def test_a_light_theme_is_softened_and_warmed(self):
        """Flat near-white glares; the backdrop is darker and warmer."""
        from ksyssupervisor.widgets import backdrop_colour
        colour = backdrop_colour(self._palette((252, 252, 252)))
        self.assertLess(colour.lightness(), 252)
        self.assertGreater(colour.lightness(), 220)     # still light
        self.assertGreater(colour.red(), colour.blue())  # warm, not cool

    def test_light_cards_sit_above_the_backdrop(self):
        """A card the same tone as its ground is no card at all."""
        from PyQt6.QtGui import QColor
        from ksyssupervisor.widgets import Card, backdrop_colour
        palette = self._palette((252, 252, 252))
        card = Card()
        card.setPalette(palette)
        card.resize(60, 40)
        card.show()
        centre = card.grab().toImage().pixelColor(30, 20)
        ground = backdrop_colour(palette)
        self.assertGreater(centre.lightness(), ground.lightness() + 6)

    def test_the_main_window_is_grounded_and_the_board_see_through(self):
        from ksyssupervisor import window as window_mod
        from ksyssupervisor.widgets import Backdrop
        original = window_mod.fit_on_screen
        window_mod.fit_on_screen = lambda w, a, b, fraction=0.9: (a, b)
        self.addCleanup(setattr, window_mod, "fit_on_screen", original)
        from PyQt6.QtCore import QSettings
        settings = QSettings("KSysSupervisorTest", "Backdrop")
        settings.clear()
        from ksyssupervisor.window import KSysSupervisor
        window = KSysSupervisor(settings=settings)
        self.addCleanup(window._stop_worker)
        window.set_layout("tiles")
        self.assertIsInstance(window._stack.currentWidget(), Backdrop)
        self.assertFalse(window.board.viewport().autoFillBackground())
        self.assertFalse(window.board.widget().autoFillBackground())


def contrast(a, b):
    """WCAG contrast ratio between two QColors: 4.5 is comfortable text."""
    def luminance(colour):
        def channel(v):
            v /= 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return (0.2126 * channel(colour.red())
                + 0.7152 * channel(colour.green())
                + 0.0722 * channel(colour.blue()))
    low, high = sorted((luminance(a), luminance(b)))
    return (high + 0.05) / (low + 0.05)


class LightThemeContrastTest(TileTestCase):
    """Breeze Light's own colours: dim text on a tinted card and in a chip
    measured 3:1 and 2.6:1 in the app, and the cards 1.02:1 against the
    ground - there, but not something anyone could read or see."""

    SCHEMES = {
        "light": {"Window": (239, 240, 241), "WindowText": (35, 38, 41),
                  "Base": (255, 255, 255), "Mid": (196, 201, 205),
                  "PlaceholderText": (112, 125, 138)},
        "dark": {"Window": (32, 35, 38), "WindowText": (252, 252, 252),
                 "Base": (20, 22, 24), "Mid": (36, 39, 42),
                 "PlaceholderText": (161, 169, 177)},
    }

    def use_scheme(self, name):
        from PyQt6.QtGui import QColor, QPalette
        original = QPalette(self.app.palette())
        palette = QPalette(original)
        for role, rgb in self.SCHEMES[name].items():
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(*rgb))
        self.app.setPalette(palette)
        self.app.processEvents()
        self.addCleanup(self.app.setPalette, original)

    def card(self, accent):
        from PyQt6.QtWidgets import QLabel
        from ksyssupervisor.widgets import Card
        card = Card()
        card.set_accent(accent)
        label = QLabel("Hot Spot", card)
        label.setForegroundRole(tiles.QPalette.ColorRole.PlaceholderText)
        card.resize(80, 40)
        card.show()
        self.app.processEvents()
        return card, label

    @staticmethod
    def dim(label):
        return label.palette().color(tiles.QPalette.ColorRole.PlaceholderText)

    COLOURS = (list(tiles.DEVICE_COLOURS.values())
               + list(tiles.LEVEL_COLOURS.values()) + [tiles.DEFAULT_COLOUR])

    def test_dim_text_reads_on_every_light_card_and_its_chips(self):
        """Every device colour, and a warm or hot tile too."""
        from PyQt6.QtGui import QColor
        self.use_scheme("light")
        for order in self.COLOURS:
            accent = QColor(order)
            card, label = self.card(accent)
            fill = card.grab().toImage().pixelColor(60, 30)
            chip = tiles.Chip(card)
            chip.set_colour(accent)
            self.assertGreaterEqual(contrast(self.dim(label), fill), 4.5,
                                    order)
            self.assertGreaterEqual(contrast(self.dim(chip.caption),
                                             chip.fill()), 4.5, order)

    def test_dim_text_stays_quieter_than_the_values(self):
        self.use_scheme("light")
        _card, label = self.card(None)
        dim = self.dim(label).lightness()
        text = label.palette().color(
            tiles.QPalette.ColorRole.WindowText).lightness()
        self.assertGreater(dim, text + 30)

    def test_a_chips_name_stays_quieter_than_its_value(self):
        self.use_scheme("light")
        card, _label = self.card(None)
        chip = tiles.Chip(card)
        dim = self.dim(chip.caption).lightness()
        text = chip.value.palette().color(
            tiles.QPalette.ColorRole.WindowText).lightness()
        self.assertGreater(dim, text + 25)

    def test_a_chips_name_follows_a_theme_switch(self):
        from PyQt6.QtGui import QColor
        self.use_scheme("light")
        card, _label = self.card(None)
        chip = tiles.Chip(card)
        light = self.dim(chip.caption)
        self.use_scheme("dark")
        self.assertNotEqual(self.dim(chip.caption), light)
        self.assertEqual(self.dim(chip.caption),
                         QColor(*self.SCHEMES["dark"]["PlaceholderText"]))

    def test_a_dark_scheme_keeps_its_own_dim_text(self):
        self.use_scheme("dark")
        _card, label = self.card(None)
        self.assertEqual(self.dim(label).getRgb()[:3],
                         self.SCHEMES["dark"]["PlaceholderText"])

    def test_dim_text_follows_a_switch_to_dark_and_back(self):
        """Written into the card's palette, so it must be written again:
        a colour left there would keep a light theme's grey on a dark card."""
        self.use_scheme("light")
        card, label = self.card(None)
        light = self.dim(label).getRgb()[:3]
        self.use_scheme("dark")
        self.assertEqual(self.dim(label).getRgb()[:3],
                         self.SCHEMES["dark"]["PlaceholderText"])
        # Everything else still follows the theme too.
        self.assertEqual(card.palette().color(
            tiles.QPalette.ColorRole.Window).getRgb()[:3],
            self.SCHEMES["dark"]["Window"])
        self.use_scheme("light")
        self.assertEqual(self.dim(label).getRgb()[:3], light)

    def test_a_light_card_shows_its_colour(self):
        """Blending near-white towards a muted accent only greyed it: the
        cards came out a few levels off white and the RAM and GPU ones the
        same grey-pink. A card has to be its colour, and not the ground's."""
        from ksyssupervisor.widgets import backdrop_colour
        self.use_scheme("light")
        ground = backdrop_colour(self.app.palette())
        fills = {}
        for order in tiles.DEVICE_COLOURS:
            accent = tiles.device_colour(order)
            card, _label = self.card(accent)
            fill = card.grab().toImage().pixelColor(60, 30)
            fills[order] = fill
            # Coloured, in the accent's hue, where the ground is a grey.
            self.assertGreater(fill.hslSaturation(),
                               ground.hslSaturation() + 60, order)
            self.assertLess(abs(fill.hslHue() - accent.hslHue()), 3, order)
        # And the purple and red cards are told apart.
        self.assertGreater(sum(abs(a - b) for a, b in zip(
            fills[20].getRgb()[:3], fills[30].getRgb()[:3])), 30)


class ThemeSwitchTest(TileTestCase):
    def test_the_menu_bar_is_repolished_on_a_palette_change(self):
        """Breeze kept the old text colour in the menu bar until reopened."""
        from unittest import mock
        from PyQt6.QtCore import QSettings
        from ksyssupervisor import window as window_mod
        original = window_mod.fit_on_screen
        window_mod.fit_on_screen = lambda w, a, b, fraction=0.9: (a, b)
        self.addCleanup(setattr, window_mod, "fit_on_screen", original)
        settings = QSettings("KSysSupervisorTest", "Theme")
        settings.clear()

        window = window_mod.KSysSupervisor(settings=settings)
        self.addCleanup(window._stop_worker)
        # Delivered straight to the window rather than broadcast through
        # QApplication.setPalette: the broadcast is what the platform theme
        # does on the desktop, but it only fires when the palette actually
        # differs, which made this flaky. PaletteChange is the per-widget
        # event the broadcast turns into, and the one changeEvent receives.
        from PyQt6.QtCore import QEvent
        with mock.patch.object(window_mod, "repolish") as repolished:
            QApplication.sendEvent(window, QEvent(QEvent.Type.PaletteChange))
        self.assertTrue(repolished.called)
        touched = repolished.call_args[0]
        self.assertIn(window.menuBar(), touched)
        self.assertIn(window.statusBar(), touched)


class CellLayoutTest(TileTestCase):
    def _board(self, count=6):
        board = TileBoard()
        models = builder().build(sample())
        # Same three models over and over is enough: the layout only ever
        # looks at how many items there are.
        board.sync(models)
        return board

    def _rects(self, board, width=1000, height=800):
        board.resize(width, height)
        board._flow.setGeometry(board._content.rect())
        return [board._flow.itemAt(i).geometry()
                for i in range(board._flow.count())]

    def test_every_tile_gets_the_same_cell(self):
        """Slots are positions, not consequences of what the neighbours hold."""
        rects = self._rects(self._board())
        sizes = {(r.width(), r.height()) for r in rects}
        self.assertEqual(len(sizes), 1)
        self.assertEqual(rects[0].height(), self._board().cell_height())

    def test_the_cell_is_measured_from_a_full_tile_not_fixed(self):
        board = self._board()
        self.assertGreater(board.cell_height(), 150)
        self.assertNotEqual(board.cell_height(), 260)

    def test_a_wider_board_needs_fewer_rows(self):
        board = self._board()
        tall = board._flow.heightForWidth(400)
        wide = board._flow.heightForWidth(1600)
        self.assertLess(wide, tall)

    def test_columns_follow_the_width_and_stop_at_the_cap(self):
        board = self._board()
        flow = board._flow
        self.assertEqual(flow.columns_for(400), 1)
        self.assertEqual(flow.columns_for(700), 2)
        # Never more columns than there are tiles, and never past the cap.
        self.assertLessEqual(flow.columns_for(4000), flow.max_columns)

    def test_tiles_do_not_overlap(self):
        rects = self._rects(self._board())
        for first in range(len(rects)):
            for second in range(first + 1, len(rects)):
                self.assertFalse(rects[first].intersects(rects[second]),
                                 "%s overlaps %s" % (rects[first],
                                                     rects[second]))

    def test_a_point_maps_to_the_slot_under_it_and_clamps(self):
        board = self._board()
        rects = self._rects(board)
        flow = board._flow
        width = board._content.width()
        for index, rect in enumerate(rects):
            self.assertEqual(flow.slot_at(rect.center(), width), index)
        # The gap after a cell belongs to it; past everything is the last.
        gap = rects[0].topRight() + QPoint(4, 0)
        self.assertEqual(flow.slot_at(gap, width), 0)
        far = QPoint(width - 1, rects[-1].bottom() + 500)
        self.assertEqual(flow.slot_at(far, width), len(rects) - 1)


class ScrollingTileTest(TileTestCase):
    """The same cell for every tile; the body scrolls, the sections fold."""

    def _board(self, cores=24):
        board = TileBoard()
        board.resize(1000, 700)
        board.show()
        board.sync(builder().build(sample(cores=cores)))
        self.app.processEvents()
        return board

    def test_the_body_scrolls_and_the_sections_still_fold(self):
        board = self._board()
        cpu = board._tiles["cpu"]
        self.assertEqual(cpu.height(), board.cell_height())
        section = cpu.more
        self.assertFalse(section.expanded())
        board.set_all_expanded(True)
        self.assertTrue(section.expanded())
        # Opening the section did not change the cell; the body scrolls.
        self.app.processEvents()
        self.assertEqual(cpu.height(), board.cell_height())
        self.assertGreater(cpu.scrollbar.maximum(), 0)

    def test_the_scroll_bar_is_only_a_handle_in_the_tile_colour(self):
        # Enough rows that the handle is well short of the bar.
        board = self._board(cores=24)
        cpu = board._tiles["cpu"]
        board.set_all_expanded(True)
        self.app.processEvents()
        bar = cpu.scrollbar
        self.assertEqual(bar.width(), tiles.ScrollHandle.WIDTH)
        self.assertEqual(bar._colour, tiles.device_colour(10))
        handle = bar._handle()
        self.assertIsNotNone(handle)
        self.assertLess(handle.height(), bar.height())
        # No groove: beside the handle the bar is the plain background that
        # its far corner is, and only the handle itself is painted.
        image = bar.grab().toImage()
        beside = image.pixelColor(int(handle.center().x()),
                                  int(handle.bottom()) + 6)
        corner = image.pixelColor(0, bar.height() - 1)
        on = image.pixelColor(int(handle.center().x()),
                              int(handle.center().y()))
        self.assertEqual(beside, corner)
        self.assertNotEqual(on, corner)

    def test_a_hot_tile_colours_its_scroll_bar_too(self):
        board = self._board()
        board.sync(builder().build(sample(temp=95.0, cores=8)))
        self.assertEqual(board._tiles["cpu"].scrollbar._colour,
                         tiles.level_colour("hot"))

    def test_dragging_the_handle_scrolls(self):
        from PyQt6.QtTest import QTest
        board = self._board(cores=16)
        cpu = board._tiles["cpu"]
        board.set_all_expanded(True)
        self.app.processEvents()
        bar = cpu.scrollbar
        handle = bar._handle()
        start = QPoint(int(handle.center().x()), int(handle.center().y()))
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(bar, start + QPoint(0, 40))
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton,
                           pos=start + QPoint(0, 40))
        self.assertGreater(bar.value(), 0)
        # A click on the empty track jumps there.
        QTest.mouseClick(bar, Qt.MouseButton.LeftButton,
                         pos=QPoint(start.x(), bar.height() - 2))
        self.assertEqual(bar.value(), bar.maximum())


class TileBoardTest(TileTestCase):
    def test_the_board_builds_one_tile_per_model(self):
        board = TileBoard()
        board.sync(builder().build(sample()))
        self.assertEqual(board.order(), ["cpu", "ram", GPU.key])

    def test_a_device_that_disappears_takes_its_tile_with_it(self):
        board = TileBoard()
        build = builder()
        board.sync(build.build(sample()))
        board.sync([m for m in build.build(sample()) if m.key != "ram"])
        self.assertNotIn("ram", board.order())

    def test_reordering_moves_the_tile_and_keeps_the_rest(self):
        board = TileBoard()
        board.sync(builder().build(sample()))
        board.apply_order([GPU.key, "cpu", "ram"])
        self.assertEqual(board.order(), [GPU.key, "cpu", "ram"])

    def test_an_order_naming_unknown_devices_is_survivable(self):
        board = TileBoard()
        board.sync(builder().build(sample()))
        board.apply_order(["gone", "ram", "also-gone"])
        self.assertEqual(sorted(board.order()), sorted(["cpu", "ram", GPU.key]))

    def test_a_second_identical_sample_writes_nothing(self):
        """Nothing moved, so no row should be touched."""
        board = TileBoard()
        build = builder()
        board.sync(build.build(sample()))

        writes = []
        original = tiles.MetricRow.set_metric

        def counting(self, metric):
            changed = original(self, metric)
            if changed:
                writes.append(metric.uid)
            return changed

        tiles.MetricRow.set_metric = counting
        try:
            board.sync(build.build(sample()))
        finally:
            tiles.MetricRow.set_metric = original
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()


class HeadlineLayoutTest(TileTestCase):
    """Rings for readings with a top, chips for the rest."""

    def models(self):
        return {m.key: m for m in builder().build(sample())}

    def test_a_cpu_leads_with_its_load_ring_and_chips(self):
        tile = tiles.DeviceTile(self.models()["cpu"])
        self.assertEqual([type(w) for w in tile._headlines],
                         [tiles.RingGauge, tiles.Chip, tiles.Chip])

    def test_two_chips_sit_beside_each_ring_and_the_rest_under(self):
        tile = tiles.DeviceTile(self.models()["cpu"])
        tile.resize(tiles.TILE_WIDTH, 400)
        tile.show()
        self.app.processEvents()
        strip = tile.headline_row
        chips = [TileTestCase.box(c, strip) for c in strip.chips]
        ring = TileTestCase.box(strip.rings[0], strip)
        # Both beside the ring, stacked.
        self.assertTrue(all(c.left() > ring.right() for c in chips))
        self.assertLess(chips[0].bottom(), chips[1].top())

    def test_the_third_chip_starts_a_line_under_the_ring(self):
        from ksyssupervisor.display import TileConfig
        build = builder()
        build.configs = {"cpu": TileConfig(
            custom=True, chips=("cpu/Tctl", "cpu/rapl_Package",
                                "cpu/clock_0", "cpu/clock_1"))}
        tile = tiles.DeviceTile(build.build(sample(cores=4))[0])
        tile.resize(tiles.TILE_WIDTH, 400)
        tile.show()
        self.app.processEvents()
        strip = tile.headline_row
        ring = TileTestCase.box(strip.rings[0], strip)
        third, fourth = [TileTestCase.box(c, strip) for c in strip.chips[2:]]
        self.assertGreater(third.top(), ring.bottom())
        self.assertLessEqual(third.left(), ring.left())
        self.assertEqual(third.top(), fourth.top())

    THEMES = {"dark": ("#202326", "#141618", "#1c1f21"),
              "light": ("#eff0f1", "#ffffff", "#e3e5e7")}

    def palette(self, theme):
        from PyQt6.QtGui import QColor
        window, base, mid = self.THEMES[theme]
        palette = tiles.QPalette()
        palette.setColor(tiles.QPalette.ColorRole.Window, QColor(window))
        palette.setColor(tiles.QPalette.ColorRole.Base, QColor(base))
        palette.setColor(tiles.QPalette.ColorRole.Mid, QColor(mid))
        return palette

    @staticmethod
    def card(palette, accent):
        window = palette.color(tiles.QPalette.ColorRole.Window)
        dark = window.lightness() < 128
        if dark:
            return blend(window.lighter(112), accent, 0.21)
        return light_tint(accent)

    def test_a_chip_is_a_well_with_a_touch_of_the_tile_in_either_theme(self):
        """A light theme's Base is white: a chip filled with it stood out
        of the card as a raised button instead of sinking into it."""
        accent = tiles.device_colour(20)

        def distance(a, b):
            return sum(abs(x - y) for x, y in zip(a.getRgb()[:3],
                                                   b.getRgb()[:3]))

        for theme in self.THEMES:
            palette = self.palette(theme)
            plain, tinted = tiles.Chip(), tiles.Chip()
            for chip in (plain, tinted):
                chip.setPalette(palette)
            tinted.set_colour(accent)
            self.assertLess(distance(tinted.fill(), accent),
                            distance(plain.fill(), accent), theme)
            self.assertLess(tinted.fill().lightness(),
                            self.card(palette, accent).lightness(), theme)

    def test_a_rings_track_stands_out_from_its_card_in_either_theme(self):
        """A light scheme's Mid can be the card's own colour, which left
        the track invisible."""
        accent = tiles.device_colour(10)
        for theme in self.THEMES:
            palette = self.palette(theme)
            card = self.card(palette, accent)
            track = tiles.track_colour(palette)
            alpha = track.alphaF()
            seen = blend(card, track, alpha)          # the track over the card
            self.assertGreater(abs(card.lightness() - seen.lightness()), 8,
                               theme)

    def test_ram_leads_with_a_ring(self):
        tile = tiles.DeviceTile(self.models()["ram"])
        self.assertEqual([type(w) for w in tile._headlines],
                         [tiles.RingGauge])
        tile.resize(320, 260)
        self.assertFalse(tile.grab().isNull())

    def test_a_ring_shows_the_share_inside_and_the_amount_under(self):
        ram = self.models()["ram"]
        self.assertEqual(tiles.ring_texts(ram.rings[0]),
                         ("38%", "12.0 / 32.0 GB"))

    def test_a_changed_layout_rebuilds_the_headline(self):
        from ksyssupervisor.display import TileConfig
        build = builder()
        tile = tiles.DeviceTile(build.build(sample())[0])
        build.configs = {"cpu": TileConfig(custom=True,
                                           rings=("cpu/usage_total",),
                                           chips=("cpu/Tctl",))}
        tile.sync(build.build(sample())[0])
        self.assertEqual([type(w) for w in tile._headlines],
                         [tiles.RingGauge, tiles.Chip])

    def test_a_tile_with_nothing_to_lead_with_has_no_headline(self):
        build = TileBuilder({})
        model = build.build([Reading("odd", "Fans", "fan1", "Fan", 900.0,
                                     "fan")])[0]
        tile = tiles.DeviceTile(model)
        self.assertEqual(tile._headlines, [])
        self.assertFalse(tile.headline_row.isVisibleTo(tile))


class EditModeTest(TileTestCase):
    def tile(self, cores=4):
        tile = tiles.DeviceTile(builder().build(sample(cores=cores))[0])
        tile.set_cell_height(200)
        return tile

    def test_editing_swaps_the_body_for_the_editor(self):
        tile = self.tile()
        tile.set_editing(True)
        self.assertTrue(tile.editor.isVisibleTo(tile))
        self.assertFalse(tile.body.isVisibleTo(tile))
        self.assertTrue(tile.edit_button.isChecked())
        uids = {m.uid for _p, m in tile.model().items}
        self.assertEqual(set(tile.editor._rows), uids)

        tile.editor.done_requested.emit()
        self.assertFalse(tile.editing())
        self.assertTrue(tile.body.isVisibleTo(tile))

    def test_a_placement_asks_for_a_new_config(self):
        tile = self.tile()
        tile.set_editing(True)
        seen = []
        tile.config_changed.connect(lambda key, config: seen.append(
            (key, config)))
        tile.editor._rows["cpu/clock_2"].placement_requested.emit(
            "cpu/clock_2", "chip")
        self.app.processEvents()             # the emission is deferred
        [(key, config)] = seen
        self.assertEqual(key, "cpu")
        self.assertEqual(config.chips[-1], "cpu/clock_2")

    def test_only_a_reading_with_a_top_is_offered_as_a_ring(self):
        tile = self.tile()
        tile.set_editing(True)
        temp = tile.editor._rows["cpu/Tctl"].actions["ring"]
        self.assertFalse(temp.isEnabled())
        self.assertIn("needs a maximum", temp.text())

    def test_a_locked_ring_offers_nothing_to_change(self):
        tile = self.tile()
        tile.set_editing(True)
        row = tile.editor._rows["cpu/usage_total"]
        self.assertTrue(row.locked)
        self.assertFalse(row.place_button.isEnabled())
        self.assertFalse(row.up.isEnabled() or row.down.isEnabled())

    def test_a_full_chip_row_refuses_another(self):
        tile = self.tile()
        build = builder()
        build.configs = {"cpu": tile.model().config.__class__(
            custom=True, chips=("cpu/Tctl", "cpu/rapl_Package", "cpu/clock_0",
                                "cpu/clock_2", "cpu/clock_3", "cpu/clock_4",
                                "cpu/clock_5", "cpu/clock_6"))}
        tile.sync(build.build(sample(cores=8))[0])
        tile.set_editing(True)
        chip = tile.editor._rows["cpu/clock_1"].actions["chip"]
        self.assertFalse(chip.isEnabled())
        self.assertIn("full", chip.text())

    def test_an_editing_tile_takes_more_rows(self):
        tile = self.tile(cores=16)
        tile.show()
        self.assertEqual(tile.row_span(), 1)
        spans = []
        tile.resized.connect(lambda: spans.append(tile.row_span()))
        tile.set_editing(True)
        self.app.processEvents()             # the editor's rows get shown
        self.assertGreater(spans[-1], 1)
        self.assertLessEqual(tile.row_span(), tiles.DeviceTile.EDIT_SPAN_MAX)

    def test_the_menu_offers_to_customize(self):
        tile = self.tile()
        texts = []
        from unittest import mock
        with mock.patch.object(tiles.QMenu, "exec",
                               lambda menu, *a: texts.extend(
                                   x.text() for x in menu.actions())):
            tile._menu(QPoint(5, 5))
        self.assertIn("Customize Tile...", texts)
        # A tile shows no min/max, so it offers nothing to clear.
        self.assertFalse([t for t in texts if "min/max" in t])


class SpanLayoutTest(TileTestCase):
    def test_a_tall_tile_keeps_its_column_and_the_rest_flow_around(self):
        board = TileBoard()
        board.sync(builder().build(sample()))
        flow = board._flow
        cpu = board._tiles["cpu"]
        self.assertEqual(flow.placements(2), [(0, 0, 1), (1, 0, 1), (0, 1, 1)])
        cpu.set_cell_height(flow.cell_height)
        cpu.row_span = lambda: 3
        self.assertEqual(flow.placements(2), [(0, 0, 3), (1, 0, 1), (1, 1, 1)])
        # The board grows by the extra rows the tall tile needs.
        self.assertEqual(flow.heightForWidth(700),
                         3 * flow.cell_height + 2 * 8 + 16)


class DragDropTest(TileTestCase):
    """A dragged tile shows where it will land and moves only on release."""

    def board(self):
        board = TileBoard()
        board.resize(1100, 700)
        board.show()
        board.sync(builder().build(sample()))
        self.app.processEvents()
        return board

    def test_nothing_moves_until_the_button_is_let_go(self):
        board = self.board()
        cpu = board._tiles["cpu"]
        last = board._tiles[GPU.key].geometry().center()
        seen = []
        board.order_changed.connect(seen.append)

        before = {key: tile.geometry() for key, tile in board._tiles.items()}
        board._drag_start(cpu)
        board._drag_move(cpu, board._content.mapToGlobal(last))
        self.assertEqual(board.order(), ["cpu", "ram", GPU.key])
        shown = [m for m in board.markers if m.isVisibleTo(board)]
        # Where the dragged tile lands, filled, then an outline for each tile
        # that makes way - in that tile's own colour.
        self.assertEqual(shown[0].geometry(), before[GPU.key])
        self.assertEqual(shown[0]._colour, cpu.accent())
        self.assertTrue(shown[0]._filled)
        moved = {(m.geometry().x(), m.geometry().y()): m for m in shown[1:]}
        self.assertEqual(set(moved), {(before["cpu"].x(), before["cpu"].y()),
                                      (before["ram"].x(), before["ram"].y())})
        ram_to = moved[(before["cpu"].x(), before["cpu"].y())]
        self.assertEqual(ram_to._colour, board._tiles["ram"].accent())
        self.assertFalse(ram_to._filled)
        self.assertIsNotNone(cpu.graphicsEffect())
        self.assertEqual(seen, [])

        board._drag_end(cpu)
        self.assertEqual(board.order(), ["ram", GPU.key, "cpu"])
        self.assertEqual(seen, [["ram", GPU.key, "cpu"]])
        self.assertFalse([m for m in board.markers if m.isVisibleTo(board)])
        self.assertFalse(board.ghost.isVisibleTo(board))
        self.assertIsNone(cpu.graphicsEffect())

    def test_a_drop_back_where_it_started_changes_nothing(self):
        board = self.board()
        cpu = board._tiles["cpu"]
        board._drag_start(cpu)
        board._drag_move(cpu, board._content.mapToGlobal(
            cpu.geometry().center()))
        board._drag_end(cpu)
        self.assertEqual(board.order(), ["cpu", "ram", GPU.key])

    def test_a_tile_that_vanishes_mid_drag_ends_the_drag(self):
        board = self.board()
        build = builder()
        cpu = board._tiles["cpu"]
        board._drag_start(cpu)
        board.sync([m for m in build.build(sample()) if m.key != "cpu"])
        self.assertIsNone(board._dragging)
        self.assertFalse([m for m in board.markers if m.isVisibleTo(board)])

    def test_a_tile_that_does_not_move_gets_no_outline(self):
        board = self.board()
        ram = board._tiles["ram"]
        gpu = board._tiles[GPU.key]
        board._drag_start(ram)
        board._drag_move(ram, board._content.mapToGlobal(
            gpu.geometry().center()))
        shown = [m for m in board.markers if m.isVisibleTo(board)]
        # RAM and the card trade places; the CPU stays and is not outlined.
        self.assertEqual(len(shown), 2)
        cpu = board._tiles["cpu"].geometry()
        self.assertNotIn(cpu, [m.geometry() for m in shown])


class TileExtrasTest(TileTestCase):
    """Renaming chips in place, the tile's colour, its defaults and heat."""

    def tile(self, build=None, **kwargs):
        build = build or builder()
        tile = tiles.DeviceTile(build.build(sample(**kwargs))[0])
        tile.set_cell_height(200)
        seen = []
        tile.config_changed.connect(lambda key, config: seen.append(config))
        return tile, seen

    def test_names_are_offered_for_renaming_only_while_editing(self):
        tile, _seen = self.tile()
        chip = tile.headline_row.chips[0]
        self.assertFalse(chip.renamable())
        self.assertFalse(chip.caption.font().underline())
        tile.set_editing(True)
        self.assertTrue(chip.renamable())
        self.assertTrue(chip.caption.font().underline())
        tile.set_editing(False)
        self.assertFalse(chip.renamable())

    def test_clicking_a_name_edits_it_in_place(self):
        from PyQt6.QtTest import QTest
        tile, seen = self.tile()
        tile.show()
        tile.set_editing(True)
        chip = tile.headline_row.chips[0]
        QTest.mouseClick(chip.caption, Qt.MouseButton.LeftButton)
        self.assertTrue(chip.editing())
        chip.name_edit.setText("Die")
        chip.name_edit.editingFinished.emit()
        self.app.processEvents()
        self.assertFalse(chip.editing())
        self.assertEqual(seen[-1].label("cpu/!hotspot"), "Die")

    def test_escape_keeps_the_old_name(self):
        from PyQt6.QtTest import QTest
        tile, seen = self.tile()
        tile.show()
        tile.set_editing(True)
        chip = tile.headline_row.chips[0]
        chip.start_editing()
        chip.name_edit.setText("Die")
        QTest.keyClick(chip.name_edit, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(chip.editing())
        self.assertEqual(seen, [])

    def test_typing_its_own_name_back_clears_the_override(self):
        from ksyssupervisor import display
        build = builder()
        build.configs = {"cpu": display.rename(display.TileConfig(),
                                               "cpu/!hotspot", "Die")}
        tile, seen = self.tile(build)
        tile._rename("cpu/!hotspot", "Hot Spot")
        self.app.processEvents()
        self.assertEqual(seen[-1], display.TileConfig())

    def test_a_chosen_colour_is_asked_for_and_kept(self):
        from PyQt6.QtGui import QColor
        tile, seen = self.tile()
        tile._ask_colour = lambda current: QColor("#336699")
        tile.choose_colour()
        self.app.processEvents()
        self.assertEqual(seen[-1].colour, "#336699")
        # Choosing the device's own colour is no override at all.
        tile._ask_colour = lambda current: tiles.device_colour(10)
        tile.choose_colour()
        self.app.processEvents()
        self.assertEqual(seen, [seen[0]])     # unchanged from the default

    def test_a_tile_draws_in_the_colour_it_was_given(self):
        from ksyssupervisor import display
        build = builder()
        build.configs = {"cpu": display.recolour(display.TileConfig(),
                                                 "#336699")}
        tile, _seen = self.tile(build)
        self.assertEqual(tile.accent().name(), "#336699")

    def test_restore_defaults_resets_the_layout_and_the_name(self):
        from ksyssupervisor import display
        build = builder()
        build.configs = {"cpu": display.recolour(display.TileConfig(),
                                                 "#336699")}
        tile, seen = self.tile(build)
        names = []
        tile.reset_name_requested.connect(names.append)
        tile.set_editing(True)
        self.assertTrue(tile.editor.reset_button.isEnabled())
        tile.editor.reset_button.click()
        self.app.processEvents()
        self.assertEqual(seen[-1], display.TileConfig())
        self.assertEqual(names, ["cpu"])

    def test_restore_defaults_keeps_the_name(self):
        from ksyssupervisor import display
        build = builder()
        build.configs = {"cpu": display.recolour(display.TileConfig(),
                                                 "#336699")}
        tile, seen = self.tile(build)
        names = []
        tile.reset_name_requested.connect(names.append)
        tile.restore_defaults()
        self.app.processEvents()                # the emit is deferred
        self.assertEqual(seen[-1], display.TileConfig())
        self.assertEqual(names, [])

    def menu_texts(self, tile, pos):
        from unittest import mock
        texts = []
        with mock.patch.object(tiles.QMenu, "exec",
                               lambda menu, *a: texts.extend(
                                   (x.text(), x) for x in menu.actions())):
            tile._menu(pos)
        return dict(texts)

    def test_the_menu_offers_both_restores(self):
        tile, _seen = self.tile()
        texts = self.menu_texts(tile, QPoint(2, 2))
        self.assertIn("Restore Defaults", texts)
        self.assertIn("Restore Defaults and Name", texts)

    def test_a_reading_can_be_sent_to_the_graphs_from_its_tile(self):
        tile, _seen = self.tile()
        tile.resize(360, 300)
        tile.show()
        self.app.processEvents()
        from ksyssupervisor import display
        # Not a slot still waiting for its reading: that has no history.
        chip = next(c for c in tile.headline_row.chips
                    if display.graph_uid(c._metric))
        pos = chip.mapTo(tile, QPoint(chip.width() // 2,
                                      chip.height() // 2))
        self.assertIs(tile.metric_at(pos), chip._metric)
        wanted = []
        tile.graph_requested.connect(wanted.append)
        actions = self.menu_texts(tile, pos)
        label = 'Show "%s" in Live Graphs' % chip._metric.label
        self.assertIn(label, actions)
        actions[label].trigger()
        self.assertEqual(wanted, [chip._metric.uid])

    def test_an_empty_slot_offers_nothing_to_graph(self):
        from ksyssupervisor import display
        tile, _seen = self.tile()
        tile.resize(360, 300)
        tile.show()
        self.app.processEvents()
        chip = next(c for c in tile.headline_row.chips
                    if not display.graph_uid(c._metric))
        pos = chip.mapTo(tile, QPoint(chip.width() // 2,
                                      chip.height() // 2))
        self.assertFalse([t for t in self.menu_texts(tile, pos)
                          if "Live Graphs" in t])

    def test_away_from_a_reading_there_is_nothing_to_graph(self):
        tile, _seen = self.tile()
        tile.resize(360, 300)
        tile.show()
        self.app.processEvents()
        self.assertIsNone(tile.metric_at(QPoint(2, 2)))
        self.assertFalse([t for t in self.menu_texts(tile, QPoint(2, 2))
                          if "Live Graphs" in t])

    def test_heat_is_said_in_words_too(self):
        tile, _seen = self.tile(temp=95.0)
        self.assertTrue(tile.heat.isVisibleTo(tile))
        self.assertEqual(tile.heat._level, "hot")
        self.assertIn("95.0", tile.heat.toolTip())
        cool, _seen = self.tile(temp=45.0)
        self.assertFalse(cool.heat.isVisibleTo(cool))

    def test_the_controls_explain_themselves_in_the_tiles_colour(self):
        tile, _seen = self.tile()
        colour = tiles.device_colour(10).name()
        for widget in (tile.edit_button, tile.grip):
            self.assertIn(colour, widget.toolTip())
        tile.set_editing(True)
        self.assertIn(colour, tile.editor.done_button.toolTip())
        row = tile.editor._rows["cpu/Tctl"]
        self.assertIn(colour, row.place_button.toolTip())

    def test_chips_beside_a_ring_are_centred_on_its_circle(self):
        tile, _seen = self.tile()
        tile.resize(tiles.TILE_WIDTH, 400)
        tile.show()
        self.app.processEvents()
        strip = tile.headline_row
        ring = self.box(strip.rings[0], strip)
        first, second = [self.box(c, strip) for c in strip.chips[:2]]
        circle = ring.top() + tiles.RingGauge.circle_centre()
        pair = (first.top() + second.bottom() + 1) / 2
        self.assertLessEqual(abs(pair - circle), 2)

    def test_the_first_chips_sit_at_one_height_on_every_tile(self):
        """A ring captioned in two lines (RAM), in one (CPU load), and no
        ring at all (a board): the chips were at three different heights."""
        board = Device("mobo", "Board", order=60)
        build = TileBuilder({d.key: d for d in (CPU, RAM, board)})
        readings = sample() + [
            Reading("ram", "Temperatures", "d%d" % n, "DIMM %d" % n, 35.0,
                    "temp") for n in (2, 4)] + [
            Reading("mobo", "Temperatures", "t%d" % n, "T%d" % n, 40.0 + n,
                    "temp") for n in range(4)]
        tops = {}
        for model in build.build(readings):
            if model.key not in ("cpu", "ram", "mobo"):
                continue
            tile = tiles.DeviceTile(model)
            tile.resize(tiles.TILE_WIDTH, 400)
            tile.show()
            self.app.processEvents()
            chips = tile.headline_row.chips
            tops[model.key] = [self.box(c, tile).top() for c in chips[:2]]
        self.assertEqual(tops["cpu"], tops["ram"])
        self.assertEqual(tops["cpu"][0], tops["mobo"][0])

    def test_drive_rings_sit_side_by_side_with_the_chips_under(self):
        from ksyssupervisor.display import TileBuilder as Builder
        disks = [Device("disk:sd%s" % c, "Disk %s" % c, order=40)
                 for c in "ab"]
        build = Builder({d.key: d for d in disks})
        readings = []
        for disk in disks:
            readings += [
                Reading(disk.key, "Temperatures", "temp1", "Temp1", 33.0,
                        "temp"),
                Reading(disk.key, "Utilization", "vol:/mnt/" + disk.key[-1],
                        disk.key[-1], 1.0, "memory_gb", total=10.0)]
        tile = tiles.DeviceTile(build.build(readings)[0])
        tile.resize(tiles.TILE_WIDTH, 400)
        tile.show()
        self.app.processEvents()
        strip = tile.headline_row
        rings = [self.box(r, strip) for r in strip.rings]
        chips = [self.box(c, strip) for c in strip.chips]
        self.assertEqual(rings[0].top(), rings[1].top())
        self.assertLess(rings[0].right(), rings[1].left())
        self.assertTrue(all(c.top() > rings[0].bottom() for c in chips))
        self.assertEqual(chips[0].top(), chips[1].top())      # two columns

    def test_the_tile_name_is_renamed_in_place_while_editing(self):
        from PyQt6.QtTest import QTest
        tile, _seen = self.tile()
        tile.show()
        names = []
        tile.name_changed.connect(lambda key, name: names.append((key, name)))
        self.assertFalse(tile.title.renamable())
        QTest.mouseClick(tile.name_label, Qt.MouseButton.LeftButton)
        self.assertFalse(tile.title.editing())      # not until editing

        tile.set_editing(True)
        self.assertTrue(tile.name_label.font().underline())
        self.assertTrue(tile.name_label.font().bold())
        QTest.mouseClick(tile.name_label, Qt.MouseButton.LeftButton)
        self.assertTrue(tile.title.editing())
        self.assertEqual(tile.title.edit.text(), "AMD Ryzen 7 9700X")
        tile.title.edit.setText("Radeon 6800")
        tile.title.edit.editingFinished.emit()
        self.assertEqual(names, [("cpu", "Radeon 6800")])
        self.assertFalse(tile.title.editing())

    def test_escape_leaves_the_tile_name_alone(self):
        from PyQt6.QtTest import QTest
        tile, _seen = self.tile()
        tile.show()
        names = []
        tile.name_changed.connect(lambda key, name: names.append(name))
        tile.set_editing(True)
        tile.title.start_editing()
        tile.title.edit.setText("Something")
        QTest.keyClick(tile.title.edit, Qt.Key.Key_Escape)
        self.assertEqual(names, [])
        self.assertEqual(tile.name_label.text(), "AMD Ryzen 7 9700X")

