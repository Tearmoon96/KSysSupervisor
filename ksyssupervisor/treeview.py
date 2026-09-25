"""The classic view: one tree, a column per number, HWMonitor style.

The other face of the same readings the tiles show. SensorTree renders
display.TreeModel and offers the board's small protocol - sync, expand all,
order - so the window can hold either and not care which.
"""

from PyQt6.QtWidgets import (QHeaderView, QMenu, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem, QTreeWidget,
                             QTreeWidgetItem)
from PyQt6.QtCore import Qt, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen

from . import display
from .geometry import screen_limits
from .providers import GROUP_ORDER
from .widgets import THEME_EVENTS, TOOL_COLOURS, app_tip, explain, repolish

#: The glyph in front of each row, by kind of reading.
SENSOR_ICONS = {
    "temp": "🌡️",
    "fan": "🌀",
    "power": "⚡",
    "voltage": "⚡",
    "utilization": "📊",
    "memory": "💾",
    "memory_gb": "💾",
    "clock": "⏱️",
}

#: The column the drag handle is drawn in.
HANDLE_COLUMN = 4


class NoHighlightDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        option.state &= ~QStyle.StateFlag.State_Selected
        option.state &= ~QStyle.StateFlag.State_MouseOver
        super().paint(painter, option, index)


class DragHandleDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        option.state &= ~QStyle.StateFlag.State_Selected
        option.state &= ~QStyle.StateFlag.State_MouseOver
        super().paint(painter, option, index)

        if option.widget is None:
            return
        item = option.widget.itemFromIndex(index)
        if item and item.parent() is None:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QColor(140, 140, 140))
            painter.setPen(Qt.PenStyle.NoPen)

            rect = option.rect
            cx = rect.x() + rect.width() / 2.0
            cy = rect.y() + rect.height() / 2.0
            r = 1.5
            gap_x = 4
            gap_y = 4

            for row in range(-1, 2):
                for col in range(-1, 1):
                    x = cx + (col * gap_x) + (gap_x // 2)
                    y = cy + (row * gap_y)
                    painter.drawEllipse(QPointF(x, y), r, r)

            painter.restore()


class SortableTreeWidget(QTreeWidget):
    """A tree whose top-level items can be dragged into a new order."""

    order_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._drag_item = None
        self._drag_active = False
        self._drop_indicator_y = -1

    def _is_handle_column(self, pos):
        return self.header().logicalIndexAt(pos.x()) == HANDLE_COLUMN

    @staticmethod
    def _capture_expansion(item):
        state = [item.isExpanded()]
        for i in range(item.childCount()):
            state.append(item.child(i).isExpanded())
        return state

    @staticmethod
    def _restore_expansion(item, state):
        if not state:
            return
        item.setExpanded(state[0])
        for i in range(min(item.childCount(), len(state) - 1)):
            item.child(i).setExpanded(state[i + 1])

    def mouseMoveEvent(self, event):
        item = self.itemAt(event.pos())

        if self._drag_active and self._drag_item:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            target = self.itemAt(event.pos())
            if target and target.parent() is None and target is not self._drag_item:
                src_idx = self.indexOfTopLevelItem(self._drag_item)
                tgt_idx = self.indexOfTopLevelItem(target)
                if src_idx != tgt_idx:
                    # takeTopLevelItem() discards the expand state of the item
                    # and its children, so capture and restore it around the move.
                    expanded = self._capture_expansion(self._drag_item)
                    self.takeTopLevelItem(src_idx)
                    tgt_idx = self.indexOfTopLevelItem(target)
                    if src_idx < tgt_idx:
                        self.insertTopLevelItem(tgt_idx + 1, self._drag_item)
                    else:
                        self.insertTopLevelItem(tgt_idx, self._drag_item)
                    self._restore_expansion(self._drag_item, expanded)
            # Update drop indicator position
            rect = self.visualItemRect(self._drag_item)
            mouse_y = event.pos().y()
            if mouse_y < rect.center().y():
                self._drop_indicator_y = rect.top()
            else:
                self._drop_indicator_y = rect.bottom()
            self.viewport().update()
            return
        elif item and item.parent() is None and self._is_handle_column(event.pos()):
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            if item and item.parent() is None and self._is_handle_column(event.pos()):
                self._drag_item = item
                self._drag_active = True
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                return

        self._drag_item = None
        self._drag_active = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_active and self._drag_item:
            self._drag_active = False
            self._drag_item = None
            self._drop_indicator_y = -1
            self.viewport().update()
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

            # Whoever holds the tree persists the order; the board does the
            # same, so both views land on one settings key through one path.
            self.order_changed.emit(self.order())

            self.clearSelection()
            self.setCurrentItem(None)

            return

        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def order(self):
        """The top-level keys in the order they are shown."""
        keys = []
        for i in range(self.topLevelItemCount()):
            key = self.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            if key:
                keys.append(key)
        return keys

    def drawRow(self, painter, option, index):
        """Draw a row without the hover highlight.

        This used to be done with a stylesheet, but setting any stylesheet puts
        the widget and its children on Qt's stylesheet painting path, which
        resolves colours once at polish time. The scrollbars then kept the old
        theme's colours after a light/dark switch while the rest of the window
        followed the new palette. Stripping the hover state here keeps the
        native style - and therefore correct theming - everywhere.
        """
        option = QStyleOptionViewItem(option)
        option.state &= ~QStyle.StateFlag.State_MouseOver
        super().drawRow(painter, option, index)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            repolish(self, self.viewport(), self.header(),
                     self.verticalScrollBar(), self.horizontalScrollBar())
        super().changeEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drag_active and self._drop_indicator_y >= 0:
            painter = QPainter(self.viewport())
            pen = QPen(QColor("#4a9eff"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(0, self._drop_indicator_y, self.viewport().width(), self._drop_indicator_y)
            painter.end()


def _group_insert_index(parent, group):
    """Keep groups in GROUP_ORDER no matter which one arrives first."""
    def rank(name):
        return GROUP_ORDER.index(name) if name in GROUP_ORDER else len(GROUP_ORDER)

    target = rank(group)
    for i in range(parent.childCount()):
        if rank(parent.child(i).text(0)) > target:
            return i
    return parent.childCount()


def row_texts(metric):
    """The label, value, min and max cells for one reading."""
    icon = SENSOR_ICONS.get(metric.kind, "")
    label = ("%s %s" % (icon, metric.label)).strip()
    if metric.total:
        # The ceiling is the only maximum worth a column; the running one
        # would just be however full it has been.
        return (label, metric.text, "//",
                display.format_value(metric.total, metric.kind))
    if metric.low is None:
        return (label, metric.text, display.MISSING, display.MISSING)
    return (label, metric.text,
            display.format_value(metric.low, metric.kind),
            display.format_value(metric.high, metric.kind))


class SensorTree(SortableTreeWidget):
    """The tree of devices, groups and readings, refreshed in place."""

    rename_requested = pyqtSignal(str)
    reset_name_requested = pyqtSignal(str)
    clear_bounds_requested = pyqtSignal(str)
    #: A reading's uid, from its row's right-click menu.
    graph_requested = pyqtSignal(str)

    #: Where a reading row keeps its uid. Not UserRole: that is the device key
    #: on a category, and apply_order() reads it from every top-level item.
    UID_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._categories = {}     # device key -> QTreeWidgetItem
        self._nodes = {}          # (device key, group) -> QTreeWidgetItem
        self._rows = {}           # reading uid -> QTreeWidgetItem

        self.setColumnCount(5)
        self.setHeaderLabels(["Sensor", "Value", "Min", "Max", ""])
        header = self.headerItem()
        for column, (title, text) in enumerate((
                ("Sensor", "Click to sort by name; click again to reverse."),
                ("Value", "The reading now."),
                ("Min", "The lowest since the app started, or since Clear "
                        "Min/Max."),
                ("Max", "The highest since the app started, or since Clear "
                        "Min/Max."),
                ("Move", "Drag a category by this handle to reorder."))):
            header.setToolTip(column, app_tip(title, text))
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.header().moveSection(HANDLE_COLUMN, 0)
        self.header().setMinimumSectionSize(1)
        self.header().setSectionResizeMode(HANDLE_COLUMN, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(HANDLE_COLUMN, 20)
        self.setIndentation(16)
        self.setAlternatingRowColors(True)
        self.setAnimated(True)
        self.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.setItemDelegate(NoHighlightDelegate(self))
        self.setItemDelegateForColumn(HANDLE_COLUMN, DragHandleDelegate(self))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    # ---- content ---------------------------------------------------------

    def _expand_new(self):
        if self.settings is None:
            return True
        return not self.settings.value("collapse_startup", False, type=bool)

    def sync(self, models):
        """Create, update and retire items to match this tick's models."""
        wanted_devices = set()
        wanted_nodes = set()
        wanted_rows = set()

        for model in models:
            wanted_devices.add(model.key)
            category = self._category_for(model)
            if category.text(0) != model.name:
                category.setText(0, model.name)
            for group, metrics in model.groups:
                wanted_nodes.add((model.key, group))
                node = self._node_for(category, model.key, group)
                for metric in metrics:
                    wanted_rows.add(metric.uid)
                    self._update_row(node, metric)

        for uid in [u for u in self._rows if u not in wanted_rows]:
            item = self._rows.pop(uid)
            parent = item.parent()
            if parent is not None:
                parent.removeChild(item)
        for key in [k for k in self._nodes if k not in wanted_nodes]:
            node = self._nodes.pop(key)
            parent = node.parent()
            if parent is not None:
                parent.removeChild(node)
        for key in [k for k in self._categories if k not in wanted_devices]:
            item = self._categories.pop(key)
            self.takeTopLevelItem(self.indexOfTopLevelItem(item))

    def _category_for(self, model):
        """Top-level item for a device, created on first use.

        Identity is the device key (PCI address, chip name), so a rename or a
        hardware change does not lose the saved name and order.
        """
        item = self._categories.get(model.key)
        if item is None:
            item = QTreeWidgetItem(self, [model.name, "", "", "", ""])
            font = QFont()
            font.setBold(True)
            item.setFont(0, font)
            item.setData(0, Qt.ItemDataRole.UserRole, model.key)
            item.setToolTip(0, app_tip(
                model.name, "Right-click to rename it or clear its min/max; "
                "drag the handle on the left to reorder."))
            self._categories[model.key] = item
            if self._expand_new():
                item.setExpanded(True)
        return item

    def _node_for(self, category, key, group):
        """Group node under a device, created on first use."""
        node = self._nodes.get((key, group))
        if node is None:
            node = QTreeWidgetItem([group])
            category.insertChild(_group_insert_index(category, group), node)
            self._nodes[(key, group)] = node
            if self._expand_new():
                category.setExpanded(True)
                node.setExpanded(True)
        return node

    def _update_row(self, node, metric):
        texts = row_texts(metric)
        item = self._rows.get(metric.uid)
        if item is None:
            item = QTreeWidgetItem(node, list(texts))
            item.setData(0, self.UID_ROLE, metric.uid)
            item.setToolTip(0, app_tip(
                metric.label, "%s. Right-click to show it in Live Graphs."
                % metric.group if metric.group else
                "Right-click to show it in Live Graphs."))
            self._rows[metric.uid] = item
            return
        if item.parent() is not node:
            # A rename of the group it belongs to; rare, but not impossible.
            old = item.parent()
            if old is not None:
                old.removeChild(item)
            node.addChild(item)
        for column, text in enumerate(texts):
            if item.text(column) != text:
                item.setText(column, text)

    def set_all_expanded(self, expanded):
        if expanded:
            self.expandAll()
        else:
            self.collapseAll()

    # ---- ordering --------------------------------------------------------

    def apply_order(self, keys):
        """Put the categories in this order, leaving unlisted ones after."""
        items, expansion = {}, {}
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            items[item.data(0, Qt.ItemDataRole.UserRole)] = item
            # Taking an item out of the tree forgets whether it was open,
            # so that is noted before any of them is taken.
            expansion[id(item)] = self._capture_expansion(item)

        for i in range(self.topLevelItemCount() - 1, -1, -1):
            self.takeTopLevelItem(i)

        placed = set()
        for key in list(keys) + list(items):
            item = items.get(key)
            if item is not None and id(item) not in placed:
                self.addTopLevelItem(item)
                self._restore_expansion(item, expansion[id(item)])
                placed.add(id(item))

    # ---- geometry --------------------------------------------------------

    def wanted_width(self, window):
        """How wide the window would need to be to show every column.

        A preference rather than a rule: the caller applies it once, only
        ever widens, and the screen caps it - so a machine with very long
        device names gets a scroll bar instead of a window running off the
        display.
        """
        columns = sum(self.columnWidth(i) for i in range(self.columnCount()))
        if not columns:
            return None
        # What the tree needs on top of its columns: the frame, and a scroll
        # bar's width so the last column is not clipped the moment one appears.
        chrome = window.width() - self.viewport().width()
        wanted = columns + chrome + self.header().minimumSectionSize()
        limits = screen_limits(window)
        if limits is not None:
            wanted = min(wanted, limits[0])
        return wanted

    # ---- menu ------------------------------------------------------------

    def _context_menu(self, position):
        item = self.itemAt(position)
        if item is None:
            return
        if item.parent() is None:
            menu = self.menu_for(item)
        elif item.data(0, self.UID_ROLE):
            menu = self.menu_for_reading(item)
        else:
            return                       # a group node: nothing to offer
        menu.exec(self.viewport().mapToGlobal(position))

    def menu_for_reading(self, item):
        """The right-click menu of one reading; separate for the tests."""
        uid = item.data(0, self.UID_ROLE)
        menu = QMenu(self)
        action = menu.addAction("Show in Live Graphs")
        action.triggered.connect(lambda: self.graph_requested.emit(uid))
        explain(action, "Show in Live Graphs",
                "Open the Live Graphs panel with this reading in it.",
                TOOL_COLOURS["graphs"])
        return menu

    def menu_for(self, item):
        """The right-click menu of a category; separate so a test can drive it."""
        key = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        rename = menu.addAction("Rename...")
        rename.triggered.connect(lambda: self.rename_requested.emit(key))
        explain(rename, "Rename", "Give this device a name of your own, "
                "here and on its tile.")
        reset = menu.addAction("Reset name")
        reset.triggered.connect(lambda: self.reset_name_requested.emit(key))
        explain(reset, "Reset Name", "Go back to the name the hardware "
                "reports.")
        menu.addSeparator()
        clear = menu.addAction("Clear min/max here")
        clear.triggered.connect(lambda: self.clear_bounds_requested.emit(key))
        explain(clear, "Clear Min/Max", "Start this device's lowest and "
                "highest readings again from now.")
        return menu
