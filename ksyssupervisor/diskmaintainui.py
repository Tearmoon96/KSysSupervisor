"""The disk utility's maintenance tools: their menu, dialogs and flows.

TRIM, fragmentation, free space, checking and the drive's self-test. What
each one means and whether it applies is decided in diskmaintain.py; this is
the part that asks, shows and hands the request to the window to run.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QLabel, QPlainTextEdit, QPushButton,
                             QRadioButton, QStyle, QVBoxLayout)

from . import diskdiag, diskfs, diskmaintain, diskops, disks, udisks
from .diskdialogs import (AdviceCard, ConfirmDialog, _FixedWidthDialog,
                          _wrapped)
from .diskwidgets import tool_colour
from .widgets import Chevron, Menu, fixed_font, tip


def _heading(text):
    label = _wrapped(text)
    font = QFont(label.font())
    font.setBold(True)
    font.setPointSizeF(font.pointSizeF() * 1.1)
    label.setFont(font)
    return label


class ActionDialog(_FixedWidthDialog):
    """Says what an operation will do and asks whether to go ahead.

    For operations that destroy nothing: every piece of advice is shown, the
    warnings as well as the notes. `tick`, when given, is a statement the
    user has to agree to before the button works - for the few things that
    are allowed but advised against. Cancel is the default whenever there
    is a warning to read.
    """

    def __init__(self, plan, parent=None, heading=None, accept=None,
                 tick="", extra=None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle(plan.title)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading(heading or "%s: %s" % (plan.title,
                                                         plan.target)))
        if extra is not None:
            layout.addLayout(extra)
        if plan.steps:
            layout.addWidget(_wrapped("What will happen:"))
            layout.addWidget(ConfirmDialog._listing(plan.steps, numbers=True))
        self.cards = [AdviceCard(advice) for advice in plan.advice]
        for card in self.cards:
            layout.addWidget(card)

        self.understood = QCheckBox(tick)
        self.understood.setVisible(bool(tick))
        layout.addWidget(self.understood)

        buttons = QDialogButtonBox()
        self.go = QPushButton(accept or plan.title)
        buttons.addButton(self.go, QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        warned = any(a.level == diskfs.WARNING for a in plan.advice)
        self.cancel.setDefault(warned)
        self.go.setDefault(not warned)
        self.go.setAutoDefault(not warned)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.understood.toggled.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args):
        self.go.setEnabled(self.understood.isHidden()
                           or self.understood.isChecked())


class WipeDialog(_FixedWidthDialog):
    """Choose what free space is overwritten with."""

    def __init__(self, disk, volume, parent=None, identity=None):
        super().__init__(parent)
        self.disk, self.volume, self.identity = disk, volume, identity
        self._plan = None
        self.setWindowTitle("Wipe free space")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Wipe the free space of %s"
                                  % diskops.describe_target(disk, volume)))
        free = (" - %s at the moment" % disks.format_size(volume.free)
                if volume.free else "")
        layout.addWidget(_wrapped(
            "Overwrites everything that is not in use%s, so that files "
            "deleted earlier cannot be brought back. The files on the "
            "volume are not touched, and it stays usable while this runs."
            % free, dim=True))

        self.patterns = {}
        for key, title, text in diskmaintain.PATTERNS:
            radio = QRadioButton(title)
            radio.setToolTip(text)
            self.patterns[key] = radio
            layout.addWidget(radio)
        self.patterns["zero"].setChecked(True)

        self.cards = [AdviceCard(advice) for advice
                      in diskmaintain.wipe_advice(disk, volume)]
        for card in self.cards:
            layout.addWidget(card)

        buttons = QDialogButtonBox()
        self.go = QPushButton("Wipe Free Space")
        buttons.addButton(self.go, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        self.go.setAutoDefault(False)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def pattern(self):
        for key, radio in self.patterns.items():
            if radio.isChecked():
                return key
        return "zero"

    def build_plan(self):
        return diskmaintain.plan_wipe(self.disk, self.volume, self.pattern(),
                                      identity=self.identity)

    def _accept(self):
        self._plan = self.build_plan()
        self.accept()

    def plan(self):
        return self._plan


class FragmentationDialog(_FixedWidthDialog):
    """What a measurement found, and whether to defragment.

    Close is the default: on most drives the right answer is to do nothing.
    A drive that defragmenting would only wear wants a tick as well.
    `result` is None where the filesystem has no tool that measures.
    """

    TICK = ("I understand that this wears the drive and that nothing is "
            "gained by it")

    def __init__(self, disk, volume, result=None, parent=None, identity=None):
        super().__init__(parent)
        self.plan_ = diskmaintain.plan_defrag(disk, volume, identity=identity)
        self.setWindowTitle("Fragmentation")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading(diskops.describe_target(disk, volume)))
        self.verdict = None
        if result is not None:
            self.verdict = diskmaintain.fragmentation_advice(result, disk)
            layout.addWidget(AdviceCard(self.verdict))
        else:
            layout.addWidget(_wrapped(
                "This filesystem has no tool that measures fragmentation, "
                "so there is nothing to go by. Defragmenting is rarely "
                "needed on Linux.", dim=True))
        self.warnings = diskmaintain.defrag_warnings(disk, volume)
        for advice in self.warnings:
            layout.addWidget(AdviceCard(advice))

        self.understood = QCheckBox(self.TICK)
        self.understood.setVisible(disk.is_solid_state)
        layout.addWidget(self.understood)

        buttons = QDialogButtonBox()
        self.go = QPushButton("Defragment")
        buttons.addButton(self.go, QDialogButtonBox.ButtonRole.AcceptRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.setDefault(True)
        self.go.setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.understood.toggled.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args):
        self.go.setEnabled(self.understood.isHidden()
                           or self.understood.isChecked())

    def plan(self):
        return self.plan_


class CheckResultDialog(_FixedWidthDialog):
    """A check's verdict in words, the tool's own output behind a toggle,
    and the offer to repair when there is something to repair."""

    def __init__(self, result, target="", parent=None):
        super().__init__(parent)
        self.result = result
        title, text, self.repairable = diskmaintain.check_summary(result)
        self.setWindowTitle("Check for errors")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)
        good = result.get("state") in ("clean", "repaired")
        icon = QIcon.fromTheme("dialog-ok" if good else "dialog-warning")
        if icon.isNull():
            icon = self.style().standardIcon(
                QStyle.StandardPixmap.SP_DialogApplyButton if good
                else QStyle.StandardPixmap.SP_MessageBoxWarning)
        badge = QLabel()
        badge.setPixmap(icon.pixmap(40, 40))
        top.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(4)
        words.addWidget(_heading(title))
        if target:
            words.addWidget(_wrapped(target, dim=True))
        top.addLayout(words, 1)
        layout.addLayout(top)
        layout.addWidget(_wrapped(text))

        row = QHBoxLayout()
        row.setSpacing(4)
        self.toggle = Chevron(tips=("Hide", "Show what the tool reported"))
        self.toggle.clicked.connect(self._toggle_output)
        row.addWidget(self.toggle)
        row.addWidget(QLabel("Details from the checking tool"))
        row.addStretch(1)
        layout.addLayout(row)
        self.output = QPlainTextEdit(result.get("output", ""))
        self.output.setReadOnly(True)
        self.output.setFont(fixed_font())
        self.output.setFixedHeight(200)
        self.output.setVisible(False)
        layout.addWidget(self.output)

        buttons = QDialogButtonBox()
        self.repair = None
        if self.repairable:
            self.repair = QPushButton("Repair...")
            buttons.addButton(self.repair,
                              QDialogButtonBox.ButtonRole.AcceptRole)
            self.repair.setAutoDefault(False)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_output(self):
        shown = not self.output.isVisible()
        self.output.setVisible(shown)
        self.toggle.set_expanded(shown)
        self.fit()


class DriveLogDialog(_FixedWidthDialog):
    """What the kernel has said about a drive, explained, with the lines
    themselves underneath for whoever wants them."""

    def __init__(self, disk, lines, parent=None):
        super().__init__(parent)
        self.setWindowTitle("What the system reports")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading(disk.title))
        self.findings = diskdiag.summary(lines, disk)
        for advice in self.findings:
            layout.addWidget(AdviceCard(advice))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.toggle = Chevron(tips=("Hide", "Show the messages"))
        self.toggle.clicked.connect(self._toggle_output)
        row.addWidget(self.toggle)
        row.addWidget(QLabel("The system's messages (%d)" % len(lines)))
        row.addStretch(1)
        layout.addLayout(row)
        self.output = QPlainTextEdit("\n".join(lines))
        self.output.setReadOnly(True)
        self.output.setFont(fixed_font())
        self.output.setFixedHeight(220)
        self.output.setVisible(False)
        layout.addWidget(self.output)
        self.toggle.setEnabled(bool(lines))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_output(self):
        shown = not self.output.isVisible()
        self.output.setVisible(shown)
        self.toggle.set_expanded(shown)
        self.fit()


class MaintainActions:
    """The Maintain menu of the disk utility window, and what its entries do.

    Mixed into the window: uses its selection, its helper plumbing
    (`run_plan`) and its hooks for dialogs, so the tests can drive every
    flow without a modal box.
    """

    #: (attribute, title, action or None, what it does)
    MAINTAIN_ENTRIES = (
        ("optimise_action", "Optimise", None,
         "The right upkeep for this kind of drive: TRIM on a solid-state "
         "drive, a look at fragmentation on a hard disk."),
        None,
        ("trim_action", "Trim Now", diskops.TRIM,
         "Tell the drive which blocks are unused. It keeps a solid-state "
         "drive fast and takes seconds. Nothing on the volume changes."),
        ("timer_action", "Weekly TRIM for All Drives", None,
         "The system's own weekly TRIM of every mounted drive that takes "
         "it. One setting for the whole computer."),
        None,
        ("analyse_action", "Analyse Fragmentation...", diskops.ANALYSE,
         "Measure how fragmented the files are, then say whether "
         "defragmenting is worth it. Changes nothing."),
        ("defrag_action", "Defragment...", diskops.DEFRAG,
         "Rewrite fragmented files in one piece. Only worth it on a hard "
         "disk, and seldom needed there."),
        None,
        ("check_action", "Check for Errors...", diskops.CHECK,
         "Check the filesystem for damage without changing anything, and "
         "offer to repair what is found."),
        ("wipe_action", "Wipe Free Space...", diskops.WIPE_FREE,
         "Overwrite everything that is not in use, so deleted files cannot "
         "be brought back. Files on the volume are not touched."),
        ("leftover_action", "Remove Leftover Wipe Files", None,
         "An earlier wipe of the free space was interrupted and its files "
         "are still taking up room."),
        None,
        ("short_test_action", "Short Self-Test (a few minutes)", None,
         "Have the drive test itself. The drive stays usable; the result "
         "appears under Details."),
        ("long_test_action", "Extended Self-Test (can take hours)", None,
         "The drive reads its whole surface. It stays usable but slower; "
         "the result appears under Details."),
        ("stop_test_action", "Stop the Self-Test", None,
         "End the self-test that is running."),
        None,
        ("log_action", "What the System Reports About This Drive...", None,
         "The errors the system has logged about this drive since it "
         "started, put into plain words: a failing drive and a bad cable "
         "look different there."),
    )

    def _build_maintain(self):
        self.trim_timer = None           # whether the weekly TRIM is on
        self.maintain_button = self._button(
            "Maintain", ("tools", "configure"), lambda: None)
        menu = Menu(self.maintain_button)
        menu.setToolTipsVisible(True)
        slots = {
            "optimise_action": self.optimise_selected,
            "trim_action": self.trim_selected,
            "analyse_action": self.analyse_selected,
            "defrag_action": self.defrag_selected,
            "check_action": self.check_selected,
            "wipe_action": self.wipe_selected,
            "leftover_action": self.remove_leftovers,
            "log_action": self.show_drive_log,
            "short_test_action": lambda: self.start_selftest("short"),
            "long_test_action": lambda: self.start_selftest("extended"),
            "stop_test_action": self.stop_selftest,
        }
        for entry in self.MAINTAIN_ENTRIES:
            if entry is None:
                menu.addSeparator()
                continue
            name, title, _action, _text = entry
            action = menu.addAction(title)
            setattr(self, name, action)
            if name == "timer_action":
                action.setCheckable(True)
                action.triggered.connect(self.toggle_trim_timer)
            else:
                action.triggered.connect(
                    lambda _checked=False, slot=slots[name]: slot())
        self.maintain_button.setMenu(menu)
        return self.maintain_button

    # ---- state --------------------------------------------------------------

    def _selftest_answer(self, disk):
        health = self.health.get(disk.name) if disk else None
        if health is None or not health.drive_path or not health.interface:
            return diskops.no(
                "This drive does not offer a self-test. Memory sticks, "
                "card readers and some USB enclosures do not.")
        return diskops.YES

    def _update_maintain(self, disk, volume, locked):
        """Enable each entry or grey it out with the reason."""
        colour = tool_colour()
        for entry in self.MAINTAIN_ENTRIES:
            if entry is None:
                continue
            name, title, action, text = entry
            item = getattr(self, name)
            if action is not None:
                answer = diskops.availability(action, disk, volume)
            elif name == "optimise_action":
                _what, answer = diskmaintain.optimise(disk, volume)
            elif name == "timer_action":
                answer = diskops.YES if self.trim_timer is not None else \
                    diskops.no("This system has no weekly TRIM timer "
                               "(fstrim.timer) to switch.")
                item.blockSignals(True)
                item.setChecked(bool(self.trim_timer))
                item.blockSignals(False)
            elif name == "log_action":
                answer = diskops.YES
            elif name == "leftover_action":
                found = bool(volume is not None
                             and diskmaintain.leftovers(volume))
                item.setVisible(found)
                answer = diskops.YES
            else:
                answer = self._selftest_answer(disk)
                health = self.health.get(disk.name)
                running = bool(health and health.selftest_remaining
                               is not None)
                if answer and name == "stop_test_action" and not running:
                    answer = diskops.no("No self-test is running.")
                elif answer and name != "stop_test_action" and running:
                    answer = diskops.no("A self-test is already running.")
            item.setEnabled(bool(answer) and not locked)
            title = title.rstrip(".")
            if answer:
                item.setToolTip(tip(title, text, colour))
            else:
                item.setToolTip(tip("%s is not available" % title,
                                    answer.reason, colour))
        self.maintain_button.setEnabled(disk.has_media)

    # ---- flows --------------------------------------------------------------

    def show_dialog(self, dialog):
        """Run a dialog; True when it was accepted. Its own method so the
        tests can answer in its place."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _target(self, action):
        """(disk, volume) when `action` can be done to the selection now."""
        disk, volume = self.disk(), self.volume()
        if disk is None or self._job is not None:
            return None, None
        if action is not None and not diskops.availability(action, disk,
                                                           volume):
            return None, None
        return disk, volume

    def _identity(self, volume):
        return self.identities.get(volume.name)

    def optimise_selected(self):
        disk, volume = self._target(None)
        if disk is None:
            return
        action, _answer = diskmaintain.optimise(disk, volume)
        if action == diskops.TRIM:
            self.trim_selected()
        elif action == diskops.ANALYSE:
            self.analyse_selected()

    def trim_selected(self):
        disk, volume = self._target(diskops.TRIM)
        if disk is None:
            return
        self.run_plan(
            diskmaintain.plan_trim(disk, volume, self._identity(volume)),
            done=lambda result: self.status.setText(
                diskmaintain.trim_done_text(result)))

    def toggle_trim_timer(self, wanted):
        if self._job is not None:
            return

        def done(result):
            self.trim_timer = bool(result.get("enabled"))
            self.status.setText(
                "The weekly TRIM is now %s for every drive."
                % ("on" if self.trim_timer else "off"))

        self.run_plan(diskmaintain.plan_timer(bool(wanted)), done=done)
        # Until the helper has answered, the tick shows what is, not what
        # was asked for.
        self._update_actions()

    def analyse_selected(self):
        disk, volume = self._target(diskops.ANALYSE)
        if disk is None:
            return
        self.run_plan(
            diskmaintain.plan_analyse(disk, volume, self._identity(volume)),
            done=lambda result: self._offer_defrag(disk, volume, result))

    def defrag_selected(self):
        disk, volume = self._target(diskops.DEFRAG)
        if disk is None:
            return
        if diskops.availability(diskops.ANALYSE, disk, volume):
            # Never without measuring first where measuring is possible.
            self.analyse_selected()
        else:
            self._offer_defrag(disk, volume, None)

    def _offer_defrag(self, disk, volume, result):
        dialog = FragmentationDialog(disk, volume, result, self,
                                     identity=self._identity(volume))
        if self.show_dialog(dialog):
            self.run_plan(dialog.plan(), done=lambda done: self.status.setText(
                diskmaintain.defrag_done_text(done)))

    def wipe_selected(self):
        disk, volume = self._target(diskops.WIPE_FREE)
        if disk is None:
            return
        dialog = WipeDialog(disk, volume, self,
                            identity=self._identity(volume))
        if self.show_dialog(dialog) and dialog.plan() is not None:
            self.run_plan(dialog.plan(), done=lambda result:
                          self.status.setText(
                              diskmaintain.wipe_done_text(result)))

    def remove_leftovers(self):
        disk, volume = self._target(None)
        if volume is None:
            return
        self.run_plan(
            diskmaintain.plan_wipe_cleanup(disk, volume,
                                           self._identity(volume)),
            done=lambda _result: self.status.setText(
                "The leftover files were removed."))

    def check_selected(self, repair=False):
        disk, volume = self._target(diskops.CHECK)
        if disk is None:
            return
        plan = diskmaintain.plan_check(disk, volume, repair=repair,
                                       identity=self._identity(volume))
        if repair or volume.mounted:
            heading = ("Repair %s?" if repair else "Check %s?") % plan.target
            if not self.show_dialog(ActionDialog(
                    plan, self, heading=heading,
                    accept="Repair" if repair else "Unmount and Check")):
                return
        self.run_plan(plan, done=lambda result: self._show_check(
            disk, volume, result))

    def _show_check(self, disk, volume, result):
        state = result.get("state")
        if result.get("was_mounted") and state in ("clean", "repaired"):
            # Put back where it was - but not a filesystem known to be
            # damaged: using it is how damage spreads.
            self._mount_after(result.get("device") or volume.path)
        self.status.setText("%s: %s." % (
            volume.title, diskmaintain.CHECK_TITLES.get(state, "checked")
            .lower()))
        dialog = CheckResultDialog(
            result, diskops.describe_target(disk, volume), self)
        if self.show_dialog(dialog) and dialog.repairable:
            plan = diskmaintain.plan_check(disk, volume, repair=True,
                                           identity=self._identity(volume))
            # The volume is unmounted by now, whatever the plan drawn from
            # the earlier listing says.
            plan.steps = [s for s, key in zip(plan.steps, plan.step_keys)
                          if key != "unmount"]
            plan.step_keys = [k for k in plan.step_keys if k != "unmount"]
            if self.show_dialog(ActionDialog(
                    plan, self, heading="Repair %s?" % plan.target,
                    accept="Repair")):
                was_mounted = result.get("was_mounted")

                def done(after):
                    after = dict(after, was_mounted=was_mounted)
                    self._show_check(disk, volume, after)
                self.run_plan(plan, done=done)

    # ---- a drive in trouble --------------------------------------------------------

    def show_drive_log(self):
        disk = self.disk()
        if disk is None or self._job is not None:
            return

        def done(result):
            lines = list(result.get("lines") or ())
            self.show_dialog(DriveLogDialog(disk, lines, self))
        self.run_plan(diskdiag.plan_log(disk), done=done)

    def rescan(self):
        if self._job is not None:
            return

        def done(_result):
            self.refresh_everything()
            self.status.setText("Looked for drives again. Anything found "
                                "appears in the list.")
        self.run_plan(diskdiag.plan_rescan(), done=done)

    # ---- the drive's own self-test ----------------------------------------------

    def start_selftest(self, kind):
        disk = self.disk()
        if disk is None or not self._selftest_answer(disk):
            return
        health = self.health[disk.name]

        def done(_result):
            self.refresh_health(refresh=True)
            return ("The %s self-test of %s has started. It runs inside "
                    "the drive; the result appears under Details."
                    % (kind, disk.title))
        self._simple(lambda: udisks.selftest_start(
            health.drive_path, health.interface, kind), done)

    def stop_selftest(self):
        disk = self.disk()
        if disk is None or not self._selftest_answer(disk):
            return
        health = self.health[disk.name]

        def done(_result):
            self.refresh_health(refresh=True)
            return "The self-test of %s was stopped." % disk.title
        self._simple(lambda: udisks.selftest_abort(
            health.drive_path, health.interface), done)

