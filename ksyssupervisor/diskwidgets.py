"""The disk utility's own widgets: drive cards, volume rows, the partition map.

Painted from the palette like every card in the application, never styled:
a stylesheet resolves its colours once and then keeps them across a
light/dark switch. The colours that are fixed - one per filesystem, three for
health - are mid-tones chosen to read against either background.
"""

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon, QPainter,
                         QPainterPath, QPalette, QPen)
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QSizePolicy, QToolTip,
                             QVBoxLayout, QWidget)

from . import diskfs, diskhealth, disks
from .widgets import (THEME_EVENTS, TOOL_COLOURS, WARNING_ACCENT, Card,
                      ElidingLabel, tip)

TOOL = "disks"

HEALTH_COLOURS = {
    diskhealth.GOOD: QColor("#5fb878"),
    diskhealth.CAUTION: WARNING_ACCENT,
    diskhealth.BAD: QColor("#d9534f"),
}

#: One colour per kind of content, so a volume has the same colour on the map
#: and on its row. Kept clear of the health colours' green, amber and red.
FS_COLOURS = {
    "ext4": "#5b8dd9", "ext3": "#5b8dd9", "ext2": "#5b8dd9",
    "btrfs": "#4fa8b8",
    "xfs": "#8a7fd6",
    "f2fs": "#6f9fd0",
    "ntfs": "#3f9fc9",
    "vfat": "#9a8fc0",
    "exfat": "#b08ac0",
    "swap": "#a08a78",
    "crypto_LUKS": "#b98ac9",
}
OTHER_COLOUR = "#8a94a0"

#: Fuller than this and a volume's bar turns to the caution colour, as a
#: nearly full disk is worth noticing before it is a full one.
NEARLY_FULL = 0.9


def tool_colour():
    return QColor(TOOL_COLOURS[TOOL])


def fs_colour(fstype):
    return QColor(FS_COLOURS.get(fstype, OTHER_COLOUR))


def drive_icon(disk):
    """The theme's icon for this kind of drive, with fallbacks down to the
    one every theme has."""
    names = {
        disks.NVME: ("drive-harddisk-solidstate", "drive-harddisk"),
        disks.SSD: ("drive-harddisk-solidstate", "drive-harddisk"),
        disks.HDD: ("drive-harddisk",),
        disks.USB: ("drive-removable-media-usb", "drive-removable-media",
                    "drive-harddisk"),
        disks.CARD: ("media-flash-sd-mmc", "media-flash",
                     "drive-removable-media", "drive-harddisk"),
    }.get(disk.kind, ("drive-harddisk",))
    for name in names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    return QIcon()


def health_text(health, short=False):
    """What a health chip says: the verdict, and the life left if known."""
    if health is None:
        return "Checking..."
    if health.status == diskhealth.UNKNOWN:
        return "No health data" if not short else "Unknown"
    if health.life_left is not None and not short:
        return "%s · %d%% life" % (health.title, health.life_left)
    return health.title


class Chip(QWidget):
    """A small rounded label, with an optional coloured dot before the text."""

    PAD_X = 8
    PAD_Y = 3
    DOT = 8

    def __init__(self, text="", colour=None, parent=None):
        super().__init__(parent)
        self._text = text
        self._colour = colour
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set(self, text, colour=None):
        if (text, colour) == (self._text, self._colour):
            return
        self._text, self._colour = text, colour
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def colour(self):
        return self._colour

    def sizeHint(self):
        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(self._text) + 2 * self.PAD_X
        if self._colour is not None:
            width += self.DOT + 5
        return QSize(width, metrics.height() + 2 * self.PAD_Y)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        text = palette.color(QPalette.ColorRole.WindowText)
        well = QColor(text)
        well.setAlpha(26)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(well)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        left = self.PAD_X
        if self._colour is not None:
            painter.setBrush(self._colour)
            painter.drawEllipse(QRectF(left, (self.height() - self.DOT) / 2.0,
                                       self.DOT, self.DOT))
            left += self.DOT + 5
        painter.setPen(text)
        painter.drawText(QRectF(left, 0, self.width() - left, self.height()),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft), self._text)
        painter.end()

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)


class UsageBar(QWidget):
    """How full a volume is. A thin rounded bar; empty when not known."""

    HEIGHT = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fraction = None
        self._colour = QColor(OTHER_COLOUR)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set(self, fraction, colour):
        if fraction is not None:
            fraction = max(0.0, min(1.0, fraction))
        if (fraction, colour) == (self._fraction, self._colour):
            return
        self._fraction, self._colour = fraction, colour
        self.update()

    def fraction(self):
        return self._fraction

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(self.palette().color(QPalette.ColorRole.WindowText))
        track.setAlpha(34)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, radius, radius)
        if self._fraction:
            fill = QRectF(rect)
            # Never narrower than the bar is tall: a sliver of a rounded bar
            # is a dot, and a volume with something on it should show one.
            fill.setWidth(max(rect.height(), rect.width() * self._fraction))
            painter.setBrush(HEALTH_COLOURS[diskhealth.BAD]
                             if self._fraction >= NEARLY_FULL
                             else self._colour)
            painter.drawRoundedRect(fill, radius, radius)
        painter.end()


class _Selectable(Card):
    """A card that is chosen by clicking it, and shows that it is."""

    clicked = pyqtSignal(str)

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.name = name
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_selected(self, selected):
        self._selected = bool(selected)
        self.set_accent(tool_colour() if selected else None)

    def is_selected(self):
        return self._selected

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.name)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                           Qt.Key.Key_Space):
            self.clicked.emit(self.name)
            return
        super().keyPressEvent(event)


def _bold(label, scale=1.0):
    font = QFont(label.font())
    font.setBold(True)
    if scale != 1.0:
        font.setPointSizeF(font.pointSizeF() * scale)
    label.setFont(font)
    return label


def _dim(label):
    # A role, not a colour: a colour written into the palette would stay
    # behind after a light/dark switch.
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


class DriveCard(_Selectable):
    """One drive in the list on the left."""

    ICON = 32

    def __init__(self, disk, parent=None):
        super().__init__(disk.name, parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 12, 10)
        row.setSpacing(10)

        self.icon = QLabel()
        self.icon.setFixedSize(self.ICON, self.ICON)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(3)
        self.title = _bold(ElidingLabel(minimum=60))
        text.addWidget(self.title)
        self.subtitle = _dim(ElidingLabel(minimum=60))
        text.addWidget(self.subtitle)

        chips = QHBoxLayout()
        chips.setContentsMargins(0, 2, 0, 0)
        chips.setSpacing(5)
        self.health = Chip()
        self.temperature = Chip()
        self.system = Chip("System")
        self.system.setToolTip(tip(
            "System drive", "The running system is on this drive, so nothing "
            "that would erase or rearrange it is offered here.",
            tool_colour()))
        self.prepared = Chip("Changes prepared")
        self.prepared.setToolTip(tip(
            "Changes prepared", "Shown as it will be after Apply Changes. "
            "Nothing on the drive has been changed yet.", tool_colour()))
        for chip in (self.health, self.temperature, self.system,
                     self.prepared):
            chips.addWidget(chip)
        chips.addStretch(1)
        text.addLayout(chips)
        row.addLayout(text, 1)

        self.disk = None
        self.set_disk(disk)
        self.set_health(None)

    def set_disk(self, disk):
        self.disk = disk
        self.icon.setPixmap(drive_icon(disk).pixmap(self.ICON, self.ICON))
        self.title.setText(disk.title)
        # The device name too: two drives of the same model are otherwise
        # two identical cards.
        self.subtitle.setText("%s · %s · %s" % (
            disk.kind_title,
            disks.format_size(disk.size) if disk.has_media else "no media",
            disk.name))
        self.system.setVisible(disk.is_system)
        self.prepared.setVisible(bool(disk.pending))
        self.health.setVisible(disk.has_media)

    def set_health(self, health):
        self.health.set(health_text(health),
                        HEALTH_COLOURS.get(health.status) if health else None)
        if health is not None and health.reasons:
            self.health.setToolTip(tip("Health: %s" % health.title,
                                       "\n".join(health.reasons),
                                       tool_colour()))
        else:
            self.health.setToolTip("")
        degrees = health.temperature if health is not None else None
        self.temperature.setVisible(degrees is not None)
        if degrees is not None:
            self.temperature.set("%d °C" % degrees)


class VolumeRow(_Selectable):
    """One volume of the selected drive."""

    def __init__(self, volume, parent=None):
        super().__init__(volume.name, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 9, 14, 10)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.swatch = _Swatch()
        top.addWidget(self.swatch)
        self.title = _bold(ElidingLabel(minimum=60))
        top.addWidget(self.title)
        self.kind = Chip()
        top.addWidget(self.kind)
        self.flag = Chip()
        top.addWidget(self.flag)
        self.pending = Chip()
        top.addWidget(self.pending)
        top.addStretch(1)
        self.size = QLabel()
        top.addWidget(self.size)
        outer.addLayout(top)

        middle = QHBoxLayout()
        middle.setSpacing(8)
        self.where = _dim(ElidingLabel(minimum=60))
        middle.addWidget(self.where, 1)
        self.usage = _dim(QLabel())
        middle.addWidget(self.usage)
        outer.addLayout(middle)

        self.bar = UsageBar()
        outer.addWidget(self.bar)

        self.volume = None
        self.set_volume(volume)

    def set_volume(self, volume):
        self.volume = volume
        colour = fs_colour(volume.fstype)
        self.swatch.set(colour)
        self.title.setText(volume.title)
        self.kind.set(diskfs.title_of(volume.fstype))
        self.size.setText(disks.format_size(volume.size))

        if volume.is_system:
            self.flag.set("System")
            self.flag.setToolTip(tip(
                "System volume", "The running system uses this volume (%s), "
                "so it cannot be formatted, unmounted or renamed."
                % ", ".join(volume.system_mounts), tool_colour()))
        self.flag.setVisible(volume.is_system)
        if volume.pending:
            from .diskplan import PENDING_TITLES
            self.pending.set("%s - pending" % PENDING_TITLES.get(
                volume.pending, volume.pending.title()))
            self.pending.setToolTip(tip(
                "Prepared, not done yet", "This is how the volume will be "
                "after Apply Changes. Nothing on the drive has been changed "
                "yet.", tool_colour()))
        self.pending.setVisible(bool(volume.pending))

        mounts = [m for m in volume.all_mountpoints() if m != disks.SWAP]
        if mounts:
            self.where.setText("%s   %s" % (volume.path, "  ".join(mounts)))
        elif volume.is_swap and volume.mounted:
            self.where.setText("%s   in use as swap" % volume.path)
        elif volume.has_filesystem:
            self.where.setText("%s   not mounted" % volume.path)
        else:
            self.where.setText(volume.path)

        # The filesystem's own size where lsblk gives it, since that is what
        # "used" is a share of - a filesystem can be smaller than its
        # partition - and otherwise the two parts added up.
        total = volume.fs_size
        if not total and volume.used is not None and volume.free is not None:
            total = volume.used + volume.free
        if volume.used is not None and total:
            text = "%s used · %s free" % (
                disks.format_size(volume.used),
                disks.format_size(volume.free if volume.free is not None
                                  else total - volume.used))
            if volume.cluster:
                text += " · %s clusters" % diskfs.format_cluster(
                    volume.cluster)
            self.usage.setText(text)
            self.usage.setToolTip(tip(
                "Clusters of %s" % diskfs.format_cluster(volume.cluster),
                "Every file takes up whole clusters, so even the smallest "
                "one uses %s here. Formatting chooses the size."
                % diskfs.format_cluster(volume.cluster), tool_colour())
                if volume.cluster else "")
            self.bar.set(volume.used / float(total), colour)
            self.bar.setToolTip("")
        else:
            # Usage is something only a mounted filesystem reports; an
            # unmounted one is not guessed at.
            self.usage.setText("usage unknown" if volume.has_filesystem
                               and not volume.is_swap else "")
            self.usage.setToolTip("")
            self.bar.set(None, colour)
            self.bar.setToolTip(tip(
                "Usage unknown", "How full a volume is can only be read "
                "while it is mounted.", tool_colour())
                if volume.has_filesystem and not volume.is_swap else "")


class _Swatch(QWidget):
    """The volume's colour, matching its segment on the map."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colour = QColor(OTHER_COLOUR)
        self.setFixedSize(12, 12)

    def set(self, colour):
        if colour != self._colour:
            self._colour = colour
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colour)
        painter.drawRoundedRect(QRectF(self.rect()), 3, 3)
        painter.end()


class PartitionMap(QWidget):
    """The drive drawn as a bar: every partition in proportion, in order.

    In proportion up to a point - a 600 MB boot partition on a 2 TB drive
    would be a hairline, so each segment has a minimum width and the rest of
    the bar is shared out by size.
    """

    picked = pyqtSignal(str)        # a volume's name; "" for free space

    HEIGHT = 46
    MIN_SEGMENT = 16
    GAP = 2
    RADIUS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._segments = []          # (name, title, detail, size, colour)
        self._selected = None
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def set_disk(self, disk):
        segments = []
        pieces = [(v.start if v.start is not None else 0, v)
                  for v in disk.volumes if not v.is_extended]
        pieces += [(gap.start, gap) for gap in disk.free]
        for _start, piece in sorted(pieces, key=lambda p: p[0]):
            if isinstance(piece, disks.FreeSpace):
                segments.append(("", "Unallocated",
                                 disks.format_size(piece.size), piece.size,
                                 None, ""))
            else:
                from .diskplan import PENDING_TITLES
                segments.append((
                    piece.name, piece.title,
                    "%s%s · %s" % (
                        "%s, pending · " % PENDING_TITLES.get(
                            piece.pending, piece.pending)
                        if piece.pending else "",
                        diskfs.title_of(piece.fstype),
                        disks.format_size(piece.size)),
                    piece.size, fs_colour(piece.fstype), piece.pending))
        if not segments and disk.pending == "image":
            segments.append(("", "Image to be written",
                             "contents known after Apply Changes",
                             disk.size, None, "image"))
        elif not segments and disk.has_media:
            segments.append(("", "No partitions",
                             disks.format_size(disk.size), disk.size, None,
                             ""))
        if segments != self._segments:
            self._segments = segments
            self.update()

    def set_selected(self, name):
        if name != self._selected:
            self._selected = name
            self.update()

    def segments(self):
        return list(self._segments)

    def layout_segments(self, width=None):
        """[(QRectF, segment)] for the bar at its current width."""
        width = self.width() if width is None else width
        count = len(self._segments)
        if not count or width <= 0:
            return []
        room = width - self.GAP * (count - 1)
        floor = min(self.MIN_SEGMENT, room / float(count))
        total = float(sum(s[3] for s in self._segments)) or 1.0
        spare = room - floor * count
        placed, left = [], 0.0
        for segment in self._segments:
            span = floor + spare * (segment[3] / total)
            placed.append((QRectF(left, 0, span, self.height()), segment))
            left += span + self.GAP
        return placed

    def segment_at(self, x):
        for rect, segment in self.layout_segments():
            if rect.left() <= x <= rect.right():
                return segment
        return None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), self.RADIUS, self.RADIUS)
        painter.setClipPath(clip)

        text_colour = palette.color(QPalette.ColorRole.WindowText)
        metrics = QFontMetrics(self.font())
        selected_rect = None
        for rect, (name, title, detail, _size, colour, pending) in \
                self.layout_segments():
            if colour is None:
                # Unallocated: hatched, so it reads as "nothing here" rather
                # than as one more kind of volume.
                base = QColor(text_colour)
                base.setAlpha(22)
                painter.fillRect(rect, base)
                hatch = QColor(text_colour)
                hatch.setAlpha(70)
                painter.fillRect(rect, QBrush(hatch,
                                              Qt.BrushStyle.BDiagPattern))
                ink = text_colour
            else:
                painter.fillRect(rect, colour)
                ink = (QColor(20, 24, 28) if colour.lightness() > 150
                       else QColor(255, 255, 255))
            if pending:
                # Prepared, not done: striped over its colour, so the bar
                # says at a glance what is still only a plan.
                stripes = QColor(ink)
                stripes.setAlpha(60)
                painter.fillRect(rect, QBrush(stripes,
                                              Qt.BrushStyle.FDiagPattern))
            if name and name == self._selected:
                selected_rect = rect

            inner = rect.adjusted(7, 4, -7, -4)
            if inner.width() >= 28:
                painter.setPen(ink)
                top = QRectF(inner.left(), inner.top(), inner.width(),
                             inner.height() / 2)
                bottom = QRectF(inner.left(), inner.center().y(),
                                inner.width(), inner.height() / 2)
                flags = int(Qt.AlignmentFlag.AlignVCenter
                            | Qt.AlignmentFlag.AlignLeft)
                bold = QFont(self.font())
                bold.setBold(True)
                painter.setFont(bold)
                painter.drawText(top, flags, QFontMetrics(bold).elidedText(
                    title, Qt.TextElideMode.ElideRight, int(inner.width())))
                painter.setFont(self.font())
                faded = QColor(ink)
                faded.setAlpha(215)
                painter.setPen(faded)
                painter.drawText(bottom, flags, metrics.elidedText(
                    detail, Qt.TextElideMode.ElideRight, int(inner.width())))

        if selected_rect is not None:
            painter.setClipping(False)
            pen = QPen(palette.color(QPalette.ColorRole.WindowText), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(selected_rect.adjusted(1, 1, -1, -1),
                                    self.RADIUS - 1, self.RADIUS - 1)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            segment = self.segment_at(event.position().x())
            if segment is not None:
                self.picked.emit(segment[0])
        super().mousePressEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            segment = self.segment_at(event.pos().x())
            if segment is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(event.globalPos(),
                                  tip(segment[1], segment[2], tool_colour()),
                                  self)
            return True
        return super().event(event)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)

