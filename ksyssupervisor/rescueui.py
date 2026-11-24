"""Making the recovery stick, from the Disk Utility on the desktop.

The dialog asks which drive and which keyboard; the window then puts the
system together (rescuebuild, as the user) and has the helper write it.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from . import diskcopy, diskops, rescuebuild
from .diskdialogs import ConfirmDialog, _FixedWidthDialog, _wrapped
from .diskmaintainui import _heading

#: Keyboard layouts offered, by their XKB name. The computer's own is added
#: when it is not among them.
KEYBOARDS = (
    ("us", "English (US)"), ("gb", "English (UK)"), ("it", "Italian"),
    ("de", "German"), ("fr", "French"), ("es", "Spanish"),
    ("latam", "Spanish (Latin America)"), ("pt", "Portuguese"),
    ("br", "Portuguese (Brazil)"), ("ch", "Swiss"), ("be", "Belgian"),
    ("nl", "Dutch"), ("se", "Swedish"), ("no", "Norwegian"),
    ("dk", "Danish"), ("fi", "Finnish"), ("pl", "Polish"),
    ("cz", "Czech"), ("hu", "Hungarian"), ("gr", "Greek"),
    ("tr", "Turkish"), ("ru", "Russian"), ("ua", "Ukrainian"),
    ("ca", "Canadian"), ("jp", "Japanese"),
)

HOW_TO_START = (
    "To start a computer from it: plug it in, switch the computer on and "
    "open its boot menu - usually F12, F11, F8 or Esc right after "
    "switching on (Esc then F9 on HP, F12 on Dell and Lenovo, F8 on Asus "
    "desktops, F11 on MSI and ASRock) - and choose the stick, the entry "
    "named UEFI. It works on any computer from about 2012 on with 2 GB of "
    "memory or more.")


class RecoveryStickDialog(_FixedWidthDialog):
    """Which drive becomes the stick, and the keyboard it starts with."""

    def __init__(self, disk, all_disks, parent=None, identities=None,
                 keyboard=None):
        super().__init__(parent)
        self.identities = identities
        self._plan = None
        self.setWindowTitle("Make a Recovery Stick")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Make a Recovery Stick"))
        layout.addWidget(_wrapped(
            "Turns a USB stick into a small system that starts a computer "
            "straight into this Disk Utility - the way to clone, resize or "
            "repair the drive a system runs from. It is put together from "
            "this computer's own system, so nothing is downloaded. On the "
            "stick, changes are prepared first and only made when you "
            "press Apply Changes.", dim=True))

        row = QHBoxLayout()
        caption = QLabel("Stick:")
        caption.setFixedWidth(92)
        row.addWidget(caption)
        self.target = QComboBox()
        row.addWidget(self.target, 1)
        layout.addLayout(row)
        self.target_note = _wrapped(dim=True)
        self.target_note.setIndent(100)
        layout.addWidget(self.target_note)

        self.candidates = []
        self.target.addItem("Choose the drive to make the stick on...", None)
        model = self.target.model()
        for disk_ in all_disks:
            if not disk_.has_media:
                continue
            answer = diskcopy.stick_availability(disk_)
            self.candidates.append((disk_, answer))
            self.target.addItem(diskops.describe_target(disk_, None),
                                disk_.name)
            if not answer:
                item = model.item(self.target.count() - 1)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(answer.reason)
        # The drive the menu was opened on is the one meant - but only if
        # it can take the stick; nothing else is chosen on the user's
        # behalf.
        if disk is not None and diskcopy.stick_availability(disk):
            self.target.setCurrentIndex(self.target.findData(disk.name))

        row = QHBoxLayout()
        caption = QLabel("Keyboard:")
        caption.setFixedWidth(92)
        row.addWidget(caption)
        self.keyboard = QComboBox()
        layout_name, variant = keyboard or rescuebuild.keyboard_layout()
        names = dict(KEYBOARDS)
        entries = list(KEYBOARDS)
        if layout_name not in names:
            entries.insert(0, (layout_name, layout_name))
        for code, title in entries:
            label = title
            if code == layout_name:
                label = "%s%s - this computer's" % (
                    title, " (%s)" % variant if variant else "")
            self.keyboard.addItem(label, (code, variant
                                          if code == layout_name else ""))
        self.keyboard.setCurrentIndex(
            [code for code, _t in entries].index(layout_name))
        row.addWidget(self.keyboard, 1)
        layout.addLayout(row)

        self.verify = QCheckBox("Check the stick afterwards (recommended)")
        self.verify.setChecked(True)
        self.verify.setToolTip(
            "Reads every file back from the stick and compares it with "
            "what was written: a stick that fails here would fail when it "
            "is needed.")
        layout.addWidget(self.verify)
        layout.addWidget(_wrapped(
            "Needs a stick of 1 GB or larger; the recovery system takes "
            "about 150 MB of it. %s" % HOW_TO_START, dim=True))

        buttons = QDialogButtonBox()
        self.start = QPushButton("Make the Stick...")
        self.start.setToolTip("Shows what will be erased and asks once "
                              "more before anything is.")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.target.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def chosen(self):
        name = self.target.currentData()
        for disk, answer in self.candidates:
            if disk.name == name and answer:
                return disk
        return None

    def keyboard_choice(self):
        return self.keyboard.currentData() or ("us", "")

    def _refresh(self, *_args):
        disk = self.chosen()
        if disk is None:
            usable = any(answer for _d, answer in self.candidates)
            self.target_note.setText(
                "Everything on the drive you choose is erased. Drives that "
                "cannot take the stick are greyed out and say why."
                if usable else
                "No drive here can take the stick. Plug in a USB stick of "
                "1 GB or larger and open this again.")
        else:
            self.target_note.setText("Will be erased: %s" % "; ".join(
                diskops.what_is_lost(disk, None)))
        self.start.setEnabled(disk is not None)
        self.fit()

    def build_plan(self):
        disk = self.chosen()
        if disk is None:
            return None
        return diskcopy.plan_recovery_stick(
            disk, verify=self.verify.isChecked(), identities=self.identities)

    def confirm(self, plan):
        return ConfirmDialog(plan, self).exec() == QDialog.DialogCode.Accepted

    def _accept(self):
        plan = self.build_plan()
        if plan is not None and self.confirm(plan):
            self._plan = plan
            self.accept()

    def plan(self):
        return self._plan
