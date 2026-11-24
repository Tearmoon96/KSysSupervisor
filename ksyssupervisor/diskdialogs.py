"""The disk utility's dialogs: format, confirm, rename.

Small on purpose. The format dialog asks the three things a format needs -
which filesystem, what name, quick or full - and puts everything else behind
"Advanced". What it adds over a bare form is the advice: the warnings that go
with a choice appear as the choice is made, before anything is confirmed.

The dialogs produce a diskops.Plan and nothing else; running it is the
window's job.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QIcon, QPalette
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QRadioButton, QStyle, QVBoxLayout,
                             QWidget)

from . import diskfs, diskops, disks, udisks
from .widgets import WARNING_ACCENT, Card, Chevron

DIALOG_WIDTH = 460


def _dim(label):
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


def _wrapped(text="", dim=False):
    label = QLabel(text)
    label.setWordWrap(True)
    return _dim(label) if dim else label


class AdviceCard(Card):
    """One piece of advice: a warning in amber, or a plain note.

    `collapsible`: only the title shows, with an arrow beside it; the
    explanation opens under it when the title or the arrow is clicked.
    """

    def __init__(self, advice, parent=None, collapsible=False):
        super().__init__(parent)
        self.advice = advice
        self.toggle = None
        warning = advice.level == diskfs.WARNING
        if warning:
            self.set_accent(WARNING_ACCENT)
        row = QHBoxLayout(self)
        row.setContentsMargins(14 if warning else 10, 8, 10, 9)
        row.setSpacing(8)

        icon = QIcon.fromTheme("dialog-warning" if warning
                               else "dialog-information")
        if icon.isNull():
            icon = self.style().standardIcon(
                QStyle.StandardPixmap.SP_MessageBoxWarning if warning
                else QStyle.StandardPixmap.SP_MessageBoxInformation)
        badge = QLabel()
        badge.setPixmap(icon.pixmap(20, 20))
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        title = _wrapped(advice.title)
        font = QFont(title.font())
        font.setBold(True)
        title.setFont(font)
        text.addWidget(title)
        self.details = _wrapped(advice.text)
        text.addWidget(self.details)
        row.addLayout(text, 1)
        if collapsible:
            self.toggle = Chevron()
            self.toggle.clicked.connect(self.flip)
            row.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
            title.setCursor(Qt.CursorShape.PointingHandCursor)
            title.mousePressEvent = lambda _event: self.flip()
            self.details.setVisible(False)

    def is_open(self):
        return not self.details.isHidden()

    def flip(self):
        """Show or hide the explanation, and let the dialog fit it."""
        opened = not self.is_open()
        self.details.setVisible(opened)
        self.toggle.set_expanded(opened)
        window = self.window()
        if hasattr(window, "fit"):
            window.fit()


def _show_pending(layout):
    """Show now the widgets of `layout` that Qt would show a moment later.

    A widget added to a layout of a window already on screen is shown only
    on the next pass of the event loop. Until then it is hidden, and a
    hidden widget counts for nothing in the layout's height: a dialog sized
    straight after adding an advice card was made too short for it, and the
    card and the text above it were squeezed into what was left. A widget
    hidden on purpose carries the explicit flag and stays as it is - Qt's
    own deferred show makes the same distinction.
    """
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget()
        if widget is not None:
            if widget.isHidden() and not widget.testAttribute(
                    Qt.WidgetAttribute.WA_WState_ExplicitShowHide):
                widget.show()
        elif item.layout() is not None:
            _show_pending(item.layout())


class ClusterField:
    """The "Cluster size" row and the note under it.

    Shared by Format and New partition so both offer the same sizes the
    same way: every size the volume can have, the usual one preselected and
    marked, and a note that says what the one chosen costs. A filesystem
    whose size cannot be chosen shows what it gets, greyed, with the reason.
    `changed` is called after the note's text changes, for the dialog to
    fit its height again.
    """

    TIP = ("A file always takes up whole clusters, so this is the space even "
           "the smallest file uses. Smaller clusters waste less space on "
           "small files; larger ones only help with very large files.")

    def __init__(self, caption_width, changed=None):
        self.changed = changed
        self.row = QWidget()
        line = QHBoxLayout(self.row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        caption = QLabel("Cluster size:")
        caption.setFixedWidth(caption_width)
        line.addWidget(caption)
        self.combo = QComboBox()
        line.addWidget(self.combo, 1)
        self.note = _wrapped(dim=True)
        self.note.setIndent(caption_width + 8)
        self.choice = None
        self._key = None
        self.combo.currentIndexChanged.connect(self._show_note)

    def update(self, fstype, size, logical_sector):
        """Offer the sizes a `size`-byte volume of `fstype` can have."""
        choice = diskfs.cluster_choice(fstype, size, logical_sector)
        # The user's pick survives a change of size or name, not of
        # filesystem: what suits exFAT says nothing about NTFS.
        keep = self.combo.currentData() if fstype == self._key else None
        if choice == self.choice and fstype == self._key:
            return
        self.choice, self._key = choice, fstype
        self.combo.blockSignals(True)
        self.combo.clear()
        if choice.sizes:
            for size_ in choice.sizes:
                title = diskfs.format_cluster(size_)
                if size_ == choice.recommended:
                    title += "  (recommended)"
                self.combo.addItem(title, size_)
            pick = keep if keep in choice.sizes else choice.recommended
            self.combo.setCurrentIndex(self.combo.findData(pick))
            self.combo.setEnabled(True)
            self.combo.setToolTip(self.TIP)
        else:
            self.combo.addItem(choice.fixed, None)
            self.combo.setEnabled(False)
            self.combo.setToolTip(choice.reason)
        self.combo.blockSignals(False)
        self._show_note()

    def value(self):
        """The cluster size chosen, or None where there is no choice."""
        if self.choice is None or not self.choice.sizes:
            return None
        return self.combo.currentData()

    def setVisible(self, shown):
        self.row.setVisible(shown)
        self.note.setVisible(shown)

    def _show_note(self, *_args):
        choice = self.choice
        if choice is None:
            return
        if not choice.sizes:
            self.note.setText(choice.reason)
            self.note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        else:
            cluster = self.combo.currentData()
            self.note.setText(diskfs.cluster_note(
                self._key, cluster, choice.recommended))
            # Dimmed while it is the recommended size; in plain text as
            # soon as it is not, so the cost is read.
            self.note.setForegroundRole(
                QPalette.ColorRole.PlaceholderText
                if cluster == choice.recommended
                else QPalette.ColorRole.WindowText)
        if self.changed is not None:
            self.changed()


class _FixedWidthDialog(QDialog):
    """A dialog one width wide, as tall as its text needs at that width.

    QWidget.adjustSize() works the height out for the width the layout would
    *like*, and a layout of wrapped labels would like to be wide - so at the
    real, narrower width the text ran to more lines than there was room for
    and the last card was cut off. The height is asked for at the width the
    dialog actually has.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedWidth(DIALOG_WIDTH)

    def fit(self):
        layout = self.layout()
        if layout is None:
            return
        if self.isVisible():
            _show_pending(layout)
        layout.activate()
        self.setFixedHeight(layout.totalHeightForWidth(DIALOG_WIDTH))

    def showEvent(self, event):
        self.fit()
        super().showEvent(event)


class FormatDialog(_FixedWidthDialog):
    """Choose how to format a volume or a whole drive.

    Opened on a partition it offers both: that partition alone, or the whole
    drive put back together as one volume. Opened on a drive (`volume` None,
    or a filesystem that sits on the bare drive) there is only the latter.

    `identities` maps a device name to disks.identity() of it as read when
    the window drew the drive list - not now: the helper compares it with
    the device it finds, and that only catches a swapped stick if this is the
    earlier reading. `identity` is the same for the one device the dialog
    was opened on.
    """

    def __init__(self, disk, volume=None, parent=None, tools=diskfs.find_tool,
                 identity=None, identities=None):
        super().__init__(parent)
        if volume is not None and volume.kind == "whole":
            volume = None
        self.disk, self.clicked = disk, volume
        self.tools = tools
        self.identities = dict(identities or {})
        if identity is not None:
            self.identities.setdefault(
                disk.name if volume is None else volume.name, identity)
        self.choices = []
        self._plan = None
        self.setWindowTitle("Format")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.scope_part = self.scope_whole = None
        if volume is None:
            target = _wrapped(diskops.describe_target(disk))
            font = QFont(target.font())
            font.setBold(True)
            target.setFont(font)
            layout.addWidget(target)
            layout.addWidget(_wrapped(
                "The whole drive: everything on it is erased and it becomes "
                "one volume of its full capacity.", dim=True))
        else:
            target = _wrapped("%s (%s)" % (disk.title, disk.name))
            font = QFont(target.font())
            font.setBold(True)
            target.setFont(font)
            layout.addWidget(target)

            self.scope_part = QRadioButton(
                "Only this partition - %s (%s), %s"
                % (volume.title, volume.name,
                   disks.format_size(volume.size)))
            layout.addWidget(self.scope_part)
            note = _wrapped("Other partitions on the drive are not touched.",
                            dim=True)
            note.setIndent(24)
            layout.addWidget(note)

            self.scope_whole = QRadioButton(
                "Whole drive - restore to one volume, %s"
                % disks.format_size(disk.size))
            layout.addWidget(self.scope_whole)
            count = len(disk.partitions)
            allowed = diskops.availability(diskops.FORMAT, disk)
            self.whole_note = _wrapped(
                "Erases %s and gives the drive back its full capacity."
                % ("all %d partitions" % count if count > 1
                   else "everything on it")
                if allowed else allowed.reason, dim=True)
            self.whole_note.setIndent(24)
            layout.addWidget(self.whole_note)
            if not allowed:
                self.scope_whole.setEnabled(False)
                self.scope_whole.setToolTip(allowed.reason)
            # A drive carrying an installer image has nothing worth keeping
            # one partition of: putting it back together is what is wanted.
            if allowed and disk.written_as_image:
                self.scope_whole.setChecked(True)
            else:
                self.scope_part.setChecked(True)

        self.filesystem = QComboBox()
        layout.addLayout(self._field("File system:", self.filesystem))
        self.works_with = self._under_field(_wrapped(dim=True))
        layout.addWidget(self.works_with)

        self.cluster = ClusterField(self.CAPTION_WIDTH, changed=self.fit)
        layout.addWidget(self.cluster.row)
        layout.addWidget(self.cluster.note)

        self.label = QLineEdit()
        self.label.setPlaceholderText("Optional")
        self.label.setClearButtonEnabled(True)
        #: The partition's present name, offered again for that partition
        #: only: it is not the name of the drive as a whole.
        self._old_label = volume.label if volume is not None else ""
        layout.addLayout(self._field("Name:", self.label))
        self.label_note = self._under_field(_wrapped(dim=True))
        layout.addWidget(self.label_note)

        self.quick = QCheckBox("Quick format")
        self.quick.setChecked(True)
        layout.addWidget(self.quick)
        self.quick_note = _wrapped(dim=True)
        self.quick_note.setIndent(24)
        layout.addWidget(self.quick_note)

        self.owner = QCheckBox("Make it writable by me")
        self.owner.setChecked(True)
        self.owner.setToolTip(
            "A new Linux filesystem belongs to the administrator, so an "
            "ordinary user cannot save anything on it. This hands its top "
            "folder to you.")
        layout.addWidget(self.owner)

        # Advanced: the partition table, for a whole drive only.
        self.advanced_toggle = Chevron(tips=("Hide", "Show"))
        self.advanced_toggle.clicked.connect(self._toggle_advanced)
        self.advanced_header = QWidget()
        header_row = QHBoxLayout(self.advanced_header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(4)
        header_row.addWidget(self.advanced_toggle)
        header_row.addWidget(QLabel("Advanced"))
        header_row.addStretch(1)
        layout.addWidget(self.advanced_header)

        self.advanced = QWidget()
        self.table = QComboBox()
        self.table.addItem("GPT - modern computers", "gpt")
        self.table.addItem("MBR - cameras, TVs, old computers", "dos")
        if not diskfs.table_allowed(disk, "dos"):
            item = self.table.model().item(1)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.setToolTip("MBR cannot describe a drive larger than 2 TB.")
        self.table.setCurrentIndex(
            self.table.findData(diskfs.default_table(disk)))
        advanced_row = QHBoxLayout(self.advanced)
        advanced_row.setContentsMargins(28, 0, 0, 0)
        advanced_row.setSpacing(8)
        advanced_row.addWidget(QLabel("Partition table:"))
        advanced_row.addWidget(self.table, 1)
        layout.addWidget(self.advanced)
        self.advanced.setVisible(False)

        self.advice_box = QVBoxLayout()
        self.advice_box.setSpacing(6)
        layout.addLayout(self.advice_box)
        self.advice_cards = []

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(rule)

        buttons = QDialogButtonBox()
        self.start = QPushButton("Format...")
        self.start.setToolTip("Shows exactly what will happen and asks once "
                              "more before anything is erased.")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.filesystem.currentIndexChanged.connect(self._refresh)
        self.label.textChanged.connect(self._refresh)
        self.quick.toggled.connect(self._refresh)
        if self.scope_whole is not None:
            self.scope_whole.toggled.connect(self._scope_changed)
        self._scope_changed()

    # ---- what is being formatted ------------------------------------------

    @property
    def whole(self):
        if self.scope_whole is None:
            return True
        return self.scope_whole.isChecked()

    @property
    def volume(self):
        """The partition being formatted, or None for the whole drive."""
        return None if self.whole else self.clicked

    @property
    def size(self):
        if self.whole:
            return self.disk.size - 2 * disks.MIB
        return self.clicked.size

    @property
    def identity(self):
        name = self.disk.name if self.whole else self.clicked.name
        return self.identities.get(name)

    def _scope_changed(self, *_args):
        """Refill what depends on the target: sizes decide the filesystems."""
        keep = self.filesystem.currentData()
        self.choices = diskfs.choices(self.disk, self.size, tools=self.tools)
        self.filesystem.blockSignals(True)
        self.filesystem.clear()
        model = self.filesystem.model()
        for row, choice in enumerate(self.choices):
            title = choice.filesystem.title
            if choice.recommended and choice.available:
                title += "  (recommended)"
            self.filesystem.addItem(title, choice.filesystem.key)
            if not choice.available:
                item = model.item(row)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(choice.reason)
        # Stay on the filesystem already chosen if it can still be made,
        # else start on the first one that can.
        available = [c.filesystem.key for c in self.choices if c.available]
        if keep in available:
            self.filesystem.setCurrentIndex(self.filesystem.findData(keep))
        elif available:
            self.filesystem.setCurrentIndex(
                self.filesystem.findData(available[0]))
        self.filesystem.blockSignals(False)

        self.advanced_header.setVisible(self.whole)
        if not self.whole:
            self.advanced.setVisible(False)
            self.advanced_toggle.set_expanded(False)

        if self._old_label:
            self.label.blockSignals(True)
            if self.whole and self.label.text() == self._old_label:
                self.label.clear()
            elif not self.whole and not self.label.text():
                self.label.setText(self._old_label)
            self.label.blockSignals(False)
        self._refresh()

    # ---- layout -----------------------------------------------------------

    #: Width of the captions before the two fields, so the fields line up
    #: and the notes under them can be indented to match.
    CAPTION_WIDTH = 92

    def _field(self, caption, widget):
        """A caption and its field on one row.

        Rows of an ordinary layout rather than a form layout: a form layout
        gives a wrapped label in its field column the height of one line,
        which cut the notes under the fields in half.
        """
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(caption)
        label.setFixedWidth(self.CAPTION_WIDTH)
        row.addWidget(label)
        row.addWidget(widget, 1)
        return row

    def _under_field(self, label):
        # An indent, not a contents margin: a label works out how tall its
        # wrapped text is from its indent, and ignores a margin - the note
        # then wrapped to two lines in the height of one.
        label.setIndent(self.CAPTION_WIDTH + 8)
        return label

    # ---- state ------------------------------------------------------------

    def selected_filesystem(self):
        key = self.filesystem.currentData()
        for choice in self.choices:
            if choice.filesystem.key == key and choice.available:
                return choice.filesystem
        return None

    def _toggle_advanced(self):
        shown = not self.advanced.isVisible()
        self.advanced.setVisible(shown)
        self.advanced_toggle.set_expanded(shown)
        self.fit()

    def _refresh(self, *_args):
        fs = self.selected_filesystem()
        if fs is None:
            self.works_with.setText("None of the tools that create a "
                                    "filesystem is installed.")
            self.start.setEnabled(False)
            return
        self.works_with.setText(fs.works_with)
        self.cluster.update(fs.key, self.size, self.disk.logical_sector)

        label, problem = diskfs.validate_label(fs, self.label.text())
        used = diskfs.label_length(fs, label)
        if problem:
            self.label_note.setText(problem)
            self.label_note.setForegroundRole(QPalette.ColorRole.WindowText)
        else:
            note = "%d of %d characters" % (used, fs.label_max)
            if label != self.label.text().strip():
                note += " - will be stored as %s" % label
            self.label_note.setText(note)
            self.label_note.setForegroundRole(
                QPalette.ColorRole.PlaceholderText)

        method = diskops.full_erase_method(self.disk)
        amount = disks.format_size(self.disk.size if self.whole
                                   else self.clicked.size)
        if self.quick.isChecked() and self.disk.discard:
            # The tools that make a filesystem tell a drive that takes TRIM
            # to drop everything first, and such a drive then does.
            self.quick_note.setText(
                "Creates the new filesystem straight away. This drive "
                "takes TRIM, so the old contents are usually discarded as "
                "well and cannot be recovered afterwards.")
        elif self.quick.isChecked():
            self.quick_note.setText(
                "Creates the new filesystem straight away. The old contents "
                "are no longer reachable but are not overwritten.")
        elif method == diskops.ERASE_DISCARD:
            self.quick_note.setText(
                "Full: first discards every block (TRIM), which takes "
                "seconds on an SSD and makes the old contents unrecoverable.")
        else:
            self.quick_note.setText(
                "Full: first overwrites all %s with zeros, so nothing of the "
                "old contents remains. This can take hours on a large drive."
                % amount)

        # On the recovery stick there is no user to own it: only root.
        self.owner.setVisible(fs.native and not udisks.on_stick())

        keep = diskfs.can_keep_identity(fs, self.volume)
        self._show_advice(diskfs.advise(fs, self.disk, self.size, self.volume,
                                        keeps_identity=keep))
        self.start.setEnabled(problem is None)
        self.fit()

    def _show_advice(self, advice):
        if [c.advice for c in self.advice_cards] == advice:
            return
        for card in self.advice_cards:
            self.advice_box.removeWidget(card)
            # Hidden as well: deletion waits for the event loop, and until
            # then a card taken out of the layout would sit on top of
            # whatever takes its place.
            card.hide()
            card.deleteLater()
        self.advice_cards = [AdviceCard(each) for each in advice]
        for card in self.advice_cards:
            self.advice_box.addWidget(card)

    # ---- result -----------------------------------------------------------

    def build_plan(self):
        fs = self.selected_filesystem()
        if fs is None:
            return None
        return diskops.plan_format(
            self.disk, self.volume, fs.key, self.label.text(),
            quick=self.quick.isChecked(),
            table=self.table.currentData() if self.whole else None,
            take_ownership=self.owner.isChecked(), identity=self.identity,
            cluster=self.cluster.value())

    def _accept(self):
        try:
            plan = self.build_plan()
        except ValueError as exc:
            self.label_note.setText(str(exc))
            return
        if plan is None:
            return
        if ConfirmDialog(plan, self).exec() == QDialog.DialogCode.Accepted:
            self._plan = plan
            self.accept()

    def plan(self):
        """The confirmed plan, or None if the dialog was cancelled."""
        return self._plan


class ConfirmDialog(_FixedWidthDialog):
    """The last word before something is erased: what goes, and what happens.

    Cancel is the default button, so Enter never erases anything. A drive
    that is built into the computer wants a tick as well - it is the one a
    slip of the mouse is most expensive on.
    """

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("%s - are you sure?" % plan.title)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon = QIcon.fromTheme("dialog-warning")
        if icon.isNull():
            icon = self.style().standardIcon(
                QStyle.StandardPixmap.SP_MessageBoxWarning)
        badge = QLabel()
        badge.setPixmap(icon.pixmap(40, 40))
        top.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        heading = _wrapped("%s %s?" % (plan.title, plan.target))
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.1)
        heading.setFont(font)
        top.addWidget(heading, 1)
        layout.addLayout(top)

        if plan.loses:
            layout.addWidget(_wrapped("This will be permanently erased:"))
            layout.addWidget(self._listing(plan.loses, "•"))

        layout.addWidget(_wrapped("What will happen:"))
        layout.addWidget(self._listing(
            plan.steps, numbers=True))

        for advice in plan.advice:
            if advice.level == diskfs.WARNING:
                layout.addWidget(AdviceCard(advice))

        self.understood = QCheckBox(
            "I understand that this cannot be undone")
        self.understood.setVisible(plan.internal)
        layout.addWidget(self.understood)

        buttons = QDialogButtonBox()
        self.go = QPushButton(plan.confirm or (
            "Erase and Format" if plan.loses else plan.title))
        buttons.addButton(self.go, QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.cancel.setDefault(True)
        self.cancel.setAutoDefault(True)
        self.go.setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.understood.toggled.connect(self._refresh)
        self._refresh()
        self.cancel.setFocus()

    def _refresh(self, *_args):
        self.go.setEnabled(not self.plan.internal
                           or self.understood.isChecked())

    @staticmethod
    def _listing(lines, bullet="", numbers=False):
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(12, 0, 0, 0)
        column.setSpacing(3)
        for index, line in enumerate(lines, 1):
            prefix = "%d." % index if numbers else bullet
            row = QHBoxLayout()
            row.setSpacing(6)
            mark = QLabel(prefix)
            mark.setFixedWidth(18)
            row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(_wrapped(line), 1)
            column.addLayout(row)
        return holder


class RenameDialog(_FixedWidthDialog):
    """Change a volume's name without touching what is on it."""

    def __init__(self, disk, volume, parent=None, identity=None):
        super().__init__(parent)
        self.disk, self.volume = disk, volume
        self.identity = identity
        self._plan = None
        self.fs = diskfs.FILESYSTEMS.get(
            "ext4" if volume.fstype in ("ext2", "ext3") else volume.fstype)
        self.setWindowTitle("Rename")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        target = _wrapped(diskops.describe_target(disk, volume))
        font = QFont(target.font())
        font.setBold(True)
        target.setFont(font)
        layout.addWidget(target)

        self.label = QLineEdit(volume.label)
        self.label.selectAll()
        layout.addWidget(self.label)
        self.note = _wrapped(dim=True)
        layout.addWidget(self.note)
        if volume.mounted:
            layout.addWidget(_wrapped(
                "The volume is unmounted for a moment while its name is "
                "written, then mounted again. Close anything using it first.",
                dim=True))

        buttons = QDialogButtonBox()
        self.rename = QPushButton("Rename")
        self.rename.setDefault(True)
        buttons.addButton(self.rename, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.label.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args):
        if self.fs is None:
            self.note.setText("")
            self.rename.setEnabled(True)
            return
        label, problem = diskfs.validate_label(self.fs, self.label.text())
        if problem:
            self.note.setText(problem)
        else:
            note = "%d of %d characters" % (
                diskfs.label_length(self.fs, label), self.fs.label_max)
            if label != self.label.text().strip():
                note += " - will be stored as %s" % label
            self.note.setText(note)
        self.rename.setEnabled(problem is None
                               and label != self.volume.label)

    def build_plan(self):
        return diskops.plan_rename(self.disk, self.volume, self.label.text(),
                                   identity=self.identity)

    def _accept(self):
        try:
            self._plan = self.build_plan()
        except ValueError as exc:
            self.note.setText(str(exc))
            return
        self.accept()

    def plan(self):
        return self._plan
