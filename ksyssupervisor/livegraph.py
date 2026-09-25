"""Live graphs: one strip per chosen reading, docked beside the main window.

Drawn in the manner of AIDA64's stress-test charts - black plot, dim grid,
clock times along the bottom, a bright line per reading with its current value
beside it - because that is a look people already know how to read at a glance.
That is the dark theme's plot. A light theme gets the same layout on its own
base colour, with a neutral grid and deeper line colours that read on white,
rather than a black slab in an otherwise light window.

Hovering a strip shows the exact time and value under the cursor.

The panel is a dock widget rather than a separate window. On Wayland an
application cannot place its own windows, so a free-standing graph window
could not be kept at the main window's side; a dock can, on every platform,
and can still be pulled out to float.
"""

import math
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                         QPalette, QPen)
from PyQt6.QtWidgets import (QComboBox, QDockWidget, QHBoxLayout, QLabel,
                             QMenu, QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

from . import display, series
from .widgets import (THEME_EVENTS, KeepOpenMenu, Menu, blend,
                      dim_text_colour, make_transparent, tool_tip)

#: Line colour per kind of reading: bright on black, as on AIDA64's charts.
LINE_COLOURS = {
    "temp": "#ff6a3d",
    "utilization": "#3fd0ff",
    "clock": "#ffd23f",
    "power": "#d17bff",
    "fan": "#7dff6a",
    "voltage": "#f2f2f2",
    "memory_gb": "#4f9dff",
    "memory": "#4f9dff",
}
DEFAULT_COLOUR = "#7dffda"

#: The same hues deepened for a light plot: each clears 4.2:1 against white,
#: so the current value, drawn in the line's colour, reads as text too.
#: Voltage's near-white becomes a dark grey for the same reason.
LIGHT_LINE_COLOURS = {
    "temp": "#d2461c",
    "utilization": "#0a7fb0",
    "clock": "#9a6c00",
    "power": "#8e3cc8",
    "fan": "#2d8a1e",
    "voltage": "#4a4a4a",
    "memory_gb": "#2a62c9",
    "memory": "#2a62c9",
}
LIGHT_DEFAULT_COLOUR = "#0f8a74"


class PlotColours:
    """Everything a strip paints with, for one theme."""

    def __init__(self, background, grid, axis_text, title_text, cursor,
                 hover_fill, lines, default_line):
        self.background = background
        self.grid = grid
        self.axis_text = axis_text
        self.title_text = title_text
        self.cursor = cursor
        self.hover_fill = hover_fill
        self.lines = lines
        self.default_line = default_line

    def line(self, kind):
        return QColor(self.lines.get(kind, self.default_line))


DARK_PLOT = PlotColours(
    background=QColor("#000000"),
    # A cool neutral grey, Breeze Dark's own cast, rather than AIDA64's
    # green: the grid is there to be read against, not looked at, and a
    # green one tinted every line colour drawn over it.
    grid=QColor(46, 50, 55),
    axis_text=QColor(150, 156, 163),
    title_text=QColor(235, 235, 235),
    cursor=QColor(255, 255, 255, 150),
    hover_fill=QColor(20, 20, 20, 230),
    lines=LINE_COLOURS,
    default_line=DEFAULT_COLOUR)


def plot_colours(palette):
    """The AIDA look on a dark theme; the theme's own base on a light one.

    Built from the palette at paint time rather than stored, so a light/dark
    switch is followed on the next repaint. A stylesheet would resolve the
    colours once, at polish time, and keep the old theme's.
    """
    window = palette.color(QPalette.ColorRole.Window)
    if window.lightness() < 128:
        return DARK_PLOT
    base = palette.color(QPalette.ColorRole.Base)
    text = palette.color(QPalette.ColorRole.Text)
    cursor = QColor(text)
    cursor.setAlpha(140)
    hover = QColor(base)
    hover.setAlpha(240)
    return PlotColours(
        background=base,
        grid=blend(base, text, 0.12),
        axis_text=dim_text_colour(palette),
        title_text=text,
        cursor=cursor,
        hover_fill=hover,
        lines=LIGHT_LINE_COLOURS,
        default_line=LIGHT_DEFAULT_COLOUR)

STRIP_HEIGHT = 128
LEFT = 52           # room for the value labels on the left
BOTTOM = 16         # room for the clock times underneath
TOP = 22            # the title band
RIGHT = 8

#: The panel's width when docked, and so how much the main window widens by.
PANEL_WIDTH = 440


def width_after_panel(width, taken, widened):
    """The width to return to once a side panel is hidden.

    `widened` is (width before, width after) opening the panel. Untouched
    since, the window goes back to exactly its old width - which is not the
    same as giving back what the panel took, because the panel's minimum width
    can have grown the window by more than was asked. Resized since, the user
    has chosen a new size, and only the panel's own share is taken off it.
    """
    before, after = widened
    if width == after:
        return before
    return max(1, width - taken)


def span_label(seconds):
    return "%d min" % (seconds // 60)


def axis_text(value, kind, step):
    """A grid value, with as many decimals as the step needs and no more."""
    decimals = 0
    while decimals < 3 and abs(step * 10 ** decimals - round(step * 10 ** decimals)) > 1e-9:
        decimals += 1
    return display.format_value(value, kind, decimals=decimals)


#: What a strip calls its device, by the start of the device's key. Short,
#: because the title shares a narrow strip with the value; hovering the strip
#: shows the full name in its place.
SHORT_NAMES = {
    "cpu": "CPU",
    "gpu": "GPU",
    "ram": "RAM",
    "mobo": "Board",
    "battery": "Battery",
}


def _renamed(model):
    return bool(model.device_name) and model.name != model.device_name


def device_labels(models):
    """key -> the device's part of a strip title, "CPU", "GPU", "Drive sdb".

    A device the user renamed goes by that name: they chose it, and it is
    what the tiles and the tree call it too. Two cards would both be "GPU",
    so a short name more than one device shares is numbered in device order.
    A drive is named by its kernel name, which is unique already.
    """
    labels = {}
    for model in models:
        prefix, _sep, rest = model.key.partition(":")
        if _renamed(model):
            labels[model.key] = model.name
        elif prefix == "disk" and rest:
            labels[model.key] = "Drive %s" % rest
        else:
            labels[model.key] = SHORT_NAMES.get(prefix, model.name)
    shared = {}
    for model in models:
        if not _renamed(model) and model.key.partition(":")[0] in SHORT_NAMES:
            shared.setdefault(labels[model.key], []).append(model.key)
    for label, keys in shared.items():
        if len(keys) > 1:
            for number, key in enumerate(keys, 1):
                labels[key] = "%s %d" % (label, number)
    return labels


def _reading_labels(model):
    """uid -> the reading's part of a strip title, "Package (°C)".

    A temperature always carries its unit. A label a device uses twice -
    "Package" is a temperature and a power on a CPU - is told apart the same
    way, "Package (W)": shorter than the group's name, and already the word
    that says what the number is. Only where the units do not separate them
    - two readings of one kind - does the group step in.
    """
    metrics = [metric for _group, group_metrics in model.groups
               for metric in group_metrics]
    counts = {}
    for metric in metrics:
        counts[metric.label] = counts.get(metric.label, 0) + 1

    def with_unit(metric):
        unit = display.unit_for(metric.kind)
        if unit and (metric.kind == "temp" or counts[metric.label] > 1):
            return "%s (%s)" % (metric.label, unit)
        return metric.label

    named = {}
    for metric in metrics:
        label = with_unit(metric)
        named[label] = named.get(label, 0) + 1
    labels = {}
    for group, group_metrics in model.groups:
        for metric in group_metrics:
            label = with_unit(metric)
            if named[label] > 1:
                label = "%s (%s)" % (metric.label, group)
            labels[metric.uid] = label
    return labels


def strip_titles(models):
    """uid -> strip title, device first and reading after: "CPU · Package".

    The device part is kept short (see device_labels) so the reading is not
    what the elision takes.
    """
    devices = device_labels(models)
    titles = {}
    for model in models:
        for uid, label in _reading_labels(model).items():
            titles[uid] = "%s · %s" % (devices[model.key], label)
    return titles


def strip_full_titles(models):
    """uid -> the title shown while the strip is hovered: the device's own
    full name in place of its short one, "AMD Ryzen 7 9700X · Package (°C)".

    The hardware's name even for a device the user renamed - the short title
    already says what they called it, and hovering is how to find out which
    hardware that is.
    """
    titles = {}
    for model in models:
        name = model.device_name or model.name
        for uid, label in _reading_labels(model).items():
            titles[uid] = "%s · %s" % (name, label)
    return titles


class GraphStrip(QWidget):
    """One reading's graph. Painted, not styled, so it follows a theme switch."""

    remove_requested = pyqtSignal(str)

    def __init__(self, uid, parent=None):
        super().__init__(parent)
        self.uid = uid
        self.title = uid
        self.kind = None
        self.total = None
        self.points = []
        self.span = series.DEFAULT_SPAN
        self.now = time.time()
        self.full_title = None           # shown instead while hovered
        self._hover_x = None
        self._hover_y = None
        self.setMouseTracking(True)
        self.setMinimumHeight(STRIP_HEIGHT)
        self.setMaximumHeight(STRIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def set_data(self, title, kind, total, points, span, now,
                 full_title=None):
        self.title, self.kind, self.total = title, kind, total
        self.points, self.span, self.now = points, span, now
        self.full_title = full_title
        self.update()

    def shown_title(self):
        """The short title, or the full one while the mouse is over the strip."""
        if self._hover_x is not None and self.full_title:
            return self.full_title
        return self.title

    def colour(self):
        return plot_colours(self.palette()).line(self.kind)

    # ---- geometry --------------------------------------------------------

    def plot_rect(self):
        return QRectF(LEFT, TOP, max(1.0, self.width() - LEFT - RIGHT),
                      max(1.0, self.height() - TOP - BOTTOM))

    def _x(self, when, plot):
        start = self.now - self.span
        return plot.left() + (when - start) / self.span * plot.width()

    def _y(self, value, low, high, plot):
        share = 0.5 if high <= low else (value - low) / (high - low)
        return plot.bottom() - share * plot.height()

    # ---- painting --------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._colours = plot_colours(self.palette())
        painter.fillRect(self.rect(), self._colours.background)

        plot = self.plot_rect()
        values = series.finite(self.points)
        low, high, step = series.axis_range(values, self.kind, self.total)

        small = QFont(self.font())
        small.setPointSizeF(max(6.5, small.pointSizeF() * 0.8))
        painter.setFont(small)
        metrics = QFontMetrics(small)

        self._paint_grid(painter, plot, low, high, step, metrics)
        self._paint_line(painter, plot, low, high)
        self._paint_title(painter, metrics)
        self._paint_hover(painter, plot, low, high, metrics)
        painter.end()

    def _paint_grid(self, painter, plot, low, high, step, metrics):
        painter.setPen(QPen(self._colours.grid, 1))
        painter.drawRect(plot)

        value = low
        while value <= high + step * 1e-6:
            y = self._y(value, low, high, plot)
            painter.setPen(QPen(self._colours.grid, 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(self._colours.axis_text)
            text = axis_text(value, self.kind, step)
            painter.drawText(QRectF(0, y - metrics.height() / 2, LEFT - 4,
                                    metrics.height()),
                             Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter, text)
            value += step

        # Vertical lines on round clock times, labelled with the time itself
        # rather than "-3 min": a spike is worth knowing the moment of.
        every = self.span / 5.0
        first = math.ceil((self.now - self.span) / every) * every
        when = first
        while when <= self.now:
            x = self._x(when, plot)
            painter.setPen(QPen(self._colours.grid, 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            label = time.strftime("%H:%M:%S", time.localtime(when))
            width = metrics.horizontalAdvance(label)
            left = min(max(plot.left(), x - width / 2), plot.right() - width)
            painter.setPen(self._colours.axis_text)
            painter.drawText(QRectF(left, plot.bottom() + 1, width + 2,
                                    BOTTOM - 1),
                             Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter, label)
            when += every

    def _paint_line(self, painter, plot, low, high):
        path = QPainterPath()
        drawing = False
        for when, value in self.points:
            if math.isnan(value):
                drawing = False             # a gap stays a gap
                continue
            point = QPointF(self._x(when, plot),
                            self._y(min(max(value, low), high), low, high,
                                    plot))
            if drawing:
                path.lineTo(point)
            else:
                path.moveTo(point)
                drawing = True
        painter.save()
        painter.setClipRect(plot)
        painter.setPen(QPen(self._colours.line(self.kind), 1.6))
        painter.drawPath(path)
        painter.restore()

    def _paint_title(self, painter, metrics):
        """The title, and the current value in the line's colour.

        No minimum or maximum beside it: the line already shows both, and
        hovering reads any point exactly.
        """
        current = self.points[-1][1] if self.points else float("nan")
        value_text = (display.MISSING if math.isnan(current)
                      else display.format_value(current, self.kind))

        bold = QFont(painter.font())
        bold.setBold(True)
        bold_metrics = QFontMetrics(bold)
        value_width = bold_metrics.horizontalAdvance(value_text) + 4

        room = self.width() - LEFT - RIGHT - value_width - 12
        painter.setPen(self._colours.title_text)
        painter.drawText(QRectF(LEFT, 2, max(0, room), TOP - 4),
                         Qt.AlignmentFlag.AlignLeft
                         | Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(self.shown_title(),
                                            Qt.TextElideMode.ElideRight,
                                            max(0, int(room))))
        painter.save()
        painter.setFont(bold)
        painter.setPen(self._colours.line(self.kind))
        painter.drawText(QRectF(self.width() - RIGHT - value_width, 2,
                                value_width, TOP - 4),
                         Qt.AlignmentFlag.AlignRight
                         | Qt.AlignmentFlag.AlignVCenter, value_text)
        painter.restore()

    def _paint_hover(self, painter, plot, low, high, metrics):
        point = self.hovered_point()
        if point is None:
            return
        when, value = point
        x = self._x(when, plot)
        painter.setPen(QPen(self._colours.cursor, 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        if math.isnan(value):
            text = "%s   no reading" % time.strftime("%H:%M:%S",
                                                    time.localtime(when))
            y = plot.center().y()
        else:
            y = self._y(min(max(value, low), high), low, high, plot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._colours.line(self.kind))
            painter.drawEllipse(QPointF(x, y), 3.5, 3.5)
            text = "%s   %s" % (time.strftime("%H:%M:%S", time.localtime(when)),
                                display.format_value(value, self.kind))

        width = metrics.horizontalAdvance(text) + 12
        height = metrics.height() + 6
        box = self.readout_rect(plot, x, y, width, height)
        painter.setPen(QPen(self._colours.line(self.kind), 1))
        painter.setBrush(self._colours.hover_fill)
        painter.drawRoundedRect(box, 3, 3)
        painter.setPen(self._colours.title_text)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)

    def readout_rect(self, plot, x, y, width, height):
        """Where the hover readout goes, for the cursor line at `x` and the
        point it reads at `y`.

        Beside the line, on the side with room, so it never hides the point
        it describes or runs off the strip; and above the mouse rather than
        the point, so the pointer is never on top of it - or below the
        pointer's arrow when there is no room above.
        """
        gap_x, gap_y = self.READOUT_GAP
        left = (x + gap_x if x + gap_x + width <= plot.right()
                else x - gap_x - width)
        mouse = self._hover_y if self._hover_y is not None else y
        top = mouse - gap_y - height
        if top < plot.top() + 2:
            top = mouse + self.POINTER_HEIGHT
        top = min(max(plot.top() + 2, top), plot.bottom() - height - 2)
        return QRectF(left, top, width, height)

    # ---- hover -----------------------------------------------------------

    #: How far the readout box keeps from the cursor, (across, up), in px.
    READOUT_GAP = (16, 12)
    #: Roughly how far a mouse pointer's arrow reaches below its tip.
    POINTER_HEIGHT = 22

    #: How far, in pixels, the cursor snaps to a recorded point. Further
    #: than that, the readout is the cursor's own time with no reading.
    SNAP = 4

    def hovered_point(self):
        """The (time, value) under the cursor, or None when not hovering.

        Anywhere over the plot, recorded or not: the cursor used to jump to
        the nearest point, so over the part of the span from before the app
        was started it sat on the first reading instead of under the mouse.
        A point within SNAP pixels - or the tick either side, at a span wide
        enough to put several ticks in one pixel - is what the cursor reads.
        """
        if self._hover_x is None:
            return None
        plot = self.plot_rect()
        if not plot.left() <= self._hover_x <= plot.right():
            return None
        per_pixel = self.span / plot.width()
        when = (self.now - self.span
                + (self._hover_x - plot.left()) * per_pixel)
        index = series.nearest(self.points, when)
        if index is not None:
            point = self.points[index]
            if abs(point[0] - when) <= max(self.SNAP * per_pixel, 1.5):
                return point
        return (when, float("nan"))

    def mouseMoveEvent(self, event):
        self._hover_x = event.position().x()
        self._hover_y = event.position().y()
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_x = self._hover_y = None
        self.update()
        super().leaveEvent(event)

    def _menu(self, position):
        menu = QMenu(self)
        action = menu.addAction("Remove from Live Graphs")
        action.triggered.connect(lambda: self.remove_requested.emit(self.uid))
        action.setToolTip(tool_tip("graphs", "Remove",
                                   "Stop graphing this reading. Its history "
                                   "is kept: add it back and it is all "
                                   "there."))
        menu.setToolTipsVisible(True)
        menu.exec(self.mapToGlobal(position))


class LiveGraphPanel(QWidget):
    """The strips, the span choice and the picker for what to graph."""

    selection_changed = pyqtSignal(list)
    span_changed = pyqtSignal(int)

    def __init__(self, selection=(), span=series.DEFAULT_SPAN, parent=None):
        super().__init__(parent)
        self._selection = [uid for uid in selection]
        self._strips = {}
        self._titles = {}           # uid -> "Device · Label"
        self._full_titles = {}      # uid -> "Full device name · Label"
        self._models = []
        self.span = span if span in series.SPANS else series.DEFAULT_SPAN

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        self.add_button = QPushButton("Add Metric")
        self.add_menu = KeepOpenMenu("Add Metric", self)
        self.add_menu.aboutToShow.connect(self._fill_menu)
        self.add_button.setMenu(self.add_menu)
        self.add_button.setToolTip(tool_tip(
            "graphs", "Add Metric", "Pick readings to graph, by device and "
            "group. The menu stays open so you can tick several."))
        bar.addWidget(self.add_button)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(lambda: self.set_selection([]))
        self.clear_button.setToolTip(tool_tip(
            "graphs", "Clear", "Remove every graph. The readings' history "
            "is kept."))
        bar.addWidget(self.clear_button)
        bar.addStretch(1)
        bar.addWidget(QLabel("Show"))
        self.span_box = QComboBox()
        self.span_box.setToolTip(tool_tip(
            "graphs", "Time span", "How far back each graph reaches. Hover "
            "over a graph to read the value at any point; right-click it "
            "to remove it."))
        for seconds in series.SPANS:
            self.span_box.addItem(span_label(seconds), seconds)
        self.span_box.setCurrentIndex(series.SPANS.index(self.span))
        self.span_box.currentIndexChanged.connect(self._span_picked)
        bar.addWidget(self.span_box)
        layout.addLayout(bar)

        self.empty = QLabel("Nothing graphed yet.\n\nChoose readings with Add "
                            "Metric, or right-click one in the tree view and "
                            "pick Show in Live Graphs.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # A role, not a colour: a colour written into the palette would stay
        # behind after a light/dark switch.
        self.empty.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(self.empty, 1)

        content = QWidget()
        self._column = QVBoxLayout(content)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(4)
        self._column.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidget(content)
        self.scroll.setWidgetResizable(True)
        # Nothing in here takes keys, and a focused scroll area is drawn by
        # Breeze with a highlight-coloured frame: clicking a graph outlined
        # the whole panel in blue for no reason.
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        make_transparent(self.scroll)
        layout.addWidget(self.scroll, 1)

        self._sync_strips()

    # ---- selection -------------------------------------------------------

    def selection(self):
        return list(self._selection)

    def set_selection(self, uids):
        uids = list(dict.fromkeys(uids))
        if uids == self._selection:
            return
        self._selection = uids
        self._sync_strips()
        self.selection_changed.emit(list(uids))

    def add(self, uid):
        if uid not in self._selection:
            self.set_selection(self._selection + [uid])

    def remove(self, uid):
        self.set_selection([u for u in self._selection if u != uid])

    def toggle(self, uid, wanted):
        (self.add if wanted else self.remove)(uid)

    def _sync_strips(self):
        for uid in [u for u in self._strips if u not in self._selection]:
            strip = self._strips.pop(uid)
            self._column.removeWidget(strip)
            strip.deleteLater()
        for position, uid in enumerate(self._selection):
            strip = self._strips.get(uid)
            if strip is None:
                strip = GraphStrip(uid)
                strip.remove_requested.connect(self.remove)
                self._strips[uid] = strip
            self._column.removeWidget(strip)
            self._column.insertWidget(position, strip)
        has_any = bool(self._selection)
        self.empty.setVisible(not has_any)
        self.scroll.setVisible(has_any)
        self.clear_button.setEnabled(has_any)

    def _span_picked(self, index):
        self.span = self.span_box.itemData(index)
        self.span_changed.emit(self.span)

    # ---- data ------------------------------------------------------------

    def set_models(self, models):
        """This tick's tree models: names for the strips, and the picker."""
        self._models = list(models)
        self._titles = strip_titles(self._models)
        self._full_titles = strip_full_titles(self._models)

    def refresh(self, store, now=None):
        now = time.time() if now is None else now
        for uid, strip in self._strips.items():
            strip.set_data(self._titles.get(uid, uid), store.kind(uid),
                           store.total(uid),
                           store.points(uid, self.span, now), self.span, now,
                           self._full_titles.get(uid))

    def title(self, uid):
        return self._titles.get(uid, uid)

    def _fill_menu(self):
        """Device > group > reading, ticked where it is already graphed.

        Built each time it opens, from the latest tick, so a device that
        appeared since - a drive plugged in - is there to pick.
        """
        self.add_menu.clear()
        if not self._models:
            self.add_menu.addAction("No readings yet").setEnabled(False)
            return
        for model in self._models:
            device_menu = Menu(model.name, self.add_menu)
            self.add_menu.addMenu(device_menu)
            for group, metrics in model.groups:
                group_menu = KeepOpenMenu(group, device_menu)
                device_menu.addMenu(group_menu)
                for metric in metrics:
                    action = group_menu.addAction(metric.label)
                    action.setCheckable(True)
                    action.setChecked(metric.uid in self._selection)
                    action.toggled.connect(
                        lambda wanted, uid=metric.uid: self.toggle(uid, wanted))


class LiveGraphDock(QDockWidget):
    """The panel, docked on the main window's right edge."""

    closed = pyqtSignal()

    def __init__(self, panel, parent=None):
        super().__init__("Live Graphs", parent)
        self.setObjectName("live-graphs")
        self.setWidget(panel)
        self.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea
                             | Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.setMinimumWidth(300)

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(PANEL_WIDTH)
        return hint

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()
