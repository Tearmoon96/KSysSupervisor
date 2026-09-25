"""The board the device tiles sit on: a grid of equal cells, and reordering.

Every tile gets the same cell, so a slot is a position in the grid and not
a consequence of what the neighbours contain. That is what makes dragging
predictable: the tile under the cursor takes the slot under the cursor, and
nothing else moves except to make room.

A real QLayout rather than a QGridLayout rebuilt on every resize. Re-adding
every item as the window is dragged invalidates hints in the middle of the
resize that caused them, and leaves the scroll area to guess its own content
height - which it gets wrong by exactly the last row.
"""

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPalette, QPen
from PyQt6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QLayout,
                             QScrollArea, QSizePolicy, QWidget)

from .tiles import (TILE_MIN_WIDTH, TILE_WIDTH, DeviceTile, device_colour,
                    measure_cell_height)
from .widgets import make_transparent

#: Past this a wide window stops adding columns and lets the tiles grow. Five
#: narrow columns of six rows each is a spreadsheet, not a glance.
MAX_COLUMNS = 4

SPACING = 8
MARGIN = 8

#: Used until the board has measured a real tile.
FALLBACK_CELL_HEIGHT = 260


class CellLayout(QLayout):
    """Equal cells in equal columns, reflowing to the width they are given."""

    def __init__(self, parent=None, margin=MARGIN, spacing=SPACING):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items = []
        self.max_columns = MAX_COLUMNS
        self.cell_height = FALLBACK_CELL_HEIGHT

    # ---- the QLayout contract -------------------------------------------

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        margins = self.contentsMargins()
        placements = self.placements(self.columns_for(width))
        rows = max((row + span for _c, row, span in placements), default=0)
        if not rows:
            return margins.top() + margins.bottom()
        return (rows * self.cell_height + (rows - 1) * self._spacing
                + margins.top() + margins.bottom())

    def setGeometry(self, rect):
        super().setGeometry(rect)
        columns = self.columns_for(rect.width())
        width = self.cell_width(rect.width(), columns)
        for item, placement in zip(self._items, self.placements(columns)):
            item.setGeometry(self.placed_rect(rect, placement, width))

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        margins = self.contentsMargins()
        if not self._items:
            return QSize(margins.left() + margins.right(),
                         margins.top() + margins.bottom())
        return QSize(TILE_MIN_WIDTH + margins.left() + margins.right(),
                     self.cell_height + margins.top() + margins.bottom())

    # ---- ordering --------------------------------------------------------

    def move_item(self, source, target):
        if source == target or not (0 <= source < len(self._items)):
            return
        target = max(0, min(target, len(self._items) - 1))
        self._items.insert(target, self._items.pop(source))
        self.invalidate()

    # ---- the arithmetic --------------------------------------------------

    def columns_for(self, width):
        margins = self.contentsMargins()
        inner = width - margins.left() - margins.right()
        fit = (inner + self._spacing) // (TILE_WIDTH + self._spacing)
        return max(1, min(self.max_columns, int(fit), max(len(self._items), 1)))

    def rows_for(self, columns):
        return (len(self._items) + columns - 1) // columns

    @staticmethod
    def _span(item):
        """How many rows an item asks for: one, unless a tile is being edited."""
        widget = item.widget() if item is not None else None
        span = getattr(widget, "row_span", None)
        return max(1, int(span())) if callable(span) else 1

    def placements(self, columns, items=None):
        """(column, row, span) per item, in order, filling row by row.

        A tile spanning several rows keeps its column and the others flow
        around it into the next free cells - the same reading order as
        before, with the tall one's cells taken. With every span at one this
        is exactly index // columns, index % columns.
        """
        taken = set()
        out = []
        cursor = 0
        for item in self._items if items is None else items:
            span = self._span(item)
            while True:
                column, row = cursor % columns, cursor // columns
                if all((column, row + r) not in taken for r in range(span)):
                    break
                cursor += 1
            for r in range(span):
                taken.add((column, row + r))
            out.append((column, row, span))
            cursor += 1
        return out

    def preview(self, rect, source, target):
        """[(item, rect)] for every item, as they would sit after a move.

        Worked out on a reordered copy, so the drop markers can show where
        each tile lands - a tall tile being edited reflows the rest - without
        anything on the board moving before the button is let go.
        """
        items = list(self._items)
        items.insert(target, items.pop(source))
        columns = self.columns_for(rect.width())
        width = self.cell_width(rect.width(), columns)
        return [(item, self.placed_rect(rect, placement, width))
                for item, placement in zip(items,
                                           self.placements(columns, items))]

    def placed_rect(self, rect, placement, width):
        margins = self.contentsMargins()
        column, row, span = placement
        x = rect.x() + margins.left() + column * (width + self._spacing)
        y = rect.y() + margins.top() + row * (self.cell_height + self._spacing)
        return QRect(x, y, width,
                     span * self.cell_height + (span - 1) * self._spacing)

    def cell_width(self, width, columns):
        margins = self.contentsMargins()
        inner = width - margins.left() - margins.right()
        return max(TILE_MIN_WIDTH // 2,
                   (inner - self._spacing * (columns - 1)) // columns)

    def cell_rect(self, rect, index, columns, width):
        margins = self.contentsMargins()
        column, row = index % columns, index // columns
        x = rect.x() + margins.left() + column * (width + self._spacing)
        y = rect.y() + margins.top() + row * (self.cell_height + self._spacing)
        return QRect(x, y, width, self.cell_height)

    def slot_at(self, position, width):
        """Which slot a point in layout coordinates falls in, clamped.

        The gaps count for the cell before them, so a drag never dead-zones
        between two tiles; past the last tile means the last slot.
        """
        if not self._items:
            return None
        margins = self.contentsMargins()
        columns = self.columns_for(width)
        cell_width = self.cell_width(width, columns)
        column = (position.x() - margins.left()) // (cell_width + self._spacing)
        row = (position.y() - margins.top()) // (self.cell_height + self._spacing)
        column = max(0, min(int(column), columns - 1))
        row = max(0, int(row))
        placements = self.placements(columns)
        for index, (col, top, span) in enumerate(placements):
            if col == column and top <= row < top + span:
                return index
        # An empty cell: the slot of the first tile placed after it.
        for index, (col, top, _span) in enumerate(placements):
            if (top, col) > (row, column):
                return index
        return len(self._items) - 1


class DropMarker(QWidget):
    """Where a tile will land: its cell, outlined in the tile's colour.

    The dragged tile's marker is filled as well as outlined; the tiles that
    make way for it get an outline only. Painted rather than styled, like
    the cards, so it follows a theme switch, and transparent to the mouse,
    so the drag underneath never notices it.
    """

    RADIUS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colour = QColor("#7f8b99")
        self._filled = True
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def set_colour(self, colour, filled=True):
        self._colour = QColor(colour)
        self._filled = filled
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(self._colour)
        fill.setAlpha(45 if self._filled else 0)
        pen = QPen(self._colour, 2 if self._filled else 1.5,
                   Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1),
                                self.RADIUS, self.RADIUS)
        painter.end()


class DragGhost(QWidget):
    """A see-through picture of the dragged tile, following the cursor."""

    OPACITY = 0.75
    SCALE = 0.6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self.offset = QPoint()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def take(self, tile, grab_point):
        """Picture `tile`, held at `grab_point` in its own coordinates."""
        pixmap = tile.grab()
        size = QSize(max(1, int(tile.width() * self.SCALE)),
                     max(1, int(tile.height() * self.SCALE)))
        self._pixmap = pixmap.scaled(
            size * pixmap.devicePixelRatio(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._pixmap.setDevicePixelRatio(pixmap.devicePixelRatio())
        self.offset = QPoint(int(grab_point.x() * self.SCALE),
                             int(grab_point.y() * self.SCALE))
        self.resize(size)

    def paintEvent(self, _event):
        if self._pixmap is None:
            return
        painter = QPainter(self)
        painter.setOpacity(self.OPACITY)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()


class TileBoard(QScrollArea):
    """The scrolling grid of device tiles, reorderable by their grips."""

    order_changed = pyqtSignal(list)
    rename_requested = pyqtSignal(str)
    reset_name_requested = pyqtSignal(str)
    #: A reading's uid, from a tile's "Show in Live Graphs".
    graph_requested = pyqtSignal(str)
    #: (tile key, name), from a tile renamed in place.
    name_changed = pyqtSignal(str, str)
    #: Never emitted - tiles show no min/max - but declared like the
    #: tree's, so the window wires both views the same way.
    clear_bounds_requested = pyqtSignal(str)
    #: (tile key, display.TileConfig), from a tile's editor.
    config_changed = pyqtSignal(str, object)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._tiles = {}
        self._dragging = None
        self._target = None         # the slot a drag would drop into
        self._measured = False

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        policy = QSizePolicy(QSizePolicy.Policy.Preferred,
                             QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self._content.setSizePolicy(policy)
        self._flow = CellLayout(self._content)
        self.setWidget(self._content)
        # Children of the content but not items of its layout: they float
        # over the tiles rather than taking a cell. One marker per tile that
        # would move, made as they are first needed.
        self.markers = []
        self.ghost = DragGhost(self._content)
        # The soft backdrop is painted by whatever holds the board; the
        # board itself stays see-through so the empty space below the last
        # tile is the same ground the tiles sit on.
        make_transparent(self)

    # ---- content ---------------------------------------------------------

    def cell_height(self):
        return self._flow.cell_height

    def _measure(self):
        """Size the shared cell once, from a reference tile."""
        if self._measured:
            return False
        self._measured = True
        height = measure_cell_height(self.settings)
        changed = height != self._flow.cell_height
        self._flow.cell_height = height
        for tile in self._tiles.values():
            tile.set_cell_height(height)
        return changed

    def sync(self, models):
        """Create, update and retire tiles to match this tick's models."""
        remeasured = self._measure()
        wanted = [m.key for m in models]
        for key in list(self._tiles):
            if key not in wanted:
                tile = self._tiles.pop(key)
                if tile is self._dragging:
                    self._end_drag()
                self._flow.removeWidget(tile)
                tile.setParent(None)
                tile.deleteLater()

        fresh = False
        for model in models:
            tile = self._tiles.get(model.key)
            if tile is None:
                tile = self._build(model)
                self._tiles[model.key] = tile
                self._flow.addWidget(tile)
                fresh = True
            else:
                tile.sync(model)
        if fresh or remeasured:
            self._flow.invalidate()
            self.fit()

    def _build(self, model):
        tile = DeviceTile(model, self.settings)
        tile.set_cell_height(self._flow.cell_height)
        tile.rename_requested.connect(self.rename_requested.emit)
        tile.reset_name_requested.connect(self.reset_name_requested.emit)
        tile.graph_requested.connect(self.graph_requested.emit)
        tile.name_changed.connect(self.name_changed.emit)
        tile.config_changed.connect(self.config_changed.emit)
        tile.resized.connect(self._relayout)
        tile.drag_started.connect(self._drag_start)
        tile.drag_moved.connect(self._drag_move)
        tile.drag_finished.connect(self._drag_end)
        return tile

    def set_all_expanded(self, expanded):
        for tile in self._tiles.values():
            tile.set_all_expanded(expanded)

    def order(self):
        """The device keys in the order they are shown."""
        keys = []
        for index in range(self._flow.count()):
            item = self._flow.itemAt(index)
            widget = item.widget() if item else None
            if isinstance(widget, DeviceTile):
                keys.append(widget.key)
        return keys

    def apply_order(self, keys):
        """Put the tiles in this order, leaving unlisted ones where they are."""
        wanted = [k for k in keys if k in self._tiles]
        for position, key in enumerate(wanted):
            current = self.order()
            if key not in current:
                continue
            self._flow.move_item(current.index(key), position)
        self.fit()

    # ---- geometry --------------------------------------------------------

    def _relayout(self):
        """A tile changed how many rows it wants: reflow, then re-fit."""
        self._flow.invalidate()
        self._flow.activate()
        self.fit()

    def fit(self):
        """Tell the scroll area how tall its content is at this width."""
        width = self.viewport().width()
        if width <= 0:
            return
        self._content.setMinimumHeight(self._flow.heightForWidth(width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()

    # ---- reordering ------------------------------------------------------
    #
    # Nothing moves while the grip is held: the marker shows the cell the tile
    # will take and a ghost of it follows the cursor, and the board reflows
    # once, on release. Reordering live under the cursor meant every tile the
    # drag passed over jumped out of the way and back.

    def _drag_start(self, tile):
        self._dragging = tile
        self._target = None
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self.ghost.take(tile, tile.grip.geometry().center()
                        + tile.header.pos())
        effect = QGraphicsOpacityEffect(tile)
        effect.setOpacity(0.4)
        tile.setGraphicsEffect(effect)

    def _colour_of(self, tile):
        """The colour the tile is showing: its own, or amber when it is hot,
        so each outline is matched to its tile at a glance."""
        if tile.accent() is not None:
            return tile.accent()
        model = tile.model()
        if model is None:
            return self.palette().color(QPalette.ColorRole.Highlight)
        return device_colour(model.order)

    def _show_markers(self, landings):
        """[(tile, rect, filled)]: a marker each, the rest hidden."""
        while len(self.markers) < len(landings):
            self.markers.append(DropMarker(self._content))
        for marker, (tile, rect, filled) in zip(self.markers, landings):
            marker.set_colour(self._colour_of(tile), filled)
            marker.setGeometry(rect)
            marker.show()
            marker.raise_()
        for marker in self.markers[len(landings):]:
            marker.hide()

    def _drag_move(self, tile, global_pos):
        if self._dragging is not tile:
            return
        position = self._content.mapFromGlobal(global_pos)
        self.ghost.move(position - self.ghost.offset)
        self.ghost.show()
        self.ghost.raise_()
        # Near the top or bottom edge the board scrolls to what is beyond.
        self.ensureVisible(position.x(), position.y(), 0, 48)

        target = self._flow.slot_at(position, self._content.width())
        current = self.order()
        if target is None or tile.key not in current:
            return
        self._target = target
        # The dragged tile's landing, and every other tile whose cell would
        # change: with a tall tile open for editing, a move reflows tiles
        # well away from the drop, and each shows where it goes.
        landings = []
        for item, rect in self._flow.preview(self._content.rect(),
                                             current.index(tile.key), target):
            other = item.widget()
            if other is tile:
                landings.insert(0, (tile, rect, True))
            elif other is not None and rect != other.geometry():
                landings.append((other, rect, False))
        self._show_markers(landings)
        self.ghost.raise_()

    def _drag_end(self, tile):
        if self._dragging is not tile:
            return
        target = self._target
        self._end_drag()
        current = self.order()
        if target is None or tile.key not in current:
            return
        source = current.index(tile.key)
        if source != target:
            self._flow.move_item(source, target)
            self._flow.activate()
            self.fit()
        # Whoever holds the board persists the order, as for the tree.
        self.order_changed.emit(self.order())

    def _end_drag(self):
        tile, self._dragging, self._target = self._dragging, None, None
        for marker in self.markers:
            marker.hide()
        self.ghost.hide()
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        if tile is not None:
            tile.setGraphicsEffect(None)
