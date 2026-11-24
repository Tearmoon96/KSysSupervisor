"""The disk utility's copying tools: their menu, dialogs and flows.

Cloning a drive or partition, saving one as an image file, and writing an
image - an installer .iso included - onto a drive. What may go where is
decided in diskcopy.py; this asks, shows and hands the request to the
window to run.
"""

import os
import shutil

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QRadioButton,
                             QVBoxLayout)

from . import diskcopy, diskops, disks, rescuebuild
from .diskdialogs import (AdviceCard, ConfirmDialog, _FixedWidthDialog,
                          _wrapped)
from .diskmaintainui import _heading
from .diskwidgets import tool_colour
from .widgets import Menu, tip

IMAGE_FILTER = ("Disk images (*.img *.img.gz *.iso *.raw *.bin *.dd);;"
                "All files (*)")


def _show_advice(dialog, layout, cards, advice):
    """Replace a dialog's advice cards; returns the cards now shown."""
    if [card.advice for card in cards] == advice:
        return cards
    for card in cards:
        layout.removeWidget(card)
        card.hide()
        card.deleteLater()
    cards = [AdviceCard(each) for each in advice]
    for card in cards:
        layout.addWidget(card)
    return cards


class CloneDialog(_FixedWidthDialog):
    """Choose where a copy goes and how it is made."""

    def __init__(self, disk, volume, all_disks, parent=None, identities=None,
                 tools=None):
        super().__init__(parent)
        self.disk = disk
        self.volume = None if volume is None or volume.kind == "whole" \
            else volume
        self.all_disks = list(all_disks)
        self.identities = identities
        self.tools = tools or diskcopy.diskfs.find_tool
        self._plan = None
        self.setWindowTitle("Clone")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Clone %s" % diskops.describe_target(
            disk, self.volume)))
        layout.addWidget(_wrapped(
            "Makes a second, identical %s. Everything on the target is "
            "erased." % ("drive" if self.volume is None else "volume"),
            dim=True))

        row = QHBoxLayout()
        row.setSpacing(8)
        caption = QLabel("Copy onto:")
        caption.setFixedWidth(92)
        row.addWidget(caption)
        self.target = QComboBox()
        row.addWidget(self.target, 1)
        layout.addLayout(row)
        self.target_note = _wrapped(dim=True)
        self.target_note.setIndent(100)
        layout.addWidget(self.target_note)

        self.exact = QRadioButton("Exact copy - every block")
        self.exact.setToolTip(
            "Copies the whole %s, the unused parts as well. The slowest "
            "way, and the one that works for anything."
            % ("drive" if self.volume is None else "partition"))
        self.smart = QRadioButton("Used blocks only - faster")
        self.smart.setToolTip(
            "Each filesystem is read by a tool that knows which blocks are "
            "in use, and only those are copied. What no tool can read that "
            "way is copied block for block.")
        self.exact.setChecked(True)
        layout.addWidget(self.exact)
        layout.addWidget(self.smart)

        self.methods = QGridLayout()
        self.methods.setContentsMargins(24, 0, 0, 0)
        self.methods.setHorizontalSpacing(12)
        self.methods.setVerticalSpacing(2)
        layout.addLayout(self.methods)
        self._method_labels = []

        self.verify = QCheckBox("Check the copy afterwards (recommended)")
        self.verify.setChecked(True)
        self.verify.setToolTip(
            "Reads the copy back from the drive and compares it with the "
            "original. Takes about as long again, and is the only proof "
            "that what was written is what is there.")
        layout.addWidget(self.verify)
        self.skip = QCheckBox("Skip unreadable areas")
        self.skip.setToolTip(
            "For a drive that is failing: copy everything that can still "
            "be read and put zeros where it cannot, instead of stopping at "
            "the first error. Every skipped area is listed at the end.")
        layout.addWidget(self.skip)
        self.grow = QCheckBox("Afterwards, let the last partition use the "
                              "free space")
        self.grow.setToolTip(
            "When the target is larger: once the copy is complete and "
            "checked, the last partition and its filesystem are enlarged "
            "into the room left at the end of the drive. If that cannot be "
            "done, the copy stays as it is and the reason is shown.")
        layout.addWidget(self.grow)
        self.grow_note = _wrapped(dim=True)
        self.grow_note.setIndent(24)
        layout.addWidget(self.grow_note)

        self.advice_box = QVBoxLayout()
        self.advice_box.setSpacing(6)
        layout.addLayout(self.advice_box)
        self.advice_cards = []

        buttons = QDialogButtonBox()
        self.start = QPushButton("Clone...")
        self.start.setToolTip("Shows exactly what will happen and asks once "
                              "more before anything is erased.")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.exact.toggled.connect(self._mode_changed)
        self.target.currentIndexChanged.connect(self._refresh)
        self.grow.toggled.connect(self._refresh)
        self._mode_changed()

    # ---- state --------------------------------------------------------------

    def mode(self):
        return diskcopy.EXACT if self.exact.isChecked() else diskcopy.SMART

    def chosen(self):
        """(disk, volume) of the selected target, or (None, None)."""
        # The first entry is "nothing chosen yet".
        index = self.target.currentIndex() - 1
        if index < 0 or index >= len(self.candidates):
            return None, None
        disk, volume, answer = self.candidates[index]
        return (disk, volume) if answer else (None, None)

    def _mode_changed(self, *_args):
        """What can take the copy depends on how it is made."""
        keep = self.target.currentData()
        self.candidates = diskcopy.targets(self.disk, self.volume,
                                           self.all_disks, self.mode())
        self.target.blockSignals(True)
        self.target.clear()
        model = self.target.model()
        # Nothing is chosen to begin with: the target is the thing that
        # gets erased, so it is never picked on the user's behalf.
        self.target.addItem("Choose what the copy goes onto...", None)
        for row, (disk, volume, answer) in enumerate(self.candidates, 1):
            name = disk.name if volume is None else volume.name
            self.target.addItem(diskops.describe_target(disk, volume), name)
            if not answer:
                item = model.item(row)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(answer.reason)
        usable = [(d.name if v is None else v.name)
                  for d, v, answer in self.candidates if answer]
        self.target.setCurrentIndex(self.target.findData(keep)
                                    if keep in usable else 0)
        self.target.blockSignals(False)
        self.skip.setVisible(self.exact.isChecked())
        self._refresh()

    def _refresh(self, *_args):
        for label in self._method_labels:
            self.methods.removeWidget(label)
            label.hide()
            label.deleteLater()
        self._method_labels = []
        if not self.exact.isChecked():
            for row, (what, how) in enumerate(diskcopy.methods(
                    self.disk, self.volume, self.mode(), self.tools)):
                first, second = _wrapped(what), _wrapped(how, dim=True)
                self.methods.addWidget(first, row, 0)
                self.methods.addWidget(second, row, 1)
                self._method_labels += [first, second]

        disk, volume = self.chosen()
        self._show_grow(disk)
        if disk is None:
            if not self.candidates:
                text = ("There is no other %s to copy onto. Connect one "
                        "and open this again."
                        % ("drive" if self.volume is None else "partition"))
            elif any(answer for _d, _v, answer in self.candidates):
                text = ("Everything on what you choose here is erased. "
                        "Entries that cannot take the copy are greyed out "
                        "and say why.")
            else:
                reasons = [c[2].reason for c in self.candidates]
                text = ("Nothing here can take the copy. %s"
                        % (reasons[0] if len(set(reasons)) == 1 else
                           "Each entry in the list says why."))
            self.target_note.setText(text)
            self.advice_cards = _show_advice(self, self.advice_box,
                                             self.advice_cards, [])
        else:
            lost = diskops.what_is_lost(disk, volume)
            self.target_note.setText("Will be erased: %s" % "; ".join(lost))
            self.advice_cards = _show_advice(
                self, self.advice_box, self.advice_cards,
                diskcopy.clone_advice(self.disk, self.volume, disk, volume,
                                      self.mode(), self.growing()))
        self.start.setEnabled(disk is not None)
        self.fit()

    def _grow_answer(self, disk):
        if self.volume is not None or disk is None:
            return None
        return diskcopy.grow_candidate(self.disk, disk, self.tools)

    def _show_grow(self, disk):
        """Whole drives only, and only onto a larger one: the checkbox is
        there but greyed, with the reason, whenever it cannot apply."""
        self.grow.setVisible(self.volume is None)
        answer = self._grow_answer(disk)
        if answer is None:
            self.grow.setEnabled(False)
            self.grow_note.setText("Choose the target first."
                                   if self.volume is None else "")
        else:
            self.grow.setEnabled(answer.volume is not None)
            self.grow_note.setText(diskcopy.grow_text(answer))
        self.grow_note.setVisible(self.volume is None)

    def growing(self):
        return self.grow.isEnabled() and self.grow.isChecked()

    # ---- result -------------------------------------------------------------

    def build_plan(self):
        disk, volume = self.chosen()
        if disk is None:
            return None
        return diskcopy.plan_clone(
            self.disk, self.volume, disk, volume, self.mode(),
            verify=self.verify.isChecked(),
            skip_errors=self.skip.isChecked(), identities=self.identities,
            tools=self.tools, grow=self.growing())

    def confirm(self, plan):
        return ConfirmDialog(plan, self).exec() == QDialog.DialogCode.Accepted

    def _accept(self):
        plan = self.build_plan()
        if plan is not None and self.confirm(plan):
            self._plan = plan
            self.accept()

    def plan(self):
        return self._plan


class _PathRow(QHBoxLayout):
    """A file path with a button that opens the file chooser."""

    def __init__(self, caption, pick):
        super().__init__()
        self.setSpacing(8)
        label = QLabel(caption)
        label.setFixedWidth(60)
        self.addWidget(label)
        self.edit = QLineEdit()
        self.addWidget(self.edit, 1)
        self.browse = QPushButton("Browse...")
        self.browse.clicked.connect(lambda: self._pick(pick))
        self.addWidget(self.browse)

    def _pick(self, pick):
        chosen = pick(self.edit.text())
        if chosen:
            self.edit.setText(chosen)


class SaveImageDialog(_FixedWidthDialog):
    """Where an image of a drive or volume is saved, and how."""

    def __init__(self, disk, volume, parent=None, identity=None,
                 folder=None, exists=os.path.exists):
        super().__init__(parent)
        self.disk = disk
        self.volume = None if volume is None or volume.kind == "whole" \
            else volume
        self.identity = identity
        self._exists = exists
        self._plan = None
        self.replace_agreed = None       # the path a replacement was agreed for
        self.setWindowTitle("Save as image")
        folder = folder or os.path.expanduser("~")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Save %s as an image file"
                                  % diskops.describe_target(disk,
                                                            self.volume)))
        layout.addWidget(_wrapped(
            "An image is a file holding every block of the %s. It can be "
            "written back later, onto this or another drive."
            % ("drive" if self.volume is None else "volume"), dim=True))

        self.path = _PathRow("Save as:", self.pick)
        self.path.edit.setText(os.path.join(
            folder, diskcopy.suggested_name(disk, self.volume)))
        layout.addLayout(self.path)
        self.note = _wrapped(dim=True)
        self.note.setIndent(68)
        layout.addWidget(self.note)

        self.compress = QCheckBox("Compress the image (smaller, slower)")
        self.compress.setToolTip(
            "Stores the image as .gz. The unused parts of the drive then "
            "take almost no room. It can still be written back from here.")
        layout.addWidget(self.compress)
        self.verify = QCheckBox("Check the image afterwards (recommended)")
        self.verify.setChecked(True)
        layout.addWidget(self.verify)
        self.skip = QCheckBox("Skip unreadable areas")
        self.skip.setToolTip(
            "For a drive that is failing: save everything that can still "
            "be read, with zeros where it cannot.")
        layout.addWidget(self.skip)

        buttons = QDialogButtonBox()
        self.start = QPushButton("Save Image")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.compress.toggled.connect(self._compress_changed)
        self.path.edit.textChanged.connect(self._refresh)
        self._refresh()

    def pick(self, current):
        chosen, _kind = QFileDialog.getSaveFileName(
            self, "Save image as", current, IMAGE_FILTER,
            options=QFileDialog.Option.DontConfirmOverwrite)
        return chosen

    def _compress_changed(self, on):
        text = self.path.edit.text()
        if on and not text.endswith(".gz"):
            self.path.edit.setText(text + ".gz")
        elif not on and text.endswith(".gz"):
            self.path.edit.setText(text[:-3])

    def problem(self):
        text = self.path.edit.text().strip()
        if not text:
            return "Choose where to save the image."
        if not os.path.isabs(text):
            return "Give the full path of the file, starting with /."
        folder = os.path.dirname(text)
        if not os.path.isdir(folder):
            return "The folder %s does not exist." % folder
        return ""

    def _refresh(self, *_args):
        problem = self.problem()
        size = self.disk.size if self.volume is None else self.volume.size
        if problem:
            self.note.setText(problem)
        else:
            try:
                room = os.statvfs(os.path.dirname(self.path.edit.text()))
                free = room.f_bavail * room.f_frsize
                self.note.setText(
                    "%s free there; the image needs %s%s."
                    % (disks.format_size(free), disks.format_size(size),
                       " at most" if self.compress.isChecked() else ""))
                if free < size and not self.compress.isChecked():
                    problem = "not enough room"
                    self.note.setText(
                        "Not enough room: %s free there, and the image "
                        "needs %s. Compressing may make it fit."
                        % (disks.format_size(free), disks.format_size(size)))
            except OSError:
                self.note.setText("")
        self.start.setEnabled(not problem)
        self.fit()

    def build_plan(self, overwrite=False):
        return diskcopy.plan_image_save(
            self.disk, self.volume, self.path.edit.text().strip(),
            compress=self.compress.isChecked(),
            verify=self.verify.isChecked(),
            skip_errors=self.skip.isChecked(), overwrite=overwrite,
            identity=self.identity)

    def ask_replace(self, path):
        from PyQt6.QtWidgets import QMessageBox
        return QMessageBox.question(
            self, "Save as image", "%s already exists. Replace it?" % path
        ) == QMessageBox.StandardButton.Yes

    def _accept(self):
        if self.problem():
            return
        path = self.path.edit.text().strip()
        overwrite = False
        if self._exists(path):
            if not self.ask_replace(path):
                return
            overwrite = True
        self._plan = self.build_plan(overwrite)
        self.accept()

    def plan(self):
        return self._plan


class WriteImageDialog(_FixedWidthDialog):
    """Choose an image file - an installer .iso included - to write onto
    the selected drive."""

    def __init__(self, disk, volume, parent=None, identity=None, folder=None,
                 inspect=diskcopy.inspect_image):
        super().__init__(parent)
        self.disk = disk
        self.volume = None if volume is None or volume.kind == "whole" \
            else volume
        self.identity = identity
        self._inspect = inspect
        self._folder = folder or os.path.expanduser("~")
        self._plan = None
        self.inspection = None
        self.setWindowTitle("Write image")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Write an image onto %s"
                                  % diskops.describe_target(disk,
                                                            self.volume)))
        layout.addWidget(_wrapped(
            "Writes an image file, block for block: one saved from here, or "
            "the installer image (.iso) of a Linux system, to make a "
            "memory stick a computer can start from. Everything on the "
            "target is erased.", dim=True))

        self.path = _PathRow("Image:", self.pick)
        layout.addLayout(self.path)
        self.note = _wrapped(dim=True)
        self.note.setIndent(68)
        layout.addWidget(self.note)

        self.verify = QCheckBox("Check the drive afterwards (recommended)")
        self.verify.setChecked(True)
        layout.addWidget(self.verify)

        self.advice_box = QVBoxLayout()
        self.advice_box.setSpacing(6)
        layout.addLayout(self.advice_box)
        self.advice_cards = []

        buttons = QDialogButtonBox()
        self.start = QPushButton("Write Image...")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.path.edit.textChanged.connect(self._refresh)
        self._refresh()

    def pick(self, current):
        chosen, _kind = QFileDialog.getOpenFileName(
            self, "Choose an image", current or self._folder, IMAGE_FILTER)
        return chosen

    KINDS = {"gzip": "A compressed image", "disk": "An image of a drive",
             "iso-hybrid": "An installer image, made to be written to a "
                           "drive",
             "iso": "A disc image", "raw": "An image"}

    def _refresh(self, *_args):
        path = self.path.edit.text().strip()
        self._problem = ""
        self.inspection = None
        advice = []
        if not path:
            self._problem = "Choose the image file."
        elif not os.path.isabs(path):
            self._problem = "Give the full path of the file, starting with /."
        else:
            self.inspection = self._inspect(path)
            kind = self.inspection["kind"]
            if kind == "unreadable":
                self._problem = self.inspection["advice"][0].text
            else:
                try:
                    plan = self.build_plan()
                    advice = list(plan.advice)
                    size = self.inspection["size"]
                    self.note.setText("%s, %s%s." % (
                        self.KINDS[kind], disks.format_size(size),
                        " compressed" if kind == "gzip" else ""))
                except ValueError as exc:
                    self._problem = str(exc)
        if self._problem:
            self.note.setText(self._problem)
        self.advice_cards = _show_advice(self, self.advice_box,
                                         self.advice_cards, advice)
        self.start.setEnabled(not self._problem)
        self.fit()

    def build_plan(self):
        return diskcopy.plan_image_write(
            self.disk, self.volume, self.path.edit.text().strip(),
            verify=self.verify.isChecked(), identity=self.identity,
            inspection=self.inspection)

    def confirm(self, plan):
        return ConfirmDialog(plan, self).exec() == QDialog.DialogCode.Accepted

    def _accept(self):
        if self._problem:
            return
        plan = self.build_plan()
        if self.confirm(plan):
            self._plan = plan
            self.accept()

    def plan(self):
        return self._plan


class CopyActions:
    """The Copy menu of the disk utility window."""

    COPY_ENTRIES = (
        ("clone_action", "Clone to Another Drive...", diskops.CLONE,
         "Make an identical second copy on another drive or partition: "
         "every block, or only the ones in use."),
        ("save_image_action", "Save as an Image File...", diskops.IMAGE_SAVE,
         "Copy every block of this into one file, to keep as a backup or "
         "write back later."),
        None,
        ("write_image_action", "Write an Image or ISO onto This...",
         diskops.IMAGE_WRITE,
         "Write an image file onto this, block for block - a saved image, "
         "or a Linux installer .iso to make a memory stick a computer can "
         "start from."),
        ("stick_action", "Make a Recovery Stick on This...",
         diskops.RECOVERY_STICK,
         "Turn this USB stick into a small system that starts a computer "
         "straight into this Disk Utility: the way to clone or resize the "
         "drive a system runs from."),
        None,
        ("rescue_action", "Rescue a Failing Drive to an Image...",
         diskops.RESCUE,
         "For a drive with read errors: copy everything that can still be "
         "read into an image file, the easy parts first, so that the rest "
         "of the work is done on the copy."),
        ("attach_action", "Open an Image File as a Drive...", None,
         "Show an image file in the list as a read-only drive, so that it "
         "can be searched for files, checked or copied like one."),
        ("detach_action", "Close This Image", diskops.IMAGE_CLOSE,
         "Take this image out of the list again. The file is not changed."),
    )

    def _build_copy(self):
        self.copy_button = self._button(
            "Copy", ("edit-copy", "document-save"), lambda: None)
        menu = Menu(self.copy_button)
        menu.setToolTipsVisible(True)
        slots = {"clone_action": self.clone_selected,
                 "save_image_action": self.save_image_selected,
                 "write_image_action": self.write_image_selected,
                 "stick_action": self.recovery_stick_selected,
                 "rescue_action": self.rescue_selected,
                 "attach_action": self.attach_image,
                 "detach_action": self.detach_image}
        for entry in self.COPY_ENTRIES:
            if entry is None:
                menu.addSeparator()
                continue
            name, title, _action, _text = entry
            action = menu.addAction(title)
            setattr(self, name, action)
            action.triggered.connect(
                lambda _checked=False, slot=slots[name]: slot())
        self.copy_button.setMenu(menu)
        return self.copy_button

    def _update_copy(self, disk, volume, locked):
        colour = tool_colour()
        for entry in self.COPY_ENTRIES:
            if entry is None:
                continue
            name, title, action, text = entry
            answer = diskops.availability(action, disk, volume) \
                if action is not None else diskops.YES
            item = getattr(self, name)
            if name == "detach_action":
                item.setVisible(disk.kind == disks.IMAGE)
            item.setEnabled(bool(answer) and not locked)
            title = title.rstrip(".")
            if answer:
                item.setToolTip(tip(title, text, colour))
            else:
                item.setToolTip(tip("%s is not available" % title,
                                    answer.reason, colour))
        self.copy_button.setEnabled(disk.has_media)

    def recovery_stick_selected(self, choose=False):
        """Make a recovery stick: on the selected drive, or - `choose`,
        from the main window's menu - on one picked in the dialog."""
        from .rescueui import RecoveryStickDialog
        if self._job is not None or self._busy:
            return
        if not self._have_read:
            # Opened together with the window, from the main window's
            # menu: the drives are still being read, and the dialog would
            # say there is no drive to make the stick on.
            if not self._after_read:
                self._after_read.append(
                    lambda: self.recovery_stick_selected(choose))
            return
        disk = None if choose else self._target(diskops.RECOVERY_STICK)[0]
        if disk is None and not choose:
            return
        dialog = RecoveryStickDialog(disk, self.disks, self,
                                     identities=self.identities)
        if not self.show_dialog(dialog) or dialog.plan() is None:
            return
        layout, variant = dialog.keyboard_choice()
        self._make_stick(dialog.plan(), rescuebuild.Settings(
            keyboard=layout, variant=variant, **self._stick_look()))

    def _stick_look(self):
        """The colours and font of this desktop, for the stick to show."""
        from PyQt6.QtWidgets import QApplication
        from .rescuewindow import desktop_palette
        app = QApplication.instance()
        palette = desktop_palette(app)
        font = app.font()
        return {"palette": palette, "font": font.family(),
                "font_size": font.pointSizeF() if font.pointSizeF() > 0
                else 10.0,
                "dark": app.palette().window().color().lightness() < 128}

    def _make_stick(self, plan, settings, folder=None):
        """Put the system together on a thread, then have the helper write
        it. The folder is removed afterwards either way: building it again
        takes seconds, keeping it takes 150 MB."""
        folder = folder or os.path.join(rescuebuild.cache_folder(), "stage")
        shutil.rmtree(folder, ignore_errors=True)
        state = {"text": "Putting the recovery system together..."}

        def progress(text, done=0, total=0):
            state["text"] = "Putting the recovery system together: %s%s" % (
                text[:1].lower() + text[1:],
                " (%d%%)" % (done * 100 // total) if total else "")

        timer = QTimer(self)
        timer.timeout.connect(lambda: self.status.setText(state["text"]))
        timer.start(250)
        self._busy = True
        self._update_actions()
        try:
            result, error = self._blocking(lambda: rescuebuild.build(
                folder, settings=settings, progress=progress))
        finally:
            timer.stop()
            self._busy = False
            self._update_actions()
        if error is not None:
            shutil.rmtree(folder, ignore_errors=True)
            self.status.setText("")
            self.notify("Make a Recovery Stick",
                        "The recovery system could not be put together:"
                        "\n\n%s" % error)
            return
        import json
        with open(result.manifest) as handle:
            manifest = json.load(handle)
        plan.request["stage"] = folder
        plan.request["files"] = manifest["files"]

        def done(outcome):
            shutil.rmtree(folder, ignore_errors=True)
            self.status.setText(diskcopy.stick_done_text(outcome))
            from .rescueui import HOW_TO_START
            missing = result.missing
            self.notify(
                "Make a Recovery Stick",
                "%s\n\n%s%s" % (
                    diskcopy.stick_done_text(outcome), HOW_TO_START,
                    "\n\nNot installed here, so not on the stick either "
                    "(what needs them is greyed out there): %s."
                    % ", ".join(missing) if missing else ""),
                warning=False)

        def failed(_error):
            shutil.rmtree(folder, ignore_errors=True)

        self.run_plan(plan, done=done, failed=failed)

    def _image_folder(self):
        return self.settings.value("disk_image_folder", "", type=str) or None

    def _remember_folder(self, path):
        self.settings.setValue("disk_image_folder", os.path.dirname(path))

    def clone_selected(self):
        disk, volume = self._target(diskops.CLONE)
        if disk is None:
            return
        dialog = CloneDialog(disk, volume, self.disks, self,
                             identities=self.identities)
        if self.show_dialog(dialog) and dialog.plan() is not None:
            self.run_plan(dialog.plan(), done=self._clone_done)

    def _clone_done(self, result):
        self.status.setText(diskcopy.clone_done_text(result))
        if result.get("grow_skipped"):
            self.notify(
                "Clone", "The copy is complete and checked, but its last "
                "partition was not enlarged:\n\n%s\n\nIt can be enlarged "
                "later with Partitions > Resize." % result["grow_skipped"],
                warning=True)
        elif (result.get("grown") or {}).get("note"):
            self.notify("Clone", result["grown"]["note"], warning=False)
        ranges = diskcopy.skipped_report(result)
        if ranges:
            self.notify(
                "Clone", "The copy is complete except for what could not be "
                "read from the source. These areas are zeros on the copy:"
                "\n\n%s\n\nFiles that lie in them are damaged on the copy. "
                "Check the copy's filesystem (Maintain > Check for Errors) "
                "before relying on it." % "\n".join(ranges[:40]),
                warning=True)

    def save_image_selected(self):
        disk, volume = self._target(diskops.IMAGE_SAVE)
        if disk is None:
            return
        name = disk.name if volume is None or volume.kind == "whole" \
            else volume.name
        dialog = SaveImageDialog(disk, volume, self,
                                 identity=self.identities.get(name),
                                 folder=self._image_folder())
        if self.show_dialog(dialog) and dialog.plan() is not None:
            plan = dialog.plan()
            self._remember_folder(plan.request["path"])

            def done(result):
                self.status.setText(diskcopy.image_saved_text(result))
                if result.get("was_mounted") and plan.remount:
                    self._mount_after(plan.request["device"])
            self.run_plan(plan, done=done)

    def pick_image(self, save, start):
        """A file chooser for an image; its own method for the tests."""
        if save:
            chosen, _kind = QFileDialog.getSaveFileName(
                self, "Rescue into", start, IMAGE_FILTER,
                options=QFileDialog.Option.DontConfirmOverwrite)
        else:
            chosen, _kind = QFileDialog.getOpenFileName(
                self, "Choose an image", start, IMAGE_FILTER)
        return chosen

    def rescue_selected(self):
        disk, volume = self._target(diskops.RESCUE)
        if disk is None:
            return
        whole = volume is None or volume.kind == "whole"
        name = disk.name if whole else volume.name
        folder = self._image_folder() or os.path.expanduser("~")
        path = self.pick_image(True, os.path.join(
            folder, diskcopy.suggested_name(disk, None if whole
                                            else volume)))
        if not path:
            return
        resume = diskcopy.unfinished(path)
        if os.path.exists(path) and not resume:
            self.notify("Rescue to image", "%s already exists and is not "
                        "an unfinished rescue. Choose another name." % path)
            return
        plan = diskcopy.plan_rescue(disk, None if whole else volume, path,
                                    identity=self.identities.get(name),
                                    resume=resume)
        from .diskmaintainui import ActionDialog
        if not self.show_dialog(ActionDialog(
                plan, self, heading="Carry on rescuing %s?" % plan.target
                if resume else "Rescue %s?" % plan.target,
                accept="Carry On" if resume else "Rescue")):
            return
        self._remember_folder(path)

        def done(result):
            self.status.setText(diskcopy.rescue_done_text(result))
            if self.ask("Rescue to image",
                        diskcopy.rescue_done_text(result),
                        "Open the Image as a Drive",
                        "Opened as a drive, the image can be searched for "
                        "files or checked without touching the failing "
                        "drive again."):
                self._attach(result.get("path") or path)
        self.run_plan(plan, done=done)

    def attach_image(self):
        if self._job is not None:
            return
        path = self.pick_image(False, self._image_folder()
                               or os.path.expanduser("~"))
        if path:
            self._remember_folder(path)
            self._attach(path)

    def _attach(self, path):
        def done(result):
            device = result.get("device", "")
            self.status.setText("%s is open as a read-only drive (%s)."
                                % (os.path.basename(path), device))
            self.refresh()
            self.select_device(device)
        self.run_plan(diskcopy.plan_attach(path), done=done)

    def detach_image(self):
        disk, _volume = self._target(None)
        if disk is None or not diskops.availability(diskops.IMAGE_CLOSE,
                                                    disk):
            return
        self.run_plan(
            diskcopy.plan_detach(disk, self.identities.get(disk.name)),
            done=lambda _result: self.status.setText(
                "%s is closed." % disk.title))

    def write_image_selected(self):
        disk, volume = self._target(diskops.IMAGE_WRITE)
        if disk is None:
            return
        name = disk.name if volume is None or volume.kind == "whole" \
            else volume.name
        dialog = WriteImageDialog(disk, volume, self,
                                  identity=self.identities.get(name),
                                  folder=self._image_folder())
        if self.show_dialog(dialog) and dialog.plan() is not None:
            plan = dialog.plan()
            self._remember_folder(plan.request["path"])
            self.run_plan(plan, done=lambda result: self.status.setText(
                diskcopy.image_written_text(result)))
