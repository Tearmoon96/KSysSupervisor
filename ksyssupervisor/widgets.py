"""Shared widgets: cards, callouts, an eliding label and a sticky menu."""

import threading

from PyQt6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel,
                             QMenu, QPushButton, QSizePolicy, QStyle,
                             QVBoxLayout, QWidget)
from PyQt6.QtCore import Qt, QRectF, QEvent, QEventLoop, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter,
                         QPalette, QPen)

from . import log

#: Amber rather than a palette role: no role means "caution", and this has to
#: read as a warning against both a light and a dark background.
WARNING_ACCENT = QColor("#e8a33d")


def blend(base, other, amount):
    """`base` moved `amount` of the way towards `other`, alpha untouched."""
    keep = 1.0 - amount
    mixed = QColor(int(base.red() * keep + other.red() * amount),
                   int(base.green() * keep + other.green() * amount),
                   int(base.blue() * keep + other.blue() * amount))
    mixed.setAlpha(base.alpha())
    return mixed


def tip(title, text="", colour=None):
    """A tooltip with a title, drawn by the platform's own tooltip.

    Rich text inside the KDE tooltip rather than a styled popup: Breeze
    draws the frame and the background, so it follows the theme, and the
    title alone takes `colour` - the tile's or the tool's - so a tooltip
    says at a glance which part of the window it belongs to.
    """
    if isinstance(colour, QColor):
        colour = colour.name()
    head = "<b>%s</b>" % _escape(title)
    if colour:
        head = '<span style="color:%s">%s</span>' % (colour, head)
    if not text:
        return head
    return "%s<br>%s" % (head, _escape(text).replace("\n", "<br>"))


#: Each tool's colour, for the titles of its tooltips, so a tooltip says
#: which tool it belongs to. Kept clear of the heat colours, amber and red.
TOOL_COLOURS = {
    "fans": "#5fa8b8",      # Fan Control, steel teal
    "stress": "#c78a66",    # Stress Test, terracotta
    "graphs": "#7d9be0",    # Live Graphs, periwinkle
}


def tool_tip(tool, title, text=""):
    return tip(title, text, TOOL_COLOURS.get(tool))


def app_tip(title, text=""):
    """A tooltip titled in the desktop's own accent colour."""
    from PyQt6.QtWidgets import QApplication
    return tip(title, text,
               QApplication.palette().color(QPalette.ColorRole.Highlight))


def explain(action, title, text, colour=None):
    """Give a menu action a titled tooltip, and its menu the will to show it."""
    action.setToolTip(tip(title, text, colour) if colour is not None
                      else app_tip(title, text))
    for widget in action.associatedObjects():
        if isinstance(widget, QMenu):
            widget.setToolTipsVisible(True)
    return action


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


THEME_EVENTS = (QEvent.Type.PaletteChange,
                QEvent.Type.ApplicationPaletteChange,
                QEvent.Type.StyleChange)


def repolish(*widgets):
    """Make widgets re-resolve their style state after a theme change.

    Under KDE's Breeze the menu bar kept the old theme's text colour after
    a light/dark switch until the window was closed and reopened: the
    palette had changed, but nothing made the style read it again. The
    same trick fixed the scroll bars once; a menu bar is no different.
    """
    for widget in widgets:
        if widget is None:
            continue
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()


#: A warm, slightly darkened tone that a light theme's near-white is pulled
#: towards behind the cards. Flat white glares; this does not.
SOFT_WHITE = QColor(243, 239, 232)


def backdrop_colour(palette):
    """What card containers are painted on.

    A dark theme's window colour is left alone. A light theme's is taken
    down a notch and warmed, so that tinted cards sit on something rather
    than float on glare - and so that the cards, which keep the window's
    own lightness, are lifted off it instead of vanishing into it.
    """
    window = palette.color(QPalette.ColorRole.Window)
    if window.lightness() < 128:
        return window
    return blend(window.darker(109), SOFT_WHITE, 0.4)


#: How far a light theme's dim text is pulled towards its full-strength text.
#: Breeze Light's PlaceholderText is 112,125,138: about 4:1 against the bare
#: window, but 3:1 on a tinted card and 2.6:1 on a chip well, which is below
#: what reads comfortably. Pulled this far it clears 4.5:1 on every card
#: colour and in every chip, and is still visibly quieter than the values.
LIGHT_DIM_PULL = 0.6


def dim_text_colour(palette, pull=LIGHT_DIM_PULL):
    """Secondary text - captions, row names - that can still be read on a card.

    A dark theme's PlaceholderText is left as the scheme made it; a light
    one's is pulled `pull` of the way to the full-strength text. The scheme's
    own value is always taken from the application, never from `palette`: a
    card writes the result into its own palette, and reading it back from
    there would darken it again on every theme event.
    """
    placeholder = QApplication.palette().color(
        QPalette.ColorRole.PlaceholderText)
    if palette.color(QPalette.ColorRole.Window).lightness() < 128:
        return placeholder
    return blend(placeholder, palette.color(QPalette.ColorRole.WindowText),
                 pull)


# A light card's tint, as a pastel of the accent's hue rather than a blend
# towards the accent: the device colours are muted on purpose, and blending
# a near-white towards a muted colour only greys it - a blue card came out
# 5 levels off white and the RAM and GPU cards read as the same grey-pink.
# Fixing the lightness instead keeps every card equally light, so the dim
# text on it reads the same whatever the colour.
LIGHT_TINT_SATURATION = 0.7
LIGHT_TINT_LIGHTNESS = 0.86


def light_tint(accent, lightness=LIGHT_TINT_LIGHTNESS):
    """`accent`'s hue as a pastel of the given HSL lightness, 0..1.

    Saturation follows the accent's own, raised - the device colours are
    muted, and a pastel of a muted colour is hardly a colour - and capped,
    so a grey accent stays a grey card instead of turning into an arbitrary
    colour.
    """
    saturation = min(LIGHT_TINT_SATURATION, accent.hslSaturationF() * 1.6)
    return QColor.fromHslF(max(accent.hslHueF(), 0.0), saturation, lightness)


def paint_backdrop(widget):
    """Fill a widget with the backdrop colour; call from its paintEvent."""
    painter = QPainter(widget)
    painter.fillRect(widget.rect(), backdrop_colour(widget.palette()))
    painter.end()


def make_transparent(scroll):
    """Let a scroll area show whatever is painted behind it.

    QScrollArea.setWidget turns on autoFillBackground for the widget it is
    given, so this has to run after that call, not before.
    """
    scroll.viewport().setAutoFillBackground(False)
    inner = scroll.widget()
    if inner is not None:
        inner.setAutoFillBackground(False)


class Backdrop(QWidget):
    """A widget whose only job is to be the soft ground the cards sit on."""

    def paintEvent(self, event):
        paint_backdrop(self)
        super().paintEvent(event)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)


class Card(QWidget):
    """A rounded panel that follows the palette, drawn rather than styled.

    Painted by hand rather than styled: any stylesheet puts the widget and
    its children on Qt's stylesheet painting path, which resolves colours once
    at polish time and then keeps them across a light/dark switch - that is
    how the scroll bars ended up in the old theme while everything else
    followed the new one. Reading the palette inside paintEvent is correct by
    construction: a palette change repaints, and the repaint asks again.
    Rounded corners are not reachable from QFrame at all, which settles the
    question anyway.
    """

    RADIUS = 8
    ACCENT_WIDTH = 3
    ACCENT_INSET = 6

    # The outline: its width, and how far it is pulled towards the accent.
    # A card on the board wants a faint one; a panel floating over cards of
    # the same tint wants an edge that reads as an edge.
    BORDER_WIDTH = 1
    BORDER_TINT = 0.45

    # How far a dark card's fill is pulled towards its accent, 0..1: enough
    # to tell two cards apart across the window, not enough to fight the text
    # on them. A light card is tinted by light_tint() instead.
    TINT_DARK = 0.21

    # How far a plain light card is lifted from the window colour towards
    # Base, 0..1, so that it sits above the darkened backdrop - and the
    # border's strength there, against the faint one a dark theme needs.
    LIFT = 0.6
    BORDER_ALPHA = 110
    BORDER_ALPHA_LIGHT = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accent = None
        self._sync_dim_text()

    def _sync_dim_text(self):
        """Give everything on the card a dim text colour it can be read by.

        Only PlaceholderText is written, onto a copy of the card's own
        palette request, so every other role keeps following the theme; and
        it is written again on every theme event, so it cannot outlive the
        theme it was worked out for.
        """
        palette = self.palette()
        colour = dim_text_colour(palette)
        if palette.color(QPalette.ColorRole.PlaceholderText) == colour:
            return
        palette = QPalette(palette)
        palette.setColor(QPalette.ColorRole.PlaceholderText, colour)
        self.setPalette(palette)

    def set_accent(self, colour):
        """A vertical bar inside the left edge, or None for a plain card.

        Repaints only on a real change: callers drive this from a once-a-second
        refresh, and a card whose state has not moved should not cost a repaint.
        """
        if colour == self._accent:
            return
        self._accent = colour
        self.update()

    def accent(self):
        return self._accent

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        palette = self.palette()
        window = palette.color(QPalette.ColorRole.Window)
        # Lifted off the background in either theme: lighter on a dark one,
        # and on a light one part of the way to Base, so the card stays above
        # the darkened backdrop once its tint has pulled it down. Qt does not
        # reliably report which theme is in use, but it does report the
        # colour, and that is what matters.
        dark = window.lightness() < 128
        if dark:
            fill = window.lighter(112)
        else:
            base = palette.color(QPalette.ColorRole.Base)
            fill = (blend(window, base, self.LIFT)
                    if base.lightness() > window.lightness() else QColor(window))

        border = QColor(palette.color(QPalette.ColorRole.Mid))
        border.setAlpha(self.BORDER_ALPHA if dark else self.BORDER_ALPHA_LIGHT)

        if self._accent is not None:
            # On a dark theme, mixed into the fill rather than painted over
            # it, so the card keeps the background's own lightness instead
            # of becoming a coloured panel; on a light one, a pastel of a
            # fixed lightness - see light_tint().
            fill = (blend(fill, self._accent, self.TINT_DARK) if dark
                    else light_tint(self._accent))
            border = blend(border, self._accent, self.BORDER_TINT)

        inset = self.BORDER_WIDTH / 2.0
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        painter.setPen(QPen(border, self.BORDER_WIDTH))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)

        if self._accent is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._accent)
            bar = QRectF(rect.left() + self.ACCENT_INSET,
                         rect.top() + self.ACCENT_INSET,
                         self.ACCENT_WIDTH,
                         rect.height() - 2 * self.ACCENT_INSET)
            radius = self.ACCENT_WIDTH / 2.0
            painter.drawRoundedRect(bar, radius, radius)

        painter.end()
        super().paintEvent(event)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            # Nothing to unpolish: there is no stylesheet state to go stale,
            # only a repaint that will read the new palette.
            self._sync_dim_text()
            self.update()
        super().changeEvent(event)


class ElidingLabel(QLabel):
    """A label that shortens its text instead of forcing the window wider.

    A plain QLabel refuses to be laid out narrower than its whole text, which
    stops the window being resized at all. Elided in the middle because both
    ends carry information: for a fan, the card or board at the front and
    which header it is at the back; for a device tile, the make and the model.

    The two hints are what make the window behave: sizeHint is the width of
    the whole name, so the window opens showing it, and minimumSizeHint is
    small, so the window can still be dragged narrower and the name gives way
    rather than the controls. Both reach the parent through the ordinary
    layout, which is why the size policy is Preferred and not Ignored - under
    Ignored neither hint reaches the layout at all, even an explicit minimum
    width, and the window could only ever be one size.
    """

    MIN_WIDTH = 80

    def __init__(self, text="", parent=None, minimum=None):
        super().__init__(text, parent)
        self._full = text
        self._minimum = self.MIN_WIDTH if minimum is None else minimum
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Preferred)

    def setText(self, text):
        if text == self._full:
            return
        self._full = text
        self.setToolTip(text)
        self._elide()
        self.updateGeometry()

    def text(self):
        """The full name, not what happens to fit - callers rename by it."""
        return self._full

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(QFontMetrics(self.font()).horizontalAdvance(self._full))
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(self._minimum)
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full,
                                           Qt.TextElideMode.ElideMiddle,
                                           max(0, self.width())))


class Chevron(QPushButton):
    """The flat expand/collapse button used wherever detail folds away."""

    def __init__(self, parent=None, tips=("Hide the details", "Show more")):
        super().__init__(parent)
        self._tips = tips
        self.setFlat(True)
        self.setFixedSize(24, 24)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_expanded(False)

    def set_expanded(self, expanded):
        icon = QIcon.fromTheme("go-up" if expanded else "go-down")
        if icon.isNull():
            self.setIcon(QIcon())
            self.setText("⌃" if expanded else "⌄")
        else:
            self.setText("")
            self.setIcon(icon)
        self.setToolTip(self._tips[0] if expanded else self._tips[1])


class SectionCard(Card):
    """A titled card, optionally switched on and off by its own checkbox.

    Stands in for a checkable QGroupBox, which draws its own frame in the
    style's colours and so cannot be made to match the tiles.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, title, checkable=False, checked=False, parent=None):
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 12)
        outer.setSpacing(6)

        bold = QFont(self.font())
        bold.setBold(True)

        self._check = None
        if checkable:
            self._check = QCheckBox(title)
            self._check.setFont(bold)
            self._check.toggled.connect(self._on_toggled)
            outer.addWidget(self._check)
        else:
            heading = QLabel(title)
            heading.setFont(bold)
            outer.addWidget(heading)
        self._heading = self._check if checkable else heading

        self.body = QWidget()
        outer.addWidget(self.body)

        if checkable:
            self._check.setChecked(checked)
            self.body.setEnabled(checked)

    def _on_toggled(self, on):
        self.body.setEnabled(on)
        self.toggled.emit(on)

    def setToolTip(self, text):
        """On the title, not the card: a tooltip on the whole card would
        pop up over every control in it that has none of its own."""
        self._heading.setToolTip(text)

    def toolTip(self):
        return self._heading.toolTip()

    def isChecked(self):
        return self._check.isChecked() if self._check is not None else True

    def setChecked(self, on):
        if self._check is not None:
            self._check.setChecked(on)


class Callout(Card):
    """A standing warning, folded down to two lines with the rest behind it.

    For text that is a guarantee about what the machine will do rather than
    decoration: collapsed rather than cut, so the chevron brings it back and
    the choice is remembered.
    """

    def __init__(self, headline, summary, detail, settings=None, setting=None,
                 tips=("Hide the details", "What this means"),
                 start_expanded=True, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setting = setting
        self.set_accent(WARNING_ACCENT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        icon = QIcon.fromTheme("dialog-warning")
        if icon.isNull():
            # Unlike the fan icon there is a standard pixmap for this one, so
            # the fallback is still a real icon rather than a drawn shape.
            icon = self.style().standardIcon(
                QStyle.StandardPixmap.SP_MessageBoxWarning)
        badge = QLabel()
        badge.setPixmap(icon.pixmap(24, 24))
        badge.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(3)

        self.headline = QLabel(headline)
        self.headline.setWordWrap(True)
        font = QFont(self.headline.font())
        font.setBold(True)
        self.headline.setFont(font)
        text.addWidget(self.headline)

        self.summary = QLabel(summary)
        self.summary.setWordWrap(True)
        self.summary.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        text.addWidget(self.summary)

        self.detail = QLabel(detail)
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self.detail)
        layout.addLayout(text, 1)

        self.toggle = Chevron(tips=tips)
        self.toggle.clicked.connect(self._toggle)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)

        expanded = start_expanded
        if settings is not None and setting:
            expanded = settings.value(setting, start_expanded, type=bool)
        self._expanded = None
        self.set_expanded(expanded)

    def set_expanded(self, expanded):
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.detail.setVisible(expanded)
        self.toggle.set_expanded(expanded)

    def _toggle(self):
        self.set_expanded(not self._expanded)
        if self.settings is None or not self.setting:
            return
        try:
            self.settings.setValue(self.setting, self._expanded)
        except Exception:
            log.exception("Could not save the state of %s", self.setting)


#: Breeze (6.7) draws a menu item's text 13px further right than its size
#: calculation allows for - MenuItem_TextLeftMargin, MenuItem_ExtraLeftMargin
#: and a pixel are in drawMenuItemControl but not menuItemSizeFromContents -
#: so a menu whose widest item opened a submenu ran that item's text into
#: its arrow. Items without an arrow have the arrow's column spare.
SUBMENU_ARROW_SLACK = 14

#: The window property Qt's Wayland plugin reads, when it creates a popup, as
#: the rectangle to anchor it to.
WAYLAND_ANCHOR = "_q_waylandPopupAnchorRect"

#: How far a submenu is laid over its parent's edge: one border's width.
SUBMENU_BORDER_OVERLAP = 1


def on_wayland():
    return QApplication.platformName() == "wayland"


class Menu(QMenu):
    """A QMenu that works around two bugs a submenu runs into.

    One is Breeze's, above: the menu is widened when it has submenus. The
    other is Qt's, on Wayland: a submenu is handed to the compositor anchored
    to the top of the item that opened it, where on X11 QMenu lifts it by the
    submenu's own top margin so the two items line up. Without the lift each
    level sat 4px lower than the last. The anchor is set here with the lift
    applied, through the property Qt reads for exactly that override.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._natural_width = None

    def showEvent(self, event):
        # Before the platform window is shown: this is where both the size
        # and the anchor are still read.
        self._fit_submenu_arrows()
        if on_wayland():
            self._anchor_to_parent_item()
        super().showEvent(event)

    def actionEvent(self, event):
        # Once QMenu lays its items out again it folds the minimum width
        # into sizeHint(), and it only does that when the items change: so
        # the natural width is measured with no minimum set, and both are
        # dropped here, as the items change - otherwise every reopening
        # would add the slack again.
        self._natural_width = None
        if self.minimumWidth():
            self.setMinimumWidth(0)
        super().actionEvent(event)

    def _fit_submenu_arrows(self):
        wanted = 0
        if any(action.menu() is not None for action in self.actions()):
            if self._natural_width is None:
                self._natural_width = self.sizeHint().width()
            wanted = self._natural_width + SUBMENU_ARROW_SLACK
        if self.minimumWidth() != wanted:
            self.setMinimumWidth(wanted)

    def opened_from(self):
        """(menu, action) this is being opened from as a submenu, or None."""
        parent = QApplication.activePopupWidget()
        if parent is self or not isinstance(parent, QMenu):
            return None
        action = parent.activeAction()
        if action is None or action.menu() is not self:
            return None
        return parent, action

    def submenu_anchor(self, parent, action):
        """The item's rectangle in its menu, lifted so this menu's first
        item lands on it rather than this menu's top edge.

        Widened to the menu's full width: the item stops short of the menu's
        edges by the style's side margin, and the compositor puts the
        submenu against the anchor, so an anchor the item's own width had
        every submenu cover the last 4px of its parent under Breeze. Across
        the whole menu it sits against the parent's edge - the right one,
        or the left one when the compositor flips it for room - less
        SUBMENU_BORDER_OVERLAP on each side, so the two borders fall on one
        line instead of drawing a double-thick one side by side.
        """
        anchor = parent.actionGeometry(action)
        anchor.setLeft(SUBMENU_BORDER_OVERLAP)
        anchor.setWidth(parent.width() - 2 * SUBMENU_BORDER_OVERLAP)
        actions = self.actions()
        if actions:
            anchor.translate(0, -self.actionGeometry(actions[0]).top())
        return anchor

    def _anchor_to_parent_item(self):
        window = self.windowHandle()
        if window is None:
            return
        opened = self.opened_from()
        # Cleared when not a submenu: the same menu can open on its own too.
        window.setProperty(WAYLAND_ANCHOR,
                           self.submenu_anchor(*opened) if opened else None)


class KeepOpenMenu(Menu):
    def mouseReleaseEvent(self, event):
        action = self.actionAt(event.position().toPoint())
        if action and action.isCheckable():
            action.trigger()
        else:
            super().mouseReleaseEvent(event)


def run_blocking(call):
    """Run `call` on a thread while this one keeps its event loop turning.

    For the calls that wait on a password prompt - starting the fan helper,
    installing it - which can block for minutes. Made on the GUI thread they
    froze the window for all of it, and the desktop offered to kill it as
    unresponsive. Returns (result, exception): exactly one is None, and the
    caller carries on as if the call had been synchronous.

    Whatever the caller does not want clicked meanwhile it has to disable
    itself: the window stays live, so its controls do too.
    """
    outcome = []

    def run():
        try:
            outcome.append((call(), None))
        except Exception as exc:        # the loop must not wait forever
            outcome.append((None, exc))

    worker = threading.Thread(target=run, name="blocking-call", daemon=True)
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(50)
    poll.timeout.connect(lambda: worker.is_alive() or loop.quit())
    worker.start()
    poll.start()
    if worker.is_alive():
        loop.exec()
    poll.stop()
    worker.join()
    return outcome[0]
