"""A roomy window for the hardware setup tips.

These tips carry multi-line shell commands, which are unreadable squeezed into
a message box. Each one gets a card here: what enabling it gains you and the
commands themselves in a selectable monospace block, with why it is missing
and what to expect afterwards a chevron away.
"""

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFont, QFontDatabase, QGuiApplication, QPalette
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFrame,
                             QHBoxLayout, QLabel, QPlainTextEdit,
                             QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

from . import APP_NAME
from .geometry import fit_on_screen
from .widgets import (THEME_EVENTS, Callout, Card, Chevron, app_tip,
                      make_transparent, paint_backdrop)


class CommandBlock(QPlainTextEdit):
    """Read-only monospace block that sizes itself to its content.

    A QPlainTextEdit rather than a styled QLabel so the text stays selectable
    and, more importantly, follows the palette on its own - no stylesheet, so
    nothing to go stale when the theme changes.
    """

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.setFont(font)
        self.setTabChangesFocus(True)

        # Fixed vertically: a scroll area hands out leftover height by shrinking
        # whatever will still shrink, and with the default Expanding policy that
        # was the last card's command block - a two-line snippet ended up one
        # line tall with the second line clipped away.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        # Grow to fit rather than scroll: these are two or three lines and the
        # whole point is to see them all at once.
        doc = self.document()
        doc.setTextWidth(max(self.viewport().width(), 200))

        # QPlainTextEdit reports its document height in LINES, not pixels
        # (QTextEdit is the one that uses pixels), so convert via the font.
        lines = max(1.0, doc.size().height())
        metrics = self.fontMetrics()
        height = int(lines * metrics.lineSpacing()
                     + 2 * doc.documentMargin()
                     + 2 * self.frameWidth()
                     + metrics.lineSpacing() * 0.35)
        return QSize(super().sizeHint().width(), height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Wrapping depends on the width we were just given, so the height has
        # to be recomputed here rather than once at construction.
        wanted = self.sizeHint().height()
        if self.minimumHeight() != wanted:
            self.setMinimumHeight(wanted)
        self.updateGeometry()

    def minimumSizeHint(self):
        return self.sizeHint()


def _wrapped(text, italic=False):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if italic:
        font = QFont(label.font())
        font.setItalic(True)
        label.setFont(font)
    return label


class AdviceCard(Card):
    """One tip: what it gains you, the commands, and the rest on request.

    This page used to spell out four captioned paragraphs per tip - "Right
    now", "What this adds", "After a reboot", "Then" - which made a list of
    three tips a wall of prose that nobody reads to the end. What a tip is
    for and what to run are above the fold; the rest is a chevron away.
    """

    def __init__(self, advice, settings=None, parent=None):
        super().__init__(parent)
        self.advice = advice
        self.settings = settings
        self._key = "advice_detail_%s" % advice.key

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        title = QLabel(advice.title)
        title.setWordWrap(True)
        bold = QFont(title.font())
        bold.setBold(True)
        title.setFont(bold)
        header_row.addWidget(title, 1)

        self.toggle = Chevron(tips=(
            app_tip("Hide the details"),
            app_tip("Details", "Why this is needed, what it changes, and "
                    "how to make it last past a reboot.")))
        self.toggle.clicked.connect(self._flip)
        header_row.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(header)

        gain = _wrapped(advice.effect)
        gain.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(gain)

        body = QFont(self.font())
        body.setBold(False)
        if advice.steps:
            layout.addWidget(self._commands(advice.steps_title, advice.steps,
                                            body))

        # Everything below is the chevron's: true, worth having, and not
        # worth making someone read three of before they find the command.
        self._detail = []
        if advice.problem:
            self._detail.append(_wrapped("Right now: %s" % advice.problem,
                                         italic=True))
        if advice.persist:
            self._detail.append(self._commands("To survive a reboot",
                                               advice.persist, body))
        if advice.persist_note:
            self._detail.append(_wrapped("After a reboot: %s"
                                         % advice.persist_note, italic=True))
        if advice.result:
            self._detail.append(_wrapped("Then: %s" % advice.result,
                                         italic=True))
        for widget in self._detail:
            layout.addWidget(widget)

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
        for widget in self._detail:
            widget.setVisible(expanded)

    def _flip(self):
        self.set_expanded(not self._expanded)
        if self.settings is None:
            return
        try:
            self.settings.setValue(self._key, self._expanded)
        except Exception:
            pass

    def _commands(self, caption, steps, body_font):
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("<b>%s</b>" % caption)
        title.setFont(body_font)
        header.addWidget(title)
        header.addStretch(1)

        copy = QPushButton("Copy")
        copy.setFont(body_font)
        copy.setToolTip(app_tip("Copy", "Copy these commands, to paste "
                                "into a terminal."))
        copy.clicked.connect(lambda: self._copy(steps, copy))
        header.addWidget(copy)
        outer.addLayout(header)

        outer.addWidget(CommandBlock("\n".join(s.command for s in steps)))

        for step in steps:
            if step.note:
                note = _wrapped("• %s" % step.note, italic=True)
                note.setFont(body_font)
                outer.addWidget(note)
        return box

    @staticmethod
    def _copy(steps, button):
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText("\n".join(s.command for s in steps))
        button.setText("Copied")
        # Reset the label so the button does not lie on a second click.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: button.setText("Copy"))


class HardwareSetupDialog(QDialog):
    """Scrollable list of setup tips, sized to actually be readable."""

    def __init__(self, advice, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Hardware Setup")
        self.setSizeGripEnabled(True)
        self.setMinimumSize(560, 400)

        layout = QVBoxLayout(self)

        if advice:
            # The warning stays: these are root commands that load kernel
            # drivers, and which one a board needs is a guess until
            # sensors-detect confirms it. It is folded rather than cut, and
            # what it costs to get wrong is on the first line, not the fourth.
            layout.addWidget(Callout(
                headline="These commands run as root, load kernel drivers "
                         "and write system files.",
                summary="Nothing here runs automatically, and %s works "
                        "without any of it." % APP_NAME,
                detail="Run them only if you are comfortable with what they "
                       "do. The one thing %s writes by itself is fan speeds, "
                       "under Tools > Fan Control, and only while you are "
                       "using it: it asks for authorisation first and puts "
                       "every fan back the way it found it when it exits."
                       % APP_NAME,
                settings=settings, setting="advice_warning_expanded",
                tips=("Hide the details", "What this means")))
        else:
            layout.addWidget(_wrapped(
                "Everything this machine exposes is already being read."))

        content = QWidget()
        inner = QVBoxLayout(content)
        inner.setSpacing(10)
        for item in advice:
            inner.addWidget(AdviceCard(item, settings))
        inner.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        make_transparent(scroll)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox()
        copy_all = buttons.addButton("Copy All Commands",
                                     QDialogButtonBox.ButtonRole.ActionRole)
        copy_all.setToolTip(app_tip(
            "Copy All Commands", "Every fix on this page as one script, in "
            "order, to paste into a terminal."))
        copy_all.clicked.connect(lambda: self._copy_all(advice, copy_all))
        copy_all.setEnabled(bool(advice))
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._restore_geometry()

    @staticmethod
    def _copy_all(advice, button):
        blocks = []
        for item in advice:
            lines = ["# %s" % item.title, "# %s:" % item.steps_title]
            lines += [s.command for s in item.steps]
            if item.persist:
                lines.append("# ...and to survive a reboot:")
                lines += [s.command for s in item.persist]
            blocks.append("\n".join(lines))

        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText("\n\n".join(blocks))
        button.setText("Copied")
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: button.setText("Copy All Commands"))

    # ---- backdrop -------------------------------------------------------

    def paintEvent(self, event):
        paint_backdrop(self)
        super().paintEvent(event)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)

    # ---- geometry -------------------------------------------------------

    def _restore_geometry(self):
        saved = self.settings.value("setup_dialog_geometry") if self.settings else None
        if saved is not None and hasattr(saved, "isEmpty") and not saved.isEmpty():
            self.restoreGeometry(saved)
            return

        # Comfortably larger than a message box, never larger than the screen.
        self.resize(*fit_on_screen(self, 820, 640))

    def closeEvent(self, event):
        if self.settings is not None:
            try:
                self.settings.setValue("setup_dialog_geometry", self.saveGeometry())
            except Exception:
                pass
        super().closeEvent(event)
