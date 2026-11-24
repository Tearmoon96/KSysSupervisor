"""The disk utility's partition editor: its menu, dialogs and flows.

New partition table, create, delete, resize and move. What is possible and
what to warn about is decided in diskparts.py; how far a partition can
really shrink or grow is asked of the privileged helper, which asks the
filesystem's own tool.
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox,
                             QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QRadioButton, QSlider, QVBoxLayout,
                             QWidget)

from . import diskfs, diskops, diskparts, diskplan, disks
from .diskdialogs import (AdviceCard, ClusterField, ConfirmDialog,
                          _FixedWidthDialog, _wrapped)
from .diskmaintainui import ActionDialog, _heading
from .diskwidgets import tool_colour
from .widgets import Menu, tip

MIB = disks.MIB
BACKED_UP = "I have a copy of what matters on this volume"


class SizeEdit(QWidget):
    """A number of bytes between two limits: a slider and a figure.

    The figure is in the units drives are sold in - GB, or MB for small
    ones. Whatever is typed or dragged is held as bytes on a MiB boundary,
    and the two ends are exact: dragging to the end gives the limit itself,
    not the limit rounded to two decimals.
    """

    changed = pyqtSignal(int)
    STEPS = 1000

    def __init__(self, low, high, value, parent=None):
        super().__init__(parent)
        self.low = -(-int(low) // MIB) * MIB
        self.high = (int(high) // MIB) * MIB
        self.high = max(self.high, self.low)
        self._value = min(max(int(value), self.low), self.high)
        self._syncing = False
        self.unit = 1000 ** 3 if self.high >= 10 * 1000 ** 3 else 1000 ** 2
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, self.STEPS)
        row.addWidget(self.slider, 1)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(2 if self.unit == 1000 ** 3 else 0)
        self.spin.setSuffix(" GB" if self.unit == 1000 ** 3 else " MB")
        self.spin.setRange(self.low / self.unit, self.high / self.unit)
        self.spin.setSingleStep(1.0)
        self.spin.setKeyboardTracking(False)
        row.addWidget(self.spin)
        self.slider.valueChanged.connect(self._slid)
        self.spin.valueChanged.connect(self._typed)
        self._show()

    def value(self):
        return self._value

    def set_value(self, value):
        value = min(max(int(value), self.low), self.high)
        value = (value // MIB) * MIB if value != self.high else value
        self._value = max(value, self.low)
        self._show()
        self.changed.emit(self._value)

    def _show(self):
        self._syncing = True
        span = self.high - self.low
        self.slider.setValue(0 if span <= 0 else round(
            (self._value - self.low) * self.STEPS / span))
        self.spin.setValue(self._value / self.unit)
        self._syncing = False

    def _slid(self, step):
        if self._syncing:
            return
        if step >= self.STEPS:
            self.set_value(self.high)
        elif step <= 0:
            self.set_value(self.low)
        else:
            self.set_value(self.low
                           + (self.high - self.low) * step // self.STEPS)

    def _typed(self, figure):
        if self._syncing:
            return
        self.set_value(round(figure * self.unit))


class NewTableDialog(_FixedWidthDialog):
    def __init__(self, disk, parent=None, identity=None):
        super().__init__(parent)
        self.disk, self.identity = disk, identity
        self._plan = None
        self.setWindowTitle("New partition table")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("New partition table on %s"
                                  % diskops.describe_target(disk)))
        layout.addWidget(_wrapped(
            "Erases the drive and leaves it empty, with no partitions. To "
            "make a drive ready to use in one step, use Format on the whole "
            "drive instead.", dim=True))
        self.gpt = QRadioButton("GPT - for today's computers and any size "
                                "of drive")
        self.mbr = QRadioButton("MBR - for cameras, televisions and old "
                                "computers; four partitions, 2 TB at most")
        layout.addWidget(self.gpt)
        layout.addWidget(self.mbr)
        if not diskfs.table_allowed(disk, "dos"):
            self.mbr.setEnabled(False)
            self.mbr.setToolTip("MBR cannot describe a drive larger than "
                                "2 TB.")
        (self.mbr if diskfs.default_table(disk) == "dos"
         else self.gpt).setChecked(True)
        buttons = QDialogButtonBox()
        self.start = QPushButton("New Table...")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def build_plan(self):
        return diskparts.plan_table_new(
            self.disk, "gpt" if self.gpt.isChecked() else "dos",
            identity=self.identity)

    def confirm(self, plan):
        return ConfirmDialog(plan, self).exec() == \
            ConfirmDialog.DialogCode.Accepted

    def _accept(self):
        plan = self.build_plan()
        if self.confirm(plan):
            self._plan = plan
            self.accept()

    def plan(self):
        return self._plan


class NewPartitionDialog(_FixedWidthDialog):
    """Where a new partition goes, how large it is and what is on it."""

    CAPTION = 92

    def __init__(self, disk, parent=None, identity=None,
                 tools=diskfs.find_tool, gap=None):
        super().__init__(parent)
        self.disk, self.identity, self.tools = disk, identity, tools
        self._plan = None
        self.setWindowTitle("New partition")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("New partition on %s"
                                  % diskops.describe_target(disk)))

        self.gaps = list(disk.free)
        self.where = QComboBox()
        for free in self.gaps:
            self.where.addItem("%s unallocated, %s into the drive" % (
                disks.format_size(free.size), disks.format_size(free.start)))
        if gap is not None and gap in self.gaps:
            self.where.setCurrentIndex(self.gaps.index(gap))
        else:
            # The largest: the likeliest to be the one meant.
            self.where.setCurrentIndex(max(
                range(len(self.gaps)), key=lambda i: self.gaps[i].size))
        self.where_row = self._field("In:", self.where)
        self.where_holder = QWidget()
        self.where_holder.setLayout(self.where_row)
        self.where_row.setContentsMargins(0, 0, 0, 0)
        self.where_holder.setVisible(len(self.gaps) > 1)
        layout.addWidget(self.where_holder)

        self.size_holder = QVBoxLayout()
        layout.addLayout(self.size_holder)
        self.size = None

        self.filesystem = QComboBox()
        layout.addLayout(self._field("File system:", self.filesystem))
        self.works_with = _wrapped(dim=True)
        self.works_with.setIndent(self.CAPTION + 8)
        layout.addWidget(self.works_with)

        self.cluster = ClusterField(self.CAPTION, changed=self.fit)
        layout.addWidget(self.cluster.row)
        layout.addWidget(self.cluster.note)

        self.label = QLineEdit()
        self.label.setPlaceholderText("Optional")
        self.label.setClearButtonEnabled(True)
        layout.addLayout(self._field("Name:", self.label))
        self.label_note = _wrapped(dim=True)
        self.label_note.setIndent(self.CAPTION + 8)
        layout.addWidget(self.label_note)

        self.owner = QCheckBox("Make it writable by me")
        self.owner.setChecked(True)
        layout.addWidget(self.owner)

        self.advice_box = QVBoxLayout()
        self.advice_box.setSpacing(6)
        layout.addLayout(self.advice_box)
        self.advice_cards = []

        buttons = QDialogButtonBox()
        self.start = QPushButton("Create")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.where.currentIndexChanged.connect(self._gap_changed)
        self.filesystem.currentIndexChanged.connect(self._refresh)
        self.label.textChanged.connect(self._refresh)
        self._gap_changed()

    def _field(self, caption, widget):
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(caption)
        label.setFixedWidth(self.CAPTION)
        row.addWidget(label)
        row.addWidget(widget, 1)
        return row

    def gap(self):
        return self.gaps[self.where.currentIndex()]

    def _gap_changed(self, *_args):
        if self.size is not None:
            self.size_holder.removeWidget(self.size_row_widget)
            self.size_row_widget.hide()
            self.size_row_widget.deleteLater()
        free = self.gap()
        self.size = SizeEdit(MIB, free.size, free.size)
        self.size_row_widget = QWidget()
        row = self._field("Size:", self.size)
        row.setContentsMargins(0, 0, 0, 0)
        self.size_row_widget.setLayout(row)
        self.size_holder.addWidget(self.size_row_widget)
        self.size.changed.connect(self._size_changed)
        self._size_changed()

    def _size_changed(self, *_args):
        """Which filesystems fit depends on the size chosen."""
        keep = self.filesystem.currentData()
        self.choices = diskfs.choices(self.disk, self.size.value(),
                                      tools=self.tools)
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
        self.filesystem.addItem("None - leave it unformatted", "")
        available = [c.filesystem.key for c in self.choices if c.available]
        if keep is not None and (keep in available or keep == ""):
            self.filesystem.setCurrentIndex(self.filesystem.findData(keep))
        elif available:
            self.filesystem.setCurrentIndex(
                self.filesystem.findData(available[0]))
        else:
            self.filesystem.setCurrentIndex(self.filesystem.count() - 1)
        self.filesystem.blockSignals(False)
        self._refresh()

    def selected(self):
        key = self.filesystem.currentData()
        return diskfs.FILESYSTEMS.get(key) if key else None

    def _refresh(self, *_args):
        fs = self.selected()
        problem = None
        advice = []
        self.label.setEnabled(fs is not None)
        self.owner.setVisible(fs is not None and fs.native)
        self.cluster.setVisible(fs is not None)
        if fs is not None:
            self.cluster.update(fs.key, self.size.value(),
                                self.disk.logical_sector)
        if fs is None:
            self.works_with.setText(
                "The partition is made and left empty, for something else "
                "to format.")
            self.label_note.setText("")
        else:
            self.works_with.setText(fs.works_with)
            label, problem = diskfs.validate_label(fs, self.label.text())
            self.label_note.setText(problem or "%d of %d characters" % (
                diskfs.label_length(fs, label), fs.label_max))
            advice = [a for a in diskfs.advise(fs, self.disk,
                                               self.size.value(), None)
                      if "mounted automatically" not in a.title]
        if [c.advice for c in self.advice_cards] != advice:
            for card in self.advice_cards:
                self.advice_box.removeWidget(card)
                card.hide()
                card.deleteLater()
            self.advice_cards = [AdviceCard(each) for each in advice]
            for card in self.advice_cards:
                self.advice_box.addWidget(card)
        self.start.setEnabled(problem is None)
        self.fit()

    def build_plan(self):
        fs = self.selected()
        return diskparts.plan_create(
            self.disk, self.gap().start, self.size.value(),
            fs.key if fs else "", self.label.text(),
            owner=self.owner.isChecked(), identity=self.identity,
            cluster=self.cluster.value() if fs else None)

    def _accept(self):
        try:
            self._plan = self.build_plan()
        except ValueError as exc:
            self.label_note.setText(str(exc))
            return
        self.accept()

    def plan(self):
        return self._plan


class ResizeDialog(_FixedWidthDialog):
    """A new size for a partition, between what its contents need and what
    the drive has room for - both as the helper reported them."""

    def __init__(self, disk, volume, limits, parent=None, identity=None):
        super().__init__(parent)
        self.disk, self.volume, self.limits = disk, volume, limits
        self.identity = identity
        self._plan = None
        self.setWindowTitle("Resize")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Resize %s"
                                  % diskops.describe_target(disk, volume)))

        current = int(limits.get("size") or volume.size)
        smallest = limits.get("smallest")
        low = current if (limits.get("shrink") or smallest is None) \
            else int(smallest)
        high = current if limits.get("grow") else int(
            limits.get("largest") or current)
        self.low, self.high, self.current = low, max(high, current), current

        lines = ["Now: %s." % disks.format_size(current)]
        if low < current:
            lines.append("Smallest: %s - what its contents need, with a "
                         "little room to work in." % disks.format_size(low))
        else:
            lines.append("Cannot be made smaller: %s" % (
                limits.get("shrink") or "its contents fill it."))
        if self.high > current:
            lines.append("Largest: %s - up to what comes next on the "
                         "drive." % disks.format_size(self.high))
        else:
            lines.append("Cannot be made larger: %s" % (
                limits.get("grow") or "there is no unallocated space "
                "directly after it. Space before it can be used by moving "
                "the partition first."))
        layout.addWidget(_wrapped(" ".join(lines), dim=True))

        self.size = SizeEdit(self.low, self.high, current)
        layout.addWidget(self.size)
        self.outcome = _wrapped()
        layout.addWidget(self.outcome)

        self.advice_box = QVBoxLayout()
        self.advice_box.setSpacing(6)
        layout.addLayout(self.advice_box)
        self.advice_cards = []

        buttons = QDialogButtonBox()
        self.start = QPushButton("Resize...")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.size.changed.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args):
        size = self.size.value()
        change = size - self.current
        if change == 0:
            self.outcome.setText("No change.")
        else:
            self.outcome.setText("%s %s, to %s." % (
                "Grows by" if change > 0 else "Shrinks by",
                disks.format_size(abs(change)), disks.format_size(size)))
        advice = diskparts.resize_advice(self.disk, self.volume, change > 0)
        if [c.advice for c in self.advice_cards] != advice:
            for card in self.advice_cards:
                self.advice_box.removeWidget(card)
                card.hide()
                card.deleteLater()
            self.advice_cards = [AdviceCard(each) for each in advice]
            for card in self.advice_cards:
                self.advice_box.addWidget(card)
        self.start.setEnabled(change != 0)
        self.fit()

    def build_plan(self):
        return diskparts.plan_resize(self.disk, self.volume,
                                     self.size.value(),
                                     identity=self.identity)

    def confirm(self, plan):
        dialog = ActionDialog(plan, self, heading="Resize %s?" % plan.target,
                              accept="Resize", tick=BACKED_UP)
        return dialog.exec() == ActionDialog.DialogCode.Accepted

    def _accept(self):
        plan = self.build_plan()
        if self.confirm(plan):
            self._plan = plan
            self.accept()

    def plan(self):
        return self._plan


class MoveDialog(_FixedWidthDialog):
    """A new place for a partition, within the unallocated space on either
    side of it."""

    def __init__(self, disk, volume, limits, parent=None, identity=None):
        super().__init__(parent)
        self.disk, self.volume, self.limits = disk, volume, limits
        self.identity = identity
        self._plan = None
        self.setWindowTitle("Move")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Move %s"
                                  % diskops.describe_target(disk, volume)))
        self.current = int(limits.get("start") or volume.start or 0)
        size = int(limits.get("size") or volume.size)
        self.low = int(limits.get("lowest_start", self.current))
        self.high = int(limits.get("highest_end", self.current + size)) - size
        self.high = (self.high // MIB) * MIB
        layout.addWidget(_wrapped(
            "It can start anywhere from %s to %s into the drive; it starts "
            "at %s now. %s free before it, %s after."
            % (disks.format_size(self.low), disks.format_size(self.high),
               disks.format_size(self.current),
               disks.format_size(self.current - self.low),
               disks.format_size(max(0, self.high - self.current))),
            dim=True))
        self.place = SizeEdit(self.low, self.high, self.current)
        layout.addWidget(self.place)
        self.outcome = _wrapped()
        layout.addWidget(self.outcome)
        ends = QHBoxLayout()
        self.to_start = QPushButton("As Far Forward as Possible")
        self.to_end = QPushButton("As Far Back as Possible")
        self.to_start.clicked.connect(lambda: self.place.set_value(self.low))
        self.to_end.clicked.connect(lambda: self.place.set_value(self.high))
        self.to_start.setToolTip("Puts all the free space after the "
                                 "partition, where it can grow into it.")
        ends.addWidget(self.to_start)
        ends.addWidget(self.to_end)
        ends.addStretch(1)
        layout.addLayout(ends)

        for advice in diskparts.move_advice(disk, volume):
            layout.addWidget(AdviceCard(advice))

        buttons = QDialogButtonBox()
        self.start = QPushButton("Move...")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.place.changed.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args):
        shift = self.place.value() - self.current
        if shift == 0:
            self.outcome.setText("No change.")
        else:
            self.outcome.setText(
                "Moves %s towards the %s of the drive."
                % (disks.format_size(abs(shift)),
                   "start" if shift < 0 else "end"))
        self.start.setEnabled(shift != 0)

    def build_plan(self):
        return diskparts.plan_move(self.disk, self.volume,
                                   self.place.value(),
                                   identity=self.identity)

    def confirm(self, plan):
        dialog = ActionDialog(plan, self, heading="Move %s?" % plan.target,
                              accept="Move", tick=BACKED_UP)
        return dialog.exec() == ActionDialog.DialogCode.Accepted

    def _accept(self):
        plan = self.build_plan()
        if self.confirm(plan):
            self._plan = plan
            self.accept()

    def plan(self):
        return self._plan


class LostPartitionsDialog(_FixedWidthDialog):
    """Partitions a search found without an entry in the table."""

    def __init__(self, disk, found, parent=None):
        super().__init__(parent)
        self.found = list(found)
        self.setWindowTitle("Lost partitions")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading(
            "%d lost partition%s found on %s" % (
                len(found), "" if len(found) == 1 else "s", disk.title)))
        layout.addWidget(_wrapped(
            "Each of these is a filesystem that is still on the drive but "
            "has no entry in the partition table, which is why it does not "
            "show up. Putting the entry back makes it appear again with "
            "everything in it. Only the table is written.", dim=True))
        self.boxes = []
        for entry in self.found:
            box = QCheckBox(diskparts.describe_found(entry))
            box.setChecked(True)
            box.toggled.connect(self._refresh)
            self.boxes.append(box)
            layout.addWidget(box)
        buttons = QDialogButtonBox()
        self.start = QPushButton("Put Back the Ticked Ones")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self, *_args):
        self.start.setEnabled(bool(self.chosen()))

    def chosen(self):
        return [entry for entry, box in zip(self.found, self.boxes)
                if box.isChecked()]


class PartitionActions:
    """The Partitions menu of the disk utility window."""

    PARTITION_ENTRIES = (
        ("new_partition_action", "New Partition...", diskops.PART_NEW,
         "Make a partition in unallocated space, with a filesystem on it."),
        ("resize_action", "Resize...", diskops.PART_RESIZE,
         "Make this partition larger or smaller, its filesystem with it. "
         "The files on it stay."),
        ("move_action", "Move...", diskops.PART_MOVE,
         "Move this partition into the unallocated space beside it - to "
         "put free space where another partition can grow into it."),
        ("delete_partition_action", "Delete...", diskops.PART_DELETE,
         "Remove this partition, turning it into unallocated space."),
        None,
        ("find_lost_action", "Search for Lost Partitions...",
         diskops.FIND_LOST,
         "For a drive that shows as empty or has a partition missing: look "
         "for filesystems that are still there without an entry in the "
         "table, and put the entries back. The files are not touched."),
        ("new_table_action", "New Partition Table...", diskops.NEW_TABLE,
         "Erase the drive and give it a new, empty partition table."),
        ("resume_move_action", "Finish Interrupted Move...", None,
         "A partition move was cut short. Carry it on from where it "
         "stopped."),
    )

    def _build_partitions(self):
        self.partitions_button = self._button(
            "Partitions", ("partitionmanager", "drive-partition",
                           "view-list-details"), lambda: None)
        menu = Menu(self.partitions_button)
        menu.setToolTipsVisible(True)
        slots = {"new_partition_action": self.new_partition,
                 "resize_action": self.resize_selected,
                 "move_action": self.move_selected,
                 "delete_partition_action": self.delete_selected,
                 "new_table_action": self.new_table,
                 "find_lost_action": self.find_lost,
                 "resume_move_action": self.resume_move}
        for entry in self.PARTITION_ENTRIES:
            if entry is None:
                menu.addSeparator()
                continue
            name, title, _action, _text = entry
            action = menu.addAction(title)
            setattr(self, name, action)
            action.triggered.connect(
                lambda _checked=False, slot=slots[name]: slot())
        self.partitions_button.setMenu(menu)
        return self.partitions_button

    def _pending_moves(self):
        return diskparts.pending_moves()

    def _update_partitions(self, disk, volume, locked):
        colour = tool_colour()
        for entry in self.PARTITION_ENTRIES:
            if entry is None:
                continue
            name, title, action, text = entry
            item = getattr(self, name)
            if action is None:
                pending = self._pending_moves()
                item.setVisible(bool(pending))
                answer = diskops.YES
            else:
                answer = diskops.availability(action, disk, volume)
            item.setEnabled(bool(answer) and not locked)
            title = title.rstrip(".")
            if answer:
                item.setToolTip(tip(title, text, colour))
            else:
                item.setToolTip(tip("%s is not available" % title,
                                    answer.reason, colour))
        self.partitions_button.setEnabled(disk.has_media)

    def _partition_done(self, plan):
        def done(result):
            self.status.setText(diskparts.done_text(plan, result))
            wanted = plan.remount or result.get("was_mounted")
            if wanted and result.get("device") \
                    and plan.request["op"] != "part_delete":
                self._mount_after(result["device"])
        return done

    def new_table(self):
        disk, _volume = self._target(None)
        if disk is None or not diskops.availability(diskops.NEW_TABLE, disk):
            return
        dialog = NewTableDialog(disk, self,
                                identity=self.identities.get(disk.name))
        if self.show_dialog(dialog) and dialog.plan() is not None:
            self.run_plan(dialog.plan(),
                          done=self._partition_done(dialog.plan()))

    def new_partition(self):
        disk, _volume = self._target(None)
        if disk is None or not diskops.availability(diskops.PART_NEW, disk):
            return
        dialog = NewPartitionDialog(disk, self,
                                    identity=self.identities.get(disk.name))
        if self.show_dialog(dialog) and dialog.plan() is not None:
            self.run_plan(dialog.plan(),
                          done=self._partition_done(dialog.plan()))

    def delete_selected(self):
        disk, volume = self._target(diskops.PART_DELETE)
        if disk is None:
            return
        plan = diskparts.plan_delete(disk, volume,
                                     identity=self._identity(volume))
        if self.show_dialog(ConfirmDialog(plan, self)):
            self.run_plan(plan, done=self._partition_done(plan))

    def _with_limits(self, action, then):
        """Ask the helper what the partition can be changed to, then go on
        with the dialog that needs to know."""
        disk, volume = self._target(action)
        if disk is None:
            return
        queue = getattr(self, "queue", None)
        if queue is not None and (volume.pending or queue.touches(disk.name)):
            # On the recovery stick, with changes prepared for this drive:
            # the room is the picture's. A partition that is not on the
            # drive yet, or whose contents will be new, is not asked about.
            if volume.pending in ("new", "restored", "formatted", "copied"):
                then(disk, volume, diskplan.limits(disk, volume))
                return
            self.run_plan(
                diskparts.plan_limits(disk, volume, self._identity(volume)),
                done=lambda real: then(disk, volume,
                                       diskplan.limits(disk, volume, real)))
            return
        self.run_plan(
            diskparts.plan_limits(disk, volume, self._identity(volume)),
            done=lambda limits: then(disk, volume, limits))

    def resize_selected(self):
        self._with_limits(diskops.PART_RESIZE, self._offer_resize)

    def _offer_resize(self, disk, volume, limits):
        if limits.get("grow") and limits.get("shrink"):
            self.notify("Resize", "This volume cannot be resized.\n\n%s"
                        % limits["grow"])
            return
        dialog = ResizeDialog(disk, volume, limits, self,
                              identity=self._identity(volume))
        if dialog.low >= dialog.high:
            self.notify(
                "Resize", "There is nothing to change: the volume is as "
                "small as its contents allow, and there is no unallocated "
                "space directly after it to grow into.", warning=False)
            return
        if self.show_dialog(dialog) and dialog.plan() is not None:
            self.run_plan(dialog.plan(),
                          done=self._partition_done(dialog.plan()))

    def move_selected(self):
        self._with_limits(diskops.PART_MOVE, self._offer_move)

    def _offer_move(self, disk, volume, limits):
        dialog = MoveDialog(disk, volume, limits, self,
                            identity=self._identity(volume))
        if dialog.low >= dialog.high:
            self.notify("Move", "There is no room to move this partition "
                        "into.", warning=False)
            return
        if self.show_dialog(dialog) and dialog.plan() is not None:
            self.run_plan(dialog.plan(),
                          done=self._partition_done(dialog.plan()))

    def find_lost(self):
        disk, _volume = self._target(None)
        if disk is None or not diskops.availability(diskops.FIND_LOST, disk):
            return
        self.run_plan(
            diskparts.plan_search(disk, self.identities.get(disk.name)),
            done=lambda result: self._offer_lost(disk, result))

    def _offer_lost(self, disk, result):
        found = list(result.get("found") or ())
        if not found:
            self.notify(
                "Lost partitions", "No lost partition was found on %s. "
                "If files are missing, search for them directly: Recover "
                "Files." % disk.title, warning=False)
            return
        dialog = LostPartitionsDialog(disk, found, self)
        if not self.show_dialog(dialog):
            return
        todo = dialog.chosen()
        restored = []

        def next_one(result=None):
            if result is not None:
                restored.append(result.get("device", ""))
            if not todo:
                self.status.setText(
                    "Put back %d partition%s: %s." % (
                        len(restored), "" if len(restored) == 1 else "s",
                        ", ".join(restored)))
                return
            entry = todo.pop(0)
            # Each restore changes the table: what was read of the drive
            # with the list is still the same drive.
            self.run_plan(diskparts.plan_restore(
                disk, entry, self.identities.get(disk.name)), done=next_one)
        next_one()

    def resume_move(self):
        if self._job is not None:
            return
        pending = self._pending_moves()
        if not pending:
            return
        record = pending[0]
        plan = diskparts.plan_resume(record)
        dialog = ActionDialog(
            plan, self, heading="Finish the interrupted move?",
            accept="Finish the Move")
        dialog.layout().insertWidget(1, _wrapped(
            diskparts.describe_move(record)))
        if self.show_dialog(dialog):
            self.run_plan(plan, done=self._partition_done(plan))

    def warn_about_pending_moves(self):
        """Said once when the window opens: a half-moved partition is the
        one thing here that cannot wait."""
        pending = self._pending_moves()
        if pending:
            self.status.setText(
                "%s Use Partitions > Finish Interrupted Move."
                % diskparts.describe_move(pending[0]))
