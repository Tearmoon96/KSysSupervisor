"""The tile widgets: one card per device, read at a glance.

Every widget here renders a display.Metric and decides nothing for itself -
what a row is called, how it is rounded and whether it leads the tile are all
settled before a widget sees it.

Nothing sets a stylesheet. Colours come from the palette at paint time, which
is what makes a light/dark switch land everywhere instead of on everything
except the scroll bars.
"""

from dataclasses import replace

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QActionGroup, QBrush, QColor, QFont, QFontMetrics, QIcon,
                         QLinearGradient, QPainter, QPainterPath,
                         QPainterPathStroker, QPalette,
                         QPen, QPolygonF)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QMenu,
                             QPushButton,
                             QScrollArea, QSizePolicy, QToolButton,
                             QVBoxLayout, QWidget)

from . import display
from .widgets import (LIGHT_TINT_LIGHTNESS, THEME_EVENTS, Card, Chevron,
                      ElidingLabel, blend, dim_text_colour, light_tint,
                      make_transparent, tip, tool_tip)

#: How wide a tile asks to be, and how narrow it will go. Both hints are
#: overridden rather than fixing a width, so the board can open showing whole
#: names and still be dragged narrow. Never QSizePolicy.Ignored: that keeps
#: both hints out of the parent layout entirely.
TILE_WIDTH = 320
TILE_MIN_WIDTH = 250

#: Detail rows go into two columns past this many, so sixteen core clocks are
#: a block rather than a column running off the bottom of the window - but
#: only where there is room for two readable rows side by side.
DETAIL_COLUMNS_AT = 7
DETAIL_TWO_COLUMN_WIDTH = 440

# One colour per kind of device, by Device.order, so a glance at the board
# finds the graphics card without reading a title. Fixed colours rather than
# palette roles, for the same reason the fan window's amber is fixed: a role
# means "selected" or "disabled", and none of them means "this is the GPU".
# Muted - mid lightness, low saturation - so a board left open all day is
# not five saturated panels, and legible tinted into a light or a dark card.
DEVICE_COLOURS = {
    10: "#4c8dd9",    # CPU, blue
    20: "#9a7bc8",    # RAM, purple
    30: "#c07070",    # GPU, dusty red
    40: "#65a57a",    # drives, sage green
    60: "#c2aa5c",    # mainboard, ochre yellow
    70: "#6dbfd6",    # battery, cyan
}
DEFAULT_COLOUR = "#7f8b99"

# Heat: saturated where the device colours are dusty, and the GPU's red and
# the board's yellow are kept well clear of them. Colour alone no longer
# carries it either - a hot tile says so in words in its header - because a
# red card running hot and a red card at rest are too alike to rely on.
LEVEL_COLOURS = {"warn": "#e8a33d", "hot": "#e0524a"}
LEVEL_WORDS = {"warn": "WARM", "hot": "HOT"}


def device_colour(order):
    return QColor(DEVICE_COLOURS.get(order, DEFAULT_COLOUR))


def tile_colour(model):
    """The colour a tile is drawn in: the user's, or its device's."""
    if model is not None and model.colour:
        return QColor(model.colour)
    return device_colour(model.order if model is not None else None)


def level_colour(level):
    colour = LEVEL_COLOURS.get(level)
    return QColor(colour) if colour else None


class MoveGrip(QWidget):
    """The four-way arrow that says a tile can be dragged somewhere else.

    Drawn rather than themed: no icon theme reliably ships a move glyph, and
    the six dots this replaces were a palette grey at low alpha, which on a
    tinted card came out as very nearly nothing.
    """

    pressed = pyqtSignal()
    moved = pyqtSignal(object)          # global QPoint
    released = pyqtSignal()

    SIZE = 22                           # the square; the glyph is centred
    ARM = 7.0                           # centre to arrowhead tip
    HEAD = 3.4                          # half-width of an arrowhead

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Drag to move this tile")
        self._hover = False
        self._dragging = False

    def _colour(self):
        """White on a dark card, its opposite on a light one.

        Asked for as white, and white is what a dark desktop gets. A light
        one cannot have it: that is the same invisibility this widget exists
        to fix, just the other way up.
        """
        window = self.palette().color(QPalette.ColorRole.Window)
        if window.lightness() < 128:
            colour = QColor(255, 255, 255)
        else:
            colour = QColor(55, 60, 66)
        colour.setAlpha(255 if self._hover else 205)
        return colour

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = self._colour()

        middle = self.SIZE / 2.0
        painter.setPen(QPen(colour, 1.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.FlatCap))
        stem = self.ARM - self.HEAD
        painter.drawLine(QPointF(middle - stem, middle),
                         QPointF(middle + stem, middle))
        painter.drawLine(QPointF(middle, middle - stem),
                         QPointF(middle, middle + stem))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            tip = QPointF(middle + dx * self.ARM, middle + dy * self.ARM)
            base = QPointF(middle + dx * stem, middle + dy * stem)
            # The arrowhead's base is square to whichever way it points.
            across = QPointF(-dy * self.HEAD, -dx * self.HEAD)
            painter.drawPolygon(QPolygonF([
                tip,
                QPointF(base.x() + across.x(), base.y() + across.y()),
                QPointF(base.x() - across.x(), base.y() - across.y())]))
        painter.end()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange):
            self.update()
        super().changeEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self.pressed.emit()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        self.moved.emit(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        self.released.emit()
        event.accept()


def coloured(metric):
    """The value, tinted by a rich-text span when it is running hot.

    Rich text rather than a colour written into the widget's palette: one
    written there outlives the theme it was chosen for, and rather than a
    stylesheet, which would drag the widget and its children onto Qt's
    stylesheet painting path.
    """
    colour = level_colour(metric.level)
    if colour is None:
        return metric.text
    return '<span style="color:%s">%s</span>' % (colour.name(), metric.text)


def small_font(font):
    font = QFont(font)
    font.setPointSizeF(max(font.pointSizeF() - 1, 6.0))
    return font


def _tooltip(metric):
    # "Package" is a temperature and a power on a CPU; the group says which.
    if metric is None:
        return ""
    return ("%s · %s" % (metric.group, metric.label) if metric.group
            else metric.label)


# The inset look shared by chips and rings: light from above, so a well is
# shaded under its top edge, rimmed dark along the top and catching a little
# light along the bottom. Kept faint - it is texture, not decoration.
INSET_SHADE = 40            # alpha of the shadow under the top edge
INSET_RIM = 62              # alpha of the dark rim at the top
INSET_RIM_MID = 18          # and halfway down
INSET_CATCH = 22            # alpha of the light caught along the bottom


def well_colour(palette):
    """What a sunken part of a card is filled with, before any tint.

    A step darker than the card, in either theme. Base - the colour of a
    text box - is that on a dark theme, and is what the chips were always
    filled with there; on a light one Base is white, lighter than the card,
    and a white pill with a shadow along its top reads as a raised button.
    Only a shallow step on a light theme: the card there is already lifted
    towards Base, and a deep well put the dim caption in it out of reach.
    """
    window = palette.color(QPalette.ColorRole.Window)
    base = QColor(palette.color(QPalette.ColorRole.Base))
    if window.lightness() >= 128:
        return window.darker(104)
    if base.lightness() > window.lightness():
        return window.darker(109)
    return base


def track_colour(palette):
    """A ring's empty track: visibly darker than the card it is on.

    Not the Mid role: on a dark theme it is, but a light scheme can derive
    Mid so close to the window that the track vanished into the card.
    """
    window = palette.color(QPalette.ColorRole.Window)
    if window.lightness() >= 128:
        return window.darker(122)
    track = QColor(palette.color(QPalette.ColorRole.Mid))
    track.setAlpha(90)
    return track


def _shade(top, height):
    shade = QLinearGradient(0, top, 0, top + height * 0.55)
    shade.setColorAt(0.0, QColor(0, 0, 0, INSET_SHADE))
    shade.setColorAt(1.0, QColor(0, 0, 0, 0))
    return shade


def _rim(top, bottom, flipped=False):
    """Dark at the top edge, light at the bottom; `flipped` the other way
    round, for the inner edge of a ring, whose wall faces the other way."""
    rim = QLinearGradient(0, top, 0, bottom)
    dark, catch = QColor(0, 0, 0, INSET_RIM), QColor(255, 255, 255,
                                                      INSET_CATCH)
    rim.setColorAt(0.0, catch if flipped else dark)
    rim.setColorAt(0.5, QColor(0, 0, 0, INSET_RIM_MID))
    rim.setColorAt(1.0, dark if flipped else catch)
    return QPen(QBrush(rim), 1)


def paint_groove(painter, shape, colour, outer, inner):
    """Fill `shape` - part or all of the band between two circles - as a
    groove sunk into the card: the ring's track, or the arc filling it."""
    painter.save()
    painter.fillPath(shape, colour)
    painter.setClipPath(shape)
    painter.fillRect(outer, _shade(outer.top(), outer.height()))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(_rim(outer.top(), outer.bottom()))
    painter.drawEllipse(outer.adjusted(0.5, 0.5, -0.5, -0.5))
    painter.setPen(_rim(inner.top(), inner.bottom(), flipped=True))
    painter.drawEllipse(inner.adjusted(-0.5, -0.5, 0.5, 0.5))
    painter.restore()


class InlineName(QWidget):
    """A name that can be edited where it stands.

    Offered for editing, it is underlined with a hand cursor, and a click
    swaps it for an edit box: Enter or clicking away keeps the new name,
    Escape keeps the old one. What an empty name means is the owner's to
    decide - for a chip and a tile alike, the name it came with.
    """

    renamed = pyqtSignal(str)

    def __init__(self, text="", minimum=30, parent=None):
        super().__init__(parent)
        self._renamable = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.label = ElidingLabel(text, minimum=minimum)
        row.addWidget(self.label, 1)
        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.hide()
        self.edit.editingFinished.connect(self._commit)
        row.addWidget(self.edit, 1)
        # Both exist before either is watched: the filter asks after both.
        self.label.installEventFilter(self)
        self.edit.installEventFilter(self)

    def setFont(self, font):
        super().setFont(font)
        self.edit.setFont(font)
        font = QFont(font)
        font.setUnderline(self._renamable)
        self.label.setFont(font)

    def setText(self, text):
        self.label.setText(text)

    def text(self):
        return self.label.text()

    def renamable(self):
        return self._renamable

    def set_renamable(self, renamable):
        renamable = bool(renamable)
        if renamable == self._renamable:
            return
        self._renamable = renamable
        font = QFont(self.label.font())
        font.setUnderline(renamable)
        self.label.setFont(font)
        self.label.setCursor(Qt.CursorShape.PointingHandCursor if renamable
                             else Qt.CursorShape.ArrowCursor)
        if not renamable:
            self.stop_editing()

    def start_editing(self):
        if not self._renamable:
            return
        self.label.hide()
        self.edit.setText(self.label.text())
        self.edit.show()
        self.edit.setFocus(Qt.FocusReason.MouseFocusReason)
        self.edit.selectAll()

    def editing(self):
        return self.edit.isVisible()

    def stop_editing(self):
        self.edit.hide()
        self.label.show()

    def _commit(self):
        if not self.edit.isVisible():
            return
        text = self.edit.text().strip()
        self.stop_editing()
        if text != self.label.text():
            self.renamed.emit(text)

    def eventFilter(self, watched, event):
        if (watched is self.label and self._renamable
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            self.start_editing()
            return True
        if (watched is self.edit and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape):
            self.stop_editing()
            return True
        return super().eventFilter(watched, event)


class Chip(QWidget):
    """A glance reading as a small pill: value, then its name.

    While the tile is being edited the name is underlined and clicking it
    edits it in place (see InlineName); an empty name goes back to the
    reading's own.
    """

    renamed = pyqtSignal(str, str)          # uid, new name ("" = its own)

    #: How far the pill is pulled towards the tile's colour: a touch, so it
    #: stays the dark well it was and only leans towards its tile.
    TINT = 0.12
    #: How much darker than its card a light theme's pill is, in HSL
    #: lightness. Deeper since the pill lost its inset there: the step in
    #: shade is now all that sets it off the card.
    WELL_DEPTH = 0.08
    #: A light theme's name in the pill, a touch darker than the card's own
    #: dim text (LIGHT_DIM_PULL): on the deeper pill that one fell to 4.3:1
    #: on the purple. Still well short of the value's full strength.
    CAPTION_PULL = 0.65

    def __init__(self, parent=None):
        super().__init__(parent)
        self._metric = None
        self._colour = None
        # Its own height, whatever the ring beside it needs.
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 3, 9, 3)
        row.setSpacing(6)
        self.value = QLabel(display.MISSING)
        font = QFont(self.value.font())
        font.setBold(True)
        self.value.setFont(font)
        row.addWidget(self.value)
        self.name = InlineName(minimum=30)
        self.name.renamed.connect(self._renamed)
        self.caption = self.name.label
        self.name_edit = self.name.edit
        self.caption.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        # Value against the left end, name against the right: the numbers
        # of a column of chips line up, and so do their names.
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        self.caption.setAlignment(right)
        self.name_edit.setAlignment(right)
        row.addWidget(self.name, 1)
        self._sync_caption()

    def set_metric(self, metric):
        if metric == self._metric:
            return
        self._metric = metric
        self.caption.setText(metric.label if metric else "")
        self.value.setText(coloured(metric) if metric else display.MISSING)
        self._sync_tip()

    def set_colour(self, colour):
        colour = QColor(colour) if colour is not None else None
        if colour != self._colour:
            self._colour = colour
            self._sync_tip()
            self.update()

    # ---- renaming --------------------------------------------------------

    def renamable(self):
        return self.name.renamable()

    def set_renamable(self, renamable):
        """Offer the name for editing - not a slot still waiting for its
        reading, which has no name of its own to change."""
        self.name.set_renamable(
            renamable and self._metric is not None
            and not display._is_placeholder(self._metric))
        self._sync_tip()

    def start_editing(self):
        self.name.start_editing()

    def editing(self):
        return self.name.editing()

    def _renamed(self, text):
        if self._metric is not None:
            self.renamed.emit(self._metric.uid, text)

    def _sync_tip(self):
        metric = self._metric
        if metric is None:
            self.setToolTip("")
            return
        body = metric.group or ""
        if self.renamable():
            body = ("Click the name to rename this reading on the tile. "
                    "Leave it empty to go back to its own name.")
        self.setToolTip(tip(metric.label, body, self._colour))

    # ---- painting --------------------------------------------------------

    def _sync_caption(self):
        """The pill's dim text, worked out again on every theme event so it
        cannot outlive its theme - the same way Card keeps its own."""
        palette = self.palette()
        colour = dim_text_colour(palette, self.CAPTION_PULL)
        if palette.color(QPalette.ColorRole.PlaceholderText) == colour:
            return
        palette = QPalette(palette)
        palette.setColor(QPalette.ColorRole.PlaceholderText, colour)
        self.setPalette(palette)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self._sync_caption()
            self.update()
        super().changeEvent(event)

    def fill(self):
        """The pill's inside: the dark well it always was, with a touch of
        the tile's colour in it."""
        palette = self.palette()
        dark = palette.color(QPalette.ColorRole.Window).lightness() < 128
        base = well_colour(palette)
        if self._colour is not None:
            # On a light theme, a deeper shade of the card's own pastel: a
            # grey well blended towards the colour sat muddy on it.
            base = (blend(base, self._colour, self.TINT) if dark
                    else light_tint(self._colour,
                                    LIGHT_TINT_LIGHTNESS - self.WELL_DEPTH))
        # On a dark theme a little of the card shows through, as it always
        # did; on a light one the well is solid.
        base.setAlpha(190 if dark else 255)
        return base

    def paintEvent(self, event):
        # Painted from the palette at paint time, like every card here, so a
        # theme switch reaches it; a stylesheet would keep the old colours.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, self.fill())

        # Inset on a dark theme only: see INSET_SHADE. On a light one the
        # shadow and rim read as a busy outline round every pill, and the
        # pastel step from the card already sets the chip apart; the rings
        # keep theirs there, a groove being what a ring is.
        if self.palette().color(QPalette.ColorRole.Window).lightness() >= 128:
            painter.end()
            super().paintEvent(event)
            return
        painter.setClipPath(path)
        painter.fillRect(rect, _shade(rect.top(), rect.height()))
        painter.setClipping(False)
        painter.setPen(_rim(rect.top(), rect.bottom()))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()
        super().paintEvent(event)


def ring_texts(metric):
    """(inside, amount) for a ring: the share, and what it is a share of.

    "7.0 / 32.0 GB" is too long for the inside of a ring, and the ring is
    already the picture of how full; so the percentage goes inside and the
    amount underneath, where it has the width of the caption.
    """
    if metric is None or metric.value is None:
        return display.MISSING, ""
    fraction = display.ring_fraction(metric)
    inside = "%d%%" % round(fraction * 100) if fraction is not None \
        else metric.text
    return inside, (metric.text if metric.total else "")


class RingGauge(QWidget):
    """A glance reading with a top: a ring, its share inside, name and amount
    underneath."""

    SIZE = 62
    THICKNESS = 6
    GAP = 3
    TOP = 1                 # the circle's top, inside the widget

    @classmethod
    def circle_centre(cls):
        return cls.TOP + cls.SIZE / 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._metric = None
        self._colour = QColor(DEFAULT_COLOUR)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)

    def _lines(self):
        return 2 if self._metric is not None and self._metric.total else 1

    def sizeHint(self):
        metrics = QFontMetrics(small_font(self.font()))
        _inside, amount = ring_texts(self._metric)
        width = max(self.SIZE, metrics.horizontalAdvance(amount)) + 8
        return QSize(width, self.SIZE + self.GAP
                     + self._lines() * metrics.height() + 2)

    def minimumSizeHint(self):
        hint = self.sizeHint()
        hint.setWidth(self.SIZE + 8)
        return hint

    def set_metric(self, metric):
        if metric == self._metric:
            return
        reshaped = (self._metric is None or metric is None
                    or bool(metric.total) != bool(self._metric.total))
        self._metric = metric
        self.setToolTip(_tooltip(metric))
        if reshaped:
            self.updateGeometry()
        self.update()

    def set_colour(self, colour):
        self._colour = QColor(colour)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        metric = self._metric

        left = (self.width() - self.SIZE) / 2.0
        ring = QRectF(left, self.TOP, self.SIZE, self.SIZE).adjusted(
            self.THICKNESS / 2, self.THICKNESS / 2,
            -self.THICKNESS / 2, -self.THICKNESS / 2)

        # The band the ring occupies, as two circles: sunk into the card
        # like the chips, the track and the arc filling it alike.
        half = self.THICKNESS / 2
        outer = ring.adjusted(-half, -half, half, half)
        inner = ring.adjusted(half, half, -half, -half)
        band = QPainterPath()
        band.setFillRule(Qt.FillRule.OddEvenFill)
        band.addEllipse(outer)
        band.addEllipse(inner)
        paint_groove(painter, band, track_colour(palette), outer, inner)

        fraction = display.ring_fraction(metric) if metric else None
        if fraction:
            # From twelve o'clock, clockwise, like every gauge people know.
            arc = QPainterPath()
            arc.arcMoveTo(ring, 90)
            arc.arcTo(ring, 90, -360 * min(fraction, 1.0))
            # Flat: the groove is the track's, and the colour lies in it.
            stroker = QPainterPathStroker()
            stroker.setWidth(self.THICKNESS)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.fillPath(stroker.createStroke(arc), self._colour)

        inside, amount = ring_texts(metric)
        room = ring.width() - self.THICKNESS * 2 - 2
        font = QFont(self.font())
        font.setBold(True)
        size = font.pointSizeF() + 2
        font.setPointSizeF(size)
        while size > 6 and QFontMetrics(font).horizontalAdvance(inside) > room:
            size -= 0.5
            font.setPointSizeF(size)
        painter.setPen(palette.color(QPalette.ColorRole.WindowText))
        painter.setFont(font)
        painter.drawText(ring, Qt.AlignmentFlag.AlignCenter, inside)

        small = small_font(self.font())
        metrics = QFontMetrics(small)
        painter.setFont(small)
        top = self.SIZE + self.GAP
        lines = [(metric.label if metric else "",
                  QPalette.ColorRole.PlaceholderText)]
        if amount:
            lines.insert(0, (amount, QPalette.ColorRole.WindowText))
        for text, role in lines:
            painter.setPen(palette.color(role))
            painter.drawText(QRectF(0, top, self.width(), metrics.height()),
                             Qt.AlignmentFlag.AlignHCenter
                             | Qt.AlignmentFlag.AlignTop,
                             metrics.elidedText(text,
                                                Qt.TextElideMode.ElideRight,
                                                self.width()))
            top += metrics.height()
        painter.end()


class HeadlineStrip(QWidget):
    """The top of a tile: its rings, each with two chips beside it.

    Rings stack down the left, and the first two chips go beside the first
    ring, centred on its circle, the next two beside the second, and so on;
    the chips left over fill whole lines of two underneath. Centred on the
    circle rather than on the whole ring: the captions under a ring run to
    one line or two, and centred on those the chips sat at a different
    height on every tile. A tile with no ring starts its chips at that same
    height, so the first chips line up across the board. With no ring the
    chips are all lines of two - which is a board's tile, fifty sensors deep
    if the user wants them. A drive tile puts its rings side by side instead,
    with every chip in the lines under them.

    Rebuilt only when the shape changes; every other tick just hands the new
    values to the widgets already there.
    """

    BESIDE = 2
    CHIP_SPACING = 6

    #: (uid, name) from any chip the user renamed in place.
    renamed = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 4, 0, 6)
        self._outer.setSpacing(0)
        self._body = None
        self._shape = None
        self._colour = QColor(DEFAULT_COLOUR)
        self._renamable = False
        self.rings = []
        self.chips = []

    @property
    def widgets(self):
        return self.rings + self.chips

    def sync(self, rings, chips, in_a_row=False):
        shape = (len(rings), len(chips), bool(in_a_row))
        if shape != self._shape:
            self._rebuild(*shape)
        for widget, metric in zip(self.widgets, tuple(rings) + tuple(chips)):
            widget.set_metric(metric)
        for chip in self.chips:
            chip.set_renamable(self._renamable)

    def set_colour(self, colour):
        self._colour = QColor(colour)
        for widget in self.widgets:
            widget.set_colour(colour)

    def set_renamable(self, renamable):
        self._renamable = bool(renamable)
        for chip in self.chips:
            chip.set_renamable(self._renamable)

    def chip_offset(self):
        """How far down the first chip sits: where a pair of chips is
        centred on a ring's circle. The same on every tile."""
        chip = Chip()
        chip.setFont(self.font())
        pair = 2 * chip.sizeHint().height() + self.CHIP_SPACING
        chip.deleteLater()
        return max(0, round(RingGauge.circle_centre() - pair / 2))

    def _rebuild(self, ring_count, chip_count, in_a_row=False):
        if self._body is not None:
            self._outer.removeWidget(self._body)
            self._body.setParent(None)
            self._body.deleteLater()
        self._shape = (ring_count, chip_count, in_a_row)
        self.rings = [RingGauge() for _ in range(ring_count)]
        self.chips = [Chip() for _ in range(chip_count)]
        for chip in self.chips:
            chip.renamed.connect(self.renamed.emit)
        self._body = QWidget()
        self.setVisible(bool(ring_count or chip_count))
        grid = QGridLayout(self._body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        top = Qt.AlignmentFlag.AlignTop

        offset = self.chip_offset()
        chips = iter(self.chips)
        if in_a_row:
            rings = QHBoxLayout()
            rings.setSpacing(8)
            for ring in self.rings:
                rings.addWidget(ring, 1, top | Qt.AlignmentFlag.AlignHCenter)
            grid.addLayout(rings, 0, 0, 1, 2)
        else:
            for row, ring in enumerate(self.rings):
                grid.addWidget(ring, row, 0,
                               top | Qt.AlignmentFlag.AlignHCenter)
                # One column for the ring and one for its chips, shared by
                # every ring, so the chips line up down the tile whichever
                # ring is widest; stretches above and below centre the pair
                # on the ring, with the same space over it as under it.
                beside = QVBoxLayout()
                beside.setSpacing(self.CHIP_SPACING)
                beside.addSpacing(offset)
                for _ in range(self.BESIDE):
                    chip = next(chips, None)
                    if chip is not None:
                        beside.addWidget(chip)
                beside.addStretch(1)
                grid.addLayout(beside, row, 1)
        grid.setColumnStretch(1, 1)

        rest = list(chips)
        if rest:
            lines = QGridLayout()
            lines.setHorizontalSpacing(8)
            lines.setVerticalSpacing(self.CHIP_SPACING)
            if not ring_count:
                lines.setContentsMargins(0, offset, 0, 0)
            for index, chip in enumerate(rest):
                lines.addWidget(chip, index // 2, index % 2, top)
            lines.setColumnStretch(0, 1)
            lines.setColumnStretch(1, 1)
            grid.addLayout(lines, 1 if in_a_row else ring_count, 0, 1, 2)
        self._outer.addWidget(self._body)
        for widget in self.widgets:
            widget.set_colour(self._colour)


class MetricRow(QWidget):
    """One reading: what it is and what it reads now.

    No bar and no range, unlike the tree. A row is scanned rather than read:
    a part-filled line under every second row looked like stray underlining,
    and the lowest and highest since start are the tree's to show - on a
    tile they were two more numbers beside the one that matters.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._metric = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(8)

        self.label = ElidingLabel("", minimum=60)
        self.label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        row.addWidget(self.label, 1)

        self.value = QLabel(display.MISSING)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.value)

    def set_metric(self, metric):
        """Update in place. Returns False when there was nothing to do.

        Compared before written because this runs once a second per row, and
        an expanded CPU is forty rows of which only a handful move per tick.
        """
        if metric == self._metric:
            return False
        self._metric = metric
        self.label.setText(metric.label)
        self.value.setText(coloured(metric))
        self.setToolTip(_tooltip(metric))
        return True


class MoreSection(QWidget):
    """Everything not placed on the tile, behind one chevron.

    One fold rather than one per group: the tile shows what was chosen, and
    the rest is a single "and also" - grouped inside by what it measures.
    """

    #: Suffixed with the device key.
    SETTING = "tile_more"

    toggled = pyqtSignal()

    def __init__(self, device, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._key = "%s_%s" % (self.SETTING, device)
        self._rows = {}
        self._captions = []
        self._shape = None
        self._detail = ()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)
        self.toggle = Chevron(tips=("Hide these", "Show these"))
        self.toggle.clicked.connect(self._flip)
        header_row.addWidget(self.toggle, 0)
        self.heading = QLabel("More readings")
        self.heading.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        header_row.addWidget(self.heading, 1)
        outer.addWidget(header)

        self.content = QWidget()
        self._grid = QGridLayout(self.content)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(1)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        outer.addWidget(self.content)

        self._expanded = None
        expanded = False
        if settings is not None:
            expanded = settings.value(self._key, False, type=bool)
        self.set_expanded(expanded)

    def set_expanded(self, expanded):
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.toggle.set_expanded(expanded)
        self.content.setVisible(expanded)

    def expanded(self):
        return bool(self._expanded)

    def set_colour(self, colour):
        """Title the chevron's tooltips in the tile's colour."""
        self.toggle._tips = (
            tip("Hide", "Fold these readings away again.", colour),
            tip("More readings", "Everything this device reports that is "
                "not placed above, grouped by what it measures.", colour))
        self.toggle.set_expanded(self._expanded)

    def _flip(self):
        self.set_expanded(not self._expanded)
        if self.settings is not None:
            try:
                self.settings.setValue(self._key, self._expanded)
            except Exception:
                pass
        self.toggled.emit()

    def count(self):
        return sum(len(metrics) for _group, metrics in self._detail)

    def sync(self, detail):
        self._detail = tuple(detail)
        self.heading.setText("More readings  (%d)" % self.count())
        shape = (tuple((g, tuple(m.uid for m in ms)) for g, ms in detail),
                 self._two_columns())
        if shape != self._shape:
            self._shape = shape
            self._replace()
        for _group, metrics in detail:
            for metric in metrics:
                row = self._rows.get(metric.uid)
                if row is not None:
                    row.set_metric(metric)

    def _two_columns(self):
        """Two columns only for a long fold, and only where two fit."""
        return (self.count() >= DETAIL_COLUMNS_AT
                and self.width() >= DETAIL_TWO_COLUMN_WIDTH)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._detail and self._shape is not None \
                and self._shape[1] != self._two_columns():
            self.sync(self._detail)

    def _replace(self):
        for widget in list(self._rows.values()) + self._captions:
            self._grid.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self._rows, self._captions = {}, []
        columns = 2 if self._two_columns() else 1
        titled = len(self._detail) > 1
        line = 0
        for group, metrics in self._detail:
            if titled:
                caption = QLabel(group)
                font = QFont(caption.font())
                font.setBold(True)
                font.setPointSizeF(max(font.pointSizeF() - 1, 6.0))
                caption.setFont(font)
                caption.setForegroundRole(QPalette.ColorRole.PlaceholderText)
                self._grid.addWidget(caption, line, 0, 1, 2)
                self._captions.append(caption)
                line += 1
            for index, metric in enumerate(metrics):
                row = MetricRow()
                row.set_metric(metric)
                self._rows[metric.uid] = row
                self._grid.addWidget(row, line + index // columns,
                                     index % columns, 1,
                                     2 if columns == 1 else 1)
            line += (len(metrics) + columns - 1) // columns


class EditorRow(QWidget):
    """One reading in the tile editor: where it sits, and its order there."""

    placement_requested = pyqtSignal(str, str)
    move_requested = pyqtSignal(str, int)

    SHORT = {display.RING: "Ring", display.CHIP: "Chip",
             display.BELOW: "Below", display.FOLDED: "More",
             display.HIDDEN: "Hidden"}

    def __init__(self, metric, placement, refused, first, last, locked=False,
                 colour=None, parent=None):
        """`refused` maps a placement this reading cannot take to why not.

        A locked reading is part of what the tile is: shown, never moved.
        """
        super().__init__(parent)
        self.uid = metric.uid
        self.placement = placement
        self.locked = locked
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self.label = ElidingLabel(metric.label, minimum=50)
        self.setToolTip(tip(metric.label, metric.group, colour))
        if placement == display.HIDDEN:
            self.label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        row.addWidget(self.label, 1)

        self.value = QLabel(metric.text)
        self.value.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        row.addWidget(self.value)

        self.place_button = QToolButton()
        self.place_button.setText(self.SHORT[placement])
        self.place_button.setToolTip(tip(
            "Placement",
            "Where this reading shows: a ring (readings with a maximum "
            "only), a chip, a row below them, inside More readings, or "
            "nowhere.", colour))
        self.place_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.place_button)
        group = QActionGroup(menu)
        self.actions = {}
        for option in display.PLACEMENTS:
            title = display.PLACEMENT_TITLES[option]
            if option in refused:
                title = "%s (%s)" % (title, refused[option])
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(option == placement)
            action.setEnabled(option not in refused)
            group.addAction(action)
            action.triggered.connect(
                lambda _checked, option=option:
                self.placement_requested.emit(self.uid, option))
            self.actions[option] = action
        self.place_button.setMenu(menu)
        if locked:
            self.place_button.setEnabled(False)
            self.place_button.setToolTip(tip(
                "Fixed ring", "Part of what this tile is, so it is always "
                "shown and stays first.", colour))
        # One width for every row's button, whichever word it shows, so the
        # values and arrows line up down the editor.
        words = QFontMetrics(self.place_button.font())
        self.place_button.setMinimumWidth(
            max(words.horizontalAdvance(w) for w in self.SHORT.values()) + 34)
        row.addWidget(self.place_button)

        ordered = (placement in (display.RING, display.CHIP, display.BELOW)
                   and not locked)
        self.up = QToolButton()
        self.up.setArrowType(Qt.ArrowType.UpArrow)
        self.up.setAutoRaise(True)
        self.up.setToolTip(tip("Move up", "Earlier in its row. For chips "
                               "this decides which ring they sit beside.",
                               colour))
        self.up.setEnabled(ordered and not first)
        self.up.clicked.connect(lambda: self.move_requested.emit(self.uid, -1))
        self.down = QToolButton()
        self.down.setArrowType(Qt.ArrowType.DownArrow)
        self.down.setAutoRaise(True)
        self.down.setToolTip(tip("Move down", "Later in its row.", colour))
        self.down.setEnabled(ordered and not last)
        self.down.clicked.connect(lambda: self.move_requested.emit(self.uid, 1))
        for button in (self.up, self.down):
            # Kept, only disabled, where there is no order: the columns stay
            # aligned from one row to the next.
            row.addWidget(button)

    def set_metric(self, metric):
        self.value.setText(metric.text)


class TileEditor(QWidget):
    """The tile's contents turned into controls: placement and order."""

    placement_requested = pyqtSignal(str, str)
    move_requested = pyqtSignal(str, int)
    reset_requested = pyqtSignal()
    colour_requested = pyqtSignal()
    done_requested = pyqtSignal()

    EMPTY = {display.RING: "Only a reading with a maximum can be a ring",
             display.HIDDEN: "Nothing hidden"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shape = None
        self._rows = {}
        self._headers = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        actions = QHBoxLayout()
        # Short, with the undo icon, so the three buttons fit the narrowest
        # tile; the tooltip says in full what it restores.
        self.reset_button = QPushButton("Defaults")
        self.reset_button.setIcon(QIcon.fromTheme("edit-undo"))
        self.reset_button.clicked.connect(self.reset_requested.emit)
        actions.addWidget(self.reset_button)
        self.colour_button = QPushButton("Colour...")
        self.colour_button.clicked.connect(self.colour_requested.emit)
        actions.addWidget(self.colour_button)
        actions.addStretch(1)
        self.done_button = QPushButton("Done")
        self.done_button.setIcon(QIcon.fromTheme("dialog-ok-apply"))
        self.done_button.clicked.connect(self.done_requested.emit)
        actions.addWidget(self.done_button)
        self._colour = None
        self.set_colour(None)
        outer.addLayout(actions)

        self.list = QWidget()
        self._list = QVBoxLayout(self.list)
        self._list.setContentsMargins(0, 4, 0, 0)
        self._list.setSpacing(2)
        outer.addWidget(self.list)

    def set_colour(self, colour):
        """The tile's colour, for the tooltips' titles."""
        if colour is not None and colour == self._colour:
            return
        self._colour = colour
        self._shape = None              # the rows carry it too: remake them
        self.reset_button.setToolTip(tip(
            "Restore Defaults", "Put this tile back as it came: its own "
            "readings in their own places, their own names, the device's "
            "colour and the device's name.", colour))
        self.colour_button.setToolTip(tip(
            "Colour", "Choose the colour this tile is drawn in.", colour))
        self.done_button.setToolTip(tip(
            "Done", "Stop editing. Everything changed is already saved.",
            colour))

    def sync(self, model, colour=None):
        if colour is not None:
            self.set_colour(colour)
        self.reset_button.setEnabled(not model.config.is_default())
        shape = tuple((p, m.uid) for p, m in model.items)
        if shape != self._shape:
            self._shape = shape
            self._replace(model)
        for _placement, metric in model.items:
            row = self._rows.get(metric.uid)
            if row is not None:
                row.set_metric(metric)

    @staticmethod
    def refusals(model, metric, placement):
        """{placement: why not} for the ones this reading cannot move to."""
        refused = {}
        for option in (display.RING, display.CHIP):
            if option == placement:
                continue
            if option == display.RING and not display.ringable(metric):
                refused[option] = "needs a maximum"
            elif not display.can_place(model.config, model, metric.uid,
                                       option):
                refused[option] = "full - %d at most" % model.limit(option)
        return refused

    def _replace(self, model):
        for widget in list(self._rows.values()) + self._headers:
            self._list.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self._rows, self._headers = {}, []

        by_place = {p: [] for p in display.PLACEMENTS}
        for placement, metric in model.items:
            by_place[placement].append(metric)
        for placement in display.PLACEMENTS:
            metrics = by_place[placement]
            title = display.PLACEMENT_TITLES[placement]
            if placement in display.GLANCE:
                title = "%ss  (%d of %d)" % (title, len(metrics),
                                             model.limit(placement))
            elif metrics:
                title = "%s  (%d)" % (title, len(metrics))
            header = QLabel(title)
            font = QFont(header.font())
            font.setBold(True)
            header.setFont(font)
            self._list.addWidget(header)
            self._headers.append(header)
            if not metrics:
                empty = QLabel(self.EMPTY.get(placement, "Nothing here yet"))
                empty.setWordWrap(True)
                empty.setForegroundRole(QPalette.ColorRole.PlaceholderText)
                self._list.addWidget(empty)
                self._headers.append(empty)
            # Locked rings lead and do not move, so the order the arrows
            # change is only that of the ones after them.
            free = [m for m in metrics if m.uid not in model.locked]
            for metric in metrics:
                locked = metric.uid in model.locked
                row = EditorRow(metric, placement,
                                self.refusals(model, metric, placement),
                                bool(free) and metric is free[0],
                                bool(free) and metric is free[-1],
                                locked=locked, colour=self._colour)
                row.placement_requested.connect(self.placement_requested.emit)
                row.move_requested.connect(self.move_requested.emit)
                self._list.addWidget(row)
                self._rows[metric.uid] = row


class ScrollHandle(QWidget):
    """The handle of a scroll bar, and nothing else, in the colour it is given.

    Not a QScrollBar. The style's bar draws a groove the height of the card,
    and under Breeze the groove is painted by an event filter the style
    installs on every QScrollBar - before the widget's own paintEvent, so a
    subclass cannot stop it. This is a plain widget standing in for the
    scroll area's real bar, which stays hidden and keeps doing the wheel and
    the range; the handle mirrors its values and drives it back.
    """

    WIDTH = 10          # the widget; the handle is narrower, inset in it
    HANDLE = 4
    MIN_LENGTH = 24

    def __init__(self, bar, parent=None):
        super().__init__(parent)
        self.bar = bar
        self._colour = None
        self._hover = False
        self._grab = None           # offset of the press inside the handle
        self.setFixedWidth(self.WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed,
                           QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        bar.valueChanged.connect(lambda _v: self.update())
        bar.rangeChanged.connect(self._range_changed)
        self._range_changed(bar.minimum(), bar.maximum())

    def set_colour(self, colour):
        if colour == self._colour:
            return
        self._colour = colour
        self.update()

    # ---- the bar's numbers ---------------------------------------------

    def value(self):
        return self.bar.value()

    def maximum(self):
        return self.bar.maximum()

    def _range_changed(self, low, high):
        # Gone entirely when there is nothing to scroll, so the rows get
        # the width back rather than a blank strip down the side.
        self.setVisible(high > low)
        self.update()

    # ---- geometry --------------------------------------------------------

    def _span(self):
        return self.bar.maximum() - self.bar.minimum()

    def _handle(self):
        """Where the handle is, from value, range and page step."""
        height = self.height()
        total = self._span() + self.bar.pageStep()
        if self._span() <= 0 or total <= 0:
            return None
        length = max(self.MIN_LENGTH, height * self.bar.pageStep() / total)
        length = min(length, height)
        travel = height - length
        top = travel * (self.bar.value() - self.bar.minimum()) / self._span()
        x = (self.WIDTH - self.HANDLE) / 2.0
        return QRectF(x, top, self.HANDLE, length)

    def _value_for_top(self, top):
        handle = self._handle()
        if handle is None:
            return self.bar.minimum()
        travel = self.height() - handle.height()
        if travel <= 0:
            return self.bar.minimum()
        share = max(0.0, min(1.0, top / travel))
        return int(round(self.bar.minimum() + share * self._span()))

    # ---- painting --------------------------------------------------------

    def paintEvent(self, event):
        handle = self._handle()
        if handle is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(self._colour
                        or self.palette().color(QPalette.ColorRole.Mid))
        colour.setAlpha(230 if (self._hover or self._grab is not None) else 150)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        radius = self.HANDLE / 2.0
        painter.drawRoundedRect(handle, radius, radius)
        painter.end()

    # ---- interaction -----------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        handle = self._handle()
        if handle is None:
            return
        y = event.position().y()
        if handle.top() <= y <= handle.bottom():
            self._grab = y - handle.top()
        else:
            # Jump so the handle is centred on the click, then drag from it.
            self._grab = handle.height() / 2.0
            self.bar.setValue(self._value_for_top(y - self._grab))
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._grab is None:
            super().mouseMoveEvent(event)
            return
        self.bar.setValue(self._value_for_top(event.position().y() - self._grab))
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._grab is None:
            super().mouseReleaseEvent(event)
            return
        self._grab = None
        self.update()
        event.accept()

    def wheelEvent(self, event):
        # The wheel over the handle scrolls the same as over the rows.
        QApplication.sendEvent(self.bar, event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange):
            self.update()
        super().changeEvent(event)


class HeatBadge(QWidget):
    """WARM or HOT in the tile's header, in the heat colours.

    Said in words as well as in colour: the card's own colour turning amber
    or red is the signal from across the room, but a red GPU card and a hot
    one are too alike for colour to be the only way of telling.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = None
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.hide()

    def _font(self):
        font = small_font(self.font())
        font.setBold(True)
        return font

    def sizeHint(self):
        metrics = QFontMetrics(self._font())
        return QSize(metrics.horizontalAdvance("WARM") + 12,
                     metrics.height() + 2)

    def set_level(self, level, metric=None):
        self._level = level
        self.setVisible(level is not None)
        if level is not None:
            what = ("%s at %s" % (metric.label, metric.text)
                    if metric is not None else "A reading")
            self.setToolTip(tip(
                "Running hot" if level == "hot" else "Running warm",
                "%s. Amber from %d °C, red from %d °C." % (
                    what, display.TEMP_WARN_C, display.TEMP_HOT_C),
                level_colour(level)))
        self.update()

    def paintEvent(self, _event):
        if self._level is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(level_colour(self._level))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setPen(QColor(25, 25, 25))
        painter.setFont(self._font())
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         LEVEL_WORDS[self._level])
        painter.end()


class DeviceTile(Card):
    """One device: its headline numbers, its rows, and the rest behind them.

    Every tile is the same size - the board decides the cell - so the body
    scrolls when what it holds does not fit, and the long tail is folded
    behind chevrons inside it.
    """

    rename_requested = pyqtSignal(str)
    reset_name_requested = pyqtSignal(str)
    #: The uid of a reading to show in Live Graphs.
    graph_requested = pyqtSignal(str)
    #: (tile key, name): renamed in place; "" is the hardware's name again.
    name_changed = pyqtSignal(str, str)
    drag_started = pyqtSignal(object)
    drag_moved = pyqtSignal(object, object)
    drag_finished = pyqtSignal(object)
    resized = pyqtSignal()
    #: (tile key, display.TileConfig): the user changed what this tile shows.
    config_changed = pyqtSignal(str, object)

    #: The body grid row that takes whatever height is left, below the rows
    #: and sections; high enough that they never reach it.
    _STRETCH_ROW = 99

    #: Most rows of the board a tile takes while it is being edited: enough
    #: for the editor to be usable without it swallowing the whole board.
    EDIT_SPAN_MAX = 4

    def __init__(self, model, settings=None, parent=None):
        super().__init__(parent)
        self.key = model.key
        self.settings = settings
        self._model = None
        self._rows = {}
        self._row_order = []
        self._cell_height = None
        self._editing = False
        self._pending_config = None
        self._tip_colour = None

        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Preferred)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        outer = QGridLayout(self)
        outer.setContentsMargins(14, 10, 12, 12)
        outer.setHorizontalSpacing(10)
        outer.setVerticalSpacing(4)
        self._outer = outer

        # The name and the grip share the top row, so the grip sits in the
        # corner without the other rows having to leave a column free for it.
        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)

        # Editable in place while the tile is, like its chips' names.
        self.title = InlineName(model.name, minimum=80)
        name_font = QFont(self.title.font())
        name_font.setBold(True)
        self.title.setFont(name_font)
        self.title.renamed.connect(self._retitle)
        self.name_label = self.title.label
        header_row.addWidget(self.title, 1)

        self.heat = HeatBadge()
        header_row.addWidget(self.heat, 0, Qt.AlignmentFlag.AlignVCenter)

        # Edit mode: the tile's own contents become the controls for them,
        # so what is being arranged is right there rather than in a dialog.
        # The same square as the grip beside it and centred on the same line:
        # a tool button sizes itself from the style's frame and icon, and
        # left to that it sat a few pixels below the grip.
        self.edit_button = QToolButton()
        self.edit_button.setIcon(QIcon.fromTheme("document-edit"))
        if self.edit_button.icon().isNull():
            self.edit_button.setText("✎")
        self.edit_button.setAutoRaise(True)
        self.edit_button.setCheckable(True)
        self.edit_button.setFixedSize(MoveGrip.SIZE, MoveGrip.SIZE)
        self.edit_button.setIconSize(QSize(MoveGrip.SIZE - 6,
                                           MoveGrip.SIZE - 6))
        self.edit_button.toggled.connect(self.set_editing)
        header_row.addWidget(self.edit_button, 0,
                             Qt.AlignmentFlag.AlignVCenter)

        self.grip = MoveGrip()
        self.grip.pressed.connect(lambda: self.drag_started.emit(self))
        self.grip.moved.connect(lambda pos: self.drag_moved.emit(self, pos))
        self.grip.released.connect(lambda: self.drag_finished.emit(self))
        header_row.addWidget(self.grip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.header = header
        outer.addWidget(header, 0, 0)

        # Inside the scrolling part with everything else: three drives' rings
        # or a board's fifty chips can be taller than the cell, and squeezed
        # to fit they lost their captions.
        self.headline_row = HeadlineStrip()
        self.headline_row.renamed.connect(self._rename)

        # The body holds the rows and the sections, and scrolls when the
        # cell is not tall enough for them. Whatever the cell has left over
        # goes below the last row, so two rows sit under the headline rather
        # than being spread down the card.
        self.body = QWidget()
        grid = QGridLayout(self.body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)
        grid.setRowStretch(self._STRETCH_ROW, 1)
        self._grid = grid
        self._next_row = 0

        self.more = MoreSection(self.key, settings)
        self.more.toggled.connect(self.resized.emit)
        grid.addWidget(self.more, 50, 0)

        # The editor shares the body's scroll area and replaces it while the
        # tile is being edited; the headline above stays, as a live preview.
        self.editor = TileEditor()
        self.editor.hide()
        self.editor.placement_requested.connect(self._place)
        self.editor.move_requested.connect(self._move)
        self.editor.reset_requested.connect(self._reset)
        self.editor.colour_requested.connect(self.choose_colour)
        self.editor.done_requested.connect(
            lambda: self.edit_button.setChecked(False))
        pages = QWidget()
        stack = QVBoxLayout(pages)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(outer.verticalSpacing())
        stack.addWidget(self.headline_row)
        stack.addWidget(self.body)
        stack.addWidget(self.editor)
        # What the cell has spare goes below everything, not between the
        # rings and the chips under them.
        stack.addStretch(1)

        # The real bar stays hidden - it still takes the wheel and holds the
        # range - and the handle beside the viewport stands in for it.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(pages)
        make_transparent(self.scroll)
        outer.addWidget(self.scroll, 2, 0)
        self.scrollbar = ScrollHandle(self.scroll.verticalScrollBar())
        outer.addWidget(self.scrollbar, 2, 1)
        outer.setRowStretch(2, 1)
        outer.setColumnStretch(0, 1)
        self.sync(model)

    # ---- content ---------------------------------------------------------

    def sync(self, model):
        """Fold one tick's model into the widgets already on screen."""
        self.name_label.setText(model.name)
        # After the headline widgets exist, since it colours their rings.
        self.headline_row.sync(model.rings, model.chips,
                               in_a_row=model.rings_in_a_row)
        self._sync_colour(model)
        self._sync_rows(model.rows)
        self._sync_detail(model.detail)
        self._model = model
        if self._editing:
            self.editor.sync(model, tile_colour(model))

    @property
    def _headlines(self):
        """The glance widgets in order: rings, then chips."""
        return self.headline_row.widgets

    # ---- editing ---------------------------------------------------------

    def editing(self):
        return self._editing

    def set_editing(self, editing):
        editing = bool(editing)
        if editing == self._editing:
            return
        self._editing = editing
        if self.edit_button.isChecked() != editing:
            self.edit_button.setChecked(editing)
        self.body.setVisible(not editing)
        self.editor.setVisible(editing)
        # Editing shows which names can be changed: underlined, and a click
        # away from an edit box - the tile's own as well as its chips'.
        self.headline_row.set_renamable(editing)
        self.title.set_renamable(editing)
        self._sync_title_tip()
        if editing and self._model is not None:
            self.editor.sync(self._model, tile_colour(self._model))
        self.scroll.verticalScrollBar().setValue(0)
        self.resized.emit()
        # Again once the editor's rows are really shown: a widget added to a
        # visible parent is shown by a queued event, and until then layouts
        # skip it - so the span asked for just now counted an empty editor.
        # A method of the tile rather than the signal's emit, so a tile
        # deleted before the timer fires takes the call with it.
        QTimer.singleShot(0, self._announce_resize)

    def _announce_resize(self):
        self.resized.emit()

    def _emit(self, config):
        # Deferred: the window answers by rebuilding this tile's editor, and
        # the row whose menu asked would otherwise be deleted mid-signal.
        # Through a method of this tile rather than a lambda, so a tile that
        # is gone by then takes the call with it instead of raising.
        if self._model is not None and config != self._model.config:
            self._pending_config = config
            QTimer.singleShot(0, self._flush_config)

    def _flush_config(self):
        config, self._pending_config = self._pending_config, None
        if config is not None:
            self.config_changed.emit(self.key, config)

    def _place(self, uid, placement):
        self._emit(display.place(self._model.config, self._model, uid,
                                 placement))

    def _move(self, uid, delta):
        self._emit(display.move(self._model.config, self._model, uid, delta))

    def _reset(self):
        """Back to the tile as it came, its name included."""
        self.restore_defaults()
        self.reset_name_requested.emit(self.key)

    def restore_defaults(self):
        """Back to the tile as it came - readings, their names, its colour -
        keeping the name the user gave the device."""
        self._emit(display.reset(self._model.config))

    def metric_at(self, pos):
        """The reading under `pos` (tile coordinates): a chip, a ring or a
        row, or None over anything else."""
        widget = self.childAt(pos)
        while widget is not None and widget is not self:
            if isinstance(widget, (Chip, RingGauge, MetricRow)):
                return widget._metric
            widget = widget.parentWidget()
        return None

    def _retitle(self, name):
        self.name_changed.emit(self.key, name)

    def _sync_title_tip(self):
        if self.title.renamable():
            self.title.setToolTip(tip(
                "Tile name", "Click to rename this device, here and in the "
                "tree. Leave it empty to go back to the name the hardware "
                "reports.", self._tip_colour))
        else:
            self.title.setToolTip("")

    def _rename(self, uid, name):
        original = dict(self._model.original_labels).get(uid)
        if name == original:
            name = ""                   # its own name again: no override
        self._emit(display.rename(self._model.config, uid, name))

    def choose_colour(self):
        """Ask for the tile's colour, starting from the one it has now."""
        colour = self._ask_colour(tile_colour(self._model))
        if colour is not None and colour.isValid():
            chosen = colour.name()
            if chosen == device_colour(self._model.order).name():
                chosen = ""             # the device's own: no override
            self._emit(display.recolour(self._model.config, chosen))

    def _ask_colour(self, current):
        # The platform's own dialog, so it is KDE's under Plasma.
        return QColorDialog.getColor(current, self, "Tile Colour")

    def model(self):
        return self._model

    def _sync_colour(self, model):
        """The tile's own colour, unless something on it is running hot.

        Heat takes the card over: a board where one tile has gone amber is
        readable across the room, which a coloured number inside it is not.
        """
        colour = tile_colour(model)
        alarm = level_colour(model.level)
        self.set_accent(alarm or colour)
        self.scrollbar.set_colour(alarm or colour)
        self.headline_row.set_colour(colour)
        self.heat.set_level(model.level, model.hottest)
        if self._tip_colour != colour.name():
            self._tip_colour = colour.name()
            self._sync_tips(colour)

    def _sync_tips(self, colour):
        """What each control does, titled in the tile's colour."""
        self.edit_button.setToolTip(tip(
            "Customize", "Choose what this tile shows and where: rings, "
            "chips, rows or hidden. While editing, click a chip's name to "
            "rename it.", colour))
        self.grip.setToolTip(tip(
            "Move", "Drag to another place on the board. Outlines show "
            "where this tile and any it displaces will land; nothing moves "
            "until you let go.", colour))
        self.more.set_colour(colour)
        self.editor.set_colour(colour)
        self._sync_title_tip()

    def _sync_rows(self, metrics):
        order = [m.uid for m in metrics]
        if order != self._row_order:
            self._row_order = order
            for row in self._rows.values():
                row.hide()
            for position, metric in enumerate(metrics):
                row = self._rows.get(metric.uid)
                if row is None:
                    row = MetricRow()
                    self._rows[metric.uid] = row
                self._grid.addWidget(row, self._next_row + position, 0)
                row.show()
        for metric in metrics:
            self._rows[metric.uid].set_metric(metric)

    def _sync_detail(self, detail):
        self.more.setVisible(bool(detail))
        self.more.sync(detail)

    def set_all_expanded(self, expanded):
        self.more.set_expanded(expanded)

    # ---- geometry --------------------------------------------------------

    def row_span(self):
        """Rows of the board this tile takes: one, or more while editing.

        Every tile has the same cell so that dragging stays predictable; the
        editor is the exception, because squeezed into one cell it showed two
        readings at a time.
        """
        if not self._editing or not self._cell_height:
            return 1
        margins = self._outer.contentsMargins()
        needed = (margins.top() + margins.bottom()
                  + 2 * self._outer.verticalSpacing()
                  + self.header.sizeHint().height()
                  + self.headline_row.sizeHint().height()
                  + self.editor.sizeHint().height())
        span = -(-needed // self._cell_height)
        return max(1, min(self.EDIT_SPAN_MAX, span))

    def set_cell_height(self, height):
        """The board's cell; None lets the content decide (measuring)."""
        if height != self._cell_height:
            self._cell_height = height
            self.updateGeometry()

    def content_height(self):
        """How tall the card would be if nothing had to scroll."""
        margins = self._outer.contentsMargins()
        spacing = self._outer.verticalSpacing()
        return (margins.top() + margins.bottom() + 2 * spacing
                + self.header.sizeHint().height()
                + self.headline_row.sizeHint().height()
                + self.body.sizeHint().height())

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(TILE_WIDTH)
        hint.setHeight(self._cell_height if self._cell_height is not None
                       else self.content_height())
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(TILE_MIN_WIDTH)
        if self._cell_height is not None:
            hint.setHeight(self._cell_height)
        return hint

    # ---- menu ------------------------------------------------------------

    def _menu(self, pos):
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        colour = tile_colour(self._model)

        def add(text, slot, what, icon=None):
            action = menu.addAction(QIcon.fromTheme(icon) if icon else QIcon(),
                                    text)
            action.setToolTip(tip(text.rstrip("."), what, colour))
            action.triggered.connect(slot)
            return action

        metric = self.metric_at(pos)
        uid = display.graph_uid(metric)
        if uid is not None:
            graph = add('Show "%s" in Live Graphs' % metric.label,
                        lambda: self.graph_requested.emit(uid), "")
            # In Live Graphs' colour, as the tree's entry is: it is that
            # tool's action, not the tile's.
            graph.setToolTip(tool_tip("graphs", "Show in Live Graphs",
                                      "Open the Live Graphs panel with this "
                                      "reading in it."))
            menu.addSeparator()
        add("Customize Tile...", lambda: self.set_editing(True),
            "Choose what this tile shows and where.", "document-edit")
        add("Tile Colour...", self.choose_colour,
            "Choose the colour this tile is drawn in.", "color-management")
        menu.addSeparator()
        add("Rename...", lambda: self.rename_requested.emit(self.key),
            "Give this device a name of your own, here and in the tree.",
            "edit-rename")
        add("Reset Name", lambda: self.reset_name_requested.emit(self.key),
            "Go back to the name the hardware reports.")
        menu.addSeparator()
        add("Restore Defaults", self.restore_defaults,
            "Put this tile back as it came: its readings, their names and "
            "its colour. The name you gave the device stays.", "edit-undo")
        add("Restore Defaults and Name", self._reset,
            "As Restore Defaults, and go back to the name the hardware "
            "reports too.")
        menu.exec(self.mapToGlobal(pos))


def measure_cell_height(settings=None):
    """How tall a cell has to be for a full tile: the same for every tile.

    Measured on a reference tile rather than fixed in pixels, so the cell
    follows the font: a graphics card's two rings with their four chips,
    BODY_MAX rows and the fold, which is the most a tile shows by default
    before it has to scroll.
    """
    rows = tuple(display.Metric(uid="!m%d" % i, key="!m%d" % i, device="!",
                                label="Reading %d" % i, group="", kind="temp",
                                value=50.0, text="50.0 °C")
                 for i in range(display.BODY_MAX))
    load = display.Metric(uid="!l", key="!l", device="!", label="Load",
                          group="", kind="utilization", value=10.0,
                          text="10 %", fraction=0.1)
    vram = display.Metric(uid="!r", key="!r", device="!", label="VRAM",
                          group="", kind="memory_gb", value=1.0,
                          text="1.0 / 16.0 GB", fraction=0.1, total=16.0)
    chips = tuple(replace(rows[0], uid="!c%d" % i, key="!c%d" % i)
                  for i in range(4))
    detail = (("More", rows[:1]),)
    model = display.TileModel(key="!", name="Reference", order=30,
                              rings=(load, vram), chips=chips, rows=rows,
                              detail=detail)
    tile = DeviceTile(model, None)
    tile.resize(TILE_WIDTH, 10)
    height = tile.content_height()
    tile.deleteLater()
    return height
