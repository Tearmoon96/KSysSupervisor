"""What the drives under an operation are doing, read while it runs.

A format, copy or repair can sit on one step for many minutes, and on a worn
USB stick a single write can take seconds. From the step alone nobody can
tell a drive that is slowly working from one that has stopped answering.
The kernel can: /sys/block/<disk>/stat counts every request the drive has
completed and how many are waiting at it right now. The file is readable
without root and reading it never waits on the drive, so it keeps answering
even when the tool doing the work - and the helper running it - is stuck
inside the kernel on that very drive.

A drive is *stalled* when requests are waiting at it and none has completed
for STALL_AFTER seconds. A drive with nothing waiting is idle, not stalled:
the tool is busy computing, or between two writes.

Standard library only, and no Qt.
"""

from __future__ import annotations

import collections
import os
import re
import subprocess
import time
from dataclasses import dataclass

SYS_BLOCK = "/sys/block"
SYS_CLASS_BLOCK = "/sys/class/block"
#: There while the system journal runs; without it (the recovery stick) the
#: kernel's messages are read with dmesg.
JOURNAL_DIR = "/run/systemd/journal"
SECTOR = 512

#: Seconds with requests waiting and none completed before a drive counts
#: as stalled. Flash drives pause for their own housekeeping - erasing
#: blocks, moving worn ones - and a cheap stick can go quiet for half a
#: minute and then carry on; a minute is past that.
STALL_AFTER = 60

#: How many seconds of readings the rate shown is taken over.
RATE_WINDOW = 5


@dataclass(frozen=True)
class Counters:
    """One reading of a drive's stat file."""
    when: float
    read: int                 # bytes read since the drive appeared
    written: int              # bytes written
    completed: int            # requests of any kind finished
    waiting: int              # requests at the drive now


def read_stat(name, root=None, clock=time.monotonic):
    """Counters for the whole disk `name`, or None if it cannot be read.

    Fields, by the kernel's Documentation/block/stat.rst: reads completed,
    reads merged, sectors read, ms reading, writes completed, writes merged,
    sectors written, ms writing, in flight, ms busy, ms queued, then (newer
    kernels) discards completed, merged, sectors, ms, and flushes completed,
    ms. Sectors are always 512 bytes there, whatever the drive's own size.
    """
    path = os.path.join(root or SYS_BLOCK, name, "stat")
    try:
        with open(path, encoding="ascii") as handle:
            fields = [int(word) for word in handle.read().split()]
    except (OSError, ValueError):
        return None
    if len(fields) < 9:
        return None
    completed = fields[0] + fields[4]
    if len(fields) >= 12:
        completed += fields[11]
    if len(fields) >= 16:
        completed += fields[15]
    return Counters(clock(), fields[2] * SECTOR, fields[6] * SECTOR,
                    completed, fields[8])


def whole_disk(name, root=None):
    """The disk a partition is on (sdb for sdb1), or `name` itself."""
    root = root or SYS_CLASS_BLOCK
    entry = os.path.join(root, name)
    if os.path.exists(os.path.join(entry, "partition")):
        parent = os.path.basename(os.path.dirname(os.path.realpath(entry)))
        if parent and os.path.isdir(os.path.join(root, parent)):
            return parent
    return name


def devices_of(request, root=None):
    """The disks an operation works on, from its request to the helper.

    Every "/dev/..." value and every bare device name anywhere in it, each
    taken to the whole disk it is on. Only what exists as a block device
    counts, so a file path, a label or a folder name is passed over.
    """
    root = root or SYS_CLASS_BLOCK
    found = []

    def consider(value):
        if not isinstance(value, str) or not value:
            return
        name = value
        if value.startswith("/dev/"):
            name = os.path.basename(os.path.realpath(value)) \
                if value.startswith("/dev/mapper/") else value[5:]
        if "/" in name or not os.path.isdir(os.path.join(root, name)):
            return
        disk = whole_disk(name, root)
        if disk not in found:
            found.append(disk)

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        else:
            consider(value)

    walk(request)
    return found


class Drive:
    """One drive's activity since the operation began."""

    def __init__(self, name, first):
        self.name = name
        self.first = first
        self.last = first
        self.history = collections.deque([first], maxlen=RATE_WINDOW + 1)
        # When the drive last finished something, or had nothing waiting.
        self.answered = first.when
        self.stalled = False

    def update(self, now):
        if now.completed != self.last.completed or now.waiting == 0:
            self.answered = now.when
        self.last = now
        self.history.append(now)

    @property
    def read(self):
        return max(0, self.last.read - self.first.read)

    @property
    def written(self):
        return max(0, self.last.written - self.first.written)

    def rate(self, field):
        """Bytes a second of `field` ("read" or "written") lately."""
        old, new = self.history[0], self.history[-1]
        span = new.when - old.when
        if span <= 0:
            return 0.0
        return max(0, getattr(new, field) - getattr(old, field)) / span

    @property
    def waiting(self):
        return self.last.waiting

    @property
    def silent_for(self):
        """Seconds requests have waited with none completed; 0 when the
        drive is answering or has nothing to answer."""
        if self.last.waiting == 0:
            return 0.0
        return max(0.0, self.last.when - self.answered)


@dataclass(frozen=True)
class Change:
    """A drive started or stopped stalling."""
    kind: str                 # STALLED or RESUMED
    drive: str
    seconds: float            # how long it has been, or was, silent
    waiting: int = 0


STALLED = "stalled"
RESUMED = "resumed"


class Monitor:
    """The drives of one operation, read once a second by the window.

    `read` and `clock` are there for the tests.
    """

    def __init__(self, names, read=read_stat, stall_after=STALL_AFTER):
        self.read = read
        self.stall_after = stall_after
        self.names = list(names)
        self.drives = {}
        for name in self.names:
            first = read(name)
            self.drives[name] = Drive(name, first) if first else None

    def sample(self):
        """Read every drive again; returns the [Change]s since last time."""
        changes = []
        for name in self.names:
            now = self.read(name)
            drive = self.drives.get(name)
            if now is None:
                continue
            if drive is None:
                self.drives[name] = Drive(name, now)
                continue
            silent_before = drive.silent_for
            drive.update(now)
            silent = drive.silent_for
            if not drive.stalled and silent >= self.stall_after:
                drive.stalled = True
                changes.append(Change(STALLED, name, silent, drive.waiting))
            elif drive.stalled and silent < self.stall_after:
                drive.stalled = False
                changes.append(Change(RESUMED, name,
                                      max(silent_before, silent)))
        return changes

    @property
    def stalled(self):
        return [d for d in self.drives.values() if d is not None and d.stalled]


def duration(seconds):
    """'45 s', '3 min 20 s', '1 h 05 min'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min %02d s" % (seconds // 60, seconds % 60)
    return "%d h %02d min" % (seconds // 3600, seconds % 3600 // 60)


def describe(drive, size_text):
    """One line about a drive for the details: what it has done, and
    whether it is answering. `size_text` formats a number of bytes."""
    if drive is None:
        return None
    parts = []
    if drive.written:
        parts.append("%s written" % size_text(drive.written))
    if drive.read:
        parts.append("%s read" % size_text(drive.read))
    rate_w, rate_r = drive.rate("written"), drive.rate("read")
    if rate_w >= 1024 or rate_r >= 1024:
        parts.append("%s/s now" % size_text(max(rate_w, rate_r)))
    silent = drive.silent_for
    if drive.waiting and silent >= 3:
        parts.append("%d %s waiting, no answer for %s" % (
            drive.waiting, "request" if drive.waiting == 1 else "requests",
            duration(silent)))
    elif drive.waiting:
        parts.append("working")
    else:
        parts.append("idle")
    return "%s: %s" % (drive.name, " · ".join(parts))


# ---- the kernel's messages --------------------------------------------------

def drive_pattern(name, sys_class_block=None):
    """(regex, [names]) that pick a drive's lines out of the kernel's
    messages. The helper has the same in its own drive_pattern, for the
    Drive Messages view; a test holds the two together.

    The drive under its own name, and under the names of the links it hangs
    off: ata3, 2:0:0:0, usb 1-4, nvme0.
    """
    words = {name}
    entry = os.path.join(sys_class_block or SYS_CLASS_BLOCK, name)
    try:
        real = os.path.realpath(entry)
    except OSError:
        real = ""
    for part in real.split("/"):
        if re.match(r"^(ata\d+|nvme\d+|host\d+|\d+:\d+:\d+:\d+|"
                    r"\d+-\d+(\.\d+)*)$", part):
            words.add(part)
    # Its partitions too (sda1, nvme0n1p2), but not a longer name that
    # merely starts the same way (sdab, ata30).
    words.discard(name)
    links = "|".join(re.escape(w) for w in sorted(words)) or "(?!)"
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:%s(?:p?\d+)?(?![A-Za-z0-9])|"
        r"(?:%s)(?![A-Za-z0-9]))" % (re.escape(name), links))
    words.add(name)
    return pattern, sorted(words)


def kernel_lines(name, since, run=subprocess.run, sys_class_block=None):
    """What the kernel has said about drive `name` since `since` (a
    time.time() value), newest last; [] if the journal cannot be read.

    Read through journalctl as the user: on most desktops the user may
    read the system journal (wheel, adm or systemd-journal group). Where
    not, this says nothing rather than asking for a password in the middle
    of an operation.
    """
    # The recovery stick keeps no journal; there the kernel's own buffer,
    # which goes back to a start a few minutes or hours ago, is read whole.
    argv = (["dmesg", "--ctime"] if not os.path.isdir(JOURNAL_DIR) else
            ["journalctl", "--dmesg", "--no-pager", "--quiet",
             "--output=short", "--since=@%d" % int(since)])
    try:
        done = run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   stdin=subprocess.DEVNULL, text=True, errors="replace",
                   timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    pattern, _words = drive_pattern(name, sys_class_block)
    return [line for line in done.stdout.splitlines()
            if pattern.search(line)][-50:]


# ---- what to tell the user --------------------------------------------------

#: Operations that replace everything on the drive they write to: whatever
#: was there is going anyway, so pulling the drive out loses nothing more.
ERASING = frozenset(("format", "table_new", "clone", "image_restore"))
#: Operations that only read the drives they are watched on.
READING = frozenset(("recover_scan", "recover_files", "rescue_image",
                     "image_create", "defrag_analyse", "partition_search",
                     "raid_scan", "raid_guess", "drive_log"))

STALL_TITLE = "The drive has stopped answering"


def stall_text(drive, seconds, waiting, op, cancellable):
    """What a stall means and what can be done about it, for `op` (the
    request's "op") at a step that can or cannot be stopped."""
    text = ("%s has had %d %s waiting for %s without finishing any. Worn "
            "USB sticks and memory cards can pause like this for minutes "
            "and then carry on, so it may still finish. If it stays like "
            "this, the drive is probably failing."
            % (drive, waiting, "request" if waiting == 1 else "requests",
               duration(seconds)))
    if cancellable:
        text += (" Stop asks the operation to end; a tool waiting on the "
                 "drive only ends once the drive answers or the system "
                 "gives up on it.")
    if op in ERASING:
        text += (" This operation replaces everything on the drive it "
                 "writes to, so unplugging the drive loses nothing that was "
                 "not already going: afterwards it needs formatting, or the "
                 "copy starting again.")
    elif op in READING:
        text += (" This operation only reads the drive: unplugging it "
                 "changes nothing on it, though the work has to start over.")
    else:
        text += (" Do not unplug it: this operation is changing what is on "
                 "the drive, and cut off halfway it can leave the drive "
                 "unreadable. Give it time.")
    return text
