"""The disk utility's file recovery: setting a search up, and its results.

The results are one set of files shown two ways - in the folders they were
in, or by kind - with the same ticks on both. What was found, in what state
it is and how it is copied out is diskrecover.py's; the reading of the drive
is the privileged helper's.
"""

import os
import time

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QImageReader, QPalette, QPixmap
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                             QDialogButtonBox, QFileDialog, QGridLayout,
                             QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMessageBox, QPushButton,
                             QRadioButton,
                             QSplitter, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from . import APP_NAME, diskops, diskrecover, diskrepair, disks
from .diskdialogs import AdviceCard, _FixedWidthDialog, _wrapped
from .diskmaintainui import _heading
from .diskwidgets import tool_colour
from .widgets import Menu, paint_backdrop, tip

NODE = Qt.ItemDataRole.UserRole
#: How many rows of one folder are put in the list at a time. A folder of
#: a hundred thousand found files opens at once all the same.
BATCH = 500
PREVIEW = 260


def _advice_cards(layout, advice):
    """Warnings first, then notes, each showing only its title until
    opened."""
    order = sorted(advice, key=lambda a: a.level != diskrecover.WARNING)
    for item in order:
        layout.addWidget(AdviceCard(item, collapsible=True))


class RecoverDialog(_FixedWidthDialog):
    """Quick Scan or Deep Scan, and how deep.

    Nothing about where recovered files go is asked here: that is chosen
    when they are recovered. A Deep Scan saves what it finds by content as
    it finds it, so it alone needs a folder on another drive now.

    `deep_only`: opened from the results of a Quick Scan, to go deeper.
    """

    def __init__(self, disk, volume, parent=None, identity=None, folder=None,
                 problem=diskrecover.folder_problem, deep_only=False,
                 scratch_base=None):
        super().__init__(parent)
        self.disk = disk
        self.volume = None if volume is None or volume.kind == "whole" \
            else volume
        self.identity = identity
        self._problem = problem
        self._scratch_base = scratch_base or diskrecover.scratch_base()
        self.folder_path = None
        self._plan = None
        self.setWindowTitle("Recover files")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Recover files from %s"
                                  % diskops.describe_target(disk,
                                                            self.volume)))
        layout.addWidget(_wrapped(
            "The drive is only read. Where recovered files go is chosen "
            "when you recover them.", dim=True))

        listable = diskrecover.can_list(disk, self.volume)
        size = disk.size if self.volume is None else self.volume.size
        self.quick = QRadioButton("Quick Scan")
        self.deep = QRadioButton("Deep Scan")
        group = QButtonGroup(self)
        group.addButton(self.quick)
        group.addButton(self.deep)
        layout.addWidget(self.quick)
        note = _wrapped(
            "Deleted files, with their names and folders. Takes seconds."
            if listable else
            "Not possible here: nothing on this drive keeps a record of "
            "deleted files.", dim=True)
        note.setIndent(24)
        layout.addWidget(note)
        layout.addWidget(self.deep)
        note = _wrapped(
            "Reads all %s: the names and folders left after a format, "
            "then files recognisable by their content. Takes as long as "
            "the drive is large." % disks.format_size(size), dim=True)
        note.setIndent(24)
        layout.addWidget(note)
        self.quick.setEnabled(listable and not deep_only)
        (self.quick if listable and not deep_only
         else self.deep).setChecked(True)
        self.deep_only = deep_only

        # How deep: only while Deep Scan is chosen.
        self.options = QWidget()
        options = QVBoxLayout(self.options)
        options.setContentsMargins(24, 0, 0, 0)
        options.setSpacing(6)
        self.all_kinds = QRadioButton("All recognisable files")
        self.some_kinds = QRadioButton("Only these kinds:")
        kinds = QButtonGroup(self)
        kinds.addButton(self.all_kinds)
        kinds.addButton(self.some_kinds)
        self.all_kinds.setChecked(True)
        options.addWidget(self.all_kinds)
        options.addWidget(self.some_kinds)
        grid = QGridLayout()
        grid.setContentsMargins(24, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        self.kinds = {}
        for index, (key, title, _families) in enumerate(diskrecover.KINDS):
            box = QCheckBox(title)
            box.setChecked(key in diskrecover.DEFAULT_KINDS)
            self.kinds[key] = box
            grid.addWidget(box, index // 3, index % 3)
        options.addLayout(grid)
        self.skip_damaged = QCheckBox("Skip damaged files")
        self.skip_damaged.setChecked(True)
        self.skip_damaged.setToolTip(tip(
            "Skip damaged files", "Most files that come back broken were "
            "stored in pieces. Untick to keep them anyway, marked "
            "damaged: half a photo can still be worth having.",
            tool_colour()))
        options.addWidget(self.skip_damaged)
        self.loose = QCheckBox("Keep unrecognised data")
        self.loose.setToolTip(tip(
            "Keep unrecognised data", "Also keep what only loosely looks "
            "like a file. Finds a little more; most of it is junk, and it "
            "takes a lot of space.", tool_colour()))
        options.addWidget(self.loose)
        self.loose_note = _wrapped(
            "Most of it will be junk, and it takes a lot of space.",
            dim=True)
        self.loose_note.setIndent(24)
        options.addWidget(self.loose_note)
        layout.addWidget(self.options)

        self.folder_label = QLabel("Keep found files in:")
        layout.addWidget(self.folder_label)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.folder = QLineEdit(folder or os.path.expanduser("~"))
        row.addWidget(self.folder, 1)
        self.browse = QPushButton("Browse...")
        self.browse.clicked.connect(self._browse)
        row.addWidget(self.browse)
        layout.addLayout(row)
        self.folder_note = _wrapped(dim=True)
        layout.addWidget(self.folder_note)

        _advice_cards(layout, diskrecover.scan_advice(disk, self.volume))

        buttons = QDialogButtonBox()
        self.start = QPushButton("Start")
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for button in (self.quick, self.deep, self.all_kinds,
                       self.some_kinds, self.loose, *self.kinds.values()):
            button.toggled.connect(self._refresh)
        self.folder.textChanged.connect(self._refresh)
        self._refresh()

    def chosen_kinds(self):
        if not self.some_kinds.isChecked():
            return []
        return [key for key, box in self.kinds.items() if box.isChecked()]

    def needs_folder(self):
        """A Deep Scan saves what it finds, so it needs a place on another
        drive. A Quick Scan does too, where the private folder in memory
        it would otherwise use is on the drive being searched."""
        return self.deep.isChecked() or bool(diskrecover.on_drive(
            self._scratch_base, self.disk))

    def pick(self, current):
        return QFileDialog.getExistingDirectory(
            self, "Keep found files in", current)

    def _browse(self):
        chosen = self.pick(self.folder.text())
        if chosen:
            self.folder.setText(chosen)

    def _refresh(self, *_args):
        deep = self.deep.isChecked()
        self.options.setEnabled(deep)
        # Only there when they apply: a Quick Scan has nothing to set.
        self.options.setVisible(deep)
        some = self.some_kinds.isChecked()
        for box in self.kinds.values():
            box.setEnabled(deep and some)
        self.loose.setEnabled(deep and not some)
        self.loose_note.setVisible(self.loose.isChecked() and not some)
        needs = self.needs_folder()
        for widget in (self.folder_label, self.folder, self.browse,
                       self.folder_note):
            widget.setVisible(needs)
        problem = ""
        if needs:
            folder = self.folder.text().strip()
            problem = self._problem(folder, self.disk)
            if problem:
                self.folder_note.setText(problem)
            else:
                try:
                    room = os.statvfs(folder)
                    self.folder_note.setText(
                        "%s free there. A folder named '%s ...' is made "
                        "in it." % (disks.format_size(
                            room.f_bavail * room.f_frsize),
                            diskrecover.WORK_PREFIX))
                except OSError:
                    self.folder_note.setText("")
        if deep and some and not self.chosen_kinds():
            problem = problem or "Tick at least one kind."
        self.start.setText("Start Deep Scan" if deep else "Start Quick Scan")
        self.start.setEnabled(not problem)
        self.fit()

    def _accept(self):
        try:
            if self.needs_folder():
                self.folder_path = diskrecover.work_folder(
                    self.folder.text().strip())
            else:
                self.folder_path = diskrecover.scratch_folder(
                    self._scratch_base)
        except OSError as exc:
            self.folder_note.setText("The folder could not be made: %s"
                                     % (exc.strerror or exc))
            self.folder_note.setVisible(True)
            return
        deep = self.deep.isChecked()
        self._plan = diskrecover.plan_scan(
            self.disk, self.volume, self.folder_path,
            quick=not self.deep_only, deep=deep, kinds=self.chosen_kinds(),
            keep_damaged=not self.skip_damaged.isChecked(),
            loose=self.loose.isChecked(), identity=self.identity)
        self.accept()

    def plan(self):
        return self._plan

    def is_deep(self):
        return self.deep.isChecked()


def _when(stamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) \
        if stamp else ""


class RecoveryResults(QWidget):
    """What a search found: pick, look, recover, or search deeper.

    `recover`, `deeper` and `save` are called with this window when the user
    asks for those; they do the work and call refresh() or set_entries().
    `path`: the search file this is kept in.
    """

    def __init__(self, record, entries, recover=None, deeper=None, save=None,
                 path=None, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.record, self.entries = record, entries
        self.path = path
        self._recover = recover
        self._deeper = deeper
        self._save = save
        self._busy = False
        self._filling = False
        self._watched = None             # a Deep Scan's folder, while it runs
        self._waiting = []               # what it has found, not yet shown
        self._watch = QTimer(self)
        self._watch.setInterval(3000)
        self._watch.timeout.connect(self._look)
        self.setWindowTitle("%s - Files found on %s" % (
            APP_NAME, record.get("what") or record.get("device", "")))
        self.resize(980, 640)

        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        self.summary = _wrapped()
        outer.addWidget(self.summary)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.folders_view = QRadioButton("Folders")
        self.folders_view.setToolTip(tip(
            "Folders", "The files in the folders they were in.",
            tool_colour()))
        self.types_view = QRadioButton("By type")
        self.types_view.setToolTip(tip(
            "By type", "Pictures, video, documents and so on, whatever "
            "folder each was in.", tool_colour()))
        group = QButtonGroup(self)
        for button in (self.folders_view, self.types_view):
            group.addButton(button)
            bar.addWidget(button)
        named = any(e.source != diskrecover.FOUND for e in entries)
        (self.folders_view if named else self.types_view).setChecked(True)
        bar.addSpacing(12)
        self.more = QPushButton()
        self.more.setVisible(False)
        self.more.clicked.connect(self.show_new)
        bar.addWidget(self.more)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find by name")
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 1)
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size", "Changed", "State"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        # The name takes what is left; the others are as wide as what is in
        # them, never cut short ("2026-11-10 2...").
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        split.addWidget(self.tree)

        side = QWidget()
        column = QVBoxLayout(side)
        column.setContentsMargins(8, 0, 0, 0)
        self.picture = QLabel()
        self.picture.setFixedSize(PREVIEW, PREVIEW)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        column.addWidget(self.picture)
        self.about = _wrapped()
        self.about.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.about)
        column.addStretch(1)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        outer.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.chosen = QLabel()
        bottom.addWidget(self.chosen, 1)
        self.deep_button = QPushButton("Deep Scan...")
        self.deep_button.setToolTip(tip(
            "Deep Scan", "Read the whole drive for the names left after a "
            "format and for files recognisable by their content. What it "
            "finds is added here.", tool_colour()))
        self.deep_button.clicked.connect(self.deeper)
        bottom.addWidget(self.deep_button)
        self.save_button = QPushButton("Save Search...")
        self.save_button.setToolTip(tip(
            "Save the search", "One file with everything found. Open it "
            "again later, or on another computer, without searching the "
            "drive again.", tool_colour()))
        self.save_button.clicked.connect(self.save)
        bottom.addWidget(self.save_button)
        self.repair_button = QPushButton("Repair the Damaged Ones")
        self.repair_button.setToolTip(tip(
            "Repair", "Rebuild damaged documents, PDFs and pictures from "
            "what is left of them. Each repaired file is saved beside the "
            "original, which is not changed.", tool_colour()))
        self.repair_button.clicked.connect(self.repair)
        bottom.addWidget(self.repair_button)
        self.open_button = QPushButton("Open the Folder")
        self.open_button.clicked.connect(self.open_folder)
        bottom.addWidget(self.open_button)
        self.go = QPushButton("Recover the Ticked Files...")
        self.go.clicked.connect(self.recover)
        bottom.addWidget(self.go)
        outer.addLayout(bottom)

        self.folders_view.toggled.connect(self.rebuild)
        self.search.textChanged.connect(self.rebuild)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemChanged.connect(self._ticked)
        self.tree.currentItemChanged.connect(self._show)
        self.tree.itemClicked.connect(self._clicked)
        self.rebuild()

    def paintEvent(self, event):
        paint_backdrop(self)
        super().paintEvent(event)

    # ---- the list -----------------------------------------------------------

    def shown(self):
        return diskrecover.matching(self.entries, self.search.text())

    def rebuild(self, *_args):
        shown = self.shown()
        build = diskrecover.by_folder if self.folders_view.isChecked() \
            else diskrecover.by_type
        self.root = build(shown)
        self._filling = True
        self.tree.clear()
        self._fill(self.tree.invisibleRootItem(), self.root)
        # A single top folder is opened: nobody wants to click through it.
        if self.tree.topLevelItemCount() <= 6:
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                if item.data(0, NODE) is not None \
                        and item.data(0, NODE).is_folder:
                    item.setExpanded(True)
        self._filling = False
        deleted = sum(1 for e in self.entries
                      if e.source == diskrecover.DELETED)
        lost = sum(1 for e in self.entries if e.source == diskrecover.LOST)
        found = len(self.entries) - deleted - lost
        parts = []
        if deleted:
            parts.append("%d deleted files still on record" % deleted)
        if lost:
            parts.append("%d files from before the format" % lost)
        if found:
            parts.append("%d files found by content" % found)
        self.summary.setText(
            ("%s. Tick what you want back." % " and ".join(parts))
            if parts else
            "Nothing was found. If the files were deleted from an SSD, or "
            "the drive has been written to since, they are gone; otherwise "
            "a Deep Scan may find them by their content.")
        self._count()

    @staticmethod
    def _ordered(node):
        return sorted(node.children.values(),
                      key=lambda n: (not n.is_folder, n.name.lower()))

    def _fill(self, item, node, start=0):
        children = self._ordered(node)
        for child in children[start:start + BATCH]:
            item.addChild(self._item(child))
        if len(children) > start + BATCH:
            more = QTreeWidgetItem(["Show %d more..."
                                    % (len(children) - start - BATCH)])
            more.setData(0, NODE, ("more", node, start + BATCH))
            more.setForeground(0, self.palette().placeholderText())
            item.addChild(more)

    def _item(self, node):
        entry = node.entry
        if entry is None:
            item = QTreeWidgetItem([
                node.name, disks.format_size(node.size), "",
                "%d file%s" % (node.files, "" if node.files == 1 else "s")])
            # Filled when opened. The arrow is asked for outright: a stand-in
            # row that is swapped for the real ones while the view is in the
            # middle of opening the folder leaves the view drawing the old
            # rows.
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        else:
            item = QTreeWidgetItem([node.name, disks.format_size(entry.size),
                                    _when(entry.mtime), entry.chance])
        item.setData(0, NODE, node)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, self._state(node))
        return item

    @staticmethod
    def _state(node):
        return (Qt.CheckState.Unchecked, Qt.CheckState.PartiallyChecked,
                Qt.CheckState.Checked)[node.state()]

    def _expanded(self, item):
        node = item.data(0, NODE)
        if node is None or isinstance(node, tuple) or not node.is_folder:
            return
        if item.childCount() == 0:
            self._filling = True
            self._fill(item, node)
            self._filling = False

    def _clicked(self, item, _column):
        marker = item.data(0, NODE)
        if isinstance(marker, tuple) and marker[0] == "more":
            parent = item.parent() or self.tree.invisibleRootItem()
            self._filling = True
            parent.removeChild(item)
            self._fill(parent, marker[1], marker[2])
            self._filling = False

    def _ticked(self, item, column):
        if self._filling or column != 0:
            return
        node = item.data(0, NODE)
        if node is None or isinstance(node, tuple):
            return
        node.choose(item.checkState(0) == Qt.CheckState.Checked)
        self._filling = True
        self._sync_down(item)
        parent = item.parent()
        while parent is not None:
            parent.setCheckState(0, self._state(parent.data(0, NODE)))
            parent = parent.parent()
        self._filling = False
        self._count()

    def _sync_down(self, item):
        for index in range(item.childCount()):
            child = item.child(index)
            node = child.data(0, NODE)
            if node is None or isinstance(node, tuple):
                continue
            child.setCheckState(0, self._state(node))
            self._sync_down(child)

    def _count(self):
        chosen = [e for e in self.entries if e.chosen]
        self.chosen.setText(
            "%d of %d files ticked, %s" % (
                len(chosen), len(self.entries),
                disks.format_size(sum(e.size for e in chosen)))
            if self.entries else "")
        self.go.setEnabled(bool(chosen) and self._recover is not None
                           and not self._busy)
        self.go.setToolTip(tip(
            "Recover", "Waits until the Deep Scan has finished: the drive "
            "is read by one thing at a time.", tool_colour())
            if self._busy else "")
        self.repair_button.setVisible(
            any(diskrepair.repairable(e) for e in self.entries))
        self.deep_button.setVisible(
            "deep" not in (self.record.get("scans") or ())
            and self._deeper is not None)
        self.deep_button.setEnabled(not self._busy)
        self.save_button.setEnabled(self._save is not None)
        self.open_button.setEnabled(bool(self._folder()))

    # ---- one file -----------------------------------------------------------

    def _show(self, item, _previous=None):
        node = item.data(0, NODE) if item is not None else None
        self.picture.clear()
        if node is None or isinstance(node, tuple):
            self.about.setText("")
            return
        if node.is_folder:
            self.about.setText("%s\n%d files, %s" % (
                node.name, node.files, disks.format_size(node.size)))
            return
        entry = node.entry
        lines = [entry.name, disks.format_size(entry.size)]
        if entry.mtime:
            lines.append("Changed %s" % _when(entry.mtime))
        if entry.source != diskrecover.FOUND and entry.folder:
            lines.append("Was in %s" % entry.folder)
        if entry.file:
            if entry.status == diskrecover.UNKNOWN:
                entry.status, entry.detail = diskrecover.inspect_file(
                    entry.file)
            if entry.category == "Pictures":
                reader = QImageReader(entry.file)
                reader.setAutoTransform(True)
                image = reader.read()
                if not image.isNull():
                    self.picture.setPixmap(QPixmap.fromImage(image).scaled(
                        PREVIEW, PREVIEW, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                elif entry.status == diskrecover.INTACT:
                    entry.status = diskrecover.DAMAGED
                    entry.detail = ("It has its beginning and its end, but "
                                    "what is between cannot be shown.")
            lines.append("")
            lines.append(diskrecover.STATUS_TITLES[entry.status])
            lines.append(entry.detail)
            item.setText(3, entry.chance if entry.source ==
                         diskrecover.DELETED and not entry.file
                         else diskrecover.STATUS_TITLES[entry.status])
        else:
            self.picture.setText("Recover it to see it")
            lines.append("")
            lines.append(entry.chance)
        self.about.setText("\n".join(lines))

    # ---- a Deep Scan running ------------------------------------------------

    def set_busy(self, busy, folder=None):
        """A Deep Scan started (with the folder it saves into) or ended."""
        self._busy = busy
        self._watched = folder if busy else None
        self._waiting = []
        if busy and folder:
            self._watch.start()
        else:
            self._watch.stop()
            self.more.setVisible(False)
        self._count()

    def _look(self):
        """What the running Deep Scan has saved so far, offered with a
        button rather than put in the list at once: the list does not move
        under the user's hands."""
        if not self._watched:
            return
        known = {e.file for e in self.entries if e.file}
        self._waiting = [e for e in diskrecover.found_files(self._watched)
                         if e.file not in known]
        self.more.setText("Show %d new file%s" % (
            len(self._waiting), "" if len(self._waiting) == 1 else "s"))
        self.more.setVisible(bool(self._waiting))

    def show_new(self):
        self.entries = self.entries + self._waiting
        self._waiting = []
        self.more.setVisible(False)
        self.rebuild()

    def set_entries(self, record, entries):
        """After a Deep Scan: everything found, at once."""
        self.record, self.entries = record, entries
        self.set_busy(False)
        self.rebuild()

    # ---- acting -------------------------------------------------------------

    def recover(self):
        if self._recover is not None and not self._busy:
            self._recover(self)

    def deeper(self):
        if self._deeper is not None and not self._busy:
            self._deeper(self)

    def save(self):
        if self._save is not None:
            self._save(self)

    def _folder(self):
        """The folder worth opening: the last one recovered into, else the
        Deep Scan's."""
        for path in reversed(self.record.get("recovered") or ()):
            if os.path.isdir(path):
                return path
        work = self.record.get("work") or ""
        return work if work and os.path.isdir(work) else ""

    def refresh(self):
        """After files were copied out: show their state."""
        self._filling = True
        self.rebuild()

    def tell(self, text):
        """A message to acknowledge; its own method for the tests."""
        QMessageBox.information(self, "Recover files", text)

    def repair(self):
        """Rebuild what can be rebuilt of the files that came back
        damaged."""
        self.repair_button.setEnabled(False)
        done = diskrepair.repair_all(
            self.entries, lambda a, b: QApplication.processEvents())
        self.repair_button.setEnabled(True)
        fixed = [entry for entry, new, _said in done if new]
        self.refresh()
        self.tell(
            "%d of %d damaged files could be repaired. Each repaired copy "
            "is beside its original, with '(repaired)' in its name."
            % (len(fixed), len(done)) if done else
            "None of the damaged files is of a kind that can be repaired.")

    def open_folder(self):
        target = self._folder()
        if target:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))


class ArraysDialog(_FixedWidthDialog):
    """The RAID arrays found on the connected drives; pick one to open."""

    def __init__(self, arrays, parent=None):
        super().__init__(parent)
        self.arrays = list(arrays)
        self.setWindowTitle("RAID arrays")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("%d RAID array%s found" % (
            len(arrays), "" if len(arrays) == 1 else "s")))
        layout.addWidget(_wrapped(
            "An array is opened read-only: its drives are read and put "
            "together in memory, and nothing is written to any of them. "
            "It then appears in the list as one drive.", dim=True))
        self.buttons = []
        first = None
        for array in self.arrays:
            line, usable, why = diskrecover.describe_array(array)
            button = QRadioButton(line)
            button.setEnabled(usable and not array.get("device"))
            self.buttons.append(button)
            layout.addWidget(button)
            if why:
                note = _wrapped(why, dim=True)
                note.setIndent(24)
                layout.addWidget(note)
            if first is None and button.isEnabled():
                first = button
        if first is not None:
            first.setChecked(True)
        box = QDialogButtonBox()
        self.start = QPushButton("Open Read-Only")
        self.start.setEnabled(first is not None)
        box.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def chosen(self):
        for array, button in zip(self.arrays, self.buttons):
            if button.isChecked() and button.isEnabled():
                return array
        return None


class GuessDialog(_FixedWidthDialog):
    """Choose the drives of an array whose own record of it is lost."""

    def __init__(self, drives, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Put drives together as an array")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_heading("Which drives were one array?"))
        layout.addWidget(_wrapped(
            "For drives from a NAS, a RAID enclosure or a controller that "
            "is gone, when Find RAID Arrays finds nothing on them. The "
            "drives are read, never written: how they fit together is "
            "worked out from the filesystem on them (NTFS, FAT32, exFAT or "
            "ext4). The order you tick them in does not matter.", dim=True))
        self.drives = []
        for disk in drives:
            problem = diskrecover.member_problem(disk)
            box = QCheckBox("%s - %s, %s" % (disk.name, disk.title,
                                              disks.format_size(disk.size)))
            box.setEnabled(not problem)
            if problem:
                box.setToolTip(tip(disk.title, problem, tool_colour()))
            box.toggled.connect(self._changed)
            self.drives.append((disk, box))
            layout.addWidget(box)
        layout.addWidget(_heading("What it was"))
        self.levels = QButtonGroup(self)
        for key, text in (("auto", "Not sure - work it out"),
                          ("raid0", "RAID 0 (striped)"),
                          ("raid5", "RAID 5 (striped, with parity)")):
            button = QRadioButton(text)
            button.setProperty("level", key)
            self.levels.addButton(button)
            layout.addWidget(button)
            if key == "auto":
                button.setChecked(True)
            button.toggled.connect(self._changed)
        self.missing = QCheckBox("One drive of the RAID 5 is missing or dead")
        self.missing.toggled.connect(self._changed)
        layout.addWidget(self.missing)
        self.note = _wrapped("", dim=True)
        layout.addWidget(self.note)
        box = QDialogButtonBox()
        self.start = QPushButton("Work It Out")
        box.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._changed()

    def chosen(self):
        return [disk for disk, box in self.drives
                if box.isChecked() and box.isEnabled()]

    def level(self):
        button = self.levels.checkedButton()
        return button.property("level") if button else "auto"

    def problem(self):
        """Why the choice cannot be worked on yet, or ""."""
        count = len(self.chosen()) + (1 if self.missing.isChecked() else 0)
        if self.missing.isChecked() and self.level() == "raid0":
            return "A RAID 0 cannot be read with a drive missing."
        if len(self.chosen()) < 2:
            return "Tick at least two drives."
        if count > diskrecover.GUESS_MOST:
            return ("At most %d drives: with more, the ways of ordering "
                    "them are too many to try." % diskrecover.GUESS_MOST)
        if (self.level() == "raid5" or self.missing.isChecked()) \
                and count < 3:
            return "A RAID 5 has at least three drives."
        return ""

    def _changed(self, _checked=False):
        problem = self.problem()
        self.start.setEnabled(not problem)
        self.note.setText(problem or (
            "This tries every order, piece size and layout; with five or "
            "six drives it can take a few minutes."))
        self.note.setVisible(bool(self.note.text()))
        if self.isVisible():
            self.fit()


class GuessResultDialog(_FixedWidthDialog):
    """What the drives were found to be; pick the way to open them."""

    def __init__(self, result, titles, parent=None):
        super().__init__(parent)
        self.setWindowTitle("How the drives fit together")
        self.candidates = list(result.get("candidates") or ())
        if result.get("sure"):
            # Certain: the others only read worse, and are left out.
            self.candidates = self.candidates[:1]
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if result.get("sure"):
            layout.addWidget(_heading("Found"))
            layout.addWidget(_wrapped(
                "Read this way, the filesystem on the drives is whole, and "
                "no other way reads nearly as well.", dim=True))
        else:
            layout.addWidget(_heading("Not sure"))
            layout.addWidget(AdviceCard(diskrecover.Advice(
                diskrecover.WARNING, "How the drives fit together is not "
                "certain", (result.get("why") or "")
                + (" Opening one of these is harmless - nothing is written "
                   "- but files read from a wrong one come out scrambled. "
                   "Check a few before trusting it."
                   if self.candidates else ""))))
        self.buttons = []
        for candidate in self.candidates:
            line, details = diskrecover.describe_guess(candidate, titles)
            button = QRadioButton(line)
            self.buttons.append(button)
            layout.addWidget(button)
            note = _wrapped(" ".join(details), dim=True)
            note.setIndent(24)
            layout.addWidget(note)
        if self.buttons:
            self.buttons[0].setChecked(True)
        box = QDialogButtonBox()
        self.start = QPushButton("Open Read-Only")
        self.start.setEnabled(bool(self.buttons))
        box.addButton(self.start, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def chosen(self):
        for candidate, button in zip(self.candidates, self.buttons):
            if button.isChecked():
                return candidate
        return None


class RecoverActions:
    """The Recover menu of the disk utility window."""

    def _build_recover(self):
        self._results = None
        self.recover_button = self._button(
            "Recover Files", ("edit-undo", "document-revert"), lambda: None)
        menu = Menu(self.recover_button)
        menu.setToolTipsVisible(True)
        self.recover_action = menu.addAction("Search This for Lost Files...")
        self.recover_action.triggered.connect(
            lambda _checked=False: self.recover_selected())
        self.reopen_action = menu.addAction("Open a Saved Search...")
        self.reopen_action.setToolTip(tip(
            "Open a saved search", "Every search is kept in a search file. "
            "Opening it shows its results again without reading the drive "
            "- also on another computer.", tool_colour()))
        self.reopen_action.triggered.connect(
            lambda _checked=False: self.reopen_search())
        menu.addSeparator()
        self.raid_action = menu.addAction("Find RAID Arrays...")
        self.raid_action.setToolTip(tip(
            "Find RAID arrays", "For drives taken out of a NAS or a "
            "computer that used them as one: find the array they belong "
            "to and open it read-only - also with a drive missing - so "
            "that its files can be searched for or copied off.",
            tool_colour()))
        self.raid_action.triggered.connect(
            lambda _checked=False: self.find_arrays())
        self.guess_action = menu.addAction(
            "Put Drives Together as an Array...")
        self.guess_action.setToolTip(tip(
            "Put drives together as an array", "For the drives of a RAID 0 "
            "or RAID 5 whose own record of the array is gone - a hardware "
            "controller's, a NAS's that wiped it: work out how they fit "
            "together from what is on them, and open them read-only.",
            tool_colour()))
        self.guess_action.triggered.connect(
            lambda _checked=False: self.guess_array())
        self.raid_close_action = menu.addAction("Close This Array")
        self.raid_close_action.triggered.connect(
            lambda _checked=False: self.close_array())
        self.recover_button.setMenu(menu)
        return self.recover_button

    def _update_recover(self, disk, volume, locked):
        answer = diskrecover.availability(disk, volume)
        self.recover_action.setEnabled(bool(answer) and not locked)
        if answer:
            self.recover_action.setToolTip(tip(
                "Search for lost files", "Look for deleted files, and for "
                "files left behind by a format. The drive is only read; "
                "what is found is copied to another drive.", tool_colour()))
        else:
            self.recover_action.setToolTip(tip(
                "Searching is not available", answer.reason, tool_colour()))

        self.raid_action.setEnabled(not locked)
        self.guess_action.setEnabled(not locked)
        self.raid_close_action.setVisible(disk.kind == disks.RAID)
        self.raid_close_action.setEnabled(not locked and not disk.is_system)

    def find_arrays(self):
        if self._job is not None:
            return
        self.run_plan(diskrecover.plan_raid_scan(), done=self._offer_arrays)

    def _offer_arrays(self, result):
        arrays = list(result.get("arrays") or ())
        if not arrays:
            self.notify(
                "RAID arrays", "None of the connected drives carries the "
                "record of a RAID array. Arrays made by a hardware "
                "controller or by Windows keep theirs in a form that "
                "cannot be read from here.", warning=False)
            return
        dialog = ArraysDialog(arrays, self)
        if not self.show_dialog(dialog) or dialog.chosen() is None:
            return

        def done(opened):
            device = opened.get("device", "")
            self.status.setText(
                "The array is open read-only as %s. Search it for files, "
                "or copy it, like any drive." % device)
            self.refresh()
            self.select_device(device)
        self.run_plan(diskrecover.plan_raid_open(dialog.chosen()),
                      done=done)

    def guess_array(self):
        if self._job is not None:
            return
        dialog = GuessDialog([d for d in self.disks
                              if d.kind != disks.RAID], self)
        if not self.show_dialog(dialog) or dialog.problem():
            return
        members = dialog.chosen()
        titles = {d.path: "%s (%s)" % (d.name, d.title) for d in members}
        identities = {d.path: self.identities.get(d.name)
                      or disks.identity(d.name) for d in members}
        plan = diskrecover.plan_raid_guess(
            members, dialog.level(), dialog.missing.isChecked(),
            {d.name: identities[d.path] for d in members})

        def offer(result):
            chosen = GuessResultDialog(result, titles, self)
            if not self.show_dialog(chosen) or chosen.chosen() is None:
                return

            def done(opened):
                device = opened.get("device", "")
                self.status.setText(
                    "The array is open read-only as %s. Search it for "
                    "files, or copy it, like any drive." % device)
                self.refresh()
                self.select_device(device)
            self.run_plan(diskrecover.plan_raid_build(chosen.chosen(),
                                                      identities),
                          done=done)
        self.run_plan(plan, done=offer)

    def close_array(self):
        disk, _volume = self._target(None)
        if disk is None or disk.kind != disks.RAID:
            return
        self.run_plan(
            diskrecover.plan_raid_close(disk,
                                        self.identities.get(disk.name)),
            done=lambda _result: self.status.setText("The array is closed."))

    def _recovery_folder(self):
        return self.settings.value("disk_recovery_folder", "", type=str) \
            or None

    def _starts(self, disk):
        """{device path: its byte offset on the drive} for telling a file
        found by content from one already named."""
        found = {disk.path: 0}
        for volume in disk.volumes:
            found[volume.path] = volume.start or 0
        return found

    def _scan(self, disk, volume, dialog, record, results=None):
        """Run the search the dialog set up; fill `record` and show (or
        update) the results."""
        plan, folder = dialog.plan(), dialog.folder_path
        deep = dialog.is_deep()
        kinds = dialog.chosen_kinds()
        device = plan.request["device"]
        if deep:
            self.settings.setValue("disk_recovery_folder",
                                   os.path.dirname(folder))
            record["work"] = folder
        if results is not None:
            results.set_busy(True, folder if deep else None)

        def done(result):
            self.status.setText(diskrecover.scan_done_text(result))
            found = diskrecover.scan_entries(result, folder)
            for entry in found:
                if entry.source == diskrecover.FOUND:
                    entry.device = device
            if kinds:
                found = diskrecover.keep_kinds(found, kinds)
            old = results.entries if results is not None else []
            entries = diskrecover.merge(old, found, self._starts(disk))
            if not deep:
                diskrecover.remove_scratch(folder)
            record["scans"] = list(record.get("scans") or ()) + (
                ["quick"] if plan.request["deleted"] else []) + (
                ["deep"] if deep else [])
            path = self._keep(record, entries, disk,
                              results.path if results is not None else None)
            if results is not None:
                results.path = path
                results.set_entries(record, entries)
            else:
                self.show_search(record, entries, path)

        def failed(_error=None):
            if results is not None:
                results.set_busy(False)
        self.run_plan(plan, done=done, failed=failed)

    def _keep(self, record, entries, disk, path=None):
        """Save a search where it is kept by itself; returns where, or
        None when that would be on the drive searched."""
        path = path or diskrecover.autosave_place(record, disk)
        if path is None:
            return None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return diskrecover.save_search(path, record, entries)
        except OSError:
            return None

    def recover_selected(self):
        disk, volume = self._target(None)
        if disk is None or not diskrecover.availability(disk, volume):
            return
        name = disk.name if volume is None or volume.kind == "whole" \
            else volume.name
        dialog = RecoverDialog(disk, volume, self,
                               identity=self.identities.get(name),
                               folder=self._recovery_folder())
        if not self.show_dialog(dialog) or dialog.plan() is None:
            return
        self._scan(disk, volume, dialog,
                   diskrecover.new_search(disk, volume))

    def deeper(self, results):
        """A Deep Scan from the results of a Quick one, added to them."""
        if self._job is not None:
            return
        disk, volume = diskrecover.find_drive(results.record.get("drive")
                                              or {}, self.disks)
        if disk is None:
            self.notify("Deep Scan", "The drive this search was made on "
                        "is not connected.")
            return
        name = disk.name if volume is None else volume.name
        dialog = RecoverDialog(disk, volume, results,
                               identity=self.identities.get(name),
                               folder=self._recovery_folder(),
                               deep_only=True)
        if not self.show_dialog(dialog) or dialog.plan() is None:
            return
        self._scan(disk, volume, dialog, results.record, results)

    def save_search_as(self, results):
        start = results.path or os.path.join(
            diskrecover.searches_folder(),
            diskrecover.search_name(results.record))
        path = self.pick_save(results, start)
        if not path:
            return
        if not path.endswith(diskrecover.SEARCH_SUFFIX):
            path += diskrecover.SEARCH_SUFFIX
        try:
            diskrecover.save_search(path, results.record, results.entries)
        except OSError as exc:
            self.notify("Save search", "It could not be saved: %s"
                        % (exc.strerror or exc))
            return
        results.path = path
        self.status.setText("Search saved as %s." % path)

    def pick_save(self, parent, start):
        path, _kind = QFileDialog.getSaveFileName(
            parent, "Save the search", start,
            "Searches (*%s)" % diskrecover.SEARCH_SUFFIX)
        return path

    def reopen_search(self):
        path = self.pick_search(diskrecover.searches_folder())
        if path:
            self.open_search(path)

    def pick_search(self, start):
        path, _kind = QFileDialog.getOpenFileName(
            self, "Open a saved search", start,
            "Searches (*%s)" % diskrecover.SEARCH_SUFFIX)
        return path

    def pick_folder(self, start, title="Recover into"):
        return QFileDialog.getExistingDirectory(self, title, start)

    def open_search(self, path, work=None):
        try:
            record, entries = diskrecover.load_search(path, work)
        except ValueError as exc:
            self.notify("Open a saved search", str(exc))
            return None
        missing = diskrecover.missing_found(entries)
        if missing and work is None:
            # Moved with the search to another place, or on a drive not
            # connected: ask where its folder is now.
            chosen = self.pick_folder(
                os.path.dirname(path), "Where is the folder '%s'?"
                % os.path.basename(record.get("work") or
                                   diskrecover.WORK_PREFIX))
            if chosen:
                return self.open_search(path, chosen)
        return self.show_search(record, entries, path)

    def show_search(self, record, entries, path=None):
        self._results = RecoveryResults(
            record, entries, recover=self._recover_ticked,
            deeper=self.deeper, save=self.save_search_as, path=path,
            parent=self)
        self._results.show()
        if path is None:
            self.status.setText(
                "The search is not kept by itself: the place it would go is "
                "on the drive searched. Use Save Search to keep it.")
        return self._results

    def _recover_ticked(self, results):
        if self._job is not None:
            return
        record, entries = results.record, results.entries
        chosen = [e for e in entries if e.chosen]
        named = [e for e in chosen if e.source != diskrecover.FOUND
                 and not e.file]
        disk, volume = diskrecover.find_drive(record.get("drive") or {},
                                              self.disks)
        if named and disk is None:
            self.notify("Recover files", "The drive this search was made on "
                        "(%s) is not connected." % (
                            (record.get("drive") or {}).get("title")
                            or record.get("device", "")))
            return
        parent = self.pick_folder(self._recovery_folder()
                                  or os.path.expanduser("~"))
        if not parent:
            return
        size = sum(e.size for e in named)
        problem = diskrecover.folder_problem(parent, disk, size)
        if problem:
            self.notify("Recover files", problem)
            return
        try:
            target = diskrecover.recovery_folder(parent)
        except OSError as exc:
            self.notify("Recover files", "The folder could not be made: %s"
                        % (exc.strerror or exc))
            return
        self.settings.setValue("disk_recovery_folder", parent)
        scratch = None

        def finish(_result=None):
            failed = diskrecover.attach_recovered(scratch, entries) \
                if scratch else []
            diskrecover.remove_scratch(scratch)
            diskrecover.move_found(entries, target)
            done = [e for e in entries if e.chosen and e.file]
            for index, entry in enumerate(done):
                entry.status, entry.detail = diskrecover.inspect_file(
                    entry.file)
                if index % 50 == 0:
                    QApplication.processEvents()
            record["recovered"] = list(record.get("recovered") or ()) \
                + [target]
            results.path = self._keep(record, entries, disk, results.path)
            # A copy beside the files, to take them and their search along.
            try:
                diskrecover.save_search(os.path.join(
                    target, diskrecover.search_name(record)), record,
                    entries)
            except OSError:
                pass
            results.refresh()
            text = "Recovered %d files into %s: %s." % (
                len(done), target,
                diskrecover.summary_text(diskrecover.summary(done)))
            if failed:
                text += " %d could not be read at all." % len(failed)
            self.status.setText(text)
            self.notify("Recover files", text, warning=False)

        if not named:
            finish()
            return
        # The search may have been made when the drive had another name.
        old = (record.get("drive") or {}).get("name") or ""
        if old and old != disk.name:
            for entry in entries:
                if entry.device.startswith("/dev/" + old):
                    entry.device = "/dev/" + disk.name \
                        + entry.device[len("/dev/" + old):]
            record["drive"]["name"] = disk.name
        try:
            scratch = diskrecover.scratch_folder()
        except OSError as exc:
            self.notify("Recover files", "A working folder could not be "
                        "made: %s" % (exc.strerror or exc))
            return
        diskrecover.write_selection(scratch, entries)
        name = disk.name if volume is None else volume.name
        self.run_plan(diskrecover.plan_recover(
            disk, volume, scratch, target, len(named), size,
            identity=self.identities.get(name)), done=finish,
            failed=lambda _error=None: diskrecover.remove_scratch(scratch))
