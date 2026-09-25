"""The fan control window.

A top-level window rather than a dialog: it is meant to sit next to the sensor
tree while you watch what a change does to the temperatures, so it has to be
movable and closable on its own.

Everything here is presentation and confirmation. The rules about what may be
written live in fanctl.py and, for anything privileged, in the helper.
"""

from PyQt6.QtCore import QEvent, QSettings, Qt, QTimer
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication,
                         QIcon, QPalette)
from PyQt6.QtWidgets import (QButtonGroup, QFrame, QGridLayout,
                             QHBoxLayout, QInputDialog, QLabel, QMenu,
                             QMessageBox, QPushButton, QRadioButton,
                             QScrollArea, QSizePolicy, QSlider, QStyle,
                             QVBoxLayout, QWidget)

from . import APP_NAME, fanctl, log
from .geometry import screen_limits
from .widgets import (THEME_EVENTS, WARNING_ACCENT, Callout, Card,
                      ElidingLabel, make_transparent, paint_backdrop,
                      run_blocking, tool_tip)

#: Below this the fan may be too slow to move useful air, and on a DC header it
#: may not spin at all. Warned about rather than forbidden - some case fans are
#: perfectly happy down here, and it is the user's machine.
LOW_PERCENT = 20

#: Milliseconds of stillness before a slider move is sent. Dragging otherwise
#: fires a write per pixel.
SLIDER_DEBOUNCE_MS = 150

REFRESH_MS = 1000
PING_MS = 5000

#: A speed slider is a 0-100 choice, so past a point more pixels buy no
#: precision - they just stretch one control across the whole window and pull
#: the reading away from the name it belongs to. Wide enough for one percent
#: per pixel, and no wider.
SLIDER_WIDTH = 260

#: How far a slider may be squeezed before the window refuses to get narrower.
#: Every card has the same layout and the same width, so they shrink in step
#: and never end up disagreeing about how long a speed slider is.
SLIDER_MIN_WIDTH = 140

#: The name is the one thing in a card that can be shortened without losing a
#: control, so it is what gives way when the window is dragged narrow.
NAME_MIN_WIDTH = ElidingLabel.MIN_WIDTH

#: Opening size. The width is a floor: _fit_to_names() widens the window to
#: whatever the longest fan name needs, and the screen caps both.
DEFAULT_WIDTH = 780
DEFAULT_HEIGHT = 1010

MIN_WIDTH = 560
MIN_HEIGHT = 420


class SpeedSlider(QSlider):
    """A slider that asks for SLIDER_WIDTH but settles for less.

    Hints rather than hard bounds, for the same reason as the name label: the
    window measures itself against sizeHint to decide how wide to open, and
    against minimumSizeHint to decide how narrow it may be dragged. A fixed
    width would have made those the same number.

    Every card has this same slider in the same column, so they shrink in step
    and never end up disagreeing about how long a speed slider is.
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        # Past this it is one control stretched across the window, buying no
        # precision on what is a 0-100 choice and pulling the reading away
        # from the name it belongs to.
        self.setMaximumWidth(SLIDER_WIDTH)

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(SLIDER_WIDTH)
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(SLIDER_MIN_WIDTH)
        return hint


class FanTile(Card):
    """One channel: what it is doing, and the controls to change it.

    A card rather than a row in a list: eight near-identical channels separated
    by a hairline are hard to tell apart at a glance, and the coloured bar down
    the left edge says which of them are no longer being regulated by the board
    without having to read every radio button.
    """

    def __init__(self, channel, panel, name, parent=None):
        super().__init__(parent)
        self.channel = channel
        # Not `self.window`: QWidget.window() is a real Qt method.
        self.panel = panel
        self._applying = False          # suppress signals during a programmatic set
        self._last_percent = 0
        self._limits = ""                # standing facts about the hardware
        self._warning = False            # a transient note worth colouring for
        self.manual_button = None        # read by _sync_accent before it exists

        grid = QGridLayout(self)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        title = QHBoxLayout()
        title.setContentsMargins(0, 0, 0, 0)
        title.setSpacing(4)

        # Super-I/O headers arrive as bare numbers, so renaming is how a fan
        # becomes identifiable at all. Ahead of the name rather than after it:
        # after, it sat at whatever column the longest name happened to end at
        # and the buttons never lined up with each other.
        self.rename_button = QPushButton()
        icon = QIcon.fromTheme("document-edit")
        if icon.isNull():
            self.rename_button.setText("✎")
        else:
            self.rename_button.setIcon(icon)
        self.rename_button.setFlat(True)
        self.rename_button.setToolTip(tool_tip(
            "fans", "Rename", "Give this fan a name you will recognise - "
            "a board's headers arrive as bare numbers."))
        self.rename_button.setFixedSize(22, 22)
        self.rename_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rename_button.clicked.connect(
            lambda: self.panel.rename_channel(self))
        title.addWidget(self.rename_button)

        # Shown whole, never shortened: the window works out how wide it has
        # to be for the longest of these and asks for that much. No trailing
        # stretch - one here would split the row's spare width with the label
        # and squeeze the name for no reason.
        self.name_label = ElidingLabel(name)
        font = QFont(self.name_label.font())
        font.setBold(True)
        self.name_label.setFont(font)
        self.name_label.setText(name)
        title.addWidget(self.name_label)
        grid.addLayout(title, 0, 0)

        # Two lines rather than one: the speed the fan is actually running at
        # is what the eye should land on, and the raw duty behind it is the
        # supporting detail - it is only ever consulted when a number looks
        # wrong. A header with no tachometer has no speed to lead with, so
        # there the duty moves up and the second line goes away rather than
        # leaving the card headed by a dash.
        readout = QVBoxLayout()
        readout.setContentsMargins(0, 0, 0, 0)
        readout.setSpacing(0)

        self.reading = QLabel("-")
        reading_font = QFont(self.reading.font())
        reading_font.setBold(True)
        reading_font.setPointSizeF(reading_font.pointSizeF() + 1)
        self.reading.setFont(reading_font)
        self.reading.setAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        readout.addWidget(self.reading)

        self.sub_reading = QLabel("")
        # setForegroundRole rather than an explicit palette colour: the role is
        # resolved against whatever palette is in force at paint time, so it
        # follows a light/dark switch, where a colour written into the widget's
        # own palette would stick.
        self.sub_reading.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.sub_reading.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
        self.sub_reading.hide()
        readout.addWidget(self.sub_reading)
        grid.addLayout(readout, 0, 1, 1, 2)

        # DC headers stall at a low duty where a PWM header would still spin,
        # so which one this is changes what a low setting will do. Shown for
        # that reason; pwmN_mode itself is never written.
        if channel.pwm_mode is False:
            self._readout_tooltip("This header drives the fan by voltage "
                                  "(DC). DC fans usually stop below roughly "
                                  "a third of full speed.")
        elif channel.pwm_mode is True:
            self._readout_tooltip("This header drives the fan by PWM.")

        modes = QHBoxLayout()
        modes.setContentsMargins(0, 0, 0, 0)
        self.auto_button = QRadioButton("Automatic")
        self.manual_button = QRadioButton("Manual")
        self.group = QButtonGroup(self)
        self.group.addButton(self.auto_button)
        self.group.addButton(self.manual_button)
        self.auto_button.setChecked(True)
        self.auto_button.setToolTip(tool_tip(
            "fans", "Automatic", "The board or card drives this fan by its "
            "own curve, as it does without KSysSupervisor."))
        self.manual_button.setToolTip(tool_tip(
            "fans", "Manual", "You set the speed with the slider; it holds "
            "there until you switch back or close the app."))
        modes.addWidget(self.auto_button)
        modes.addWidget(self.manual_button)
        modes.addStretch(1)
        grid.addLayout(modes, 1, 0)

        self.slider = SpeedSlider()
        self.slider.setRange(0, 100)
        self.slider.setEnabled(False)
        if not channel.coarse:
            # Said here rather than in the card text: it is a deliberate margin
            # of about one percent of fan speed, not a limitation of the
            # hardware, and the raw value beside the reading is the honest one.
            self.slider.setToolTip(tool_tip(
                "fans", "Fan speed",
                "In Manual, drag to set the duty. 100%% here is %d of %d, "
                "just short of full duty: the margin keeps this channel out "
                "of a range some drivers give a second meaning to, and costs "
                "roughly 1%% of fan speed."
                % (channel.duty_ceiling, channel.pwm_max)))
        else:
            self.slider.setToolTip(tool_tip(
                "fans", "Fan speed", "In Manual, drag to set the duty. This "
                "fan has only a few speed steps, so the slider snaps to "
                "them."))
        grid.addWidget(self.slider, 1, 1)

        self.percent_label = QLabel("-")
        self.percent_label.setMinimumWidth(64)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.percent_label, 1, 2)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        note_font = QFont(self.note.font())
        note_font.setItalic(True)
        self.note.setFont(note_font)
        self.note.hide()
        grid.addWidget(self.note, 2, 0, 1, 3)

        # Spare width goes to the name, not to the slider: the slider is a
        # fixed-size control that gains nothing from being longer, while the
        # name is the part that was being cut short.
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 0)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SLIDER_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply_slider)

        self.slider.valueChanged.connect(self._slider_moved)
        self.auto_button.toggled.connect(self._mode_changed)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

        self._describe_limits()
        self._sync_accent()

    def _readout_tooltip(self, text):
        text = tool_tip("fans", "Reading", text)
        self.reading.setToolTip(text)
        self.sub_reading.setToolTip(text)

    def _describe_limits(self):
        """Say up front when a channel cannot do what the controls imply.

        Kept apart from set_note(): these are properties of the hardware that
        stay true for the life of the window, so switching back to Automatic
        must not wipe them the way it wipes a low-speed warning.
        """
        channel = self.channel
        limits = []

        if channel.has_enable and not channel.enable_writable:
            self.auto_button.setEnabled(False)
            self.manual_button.setEnabled(False)
            limits.append("This driver publishes the fan mode read-only, so "
                          "the speed cannot be taken over here.")
        elif not channel.has_enable:
            # No mode attribute at all: the channel is always manual, so there
            # is nothing to switch and nothing to hand back. Saying so beats an
            # Automatic radio button that does nothing.
            self.auto_button.setEnabled(False)
            limits.append("This channel has no automatic mode - it is always "
                          "under direct control.")
        elif not channel.enable_readable:
            # dell-smm's global BIOS switch is write-only: the mode shown is
            # the last one set from here, and nothing else can be known.
            limits.append("This driver does not report the fan mode, so "
                          "Automatic and Manual show what was last set here, "
                          "not what the firmware is doing.")

        if channel.coarse:
            # A percentage over three steps would round two of them together.
            self.slider.setRange(channel.pwm_min, channel.pwm_max)
            limits.append("This driver offers %d speed steps rather than a "
                          "continuous range."
                          % (channel.pwm_max - channel.pwm_min + 1))
        elif channel.pwm_min > 0:
            # 0% on this card's slider is the driver's floor, not a stopped
            # fan, and a card that does not say so implies the fan can be
            # stopped from here when it cannot.
            limits.append("This driver enforces a minimum speed, so 0%% here "
                          "is its slowest setting rather than stopped.")

        if channel.fan_input is None:
            self._readout_tooltip("This header has no tachometer, so no "
                                  "speed is reported for it.")
            limits.append("No tachometer on this header - the speed it ends up "
                          "running at is not reported.")

        self._limits = " ".join(limits)
        if self._limits:
            self.set_note("")

    # ---- display --------------------------------------------------------

    def refresh(self, state):
        """Update from a fresh reading, without fighting the user's input."""
        rpm = "%d RPM" % state.rpm if state.rpm is not None else None
        duty = "PWM %d" % state.pwm if state.pwm is not None else None
        primary, secondary = (rpm, duty) if rpm else (duty, None)
        self.reading.setText(primary or "-")
        self.sub_reading.setText(secondary or "")
        self.sub_reading.setVisible(bool(secondary))

        # Whatever the driver says, whoever put it there. A fan deliberately
        # left pinned by an earlier session, or set manual by the firmware,
        # really is under manual control and the card has to show that - saying
        # "Automatic" over a fan that is not being regulated is the one thing
        # this window must never do. The claim needed to write to it is taken
        # when the user actually asks for something, not held pre-emptively.
        manual = state.manual
        # Only follow the hardware while the user is not driving this card: the
        # sysfs value lags a write by a tick, and snapping the slider back to
        # the old number mid-drag would be maddening.
        if not self._applying and not self.slider.isSliderDown() \
                and not self._debounce.isActive():
            self._applying = True
            # A channel with no pwmN_enable, or a write-only one, has no mode to
            # read back, so the only record of whether the user took it over is
            # this card itself. Following the unreadable None there would flip
            # the card to Automatic over a fan the user had just taken over.
            if self.channel.has_enable and self.channel.enable_readable \
                    and manual != self.manual_button.isChecked():
                (self.manual_button if manual else self.auto_button).setChecked(True)
                self.slider.setEnabled(manual)
            if self.channel.coarse:
                if state.pwm is not None:
                    self.slider.setValue(state.pwm)
                    self._last_percent = state.pwm
                    self.percent_label.setText(self._label_for(state.pwm))
            elif state.percent is not None:
                self.slider.setValue(state.percent)
                self._last_percent = state.percent
                self.percent_label.setText(self._label_for(state.percent))
            self._applying = False
            self._sync_accent()

    def _label_for(self, value):
        if self.channel.coarse:
            return "step %d/%d" % (value, self.channel.pwm_max)
        return "%d %%" % value

    def set_note(self, text, warning=False):
        """A transient message, shown alongside anything permanent about the
        hardware rather than in place of it."""
        self._warning = bool(text) and warning
        parts = []
        if text:
            parts.append(("⚠ " if warning else "") + text)
        if self._limits:
            parts.append(self._limits)
        self._sync_accent()
        if not parts:
            self.note.hide()
            return
        self.note.setText("\n".join(parts))
        self.note.show()

    def _sync_accent(self):
        """Colour the edge bar from the one thing worth seeing from a distance.

        Order matters: a fan that has been dropped to a speed it may not spin
        at is a more urgent fact about the card than the mode that got it
        there, so the warning wins over the manual highlight.
        """
        # getattr, not the attribute: Card.__init__ rewrites the palette on a
        # light theme, and that PaletteChange lands here before FanTile's own
        # __init__ has set anything at all.
        if getattr(self, "manual_button", None) is None:
            return                      # still being built
        if self._warning:
            self.set_accent(WARNING_ACCENT)
        elif self.manual_button.isChecked():
            self.set_accent(self.palette().color(QPalette.ColorRole.Highlight))
        else:
            self.set_accent(None)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange):
            # The manual accent is a palette colour, so it has to be taken
            # again rather than repainted: Card only knows the colour it was
            # handed, and that one has just been retired.
            self._sync_accent()

    def display_name(self):
        return self.name_label.text()

    def set_display_name(self, name):
        self.name_label.setText(name)

    def is_manual(self):
        return self.manual_button.isChecked()

    # ---- interaction ----------------------------------------------------

    def _context_menu(self, position):
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        rename = menu.addAction("Rename Fan...")
        rename.setToolTip(tool_tip("fans", "Rename",
                                   "Give this fan a name of your own."))
        reset = menu.addAction("Reset to Default Name")
        reset.setToolTip(tool_tip("fans", "Reset Name",
                                  "Go back to the driver's name for it."))
        action = menu.exec(self.mapToGlobal(position))
        if action is rename:
            self.panel.rename_channel(self)
        elif action is reset:
            self.panel.reset_channel_name(self)

    def mouseDoubleClickEvent(self, event):
        self.panel.rename_channel(self)
        event.accept()

    def _mode_changed(self, _checked):
        if self._applying:
            return
        if self.manual_button.isChecked():
            if not self.panel.begin_manual(self):
                # Authorisation failed or was dismissed; do not leave the UI
                # claiming a control the user does not actually have.
                self._show_mode(False)
                return
            self.slider.setEnabled(True)
            self._sync_accent()
            self._apply_slider()
        else:
            if not self.panel.restore_channel(self):
                # The fan is still under manual control - the prompt was
                # dismissed, or the driver would not take it back. Showing
                # Automatic here would be a plain lie about a fan that nothing
                # is regulating.
                self._show_mode(True)
                return
            self.slider.setEnabled(False)
            self.set_note("")

    def _show_mode(self, manual):
        """Move the radio without letting it look like the user did it."""
        self._applying = True
        (self.manual_button if manual else self.auto_button).setChecked(True)
        self.slider.setEnabled(manual)
        self._applying = False
        self._sync_accent()

    def _slider_moved(self, value):
        self.percent_label.setText(self._label_for(value))
        if self._applying or not self.manual_button.isChecked():
            return
        self._debounce.start()

    def _apply_slider(self):
        percent = self.slider.value()
        pwm = (percent if self.channel.coarse
               else self.channel.to_pwm(percent))

        # Only a channel whose floor really is off can be stopped; where the
        # driver enforces a minimum, the bottom of the slider is its slowest
        # setting and asking "stop this fan?" would be a false alarm.
        if percent == 0 and self._last_percent != 0 and self.channel.pwm_min == 0:
            if not self.panel.confirm_stop(self):
                self._applying = True
                self.slider.setValue(self._last_percent)
                self.percent_label.setText(self._label_for(self._last_percent))
                self._applying = False
                return

        if self.panel.apply_speed(self, pwm):
            self._last_percent = percent
            if pwm <= self.channel.pwm_min and self.channel.pwm_min == 0:
                self.set_note("Stopped. This fan is not moving any air.", True)
            elif not self.channel.coarse and percent < LOW_PERCENT:
                # DC headers stall well before PWM ones do, so the warning is
                # worth being blunter about there.
                if self.channel.pwm_mode is False:
                    self.set_note("Very low, and this is a DC header - the fan "
                                  "may stop completely and stop reporting a "
                                  "speed.", True)
                else:
                    self.set_note("Very low. Some fans stall below this and "
                                  "stop reporting a speed.", True)
            else:
                self.set_note("")


#: The standing guarantee about manual control. Folded to two lines, not
#: cut: what it says is what happens to the hardware. It starts open,
#: because the first time someone opens this window is exactly when it is
#: worth reading.
INTRO_HEADLINE = ("Manual turns off your board's fan regulation for that "
                  "channel.")
INTRO_SUMMARY = ("Everything is put back the way it was found when this "
                 "window closes.")
INTRO_DETAIL = ("%s restores every fan it touched when it exits - including "
                "if it crashes or is killed, since the kernel closing the "
                "pipe is the helper's cue to put things back. Fans stay where "
                "you left them only if you explicitly ask for that when "
                "closing." % APP_NAME)


class SectionHeader(QWidget):
    """A section's title and a rule running out to the right of it."""

    def __init__(self, title, count, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)

        self.title = QLabel(title)
        font = QFont(self.title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.1)
        self.title.setFont(font)
        row.addWidget(self.title)

        self.count = QLabel("%d fan%s" % (count, "" if count == 1 else "s"))
        # A role, not a colour: a colour written into the palette would stay
        # behind after a light/dark switch.
        self.count.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        row.addWidget(self.count)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Sunken)
        row.addWidget(rule, 1)


class FanControlWindow(QWidget):
    """Manual fan speeds, for as long as this window's session lasts."""

    def __init__(self, settings=None):
        # No parent on purpose: this makes it a real top-level window, with its
        # own task manager entry, that neither closes with nor stays above the
        # main window.
        super().__init__(None)
        self.setWindowTitle("%s - Fan Control" % APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        self.settings = settings or QSettings("KSysSupervisor", "Settings")
        self.client = fanctl.HelperClient()
        self.channels = fanctl.discover_channels()
        self.rows = {}
        self._closed = False   # shutdown() already ran; do not re-ask on close
        self._starting = False  # the polkit prompt is up; see _ensure_helper
        self._kept = set()     # channel keys the helper will not restore

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.intro = Callout(INTRO_HEADLINE, INTRO_SUMMARY, INTRO_DETAIL,
                             settings=self.settings,
                             setting="fan_intro_expanded",
                             tips=("Hide the details",
                                   "What happens to the fans"))
        layout.addWidget(self.intro)

        content = QWidget()
        inner = QVBoxLayout(content)
        # No separators between cards: each channel is its own card, and a
        # line between two only reads as a third thing between them. The
        # sections - GPU, CPU, board - get a heading instead. The margin keeps
        # the card borders off the scroll bar.
        inner.setSpacing(10)
        inner.setContentsMargins(2, 2, 2, 2)
        self._tile_layout = inner

        self._adopt_legacy_names()
        cards = fanctl.gpu_cards()
        self.section_headers = []
        for number, (title, channels) in enumerate(
                fanctl.sections(self.channels)):
            if number:
                inner.addSpacing(8)     # more air between sections than cards
            header = SectionHeader(title, len(channels))
            self.section_headers.append(header)
            inner.addWidget(header)
            for channel in channels:
                row = FanTile(channel, self, self._name_for(channel, cards))
                self.rows[channel.key] = row
                inner.addWidget(row)
        inner.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        make_transparent(scroll)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.restore_all_button = QPushButton("Restore All to Automatic")
        self.restore_all_button.clicked.connect(self.restore_all)
        self.restore_all_button.setToolTip(tool_tip(
            "fans", "Restore All to Automatic", "Hand every fan back to the "
            "board's or card's own control now."))
        buttons.addWidget(self.restore_all_button)

        self.keep_all_button = QPushButton("Keep Manual Settings")
        self.keep_all_button.setToolTip(tool_tip(
            "fans", "Keep Manual Settings",
            "Leave the fans you have set by hand where they are, so they "
            "stay after this window and %s close." % APP_NAME))
        self.keep_all_button.clicked.connect(self.keep_all)
        buttons.addWidget(self.keep_all_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        close.setToolTip(tool_tip(
            "fans", "Close", "Close this window. You are asked what to do "
            "with any fan still under manual control."))
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(REFRESH_MS)

        # Keeps the helper's deadman from firing while the window simply sits
        # there with a fan under manual control.
        self._ping_timer = QTimer(self)
        self._ping_timer.timeout.connect(self._ping)
        self._ping_timer.start(PING_MS)

        self._restore_geometry()
        self._refresh()

    # ---- backdrop -------------------------------------------------------

    def paintEvent(self, event):
        paint_backdrop(self)
        super().paintEvent(event)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)

    # ---- naming ---------------------------------------------------------

    def _adopt_legacy_names(self):
        """Move names saved under the old "chip:index" key to the new one.

        Copied to every channel the old key could have meant: two cards of
        one chip shared a single name under the old key, so that is what each
        of them was showing, and picking one would be a guess.
        """
        for channel in self.channels:
            if channel.key == channel.legacy_key:
                continue
            new = "fan_name_%s" % channel.key
            old = "fan_name_%s" % channel.legacy_key
            saved = self.settings.value(old, "", type=str)
            if saved and not self.settings.value(new, "", type=str):
                self.settings.setValue(new, saved)
        for legacy in {c.legacy_key for c in self.channels
                       if c.key != c.legacy_key}:
            self.settings.remove("fan_name_%s" % legacy)

    def _name_for(self, channel, cards=None):
        saved = self.settings.value("fan_name_%s" % channel.key, "", type=str)
        return saved or fanctl.default_name(channel, cards)

    def rename_channel(self, row):
        """Super-I/O fans arrive as 'fan1'..'fan7' with no labels at all, so
        naming them is the only way to know which header is which."""
        name, ok = QInputDialog.getText(self, "Rename Fan", "Name for this fan:",
                                        text=row.display_name())
        if ok and name.strip():
            row.set_display_name(name.strip())
            self.settings.setValue("fan_name_%s" % row.channel.key, name.strip())

    def reset_channel_name(self, row):
        self.settings.remove("fan_name_%s" % row.channel.key)
        row.set_display_name(fanctl.default_name(row.channel))

    # ---- helper plumbing ------------------------------------------------

    def begin_manual(self, row):
        """Make sure the helper is up and the channel is claimed.

        Returns False when the user dismissed the authentication prompt, which
        is a normal outcome rather than an error worth a dialog.
        """
        if not self._ensure_helper():
            return False
        try:
            self.client.claim(row.channel)
        except fanctl.HelperError as exc:
            self._error("Could not take control of %s" % row.display_name(), exc)
            return False
        return True

    def _ensure_helper(self):
        if self.client.running:
            return True

        reason = fanctl.unavailable_reason()
        if reason:
            QMessageBox.information(self, "Fan control unavailable", reason)
            return False

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.status.setText("Waiting for authorisation...")
        # The cards go inert while the prompt is up. The window keeps
        # painting, so a second card could otherwise be clicked, and its
        # claim would wait on the handshake's lock on this very thread.
        self._starting = True
        for row in self.rows.values():
            row.setEnabled(False)
        try:
            error = self._start_helper()
        finally:
            self._starting = False
            for row in self.rows.values():
                row.setEnabled(True)
            QGuiApplication.restoreOverrideCursor()
        if error is not None:
            log.info("Fan control helper did not start: %s", error)
            self.status.setText("Not authorised - fans are still under "
                                "automatic control.")
            return False

        self.status.setText("Fan control active. Fans return to automatic when "
                            "this window or KSysSupervisor closes.")
        return True

    def _start_helper(self):
        """Run HelperClient.start() off this thread. Returns its error or None.

        start() blocks until the polkit prompt is answered, for up to two
        minutes; see run_blocking() for why that cannot happen here.
        """
        _result, error = run_blocking(self.client.start)
        if error is None or isinstance(error, fanctl.HelperError):
            return error
        log.error("Starting the fan control helper failed: %r", error)
        return fanctl.HelperError(str(error))

    def holds(self, row_channel):
        """Whether the helper has this channel claimed for this session."""
        return self.client.holds(row_channel)

    def apply_speed(self, row, pwm):
        # A row can be showing Manual without this session holding it - a fan
        # left pinned by an earlier run is adopted on sight - so the claim is
        # taken here, the first time the user actually asks for a speed.
        if not self.claim_if_needed(row):
            return False
        try:
            self.client.set_pwm(row.channel, pwm)
            return True
        except fanctl.HelperError as exc:
            # A driver that accepts the write and then ignores it lands here,
            # which is exactly the case that must not look like success.
            row.set_note("Could not set this speed: %s" % exc, True)
            log.warning("Setting %s to pwm %d failed: %s",
                        row.channel.key, pwm, exc)
            return False

    def confirm_stop(self, row):
        """A fan at 0% is a fan that has stopped, so say so before doing it."""
        box = QMessageBox(self)
        box.setWindowTitle("Stop this fan?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Setting %s to 0%% will stop it completely."
                    % row.display_name())
        box.setInformativeText(
            "Nothing will be cooled by this fan until you raise it again. "
            "Continue?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setEscapeButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def restore_channel(self, row):
        """Hand one channel back to the board. False means it is still manual.

        Claims the channel first when this session does not already hold it,
        which is the case every time the window is reopened onto fans a
        previous run deliberately left pinned: changing the mode needs root
        whether we are taking the fan over or giving it back.
        """
        if not self.claim_if_needed(row):
            return False
        try:
            self.client.set_auto(row.channel)
        except fanctl.HelperError as exc:
            self._error("Could not restore %s" % row.display_name(), exc)
            return False
        return True

    def claim_if_needed(self, row):
        """Make sure the helper is up and holds this channel."""
        if self.client.holds(row.channel):
            return True
        return self.begin_manual(row)

    def restore_all(self):
        """Put every manual channel back. False if any of them stayed manual."""
        for row in self.rows.values():
            if row.is_manual():
                row.auto_button.setChecked(True)

        # Read back from the rows rather than trusting the loop: each switch
        # goes through the helper and can be refused, and reporting success
        # over a fan that is still pinned would be the worst kind of wrong.
        stuck = [row.display_name() for row in self.rows.values()
                 if row.is_manual()]
        if stuck:
            self.status.setText(
                "Still under manual control: %s." % ", ".join(stuck))
            return False
        self.status.setText("All fans are back under automatic control.")
        return True

    def _ping(self):
        # Not while starting: the handshake holds the client's lock, and a
        # ping would wait on it here, on the thread painting the window.
        if self.client.running and not self._starting:
            try:
                self.client.ping()
            except fanctl.HelperError:
                pass

    def _refresh(self):
        for key, row in self.rows.items():
            row.refresh(fanctl.read_state(row.channel))
        self._update_keep_button()

    def _error(self, title, exc):
        log.warning("%s: %s", title, exc)
        QMessageBox.warning(self, title, str(exc))

    # ---- shutdown -------------------------------------------------------

    def is_finished(self):
        """True once this window has released the helper and needs replacing."""
        return self._closed

    def manual_rows(self):
        """Manual channels the helper would still put back on the way out.

        Ones already kept are left out: they have been settled deliberately,
        and asking about them again on every close would train the answer out
        of meaning anything.
        """
        return [row for row in self.rows.values()
                if row.is_manual() and row.channel.key not in self._kept]

    def keepable_rows(self):
        """Manual channels that are not already permanent."""
        return self.manual_rows()

    def _update_keep_button(self):
        """Nothing to make permanent unless a fan is actually set by hand.

        Three states rather than two: a button greyed out because everything is
        already permanent means something quite different from one greyed out
        because nothing is set, and the tooltip is the only place that can say
        which.
        """
        keepable = bool(self.keepable_rows())
        self.keep_all_button.setEnabled(keepable)
        if keepable:
            tip = ("Leave the fans you have set by hand where they are, so "
                   "they stay after this window and %s close." % APP_NAME)
        elif any(row.is_manual() for row in self.rows.values()):
            tip = ("These manual settings are already set to outlast %s."
                   % APP_NAME)
        else:
            tip = "No fan is under manual control. Set one to Manual first."
        self.keep_all_button.setToolTip(tool_tip(
            "fans", "Keep Manual Settings", tip))

    def confirm_shutdown(self, parent=None):
        """Decide what happens to manually-controlled fans. False cancels.

        Asked rather than assumed, because leaving a fan pinned is a legitimate
        thing to want and silently undoing it would be its own surprise - but it
        takes two deliberate answers, since the consequence outlives the app.
        """
        if self._closed:
            return True                 # already settled when this window closed
        if self._starting:
            # The prompt is still up and the helper half started; tearing it
            # down now would block on the handshake. Answer the prompt first.
            self.status.setText("Waiting for authorisation - answer or cancel "
                                "the password prompt before closing.")
            return False
        manual = self.manual_rows()
        if not manual:
            return True

        names = "\n".join("  - %s" % row.display_name() for row in manual)
        box = QMessageBox(parent or self)
        box.setWindowTitle("Fans under manual control")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("%d fan(s) are under manual control:\n\n%s" % (len(manual), names))
        box.setInformativeText(
            "Restore them to automatic control before closing?")
        restore = box.addButton("Restore Automatic",
                                QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Keep Manual Settings",
                             QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(restore)
        box.setEscapeButton(cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is cancel:
            return False
        if clicked is keep:
            return self._confirm_keep(manual, parent)

        if self.restore_all():
            return True

        # Asked to restore, and it did not happen - the prompt was dismissed,
        # or a driver refused. Closing now would leave fans pinned after the
        # user explicitly asked for the opposite, so the choice comes back.
        again = QMessageBox(parent or self)
        again.setWindowTitle("Some fans are still under manual control")
        again.setIcon(QMessageBox.Icon.Warning)
        again.setText("Not every fan could be handed back to the board.")
        again.setInformativeText(
            "They are still at a fixed speed with no thermal regulation. "
            "Try again, or close anyway and leave them as they are.")
        retry = again.addButton("Try Again", QMessageBox.ButtonRole.AcceptRole)
        anyway = again.addButton("Close Anyway",
                                 QMessageBox.ButtonRole.DestructiveRole)
        again.setDefaultButton(retry)
        again.exec()
        if again.clickedButton() is anyway:
            return True
        return self.confirm_shutdown(parent)

    def _keep_warning(self, manual, parent=None):
        """The consequence, spelled out. True if the user accepted it.

        Shared by the two ways of getting here - the button, and choosing to
        keep the settings on the way out - because the consequence is the same
        either way and it is the one thing in this window that outlives it.
        """
        confirm = QMessageBox(parent or self)
        confirm.setWindowTitle("Leave fans under manual control?")
        confirm.setIcon(QMessageBox.Icon.Critical)
        confirm.setText("These fans will stay locked at their current speed "
                        "with no thermal regulation.")
        confirm.setInformativeText(
            "Your board will no longer adjust them. <b>If the system gets hot, "
            "these fans will not speed up.</b><br><br>"
            "The setting lasts until you reboot or change it back - closing "
            "%s will not undo it, and neither will anything else.<br><br>"
            "Leave %d fan(s) under manual control?" % (APP_NAME, len(manual)))
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.No)
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        confirm.setEscapeButton(QMessageBox.StandardButton.No)
        return confirm.exec() == QMessageBox.StandardButton.Yes

    def _apply_keep(self, manual, parent=None):
        """Exempt these channels from the restore. Returns the ones that stuck.

        A channel the helper would not exempt is reported rather than counted:
        it goes back to automatic when the helper exits, which is the opposite
        of what was just asked for.
        """
        kept, failed = [], []
        for row in manual:
            try:
                self.client.keep(row.channel)
                kept.append(row)
                self._kept.add(row.channel.key)
            except fanctl.HelperError as exc:
                failed.append("%s (%s)" % (row.display_name(), exc))
        if failed:
            QMessageBox.warning(
                parent or self, "Could not keep every setting",
                "These fans will go back to automatic control after all:\n\n"
                + "\n".join("  - %s" % item for item in failed))
        if kept:
            # Worth a log line: this outlives the application, so if the
            # machine is found later with a fan pinned, the reason is recorded.
            log.warning("User chose to leave %d fan(s) under manual control: %s",
                        len(kept), ", ".join(row.display_name() for row in kept))
        return kept

    def keep_all(self):
        """Make the current manual settings outlast the application.

        The same question the close prompt asks, reachable without closing:
        setting a fan and then having to quit to make it stick is a strange
        way to have to do it.
        """
        manual = self.keepable_rows()
        if not manual:
            self.status.setText("No fan is under manual control, so there is "
                                "nothing to keep.")
            return False
        if not self._keep_warning(manual):
            return False

        kept = self._apply_keep(manual)
        if kept:
            self.status.setText(
                "%d fan(s) will stay where they are after %s closes: %s."
                % (len(kept), APP_NAME,
                   ", ".join(row.display_name() for row in kept)))
        self._update_keep_button()
        return bool(kept)

    def _confirm_keep(self, manual, parent=None):
        if not self._keep_warning(manual, parent):
            # Back out to the first question rather than guessing.
            return self.confirm_shutdown(parent)
        self._apply_keep(manual, parent)
        return True

    def shutdown(self):
        """Stop talking to the helper. Anything not kept is restored by it."""
        self._closed = True
        self._refresh_timer.stop()
        self._ping_timer.stop()
        self.client.stop()

    def closeEvent(self, event):
        # The main window asks the question itself and then closes this one, so
        # only ask when we are being closed on our own account.
        if not self._closed and not self.confirm_shutdown():
            event.ignore()
            return
        self._save_geometry()
        self.shutdown()
        super().closeEvent(event)

    # ---- geometry -------------------------------------------------------

    def _screen_cap(self):
        """How large this window is allowed to get. Never off the display."""
        return screen_limits(self)

    def _chrome_width(self):
        """Everything between a card's edge and the window's.

        The layout margins on both sides, and a vertical scroll bar - which
        this window always ends up with.
        """
        outer = self.layout().contentsMargins()
        inner = self._tile_layout.contentsMargins()
        bar = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        return (inner.left() + inner.right()
                + outer.left() + outer.right() + bar + 4)

    def _natural_width(self):
        """The width at which no fan name has to be shortened.

        Fan names are as long as the card they came from - "Navi 21 [Radeon RX
        6800/6800 XT / 6900 XT] - GPU Fan 1" - so rather than picking a width
        and cutting them to it, the opening width is worked out from them.
        """
        if not self.rows:
            return 0
        widest = max(row.sizeHint().width() for row in self.rows.values())
        return widest + self._chrome_width()

    def _floor_width(self):
        """The narrowest this window can get before controls start colliding.

        Driven by the cards' own minimums - the two radio buttons, a squeezed
        slider and the percentage - since those cannot give way. The name can,
        and does: it elides from here.
        """
        if not self.rows:
            return MIN_WIDTH
        needed = max(row.minimumSizeHint().width() for row in self.rows.values())
        return needed + self._chrome_width()

    def _apply_minimum_width(self):
        """Let the window be dragged down to what the controls actually need."""
        floor = self._floor_width()
        cap = self._screen_cap()
        if cap is not None:
            floor = min(floor, cap[0])
        self.setMinimumWidth(floor)

    def _fit_to_names(self):
        """Open wide enough to show every name whole.

        A starting size, not a floor. The name is the one thing in a card that
        can be shortened without losing a control, so the window stays
        shrinkable past this point and the names elide instead.
        """
        wanted = self._natural_width()
        if not wanted:
            return
        cap = self._screen_cap()
        if cap is not None:
            wanted = min(wanted, cap[0])
        if self.width() < wanted:
            self.resize(wanted, self.height())

    def _restore_geometry(self):
        self._apply_minimum_width()

        saved = self.settings.value("fan_window_geometry")
        if saved is not None and hasattr(saved, "isEmpty") and not saved.isEmpty():
            # A size the user chose, including a narrow one. Only the floor
            # applies here - widening it back would undo the choice.
            self.restoreGeometry(saved)
            return

        # Tall by default: eight channels is an ordinary desktop, and a window
        # that shows two of them at a time has to be resized before it is any
        # use. Width is a floor, not a target - _fit_to_names widens it to
        # whatever the longest name needs. Both clamped, so a laptop screen
        # still gets a window that fits.
        width, height = DEFAULT_WIDTH, DEFAULT_HEIGHT
        cap = self._screen_cap()
        if cap is not None:
            width, height = min(width, cap[0]), min(height, cap[1])
        self.resize(width, height)
        self._fit_to_names()

    def _save_geometry(self):
        try:
            self.settings.setValue("fan_window_geometry", self.saveGeometry())
        except Exception:
            log.exception("Could not save the fan window geometry")
