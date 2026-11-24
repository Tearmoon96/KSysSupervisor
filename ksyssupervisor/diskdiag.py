"""A drive that misbehaves or does not show up: what can be told about it.

Three things software can do for a drive in trouble. Recognise one that has
dropped into its controller's factory mode, by the name and size it then
reports. Put the kernel's own complaints about a drive into plain words.
And have the system look for drives again without a restart.

Nothing here repairs a drive. It says what is wrong and what to do next.

Qt-free.
"""

from __future__ import annotations

import re

from . import disks
from .diskfs import INFO, WARNING, Advice
from .diskops import Plan

#: Names a solid-state drive's controller reports when the drive's own
#: firmware could not start and the controller fell back to its maker's.
FACTORY_NAMES = (
    (r"SATAFIRM\s+S11", "Phison"),
    (r"\bPS31\d\d\b", "Phison"),
    (r"\bSM22\d\d(XT|AA|G)?\b", "Silicon Motion"),
)
#: Below this, a built-in drive is not reporting a real capacity.
TINY = 64 * 1024 * 1024


def factory_mode(disk):
    """An Advice when the drive looks to be in its controller's factory
    mode, else None."""
    name = "%s %s" % (disk.vendor, disk.model)
    maker = next((who for pattern, who in FACTORY_NAMES
                  if re.search(pattern, name, re.I)), None)
    fixed = not (disk.removable or disk.kind in (disks.USB, disks.CARD))
    tiny = fixed and 0 < disk.size < TINY
    if maker is None and not tiny:
        return None
    what = ("It reports the name of its controller's maker (%s) instead of "
            "its own" % maker) if maker else \
        "It reports a capacity of only %s" % disks.format_size(disk.size)
    return Advice(
        WARNING, "This drive's firmware has failed",
        "%s. That is what a solid-state drive does when its internal map "
        "of where everything is stored is damaged: the controller starts "
        "in a factory mode in which the stored data cannot be reached. No "
        "program can read it in this state. The maker's tools that bring "
        "such a drive back to life do so by erasing it. If what is on it "
        "matters, do not use them - a data recovery laboratory can often "
        "read the memory directly." % what)


#: (pattern, title, what it means and what to do). First match per title.
SYMPTOMS = (
    (r"I/O error, dev|Buffer I/O error|[Mm]edium [Ee]rror|"
     r"Unrecovered read error|critical medium error|\bUNC\b|"
     r"auto reallocate failed",
     "The drive cannot read some of what is stored on it",
     "The system asked for data and the drive answered that it could not "
     "read it. That is physical damage, and it usually spreads. Copy the "
     "drive now, before anything else: Copy > Save as an Image File with "
     "'Skip unreadable areas' ticked, then work on the copy."),
    (r"hard resetting link|link is slow to respond|SError|"
     r"interface fatal error|BadCRC|ICRC|failed command: (READ|WRITE)|"
     r"exception Emask",
     "The connection to the drive keeps failing",
     "The link between the computer and the drive was reset or reported "
     "transmission errors. The usual causes are the data cable, the port, "
     "or the power the drive gets - not the drive itself. Reseat or "
     "replace the cable, try another port, and for a USB drive another "
     "cable and a port directly on the computer."),
    (r"device descriptor read|unable to enumerate USB device|"
     r"device not accepting address|over-current|Cannot enable|"
     r"reset (high|super|full)[- ]?speed.* USB device",
     "The USB connection does not come up properly",
     "The computer sees something being plugged in but cannot talk to it "
     "reliably. Try another cable and another port, directly on the "
     "computer rather than through a hub; a 2.5-inch hard disk in an "
     "enclosure may need a port that supplies more power, or a powered "
     "hub."),
    (r"nvme\S*:? .*(timeout|controller is down|Device not ready|"
     r"failed to set APST|Removing after probe failure)",
     "The NVMe drive stops answering",
     "The drive's controller did not reply in time and the system gave up "
     "on it. Reseat the drive in its slot. If it keeps happening the "
     "drive is failing: copy what is on it at the first chance it is "
     "readable."),
    (r"[Ww]rite[- ][Pp]rotect is on|write-protect(ed)?|"
     r"Remounting filesystem read-only|forced? to? ?read-only",
     "The drive is read-only",
     "Either a write-protect switch is set (SD cards have one on the "
     "side), or the drive has locked itself against writing - which is "
     "what flash memory does when it has worn out. What is on it can "
     "still be read: copy it off."),
    (r"detected capacity change from \d+ to 0\b|"
     r"Read Capacity\(\d+\) failed|Sense not available",
     "The drive does not report its size",
     "The drive is connected but will not say how large it is, so nothing "
     "on it can be reached. With a card reader this just means no card is "
     "in it. Otherwise try another cable, port or adapter; if it stays "
     "like this the drive's electronics have failed."),
)


def explain(lines):
    """[Advice] - what the kernel's messages about a drive amount to."""
    found, seen = [], set()
    text = "\n".join(lines)
    for pattern, title, meaning in SYMPTOMS:
        if title not in seen and re.search(pattern, text):
            seen.add(title)
            found.append(Advice(WARNING, title, meaning))
    return found


def summary(lines, disk=None):
    """[Advice] for a drive: its messages explained, or that there are
    none."""
    found = explain(lines)
    if not found:
        found.append(Advice(
            INFO, "The system has nothing to report about this drive"
            if lines == [] else "Nothing here points at a known fault",
            "No error from this drive is on record since the computer was "
            "started." if lines == [] else
            "The messages below mention the drive, but none of them is "
            "one of the signs of a failing drive or a bad connection."))
    return found


def plan_log(disk):
    return Plan(title="Reading the system's messages", target=disk.title,
                steps=["Read what the kernel has logged about this drive "
                       "since the computer was started. Changes nothing."],
                request={"op": "drive_log", "name": disk.name})


def plan_rescan():
    return Plan(title="Looking for drives", target="every port",
                steps=["Ask every disk controller to look for drives "
                       "again"],
                request={"op": "rescan"})
