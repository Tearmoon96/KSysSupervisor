"""The recovery stick's screen: the Disk Utility, full screen, with a bar
for the keyboard, restarting and switching off.

Runs only on the stick (as root, from the system rescuebuild put
together); rescueinit started it and acts on how it ends.
"""

from __future__ import annotations

import json
import os
import struct
import sys

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import (QColor, QCursor, QFont, QIcon, QImage, QPainter,
                         QPalette, QPixmap)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                             QDialog, QFrame, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from . import APP_NAME, APP_VERSION, diskplan, rescuebuild, rescueinit

#: Palette roles carried over from the desktop the stick was made on.
PALETTE_ROLES = ("Window", "WindowText", "Base", "AlternateBase", "Text",
                 "Button", "ButtonText", "Highlight", "HighlightedText",
                 "ToolTipBase", "ToolTipText", "PlaceholderText", "Link",
                 "Mid", "Midlight", "Dark", "Light", "Shadow")


def read_settings(path=rescuebuild.MARKER):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def palette_of(colours):
    """A QPalette from {role: "#rrggbb"} for the active and inactive state,
    with disabled text dimmed the way Breeze does it."""
    palette = QPalette()
    for name, value in colours.items():
        role = getattr(QPalette.ColorRole, name, None)
        if role is None:
            continue
        colour = QColor(value)
        for group in (QPalette.ColorGroup.Active,
                      QPalette.ColorGroup.Inactive):
            palette.setColor(group, role, colour)
        if name in ("WindowText", "Text", "ButtonText"):
            dim = QColor(colour)
            dim.setAlphaF(0.45)
            palette.setColor(QPalette.ColorGroup.Disabled, role, dim)
        else:
            palette.setColor(QPalette.ColorGroup.Disabled, role, colour)
    return palette


def desktop_palette(app):
    """The colours in use here, for the stick to show the same."""
    palette = app.palette()
    return {name: palette.color(QPalette.ColorGroup.Active,
                                getattr(QPalette.ColorRole, name)).name()
            for name in PALETTE_ROLES}


class Scrim(QWidget):
    """A dark veil over the screen behind the dialog on top.

    The stick has no window manager: a dialog has no frame and no shadow,
    and over the window - or over another dialog - it is hard to tell where
    it ends. The veil sits just under whichever dialog is on top.
    """

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.dialogs = []
        #: What the screen showed as each dialog opened. The firmware's
        #: screen draws no window see-through, so the veil shows a darkened
        #: picture of what is behind instead - a dialog under a dialog
        #: included.
        self.pictures = {}
        #: The window everything opens over.
        self.behind = None

    def paintEvent(self, _event):
        painter = QPainter(self)
        picture = self.pictures.get(id(self.dialogs[-1])) \
            if self.dialogs else None
        if picture is not None and not picture.isNull():
            painter.drawPixmap(0, 0, picture)
        else:
            painter.fillRect(self.rect(), self.palette().window())
        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))

    def eventFilter(self, watched, event):
        if not isinstance(watched, QWidget) or watched is self \
                or not watched.isWindow():
            return False
        kind = watched.windowType()
        if kind in (Qt.WindowType.Popup, Qt.WindowType.ToolTip,
                    Qt.WindowType.SplashScreen) or not isinstance(
                        watched, QDialog):
            return False
        if event.type() == QEvent.Type.Show:
            if watched not in self.dialogs:
                self.pictures[id(watched)] = self.picture()
                self.dialogs.append(watched)
            QTimer.singleShot(0, self.restack)
        elif event.type() in (QEvent.Type.Hide, QEvent.Type.Close):
            if watched in self.dialogs:
                self.dialogs.remove(watched)
                self.pictures.pop(id(watched), None)
            QTimer.singleShot(0, self.restack)
        return False

    def picture(self):
        """What is on the screen before a new dialog opens: the window, and
        every dialog already open where it is. Drawn from the widgets
        themselves - the firmware's screen cannot be read back."""
        screen = QApplication.primaryScreen()
        if self.behind is None or screen is None:
            return None
        picture = QPixmap(screen.geometry().size())
        picture.fill(self.palette().window().color())
        painter = QPainter(picture)
        painter.drawPixmap(self.behind.geometry().topLeft(),
                           self.behind.grab())
        for dialog in self.dialogs:
            if dialog.isVisible():
                painter.fillRect(picture.rect(), QColor(0, 0, 0, 120))
                painter.drawPixmap(dialog.geometry().topLeft(),
                                   dialog.grab())
        painter.end()
        return picture

    def restack(self):
        self.dialogs = [d for d in self.dialogs if d.isVisible()]
        if not self.dialogs:
            self.hide()
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.show()
        self.update()
        self.raise_()
        top = self.dialogs[-1]
        top.raise_()
        top.activateWindow()


class ChangesPanel(QFrame):
    """The prepared changes, in the order they will run, and the one button
    that carries them out."""

    MAX_ROWS = 5

    def __init__(self, disk_window, parent=None):
        super().__init__(parent)
        self.disk_window = disk_window
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 12)
        outer.setSpacing(6)

        head = QHBoxLayout()
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        head.addWidget(self.title)
        head.addStretch(1)
        self.remove = QPushButton("Remove")
        self.remove.setToolTip("Take the selected change off the list, and "
                               "every later one that depends on it.")
        self.remove.clicked.connect(self._remove)
        self.discard = QPushButton("Discard All")
        self.discard.setToolTip("Empty the list. Nothing on any drive has "
                                "been changed, so nothing needs undoing.")
        self.discard.clicked.connect(self._discard)
        self.apply = QPushButton()
        self.apply.setDefault(True)
        bold = self.apply.font()
        bold.setBold(True)
        self.apply.setFont(bold)
        self.apply.setToolTip("Carry out every change in the list, in "
                              "order. Asks once more first.")
        self.apply.clicked.connect(disk_window.apply_changes)
        for button in (self.remove, self.discard, self.apply):
            head.addWidget(button)
        outer.addLayout(head)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        outer.addWidget(self.note)
        self.list = QListWidget()
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        outer.addWidget(self.list)
        self.refresh()

    @property
    def queue(self):
        return self.disk_window.queue

    def refresh(self):
        changes = list(self.queue.changes)
        count = len(changes)
        self.title.setText("Prepared changes (%d)" % count if count
                           else "No changes prepared")
        applying = self.disk_window._applying
        self.note.setText(
            "Applying: each change leaves the list as it is done. The "
            "drives above show what is done and what is still to come."
            if applying and count else
            "Nothing has been changed on any drive yet. The drives above "
            "are shown as they will be; the changes run in this order when "
            "you press Apply Changes, and stop at the first one that fails."
            if count else
            "Choose what to do with the drives above. Each change is added "
            "here and shown on the drives; nothing is changed until you "
            "press Apply Changes.")
        keep = self.list.currentRow()
        self.list.clear()
        for number, change in enumerate(changes, 1):
            item = QListWidgetItem("%d.  %s%s" % (
                number, change.title,
                "   - stopped: %s" % change.error.split("\n")[0]
                if change.error else ""))
            item.setToolTip("\n".join(
                ["%s:" % change.plan.title] + ["  - %s" % step for step in
                                               change.plan.steps]
                + (["", "Will be lost: %s" % "; ".join(change.plan.loses)]
                   if change.plan.loses else [])))
            if change.error:
                item.setForeground(QColor("#c0392b"))
            self.list.addItem(item)
        if 0 <= keep < count:
            self.list.setCurrentRow(keep)
        rows = min(max(count, 1), self.MAX_ROWS)
        self.list.setFixedHeight(
            self.list.sizeHintForRow(0) * rows + 2 * self.list.frameWidth()
            + 4 if count else 0)
        self.list.setVisible(bool(count))
        self.apply.setText("Apply Changes (%d)" % count if count
                           else "Apply Changes")
        self._update_buttons()

    def _busy(self):
        window = self.disk_window
        return window._applying or window._job is not None or window._busy

    def _update_buttons(self):
        count = len(self.queue.changes)
        busy = self._busy()
        self.apply.setEnabled(bool(count) and not busy)
        self.discard.setEnabled(bool(count) and not busy)
        self.remove.setEnabled(self.list.currentRow() >= 0 and not busy)

    def _remove(self):
        row = self.list.currentRow()
        if row < 0 or row >= len(self.queue.changes):
            return
        change = self.queue.changes[row]
        later = self.queue.dependents(change)
        if later:
            text = ("These were prepared on top of it and go too:\n\n%s"
                    % "\n".join("  - %s" % c.title for c in later))
            if QMessageBox.question(
                    self, "Remove a change",
                    "Remove \"%s\"?\n\n%s" % (change.title, text)) \
                    != QMessageBox.StandardButton.Yes:
                return
        self.disk_window.remove_change(change)

    def _discard(self):
        if QMessageBox.question(
                self, "Discard all changes",
                "Remove all %d prepared changes from the list? Nothing on "
                "any drive has been changed." % len(self.queue.changes)) \
                != QMessageBox.StandardButton.Yes:
            return
        self.queue.clear()
        self.disk_window.queue_changed()


class RescueWindow(QWidget):
    """Everything on the screen."""

    def __init__(self, settings, disk_window=None):
        super().__init__(None)
        self.settings = settings
        self.exit_code = None
        self.setWindowTitle("%s Recovery" % APP_NAME)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QWidget()
        bar.setAutoFillBackground(True)
        bar.setBackgroundRole(QPalette.ColorRole.Highlight)
        bar.setForegroundRole(QPalette.ColorRole.HighlightedText)
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 8, 12, 8)
        title = QLabel("%s Recovery" % APP_NAME)
        font = title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        title.setFont(font)
        title.setForegroundRole(QPalette.ColorRole.HighlightedText)
        row.addWidget(title)
        made = QLabel("made %s" % settings.get("built", "?"))
        made.setForegroundRole(QPalette.ColorRole.HighlightedText)
        made.setToolTip("Put together from the computer it was made on: "
                        "%s %s, kernel %s." % (
                            APP_NAME, settings.get("version", "?"),
                            settings.get("kernel", "?")))
        row.addWidget(made)
        row.addStretch(1)

        caption = QLabel("Keyboard:")
        caption.setForegroundRole(QPalette.ColorRole.HighlightedText)
        row.addWidget(caption)
        self.keyboard = QComboBox()
        current = os.environ.get("XKB_DEFAULT_LAYOUT") or settings.get(
            "keyboard") or "us"
        from .rescueui import KEYBOARDS
        entries = list(KEYBOARDS)
        if current not in dict(entries):
            entries.insert(0, (current, current))
        for code, name in entries:
            self.keyboard.addItem(name, code)
        self.keyboard.setCurrentIndex(self.keyboard.findData(current))
        self.keyboard.setToolTip("The layout of the keyboard attached. "
                                 "Changing it restarts the Disk Utility.")
        self.keyboard.activated.connect(self._keyboard_chosen)
        row.addWidget(self.keyboard)
        row.addSpacing(12)

        self.restart = QPushButton("Restart")
        self.restart.setToolTip("Restart the computer. Take the stick out "
                                "first to start the installed system.")
        self.restart.clicked.connect(
            lambda: self.leave(rescueinit.EXIT_REBOOT))
        self.power = QPushButton("Power Off")
        self.power.setToolTip("Switch the computer off. Everything on the "
                              "stick is written out first.")
        self.power.clicked.connect(
            lambda: self.leave(rescueinit.EXIT_POWER_OFF))
        row.addWidget(self.restart)
        row.addWidget(self.power)
        outer.addWidget(bar)

        if disk_window is None:
            from .diskwindow import DiskUtilityWindow
            disk_window = DiskUtilityWindow(queue=diskplan.Queue())
        self.disk_window = disk_window
        outer.addWidget(disk_window, 1)
        self.changes = ChangesPanel(disk_window)
        outer.addWidget(self.changes)
        disk_window.queue_listener = self.changes.refresh
        # The panel's buttons follow the window's own: a job starting or
        # ending re-checks them.
        disk_window.job_listener = self.changes._update_buttons

    def busy(self):
        return self.disk_window._job is not None

    def ask(self, title, text):
        answer = QMessageBox.question(self, title, text)
        return answer == QMessageBox.StandardButton.Yes

    def _keyboard_chosen(self, _index):
        code = self.keyboard.currentData()
        if code == (os.environ.get("XKB_DEFAULT_LAYOUT") or "us"):
            return
        if self.busy():
            self.keyboard.setCurrentIndex(self.keyboard.findData(
                os.environ.get("XKB_DEFAULT_LAYOUT") or "us"))
            QMessageBox.warning(self, "Keyboard", "An operation is running. "
                                "Change the keyboard when it has finished.")
            return
        try:
            with open(rescueinit.KEYBOARD_FILE, "w") as handle:
                handle.write(code + "\n")
        except OSError:
            return
        self.exit_code = rescueinit.EXIT_RESTART
        QApplication.instance().quit()

    def leave(self, code):
        if self.busy():
            QMessageBox.warning(
                self, "%s Recovery" % APP_NAME,
                "An operation is still running. Wait for it to finish, or "
                "stop it, before switching off.")
            return
        what = "restart" if code == rescueinit.EXIT_REBOOT else "switch off"
        pending = len(self.disk_window.queue.changes) \
            if self.disk_window.queue is not None else 0
        if not self.ask("%s Recovery" % APP_NAME,
                        "Do you want to %s the computer now?%s" % (
                            what, "\n\nThe %d prepared change%s will not be "
                            "made: nothing on the drives was changed."
                            % (pending, "" if pending == 1 else "s")
                            if pending else "")):
            return
        self.exit_code = code
        QApplication.instance().quit()


XCURSOR_IMAGE = 0xfffd0002
#: The arrow's nominal size at a scale factor of 1, as desktops default to.
CURSOR_SIZE = 24


def xcursor_image(data, size):
    """(width, height, x hot spot, y hot spot, ARGB bytes) of the image in
    an Xcursor file whose nominal size is nearest `size` (the larger one
    on a tie), or None when the data is not an Xcursor file."""
    if len(data) < 16 or data[:4] != b"Xcur":
        return None
    try:
        header, _version, count = struct.unpack_from("<III", data, 4)
        best = None
        for index in range(count):
            kind, nominal, where = struct.unpack_from(
                "<III", data, header + 12 * index)
            if kind != XCURSOR_IMAGE:
                continue
            key = (abs(nominal - size), -nominal)
            if best is None or key < best[0]:
                best = (key, where)
        if best is None:
            return None
        width, height, x_hot, y_hot = struct.unpack_from(
            "<IIII", data, best[1] + 16)
        start = best[1] + 36
        pixels = data[start:start + 4 * width * height]
        if not width or not height or len(pixels) != 4 * width * height:
            return None
        return width, height, x_hot, y_hot, pixels
    except struct.error:
        return None


def arrow_cursor(path, scale):
    """The desktop's arrow from its Xcursor file, at the screen's scale;
    Qt's own arrow when there is none."""
    try:
        with open(path or "", "rb") as handle:
            found = xcursor_image(handle.read(), round(CURSOR_SIZE * scale))
    except OSError:
        found = None
    if found is None:
        return QCursor(Qt.CursorShape.ArrowCursor)
    width, height, x_hot, y_hot, pixels = found
    image = QImage(pixels, width, height, 4 * width,
                   QImage.Format.Format_ARGB32_Premultiplied).copy()
    return QCursor(QPixmap.fromImage(image), x_hot, y_hot)


def run(qt_args):
    """The stick's application; its exit code tells rescueinit what next."""
    settings = read_settings()
    if settings is None or os.geteuid() != 0:
        print("--rescue only works on the %s recovery stick." % APP_NAME,
              file=sys.stderr)
        return 2
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(APP_VERSION)
    app = QApplication([sys.argv[0]] + list(qt_args))
    app.setStyle("Fusion")
    if settings.get("palette"):
        app.setPalette(palette_of(settings["palette"]))
    if settings.get("font"):
        app.setFont(QFont(settings["font"], int(round(
            settings.get("font_size") or 10))))
    if settings.get("mono"):
        # The fixed-width text (a job's output, the kernel's messages) asks
        # for "monospace", which fontconfig on the stick resolves to nothing.
        QFont.insertSubstitution("monospace", settings["mono"])
    # One pointer, the arrow, everywhere: over text, links and splitters
    # too. An override cursor wins over every widget's own.
    try:
        scale = float(os.environ.get("QT_SCALE_FACTOR") or 1)
    except ValueError:
        scale = 1.0
    app.setOverrideCursor(arrow_cursor(settings.get("cursor"), scale))
    # The icons the build took along, from the desktop's own theme. Without
    # a desktop's platform theme Qt has no idea where themes live.
    QIcon.setThemeSearchPaths(["/usr/share/icons"]
                              + list(QIcon.themeSearchPaths()))
    QIcon.setThemeName("breeze-dark" if settings.get("dark") else "breeze")
    QIcon.setFallbackThemeName("hicolor")
    scrim = Scrim()
    app.installEventFilter(scrim)
    window = RescueWindow(settings)
    scrim.behind = window
    window.showFullScreen()
    app.exec()
    return window.exit_code if window.exit_code is not None else 1
