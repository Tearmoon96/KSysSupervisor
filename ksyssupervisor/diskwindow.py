"""The disk utility window.

One layout: the drives down the left, the selected drive on the right - its
partitions as a bar and as rows, the actions that apply to whatever is
selected, and the drive's details and health underneath.

Everything here is presentation and confirmation. What may be done to which
device is decided in diskops.py and, for anything that writes, decided again
by the privileged helper. Reading is done on a thread - lsblk and the SMART
service can both take their time over a sleeping disk - and so is every
operation, so the window stays live under a password prompt and a format.
"""

import os
import threading
import time

from PyQt6.QtCore import QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPalette
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QMessageBox, QPlainTextEdit, QProgressBar,
                             QPushButton, QScrollArea, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from . import (APP_NAME, diskactivity, diskctl, diskdiag, diskentry,
               diskhealth, diskmaintain, diskops, diskplan, disks, log,
               udisks)
from .diskdialogs import (AdviceCard, ConfirmDialog, FormatDialog,
                          RenameDialog)
from .diskcopyui import CopyActions
from .diskmaintainui import MaintainActions
from .diskpartsui import PartitionActions
from .diskrecoverui import RecoverActions
from .diskfs import WARNING, Advice
from .diskwidgets import (HEALTH_COLOURS, Chip, DriveCard, PartitionMap,
                          VolumeRow, _Selectable, drive_icon, health_text,
                          tool_colour)
from .geometry import fit_on_screen
from .widgets import (THEME_EVENTS, Card, Chevron, ElidingLabel, Menu,
                      SectionCard, WrappedLabel, fixed_font, make_transparent,
                      paint_backdrop, run_blocking, tip)

#: How often a running job's drives are looked at for the details view and
#: for a stall. Reading their counters costs a few small sysfs reads.
ACTIVITY_MS = 1000
#: Lines the details view keeps of what the job and its tools said.
LOG_LINES = 500

#: How often the list of drives is read again. Plugging a stick in shows up
#: within this; reading costs one lsblk run.
POLL_MS = 3000
#: UDisks reads SMART data every ten minutes itself; asking more often only
#: returns the same numbers.
HEALTH_MS = 10 * 60 * 1000

DEFAULT_SIZE = (1100, 780)
MIN_SIZE = (780, 480)
LIST_WIDTH = 310

CONNECTIONS = {"sata": "SATA", "ata": "ATA", "nvme": "NVMe (PCIe)",
               "usb": "USB", "mmc": "SD / MMC", "sas": "SAS", "scsi": "SCSI",
               "virtio": "Virtual"}
TABLES = {"gpt": "GPT", "dos": "MBR", "": "None"}


# ---- facts, as text ----------------------------------------------------------

def _count(value):
    return "{:,}".format(value)


def powered_on_text(hours):
    if hours >= 8760:
        return "%s hours (about %.1f years)" % (_count(hours), hours / 8760.0)
    if hours >= 48:
        return "%s hours (%d days)" % (_count(hours), hours // 24)
    return "%d hours" % hours


def drive_facts(disk, health=None):
    """[(caption, value)] describing the drive itself."""
    facts = [("Model", disk.title)]
    if disk.serial:
        facts.append(("Serial number", disk.serial))
    if disk.revision:
        facts.append(("Firmware", disk.revision))
    kind = disk.kind_title
    if health is not None and health.rotation_rate:
        kind += ", %s rpm" % _count(health.rotation_rate)
    facts.append(("Type", kind))
    if disk.transport:
        facts.append(("Connection", CONNECTIONS.get(disk.transport,
                                                    disk.transport.upper())))
    facts.append(("Device", disk.path))
    if not disk.has_media:
        facts.append(("Capacity", "No media"))
        return facts
    facts.append(("Capacity", "%s (%s bytes)" % (disks.format_size(disk.size),
                                                 _count(disk.size))))
    facts.append(("Partition table", TABLES.get(disk.table, disk.table)))
    if disk.physical_sector != disk.logical_sector:
        facts.append(("Sector size", "%d bytes, %d physical"
                      % (disk.logical_sector, disk.physical_sector)))
    else:
        facts.append(("Sector size", "%d bytes" % disk.logical_sector))
    if disk.is_solid_state:
        facts.append(("TRIM", "Supported" if disk.discard
                      else "Not passed on by this device"))
    return facts


def health_facts(disk, health):
    """[(caption, value)] from the drive's SMART data."""
    if health is None:
        return [("Health", "Checking...")]
    if health.status == diskhealth.UNKNOWN:
        return [("Health", "No data")]
    facts = [("Health", health.title)]
    if health.life_left is not None:
        facts.append(("Life left", "%d%%" % health.life_left))
    elif disk.is_solid_state and health.kind:
        facts.append(("Life left", "Not reported by this drive"))
    if health.temperature is not None:
        facts.append(("Temperature", "%d °C" % health.temperature))
    if health.power_on_hours is not None:
        facts.append(("Powered on", powered_on_text(health.power_on_hours)))
    if health.power_cycles is not None:
        facts.append(("Power cycles", _count(health.power_cycles)))
    if health.written is not None:
        facts.append(("Data written", disks.format_size(health.written)))
    if health.read is not None:
        facts.append(("Data read", disks.format_size(health.read)))
    for caption, value in (("Reallocated sectors", health.reallocated),
                           ("Pending sectors", health.pending),
                           ("Uncorrectable sectors", health.uncorrectable),
                           ("Media errors", health.media_errors),
                           ("Unsafe shutdowns", health.unsafe_shutdowns)):
        if value is not None:
            facts.append((caption, _count(value)))
    if health.spare is not None:
        text = "%d%%" % health.spare
        if health.spare_threshold is not None:
            text += " (fails below %d%%)" % health.spare_threshold
        facts.append(("Spare area", text))
    if health.selftest:
        facts.append(("Self-test", diskhealth.selftest_text(health)))
    if health.updated:
        facts.append(("Read", diskhealth.age_text(health.updated)))
    return facts


def health_advice(health):
    """The verdict's reasons and notes, as cards to show under the facts."""
    if health is None:
        return []
    found = []
    if health.status == diskhealth.UNKNOWN:
        found.append(Advice("info", "No health data", health.reasons[0]
                            if health.reasons else ""))
    elif health.reasons:
        found.append(Advice(
            WARNING, "Health: %s" % health.title, "\n".join(health.reasons)))
    for note in health.notes:
        found.append(Advice("info", "Worth knowing", note))
    if health.life_note and health.life_left is not None \
            and "estimated" in health.life_note:
        found.append(Advice("info", "Life left is an estimate",
                            "It is %s." % health.life_note))
    return found


# ---- small pieces ------------------------------------------------------------

class DriveHeader(_Selectable):
    """The selected drive itself, at the top of the right side.

    Selectable like a volume row: with this selected the actions apply to
    the whole drive.
    """

    ICON = 48
    MARGINS = (16, 12, 14, 12)
    SPACING = 12

    def __init__(self, parent=None):
        super().__init__("", parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(*self.MARGINS)
        column.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(self.SPACING)
        column.addLayout(row)
        self._top = row
        self.icon = QLabel()
        self.icon.setFixedSize(self.ICON, self.ICON)
        row.addWidget(self.icon)

        text = QVBoxLayout()
        text.setSpacing(3)
        self.title = ElidingLabel(minimum=80)
        font = QFont(self.title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.25)
        self.title.setFont(font)
        text.addWidget(self.title)
        self.subtitle = ElidingLabel(minimum=80)
        self.subtitle.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        text.addWidget(self.subtitle)
        row.addLayout(text, 1)

        self.system = Chip("System")
        row.addWidget(self.system)
        self.health = Chip()
        row.addWidget(self.health)

        # The tool menus. Beside the title when there is room for both,
        # else on a line of their own under it - never squeezing the title,
        # never making the window wider than it has to be.
        self.tools = QWidget()
        self.tools.setCursor(Qt.CursorShape.ArrowCursor)
        self.tools_layout = QHBoxLayout(self.tools)
        self.tools_layout.setContentsMargins(0, 0, 0, 0)
        self.tools_layout.setSpacing(8)
        self._below = QHBoxLayout()
        self._below.setSpacing(0)
        self._below.addSpacing(self.ICON + self.SPACING)
        self._below.addStretch(1)
        column.addLayout(self._below)
        self._beside = None
        self._place_tools(True)

    def add_tool(self, button):
        self.tools_layout.addWidget(button)
        self._place_tools(self._fits(self.width()))

    def tools_beside(self):
        """Whether the menus are on the title's line."""
        return bool(self._beside)

    def _chips_width(self):
        return sum(chip.sizeHint().width() + self.SPACING
                   for chip in (self.system, self.health)
                   if not chip.isHidden())

    def _fits(self, width):
        left, _top, right, _bottom = self.MARGINS
        text = max(self.title.sizeHint().width(),
                   self.subtitle.sizeHint().width())
        needed = (left + self.ICON + self.SPACING + text + self._chips_width()
                  + self.SPACING + self.tools.sizeHint().width() + right)
        return needed <= width

    def _place_tools(self, beside):
        if beside == self._beside:
            return
        self._beside = beside
        if beside:
            self._below.removeWidget(self.tools)
            self._top.addWidget(self.tools)
        else:
            self._top.removeWidget(self.tools)
            self._below.insertWidget(1, self.tools)
        self.updateGeometry()

    def minimumSizeHint(self):
        # As narrow as the menus on their own line need: wider, the window
        # could not be made narrower than title and menus side by side.
        hint = super().minimumSizeHint()
        left, _top, right, _bottom = self.MARGINS
        stacked = left + right + self.ICON + self.SPACING + max(
            self.tools.sizeHint().width(), 80 + self._chips_width())
        return QSize(min(hint.width(), stacked), hint.height())

    def resizeEvent(self, event):
        self._place_tools(self._fits(event.size().width()))
        super().resizeEvent(event)

    def set_disk(self, disk, health):
        self.icon.setPixmap(drive_icon(disk).pixmap(self.ICON, self.ICON))
        self.title.setText(disk.title)
        parts = [disk.kind_title]
        if disk.has_media:
            parts.append(disks.format_size(disk.size))
            parts.append("whole drive")
        else:
            parts.append("no media")
        parts.append(disk.path)
        self.subtitle.setText(" · ".join(parts))
        self.system.setVisible(disk.is_system)
        self.health.setVisible(disk.has_media)
        self.health.set(health_text(health),
                        HEALTH_COLOURS.get(health.status) if health else None)
        self._place_tools(self._fits(self.width()))


class FactGrid(QWidget):
    """Captions and values in two columns; rebuilt only when they change."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(4)
        self._grid.setColumnStretch(1, 1)
        self._facts = None

    def set_facts(self, facts):
        facts = list(facts)
        if facts == self._facts:
            return
        self._facts = facts
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget() is not None:
                # Hidden as well as deleted: deletion waits for the event
                # loop, and until then the old labels would show through
                # the new ones.
                item.widget().hide()
                item.widget().deleteLater()
        for row, (caption, value) in enumerate(facts):
            name = QLabel(caption)
            name.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            name.setAlignment(Qt.AlignmentFlag.AlignTop)
            text = QLabel(str(value))
            text.setWordWrap(True)
            text.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self._grid.addWidget(name, row, 0)
            self._grid.addWidget(text, row, 1)

    def facts(self):
        return list(self._facts or ())


class DiskUtilityWindow(QWidget, MaintainActions, CopyActions,
                        PartitionActions, RecoverActions):
    """Drives, their volumes and health, and the tools to manage them."""

    # Emitted from worker threads; the handlers run on the GUI thread.
    _inventory_read = pyqtSignal(object, object, object)
    _health_read = pyqtSignal(object)
    _job_event = pyqtSignal(object)
    _job_done = pyqtSignal(object, object)
    _kernel_read = pyqtSignal(object, object)

    def __init__(self, settings=None, select=None, inventory=None,
                 health=None, client=None, background=True, timer=None,
                 queue=None):
        # No parent on purpose, like the other tool windows: a real top-level
        # window with its own task manager entry.
        super().__init__(None)
        self.setWindowTitle("%s - Disk Utility" % APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(*MIN_SIZE)

        self.settings = settings or QSettings("KSysSupervisor", "Settings")
        # Replaced by the tests: where the drive list and the health come
        # from, the helper's client, and whether work goes to a thread.
        self._read_inventory = inventory or disks.inventory
        self._read_health = health or diskhealth.read_all
        self._read_timer = timer or diskmaintain.timer_enabled
        self._timer_read = None
        self.client = client or diskctl.DiskHelperClient()
        self._background = background

        self.disks = []
        #: The drives as they are. self.disks is what is shown: the same,
        #: or on the recovery stick the picture with the prepared changes.
        self.real_disks = []
        #: diskplan.Queue on the recovery stick, where nothing that writes
        #: runs before Apply Changes; None on the desktop.
        self.queue = queue
        self._applying = False
        #: Called whenever the prepared changes change (the stick's panel),
        #: and whenever what may be clicked changes (a job starts or ends).
        self.queue_listener = None
        self.job_listener = None
        self.identities = {}
        #: Whether the drive list has been read at least once, and what
        #: waits for that: a dialog opened along with the window would
        #: otherwise list no drives at all.
        self._have_read = False
        self._after_read = []
        self.health = {}
        self.selected_disk = None        # a drive's name
        self.selected_volume = None      # a volume's name, None = whole drive
        self._wanted = select            # device to select once it is seen
        self._reading = False
        self._reading_health = False
        self._job = None                 # the diskops.Plan being carried out
        self._job_callback = None        # what to do with its result
        self._job_failed = None          # and what to do if it fails
        self._job_started = 0.0
        self._step_started = 0.0
        # While a job runs: its drives' activity, read once a second.
        self._monitor = None
        self._new_monitor = diskactivity.Monitor
        self._read_kernel = diskactivity.kernel_lines
        self._job_wall_start = 0.0       # time.time(), for the journal
        self._step_cancellable = False
        self._step_counted_bytes = False
        self._last_output = None
        self._stall_cards = []
        self._busy = False               # an unprivileged action is running
        self._closed = False
        self.drive_cards = {}
        self.volume_rows = {}

        self._build()

        # Queued: every one of these is emitted from a worker thread.
        queued = Qt.ConnectionType.QueuedConnection
        self._inventory_read.connect(self._apply_inventory, queued)
        self._health_read.connect(self._apply_health, queued)
        self._job_event.connect(self._on_job_event, queued)
        self._job_done.connect(self._on_job_done, queued)
        self._kernel_read.connect(self._on_kernel_read, queued)

        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(ACTIVITY_MS)
        self._activity_timer.timeout.connect(self._sample_activity)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self.refresh)
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self.refresh_health)
        if background:
            self._poll.start(POLL_MS)
            self._health_timer.start(HEALTH_MS)

        self._restore_geometry()
        self.refresh()
        self.warn_about_pending_moves()

    # ---- construction -----------------------------------------------------

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        columns = QHBoxLayout()
        columns.setSpacing(12)

        # The drives.
        self._list_holder = QWidget()
        self._list_layout = QVBoxLayout(self._list_holder)
        self._list_layout.setContentsMargins(2, 2, 2, 2)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch(1)
        left = QScrollArea()
        left.setWidget(self._list_holder)
        left.setWidgetResizable(True)
        left.setFrameShape(QFrame.Shape.NoFrame)
        left.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left.setFixedWidth(LIST_WIDTH)
        make_transparent(left)
        columns.addWidget(left)

        # The selected drive.
        self._detail_holder = QWidget()
        detail = QVBoxLayout(self._detail_holder)
        detail.setContentsMargins(2, 2, 2, 2)
        detail.setSpacing(10)

        self.header = DriveHeader()
        self.header.clicked.connect(lambda _name: self.select_volume(None))
        self.header.setToolTip(tip(
            "Whole drive", "Select the drive itself: Format then erases "
            "everything on it and makes one new volume.", tool_colour()))
        detail.addWidget(self.header)

        self.map = PartitionMap()
        self.map.picked.connect(self._map_picked)
        detail.addWidget(self.map)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(8)
        detail.addLayout(self._rows_layout)

        self.empty_note = QLabel()
        self.empty_note.setWordWrap(True)
        self.empty_note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        detail.addWidget(self.empty_note)

        detail.addWidget(self._build_actions())
        detail.addWidget(self._build_details())
        detail.addStretch(1)

        right = QScrollArea()
        right.setWidget(self._detail_holder)
        right.setWidgetResizable(True)
        right.setFrameShape(QFrame.Shape.NoFrame)
        make_transparent(right)
        columns.addWidget(right, 1)
        outer.addLayout(columns, 1)

        outer.addWidget(self._build_job_panel())

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)

    def _button(self, text, icons, slot):
        button = QPushButton(text)
        for name in icons:
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                button.setIcon(icon)
                break
        button.clicked.connect(slot)
        return button

    def _build_actions(self):
        card = Card()
        column = QVBoxLayout(card)
        column.setContentsMargins(14, 10, 14, 12)
        column.setSpacing(8)

        self.target_caption = ElidingLabel(minimum=80)
        font = QFont(self.target_caption.font())
        font.setBold(True)
        self.target_caption.setFont(font)
        column.addWidget(self.target_caption)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.format_button = self._button(
            "Format...", ("draw-eraser", "edit-clear"), self.format_selected)
        self.mount_button = self._button(
            "Mount", ("media-mount", "drive-harddisk"), self.toggle_mount)
        self.rename_button = self._button(
            "Rename...", ("edit-rename",), self.rename_selected)
        self.eject_button = self._button(
            "Eject", ("media-eject",), self.eject_selected)
        for button in (self.format_button, self.mount_button,
                       self.rename_button, self.eject_button):
            row.addWidget(button)
        row.addStretch(1)
        self.refresh_button = self._button(
            "Refresh", ("view-refresh",), self.refresh_everything)
        self.refresh_button.setToolTip(tip(
            "Refresh", "Read the drives and their health again now.",
            tool_colour()))
        row.addWidget(self.refresh_button)
        row.addWidget(self._build_options())
        column.addLayout(row)

        # The tools, grouped by what they are for, sit in the drive's own
        # banner at the top. They act on what is selected, as these do.
        for button, title, text in (
                (self._build_maintain(), "Maintain",
                 "TRIM, defragmenting, checking for errors, wiping free "
                 "space, the drive's self-test and its messages."),
                (self._build_copy(), "Copy",
                 "Clone, save or write an image, rescue a failing drive, "
                 "open an image as a drive."),
                (self._build_partitions(), "Partitions",
                 "New partition table; create, delete, resize or move a "
                 "partition; find lost ones."),
                (self._build_recover(), "Recover Files",
                 "Deleted files, files lost to a format, repair of damaged "
                 "files, RAID arrays.")):
            button.setToolTip(tip(title, text + " For what is selected: "
                                  "the drive, or one of its volumes.",
                                  tool_colour()))
            self.header.add_tool(button)
        return card

    def _build_options(self):
        """The one setting this window has: whether the desktop offers it
        on a drive's right-click menu."""
        self.options_button = self._button(
            "", ("application-menu", "configure"), lambda: None)
        if self.options_button.icon().isNull():
            self.options_button.setText("Options")
        self.options_button.setToolTip(tip("Options", "", tool_colour()))
        menu = Menu(self.options_button)
        menu.setToolTipsVisible(True)
        self.entry_action = menu.addAction(
            "Show in the File Manager's Right-Click Menu")
        self.entry_action.setCheckable(True)
        self.entry_action.toggled.connect(self._toggle_entry)
        self.rescan_action = menu.addAction("Look for Drives Again")
        self.rescan_action.setToolTip(tip(
            "Look for drives again", "Has every disk controller search its "
            "ports, as a restart would. For a drive that was connected "
            "while the computer was on and has not appeared.",
            tool_colour()))
        self.rescan_action.triggered.connect(
            lambda _checked=False: self.rescan())
        self.options_button.setMenu(menu)
        self._sync_entry_action()
        return self.options_button

    def _sync_entry_action(self):
        where = diskentry.installed()
        self.entry_action.blockSignals(True)
        self.entry_action.setChecked(where is not None)
        self.entry_action.blockSignals(False)
        if where == diskentry.SYSTEM:
            # Put there for every user by the installer, which is also what
            # takes it away again.
            self.entry_action.setEnabled(False)
            self.entry_action.setToolTip(tip(
                "Right-click menu", "Installed for all users together with "
                "%s. Uninstalling removes it." % APP_NAME, tool_colour()))
        elif diskentry.render() is None:
            self.entry_action.setEnabled(False)
            self.entry_action.setToolTip(tip(
                "Right-click menu", "This copy does not ship the entry.",
                tool_colour()))
        else:
            self.entry_action.setEnabled(True)
            self.entry_action.setToolTip(tip(
                "Right-click menu", "Adds \"%s Disk Utility\" to what "
                "Plasma offers when you right-click a drive or volume in "
                "the file manager's Places panel, and in the Device "
                "Notifier." % APP_NAME, tool_colour()))

    def _toggle_entry(self, wanted):
        try:
            if wanted:
                diskentry.install()
            else:
                diskentry.remove()
        except OSError as exc:
            self.notify("Disk Utility", "The right-click entry could not be "
                        "%s:\n\n%s" % ("added" if wanted else "removed", exc))
        self._sync_entry_action()

    def _build_details(self):
        self.details = SectionCard("Details")
        column = QVBoxLayout(self.details.body)
        column.setContentsMargins(0, 2, 0, 0)
        column.setSpacing(10)

        facts = QHBoxLayout()
        facts.setSpacing(28)
        self.drive_grid = FactGrid()
        self.health_grid = FactGrid()
        facts.addWidget(self.drive_grid, 1, Qt.AlignmentFlag.AlignTop)
        facts.addWidget(self.health_grid, 1, Qt.AlignmentFlag.AlignTop)
        column.addLayout(facts)

        self._advice_layout = QVBoxLayout()
        self._advice_layout.setSpacing(6)
        column.addLayout(self._advice_layout)
        self._advice_cards = []

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(4)
        self.attributes_toggle = Chevron(
            tips=("Hide the table", "Show every SMART attribute"))
        self.attributes_toggle.clicked.connect(self._toggle_attributes)
        toggle_row.addWidget(self.attributes_toggle)
        self.attributes_caption = QLabel("SMART attributes")
        toggle_row.addWidget(self.attributes_caption)
        toggle_row.addStretch(1)
        self._attributes_header = QWidget()
        self._attributes_header.setLayout(toggle_row)
        toggle_row.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self._attributes_header)

        self.attributes = QTreeWidget()
        self.attributes.setRootIsDecorated(False)
        self.attributes.setAlternatingRowColors(True)
        self.attributes.setHeaderLabels(
            ["ID", "Attribute", "Value", "Worst", "Threshold", "Raw"])
        self.attributes.setSelectionMode(
            QTreeWidget.SelectionMode.NoSelection)
        self.attributes.setMinimumHeight(220)
        # What the user chose; the table itself is also hidden whenever the
        # selected drive has no attributes to put in it.
        self._attributes_open = self.settings.value(
            "disk_attributes_expanded", False, type=bool)
        self.attributes.setVisible(False)
        self.attributes_toggle.set_expanded(self._attributes_open)
        column.addWidget(self.attributes)
        self._shown_attributes = None
        return self.details

    def _build_job_panel(self):
        self.job_panel = Card()
        self.job_panel.set_accent(tool_colour())
        column = QVBoxLayout(self.job_panel)
        column.setContentsMargins(16, 10, 14, 12)
        column.setSpacing(6)

        top = QHBoxLayout()
        self.job_title = ElidingLabel(minimum=80)
        font = QFont(self.job_title.font())
        font.setBold(True)
        self.job_title.setFont(font)
        top.addWidget(self.job_title, 1)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_job)
        top.addWidget(self.stop_button)
        column.addLayout(top)

        self.job_step = WrappedLabel("")
        column.addWidget(self.job_step)
        self.job_bar = QProgressBar()
        self.job_bar.setTextVisible(False)
        column.addWidget(self.job_bar)
        # Shortened rather than wrapped: it also shows what the tool last
        # said, and a long line must not widen the window.
        self.job_rate = ElidingLabel(minimum=80)
        self.job_rate.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        column.addWidget(self.job_rate)

        # A drive that stops answering is said here, details open or not.
        self._stall_layout = QVBoxLayout()
        self._stall_layout.setSpacing(6)
        column.addLayout(self._stall_layout)

        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.setSpacing(4)
        self.job_details_toggle = Chevron(
            tips=("Hide the details", "What exactly is happening"))
        self.job_details_toggle.clicked.connect(self._toggle_job_details)
        toggle_row.addWidget(self.job_details_toggle)
        toggle_row.addWidget(QLabel("Details"))
        toggle_row.addStretch(1)
        column.addLayout(toggle_row)

        self.job_details = QWidget()
        details = QVBoxLayout(self.job_details)
        details.setContentsMargins(28, 0, 0, 0)
        details.setSpacing(4)
        self.job_activity = WrappedLabel("")
        details.addWidget(self.job_activity)
        self.job_elapsed = QLabel("")
        self.job_elapsed.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        details.addWidget(self.job_elapsed)
        self.job_log = QPlainTextEdit()
        self.job_log.setReadOnly(True)
        self.job_log.setMaximumBlockCount(LOG_LINES)
        self.job_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.job_log.setFont(fixed_font())
        self.job_log.setFixedHeight(150)
        details.addWidget(self.job_log)
        column.addWidget(self.job_details)
        self._job_details_open = self.settings.value(
            "disk_job_details_expanded", False, type=bool)
        self.job_details.setVisible(self._job_details_open)
        self.job_details_toggle.set_expanded(self._job_details_open)

        self.job_panel.setVisible(False)
        return self.job_panel

    def _toggle_job_details(self):
        self._job_details_open = not self._job_details_open
        self.job_details.setVisible(self._job_details_open)
        self.job_details_toggle.set_expanded(self._job_details_open)
        self.settings.setValue("disk_job_details_expanded",
                               self._job_details_open)

    # ---- backdrop -----------------------------------------------------------

    def paintEvent(self, event):
        paint_backdrop(self)
        super().paintEvent(event)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)

    # ---- reading ------------------------------------------------------------

    def _start(self, work, signal, handler):
        """Run `work` and hand its result tuple to `handler`: on a thread
        through `signal`, or straight away when threads are switched off."""
        if not self._background:
            handler(*work())
            return
        threading.Thread(target=lambda: signal.emit(*work()),
                         name="disk-utility", daemon=True).start()

    def refresh(self):
        """Read the list of drives again, unless a read or a job is running."""
        if self._reading or self._job is not None or self._closed:
            return
        self._reading = True
        self._start(self._inventory_work, self._inventory_read,
                    self._apply_inventory)

    def _inventory_work(self):
        try:
            found = self._read_inventory()
            # Read in the same breath as the list: these are what the helper
            # later checks a device against, so they have to describe the
            # devices as they were when the user saw them.
            identities = {}
            for disk in found:
                for name in [disk.name] + [v.name for v in disk.volumes]:
                    if name not in identities:
                        identities[name] = disks.identity(name)
            try:
                self._timer_read = self._read_timer()
            except Exception:
                self._timer_read = None
            return found, identities, None
        except disks.InventoryError as exc:
            return None, None, str(exc)
        except Exception as exc:
            log.exception("Could not read the drive list")
            return None, None, str(exc)

    def _apply_inventory(self, found, identities, error):
        self._reading = False
        if self._closed:
            return
        if error is not None:
            self.status.setText("The drives could not be read: %s" % error)
            return
        found = self.ordered(found)
        self.real_disks = found
        if self.queue is not None:
            found = self.queue.view(found)
        changed = found != self.disks
        self.disks, self.identities = found, identities
        if self.queue is not None and self.queue_listener is not None:
            self.queue_listener()
        if self._timer_read != self.trim_timer:
            self.trim_timer = self._timer_read
            changed = changed or bool(self.drive_cards)
        if not self._have_read:
            self._have_read = True
            waiting, self._after_read = self._after_read, []
            for action in waiting:
                # After this read has been shown, not in the middle of it.
                QTimer.singleShot(0, action)
        if not changed and self.drive_cards:
            return

        self._sync_drive_list()
        if self._wanted:
            self._select_wanted()
        if self.disk() is None:
            # Whatever was selected is gone: fall back to the first drive
            # that has something in it.
            first = next((d for d in self.disks if d.has_media),
                         self.disks[0] if self.disks else None)
            self.selected_disk = first.name if first else None
            self.selected_volume = None
        elif self.selected_volume and self.volume() is None:
            self.selected_volume = None
        self._show_selection()

        if any(d.name not in self.health for d in self.disks):
            self.refresh_health()

    def refresh_health(self, refresh=False):
        if self._reading_health or self._closed or not self.disks:
            return
        self._reading_health = True
        targets = [d for d in self.disks if d.has_media]

        def work():
            try:
                return (self._read_health(targets, refresh=refresh),)
            except Exception:
                # An answer for every drive all the same, or the cards would
                # say "Checking..." for good.
                log.exception("Could not read drive health")
                return ({disk.name: diskhealth.unknown(
                    "The drive's health data could not be read.")
                    for disk in targets},)
        self._start(work, self._health_read, self._apply_health)

    def _apply_health(self, found):
        self._reading_health = False
        if self._closed:
            return
        self.health.update(found)
        for name, card in self.drive_cards.items():
            card.set_health(self.health.get(name))
        self._show_details()

    def refresh_everything(self):
        self.status.setText("")
        self.refresh()
        self.refresh_health(refresh=True)

    @staticmethod
    def ordered(found):
        """Built-in drives first, then removable ones, empty readers last;
        within each, the order the kernel lists them in."""
        def rank(disk):
            if not disk.has_media:
                return 2
            portable = (disk.removable or disk.hotplug
                        or disk.kind in (disks.USB, disks.CARD))
            return 1 if portable else 0
        return sorted(found, key=rank)

    # ---- selection ----------------------------------------------------------

    def disk(self):
        for disk in self.disks:
            if disk.name == self.selected_disk:
                return disk
        return None

    def volume(self):
        disk = self.disk()
        if disk is None or self.selected_volume is None:
            return None
        return disk.volume(self.selected_volume)

    def select_disk(self, name):
        if name == self.selected_disk and self.selected_volume is None:
            return
        self.selected_disk, self.selected_volume = name, None
        self._show_selection()

    def select_volume(self, name):
        self.selected_volume = name or None
        self._show_selection(rebuild=False)

    def _map_picked(self, name):
        self.select_volume(name or None)

    def select_device(self, device):
        """Select a drive or volume by name or /dev path, now or as soon as
        it shows up in the list."""
        self._wanted = device
        if self.disks:
            self._select_wanted()
            self._show_selection()

    def _select_wanted(self):
        name = os.path.basename(self._wanted or "")
        self._wanted = None
        for disk in self.disks:
            if disk.name == name:
                self.selected_disk, self.selected_volume = name, None
                return
            if disk.volume(name) is not None:
                self.selected_disk, self.selected_volume = disk.name, name
                return

    # ---- showing ------------------------------------------------------------

    def _sync_drive_list(self):
        names = [d.name for d in self.disks]
        if names != list(self.drive_cards):
            for card in self.drive_cards.values():
                self._list_layout.removeWidget(card)
                card.hide()
                card.deleteLater()
            self.drive_cards = {}
            for index, disk in enumerate(self.disks):
                card = DriveCard(disk)
                card.clicked.connect(self.select_disk)
                self._list_layout.insertWidget(index, card)
                self.drive_cards[disk.name] = card
        for disk in self.disks:
            card = self.drive_cards[disk.name]
            card.set_disk(disk)
            card.set_health(self.health.get(disk.name))

    def _show_selection(self, rebuild=True):
        disk = self.disk()
        for name, card in self.drive_cards.items():
            card.set_selected(name == self.selected_disk)
        has_disk = disk is not None
        self._detail_holder.setVisible(has_disk)
        if not has_disk:
            self.status.setText("No drives were found.")
            return

        self.header.set_disk(disk, self.health.get(disk.name))
        self.header.set_selected(self.selected_volume is None)
        self.map.set_disk(disk)
        self.map.setVisible(disk.has_media)
        self.map.set_selected(self.selected_volume)

        shown = [v for v in disk.volumes if not v.is_extended]
        if rebuild and [v.name for v in shown] != list(self.volume_rows):
            for row in self.volume_rows.values():
                self._rows_layout.removeWidget(row)
                row.hide()
                row.deleteLater()
            self.volume_rows = {}
            for volume in shown:
                row = VolumeRow(volume)
                row.clicked.connect(self.select_volume)
                self._rows_layout.addWidget(row)
                self.volume_rows[volume.name] = row
        for volume in shown:
            row = self.volume_rows.get(volume.name)
            if row is not None:
                row.set_volume(volume)
                row.set_selected(volume.name == self.selected_volume)

        if not disk.has_media:
            self.empty_note.setText(
                "There is no media in this device. Insert a card and it "
                "appears here.")
        elif not shown:
            self.empty_note.setText(
                "This drive has no partitions. Format it to start using it.")
        self.empty_note.setVisible(not shown)

        self._update_actions()
        self._show_details()

    def _update_actions(self):
        disk, volume = self.disk(), self.volume()
        if disk is None:
            return
        locked = self._job is not None or self._busy
        if volume is None:
            self.target_caption.setText("Whole drive: %s" % disk.title)
        else:
            self.target_caption.setText(
                "Volume: %s (%s)" % (volume.title, volume.name))

        mounted = volume is not None and volume.mounted
        self.mount_button.setText("Unmount" if mounted else "Mount")

        entries = (
            (self.format_button, diskops.FORMAT, "Format",
             "Erase this and create a new, empty filesystem. Asks for the "
             "filesystem and a name, then shows what will be lost."),
            (self.mount_button,
             diskops.UNMOUNT if mounted else diskops.MOUNT,
             "Unmount" if mounted else "Mount",
             "Detach this volume so it can be removed or changed safely."
             if mounted else
             "Make this volume's files reachable, where the file manager "
             "would put it."),
            (self.rename_button, diskops.RENAME, "Rename",
             "Change the volume's name. Nothing on it is touched."),
            (self.eject_button, diskops.EJECT, "Eject",
             "Unmount everything on this drive and power it down, so it "
             "can be unplugged."),
        )
        for button, action, title, text in entries:
            answer = diskops.availability(action, disk, volume)
            button.setEnabled(bool(answer) and not locked)
            if answer:
                button.setToolTip(tip(title, text, tool_colour()))
            else:
                # Greyed out, never hidden: the tooltip says what is in the
                # way, so the button is an answer rather than a dead end.
                button.setToolTip(tip("%s is not available" % title,
                                      answer.reason, tool_colour()))
        self.refresh_button.setEnabled(not locked)
        self._update_maintain(disk, volume, locked)
        self._update_copy(disk, volume, locked)
        self._update_partitions(disk, volume, locked)
        self._update_recover(disk, volume, locked)
        if self.job_listener is not None:
            self.job_listener()

    def _show_details(self):
        disk = self.disk()
        if disk is None:
            return
        health = self.health.get(disk.name) if disk.has_media else None
        self.header.set_disk(disk, health)
        facts = drive_facts(disk, health)
        if disk.has_media and (disk.discard or disk.is_solid_state):
            # How this volume is actually trimmed, in place of the bare
            # "supported": the selected volume, else the first one in use.
            volume = self.volume() or next(iter(disk.mounted_volumes), None)
            facts = [(caption, value) for caption, value in facts
                     if caption != "TRIM"]
            facts.append(("TRIM", diskmaintain.trim_state(
                disk, volume, self.trim_timer)))
        self.drive_grid.set_facts(facts)
        self.health_grid.set_facts(health_facts(disk, health)
                                   if disk.has_media else [])

        advice = health_advice(health)
        failed = diskdiag.factory_mode(disk)
        if failed is not None:
            advice = [failed] + advice
        if [c.advice for c in self._advice_cards] != advice:
            for card in self._advice_cards:
                self._advice_layout.removeWidget(card)
                card.hide()
                card.deleteLater()
            self._advice_cards = [AdviceCard(each) for each in advice]
            for card in self._advice_cards:
                self._advice_layout.addWidget(card)

        attributes = list(health.attributes) if health else []
        self._attributes_header.setVisible(bool(attributes))
        self.attributes.setVisible(bool(attributes) and self._attributes_open)
        if attributes != self._shown_attributes:
            self._shown_attributes = attributes
            self.attributes.clear()
            for attribute in attributes:
                item = QTreeWidgetItem([
                    "%d" % attribute.id, attribute.name,
                    "%d" % attribute.value, "%d" % attribute.worst,
                    "%d" % attribute.threshold, attribute.raw_text])
                if attribute.failing:
                    for column in range(6):
                        item.setForeground(
                            column, HEALTH_COLOURS[diskhealth.BAD])
                self.attributes.addTopLevelItem(item)
            for column in (0, 2, 3, 4, 5):
                self.attributes.resizeColumnToContents(column)
            self.attributes.setColumnWidth(1, 260)

    def _toggle_attributes(self):
        self._attributes_open = not self._attributes_open
        self.attributes.setVisible(self._attributes_open
                                   and bool(self._shown_attributes))
        self.attributes_toggle.set_expanded(self._attributes_open)
        self.settings.setValue("disk_attributes_expanded",
                               self._attributes_open)

    # ---- telling the user ---------------------------------------------------

    def notify(self, title, text, warning=True):
        """A message that needs acknowledging. Its own method so the tests
        can catch it instead of a modal box."""
        box = (QMessageBox.warning if warning else QMessageBox.information)
        box(self, title, text)

    def ask(self, title, text, accept, informative=""):
        """A yes/no question; True when `accept` was chosen."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        if informative:
            box.setInformativeText(informative)
        yes = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    def _blocking(self, call):
        """(result, exception) of `call`, with the window kept alive."""
        if self._background:
            return run_blocking(call)
        try:
            return call(), None
        except Exception as exc:
            return None, exc

    # ---- unprivileged actions -----------------------------------------------

    def _simple(self, call, done):
        """Run a mount, unmount or eject and report how it went."""
        if self._busy or self._job is not None:
            return
        self._busy = True
        self._update_actions()
        self.status.setText("Working...")
        try:
            result, error = self._blocking(call)
        finally:
            self._busy = False
        if error is not None:
            self.status.setText("")
            self.notify("Disk Utility", str(error))
        else:
            self.status.setText(done(result))
        self._update_actions()
        self.refresh()

    def toggle_mount(self):
        disk, volume = self.disk(), self.volume()
        if volume is None:
            return
        mounted = volume.mounted
        action = diskops.UNMOUNT if mounted else diskops.MOUNT
        if not diskops.availability(action, disk, volume):
            return
        if mounted:
            self._simple(lambda: udisks.unmount(volume.path),
                         lambda _r: "%s is unmounted." % volume.title)
        else:
            self._simple(lambda: udisks.mount(volume.path),
                         lambda where: "%s is mounted at %s."
                         % (volume.title, where) if where
                         else "%s is mounted." % volume.title)

    def eject_selected(self):
        disk = self.disk()
        if disk is None or not diskops.availability(diskops.EJECT, disk):
            return

        def eject():
            for volume in disk.volumes:
                for inner in (volume,) + tuple(volume.inner):
                    if inner.mountpoints:
                        udisks.unmount(inner.path)
            try:
                udisks.power_off(disk.path)
            except udisks.UDisksError as exc:
                # A card reader stays connected with its card unmounted; that
                # is as ejected as it gets.
                log.info("Could not power off %s: %s", disk.path, exc)
                return False
            return True

        self._simple(eject, lambda off: "%s can be removed now." % disk.title
                     if off else "Everything on %s is unmounted; it can be "
                     "removed now." % disk.title)

    # ---- privileged actions -------------------------------------------------

    def format_selected(self):
        disk, volume = self.disk(), self.volume()
        if disk is None or self._job is not None:
            return
        if not diskops.availability(diskops.FORMAT, disk, volume):
            return
        dialog = FormatDialog(disk, volume, self,
                              identities=self.identities)
        if dialog.exec() and dialog.plan() is not None:
            self.run_plan(dialog.plan())

    def rename_selected(self):
        disk, volume = self.disk(), self.volume()
        if volume is None or self._job is not None:
            return
        if not diskops.availability(diskops.RENAME, disk, volume):
            return
        dialog = RenameDialog(disk, volume, self,
                              identity=self.identities.get(volume.name))
        if dialog.exec() and dialog.plan() is not None:
            self.run_plan(dialog.plan())

    def ensure_helper(self):
        """True once the privileged helper can be started.

        A missing or outdated helper is installed from here, with one
        password prompt; anything else is explained.
        """
        state, reason = diskctl.availability()
        if state == diskctl.READY:
            return True
        if not diskctl.can_install_helper(state):
            self.notify("Disk Utility", "%s\n\nInstall it with:\n    %s"
                        % (reason, diskctl.manual_install_hint()))
            return False
        updating = state == diskctl.OUTDATED_HELPER
        if not self.ask(
                "Disk Utility",
                "The disk helper needs updating." if updating
                else "Changing a disk needs a small helper installed.",
                "Update Helper" if updating else "Install Helper",
                "Formatting needs root, so a small helper is installed "
                "system-wide at %s. You are asked for your password once "
                "for this, and once more when it is first used.\n\n"
                "Only the helper is installed. %s itself stays where it is "
                "and your settings are not changed."
                % (os.path.dirname(diskctl.HELPER_INSTALL_PATH), APP_NAME)):
            return False
        self.status.setText("Waiting for authorisation...")
        result, error = self._blocking(diskctl.install_bundled_helper)
        self.status.setText("")
        ok, message = result or (False, str(error))
        if not ok:
            self.notify("Disk Utility", message)
            return False
        state, reason = diskctl.availability()
        if state != diskctl.READY:
            self.notify("Disk Utility", reason)
            return False
        return True

    def run_plan(self, plan, done=None, failed=None):
        """Carry out a confirmed plan through the helper.

        `done`, when given, is called with the helper's result once the
        operation has succeeded, in place of the one-line report. `failed`
        is called with the error when it did not - refused, stopped, or not
        started at all - so whoever waits on it can stop waiting.
        """
        if self.queue is not None and not self._applying:
            if diskplan.is_prepared(plan.request):
                self._prepare(plan, done, failed)
                return
            problem = diskplan.reading_problem(plan.request, self.disks,
                                               self.queue)
            if problem:
                self.notify(plan.title, problem, warning=False)
                if failed is not None:
                    failed(None)
                return
        if self._job is not None or not self.ensure_helper():
            if failed is not None:
                failed(None)
            return
        self._job = plan
        self._job_callback = done
        self._job_failed = failed
        self._job_started = time.monotonic()
        self._job_wall_start = time.time()
        self._step_started = 0.0
        self._step_cancellable = False
        self._step_counted_bytes = False
        self._last_output = None
        self.status.setText("")
        self.job_title.setText("%s: %s" % (plan.title, plan.target))
        self.job_step.setText("Waiting for authorisation...")
        self.job_rate.setText("")
        self.job_bar.setRange(0, 0)
        self.stop_button.setEnabled(False)
        self.job_log.clear()
        self.job_activity.setText("")
        self.job_elapsed.setText("")
        self._clear_stall()
        self._log("%s: %s" % (plan.title, plan.target))
        self._start_activity(plan)
        self.job_panel.setVisible(True)
        self._update_actions()

        def work():
            try:
                return self.client.run(plan.request,
                                       on_event=self._emit_event), None
            except Exception as exc:
                return None, exc
        self._start(work, self._job_done, self._on_job_done)

    # ---- prepared changes (the recovery stick) ---------------------------------

    def _prepare(self, plan, done=None, failed=None, extra=None):
        """Hold a confirmed plan back until Apply Changes."""
        try:
            self.queue.add(plan, self.disks, self.identities, done=done,
                           failed=failed, extra=extra)
        except diskplan.PlanError as exc:
            self.notify(plan.title, str(exc))
            if failed is not None:
                failed(None)
            return
        self.status.setText("Prepared: %s. Nothing is changed until you "
                            "press Apply Changes." % plan.title)
        self.queue_changed()

    def queue_changed(self):
        """Draw the picture again after the list changed."""
        self.disks = self.queue.view(self.real_disks)
        self._sync_drive_list()
        if self.disk() is None:
            self.selected_disk = self.disks[0].name if self.disks else None
            self.selected_volume = None
        elif self.selected_volume and self.volume() is None:
            self.selected_volume = None
        self._show_selection()
        self._update_actions()
        if self.queue_listener is not None:
            self.queue_listener()

    def remove_change(self, change):
        gone = self.queue.remove(change)
        self.queue_changed()
        return gone

    def apply_changes(self):
        """Carry out every prepared change, in order, after one last look at
        everything they do."""
        if self.queue is None or not self.queue.changes or self._applying \
                or self._job is not None or self._busy:
            return
        changes = list(self.queue.changes)
        summary = diskplan.summary(changes)
        if not self.show_dialog(ConfirmDialog(summary, self)):
            return
        self._applying = True
        if self.queue_listener is not None:
            self.queue_listener()
        self._apply_next(len(changes), 1)

    def _apply_next(self, total, index):
        if not self.queue.changes:
            self._applying = False
            self.status.setText(
                "Done: %s made. The drives are as the list said."
                % ("the change was" if total == 1 else "both changes were"
                   if total == 2 else "all %d changes were" % total))
            self.refresh()
            self.queue_changed()
            return
        change = self.queue.changes[0]
        change.error = ""
        outcome, failure = self._blocking(self._inventory_work)
        found, identities, error = outcome or (None, None, str(failure))
        if error is None:
            self.real_disks, self.identities = self.ordered(found), identities
        try:
            if error is not None:
                raise diskplan.PlanError("The drives could not be read: %s"
                                         % error)
            request = self.queue.request_for(change, self.real_disks)
        except diskplan.PlanError as exc:
            self._apply_stopped(change, str(exc))
            return
        plan = diskplan.copy_plan(change.plan, request)
        plan.title = "Change %d of %d - %s" % (index, total, plan.title)

        def done(result):
            if change in self.queue.changes:
                self.queue.changes.remove(change)
            if self.queue_listener is not None:
                self.queue_listener()
            if change.done is not None:
                try:
                    change.done(result)
                except Exception:
                    log.exception("After %s", change.title)
            if self._background:
                QTimer.singleShot(0, lambda: self._apply_next(total,
                                                              index + 1))
            else:
                self._apply_next(total, index + 1)

        def failed(error):
            self._apply_stopped(change, str(error) if error else
                                "It did not start.", notified=True)
            if change.failed is not None:
                change.failed(error)

        self.run_plan(plan, done=done, failed=failed)

    def _apply_stopped(self, change, reason, notified=False):
        """The first change that went wrong stops the rest: they were
        prepared on the drives as that one would have left them."""
        self._applying = False
        change.error = reason
        left = len(self.queue.changes)
        self.status.setText("Stopped: %d prepared change%s not made."
                            % (left, "" if left == 1 else "s"))
        if not notified:
            self.notify("Apply Changes",
                        "%s could not be made:\n\n%s\n\nNothing after it "
                        "was done. The changes before it were made; the "
                        "list holds what is left." % (change.title, reason))
        self.refresh()
        self.queue_changed()

    def _emit_event(self, event):
        if self._background:
            self._job_event.emit(event)
        else:
            self._on_job_event(event)

    def _on_job_event(self, event):
        if self._job is None:
            return
        if event.get("event") == "step":
            text = "Step %d of %d: %s" % (
                event.get("index", 0), event.get("total", 0),
                event.get("text", ""))
            self.job_step.setText(text)
            self._log(text)
            self.job_bar.setRange(0, 0)
            self.job_rate.setText("")
            self._step_started = time.monotonic()
            self._step_cancellable = bool(event.get("cancellable"))
            self._step_counted_bytes = False
            self.stop_button.setEnabled(bool(event.get("cancellable")))
            self.stop_button.setToolTip(
                "" if event.get("cancellable") else
                "This step is short and has to finish: stopping halfway "
                "would leave the device in a state nothing can use.")
        elif event.get("event") == "note":
            # A count rather than an amount: shown where the rate would be.
            self.job_rate.setText(str(event.get("text", "")))
            self._log(str(event.get("text", "")))
        elif event.get("event") == "output":
            # What the tool itself just said: in the details, and - when
            # the step has no amount of its own to show - under the bar,
            # where "Writing inode tables: 120/954" is the finer step.
            text = str(event.get("text", ""))
            if text and text != self._last_output:
                self._last_output = text
                self._log(text, tool=True)
                if not self._step_counted_bytes:
                    self.job_rate.setText(text)
        elif event.get("event") == "progress":
            done, total = event.get("done", 0), event.get("total", 0)
            if total > 0:
                self.job_bar.setRange(0, 1000)
                self.job_bar.setValue(int(done * 1000 / total))
                if event.get("unit") != "items":
                    self._step_counted_bytes = True
                    self.job_rate.setText(self._rate_text(done, total))

    def _rate_text(self, done, total):
        elapsed = time.monotonic() - (self._step_started
                                      or self._job_started)
        if elapsed < 1.0 or done <= 0:
            return "%s of %s" % (disks.format_size(done),
                                 disks.format_size(total))
        rate = done / elapsed
        left = (total - done) / rate if rate > 0 else 0
        if left >= 5400:
            remaining = "about %.1f hours left" % (left / 3600.0)
        elif left >= 90:
            remaining = "about %d minutes left" % round(left / 60.0)
        else:
            remaining = "less than a minute left" if left < 45 \
                else "about a minute left"
        return "%s of %s · %s/s · %s" % (
            disks.format_size(done), disks.format_size(total),
            disks.format_size(rate), remaining)

    # ---- what the job's drives are doing ------------------------------------

    def _log(self, text, tool=False):
        """A line in the details: the time, and what happened."""
        self.job_log.appendPlainText("%s  %s%s" % (
            time.strftime("%H:%M:%S"), "  " if tool else "", text))

    def _start_activity(self, plan):
        names = diskactivity.devices_of(plan.request)
        try:
            self._monitor = self._new_monitor(names) if names else None
        except Exception:
            log.exception("Could not start watching %s", names)
            self._monitor = None
        if self._monitor is not None and self._background:
            self._activity_timer.start()
        self._show_activity()

    def _stop_activity(self):
        self._activity_timer.stop()
        self._monitor = None
        self._clear_stall()

    def _sample_activity(self):
        """Once a second while a job runs: its drives, and whether one of
        them has stopped answering."""
        if self._job is None or self._monitor is None:
            return
        try:
            changes = self._monitor.sample()
        except Exception:
            log.exception("Could not read the drives' activity")
            return
        for change in changes:
            if change.kind == diskactivity.STALLED:
                self._log("%s: no answer for %s, %d requests waiting" % (
                    change.drive, diskactivity.duration(change.seconds),
                    change.waiting))
            else:
                self._log("%s paused for %s, then carried on" % (
                    change.drive, diskactivity.duration(change.seconds)))
        # The warning first: what the kernel said is added to it.
        self._show_activity()
        for change in changes:
            if change.kind == diskactivity.STALLED:
                self._read_kernel_messages(change.drive)
                self._notify_stall(change)

    def _show_activity(self):
        now = time.monotonic()
        step = now - (self._step_started or self._job_started)
        whole = now - self._job_started
        self.job_elapsed.setText(
            "This step: %s · the whole operation: %s" % (
                diskactivity.duration(step), diskactivity.duration(whole))
            if self._step_started else
            "Running for %s" % diskactivity.duration(whole))
        monitor = self._monitor
        if monitor is None:
            self.job_activity.setText(
                "What the drive is doing cannot be read for this "
                "operation.")
        else:
            lines = [diskactivity.describe(monitor.drives.get(name),
                                           disks.format_size)
                     for name in monitor.names]
            lines = [line for line in lines if line]
            self.job_activity.setText(
                "\n".join(lines) or "What the drive is doing cannot be "
                "read: the system does not count its requests.")
            self._show_stall(monitor.stalled)

    def _show_stall(self, stalled):
        """The stall card: there while a drive is stalled, its numbers kept
        current, gone once every drive answers again."""
        if not stalled:
            self._clear_stall()
            return
        drive = stalled[0]
        text = diskactivity.stall_text(
            drive.name, drive.silent_for, drive.waiting,
            (self._job.request.get("op") if self._job else ""),
            self._step_cancellable)
        if not self._stall_cards:
            card = AdviceCard(Advice(WARNING, diskactivity.STALL_TITLE, text))
            self._stall_cards = [card]
            self._stall_layout.addWidget(card)
        else:
            self._stall_cards[0].details.setText(text)

    def _clear_stall(self):
        for card in self._stall_cards:
            self._stall_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._stall_cards = []

    def _read_kernel_messages(self, name):
        """What the kernel said about a stalled drive, read on a thread:
        journalctl can take a moment on a large journal."""
        since = self._job_wall_start
        reader = self._read_kernel
        self._start(lambda: (name, reader(name, since)),
                    self._kernel_read, self._on_kernel_read)

    def _on_kernel_read(self, name, lines):
        if self._job is None or not lines:
            return
        self._log("The system's messages about %s:" % name)
        for line in lines[-8:]:
            self._log(line, tool=True)
        if not self._stall_cards:
            return
        for advice in diskdiag.explain(lines):
            if advice.title in [c.advice.title for c in self._stall_cards]:
                continue
            card = AdviceCard(advice)
            self._stall_cards.append(card)
            self._stall_layout.addWidget(card)

    def _notify_stall(self, change):
        """On the desktop as well when the window is not in front: whoever
        started a long format has likely gone to do something else."""
        if self.isActiveWindow() or not self._background:
            return
        title = diskactivity.STALL_TITLE
        text = "%s has not answered for %s. Open the Disk Utility for " \
            "what this means." % (change.drive,
                                  diskactivity.duration(change.seconds))
        threading.Thread(target=diskhealth.desktop_notify,
                         args=(title, text), name="disk-notify",
                         daemon=True).start()

    def stop_job(self):
        if self._job is None:
            return
        self.stop_button.setEnabled(False)
        self.job_step.setText("Stopping...")
        self.client.cancel()

    def _on_job_done(self, result, error):
        plan, self._job = self._job, None
        callback, self._job_callback = self._job_callback, None
        failed, self._job_failed = getattr(self, "_job_failed", None), None
        self._stop_activity()
        self.job_panel.setVisible(False)
        if self._closed or plan is None:
            return
        if error is not None and failed is not None:
            failed(error)
        if isinstance(error, diskctl.Cancelled):
            self.status.setText(str(error))
        elif error is not None:
            log.warning("%s of %s failed: %s", plan.title, plan.target, error)
            self.notify("%s did not complete" % plan.title, str(error))
        elif callback is not None:
            self._update_actions()
            callback(result or {})
        else:
            result = result or {}
            self.status.setText(self._done_text(plan, result))
            if plan.remount and result.get("device"):
                self._mount_after(result["device"])
            if result.get("warning"):
                self.notify(plan.title, result["warning"], warning=False)
        self._update_actions()
        self.refresh()

    @staticmethod
    def _done_text(plan, result):
        if plan.request.get("op") == "format":
            label = result.get("label")
            return "Formatted %s%s." % (
                result.get("device", plan.target),
                " as '%s'" % label if label else "")
        if plan.request.get("op") == "label":
            return "Renamed to '%s'." % result.get("label", "")
        return "%s finished." % plan.title

    def _mount_after(self, device):
        """Mount the result again. Failing is not an error worth a dialog:
        the operation itself succeeded and the volume is there to mount."""
        _where, error = self._blocking(lambda: udisks.mount(device))
        if error is None or "already mounted" in str(error).lower():
            # The desktop's own automounter may have got there first, which
            # is the same outcome.
            return
        log.info("Could not mount %s afterwards: %s", device, error)
        self.status.setText(self.status.text()
                            + " It is not mounted: %s" % error)

    # ---- closing ------------------------------------------------------------

    def is_finished(self):
        """Whether shutdown() has run, so a reopening needs a new window."""
        return self._closed

    def confirm_shutdown(self, parent=None):
        """Ask what to do about an operation still running. False to stay."""
        if self._job is None:
            return True
        box = QMessageBox(parent or self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Disk Utility")
        box.setText("A disk operation is still running.")
        box.setInformativeText(
            "%s\n\nIt is safest to wait for it. If you close anyway, the "
            "operation is not interrupted: it carries on to the end in the "
            "background, without anything showing its progress."
            % self.job_title.text())
        wait = box.addButton("Keep Waiting", QMessageBox.ButtonRole.RejectRole)
        box.addButton("Close Anyway", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(wait)
        box.exec()
        return box.clickedButton() is not wait

    def shutdown(self):
        """Stop reading and let go of the helper."""
        self._closed = True
        self._poll.stop()
        self._health_timer.stop()
        self._activity_timer.stop()
        # With an operation in flight the helper is left to finish it; with
        # none it exits at once.
        self.client.stop(patience=0.5 if self._job is not None else 30)

    def closeEvent(self, event):
        if not self._closed and not self.confirm_shutdown():
            event.ignore()
            return
        self._save_geometry()
        self.shutdown()
        super().closeEvent(event)

    # ---- geometry -----------------------------------------------------------

    def _restore_geometry(self):
        saved = self.settings.value("disk_window_geometry")
        if saved is not None and hasattr(saved, "isEmpty") \
                and not saved.isEmpty():
            self.restoreGeometry(saved)
            return
        self.resize(*fit_on_screen(self, *DEFAULT_SIZE))

    def _save_geometry(self):
        try:
            self.settings.setValue("disk_window_geometry", self.saveGeometry())
        except Exception:
            log.exception("Could not save the disk window geometry")
