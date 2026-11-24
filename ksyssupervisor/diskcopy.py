"""Copying: a drive onto another, and to and from an image file.

What may be copied where, how each partition would be copied, what the user
should know beforehand, and the requests that have the privileged helper do
it. The helper decides everything again for itself.

Qt-free.
"""

from __future__ import annotations

import os
import time
from typing import NamedTuple, Optional

from . import diskfs, diskops, disks
from .diskfs import INFO, WARNING, Advice
from .diskops import Plan, describe_target, what_is_lost

EXACT = "exact"
SMART = "smart"

#: The tool that copies each filesystem's used blocks; the helper's table.
SMART_TOOLS = {"ext2": "e2image", "ext3": "e2image", "ext4": "e2image",
               "ntfs": "ntfsclone", "xfs": "xfs_copy"}

#: Sectors a GPT keeps for its second copy after the last partition.
GPT_BACKUP_SECTORS = 34

MIB = 1024 * 1024

#: Partition types "use the free space" leaves at their size: EFI System,
#: Microsoft reserved, Windows recovery (GPT); EFI and hidden recovery (MBR).
#: The helper's NO_GROW_TYPES.
WINDOWS_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
NO_GROW_TYPES = frozenset((disks.GPT_EFI, disks.GPT_MSR, WINDOWS_RECOVERY,
                           "ef", "27"))

#: Filesystems a partition can be enlarged with, and the tool that does it;
#: the helper's RESIZE table, growing direction.
GROW_TOOLS = {"ext2": "resize2fs", "ext3": "resize2fs", "ext4": "resize2fs",
              "ntfs": "ntfsresize", "btrfs": "btrfs", "xfs": "xfs_growfs",
              "f2fs": "resize.f2fs", "swap": "mkswap"}
#: Why the others cannot be; the helper's NO_RESIZE, shortened.
NO_GROW = {
    "vfat": "FAT cannot be enlarged with the tools on this system",
    "exfat": "exFAT cannot be enlarged: no tool for it exists",
    "crypto_LUKS": "an encrypted volume is enlarged from inside, with the "
                   "tools of the system that uses it",
    "LVM2_member": "an LVM volume is enlarged with LVM's own tools",
    "bitlocker": "a volume encrypted by Windows is enlarged from Windows",
}

LIVE_SYSTEM = (
    "%s holds the running system (%s). A drive that is in use cannot be "
    "copied reliably: what is on it changes while it is being read, and the "
    "copy would not be consistent. To copy it, start the computer from a "
    "live USB stick and use the Disk Utility from there.")


def _portable(disk):
    return (disk.removable or disk.hotplug
            or disk.kind in (disks.USB, disks.CARD))


# ---- what can be copied -------------------------------------------------------

def source_availability(disk, volume=None):
    """Whether this drive or volume can be copied from, and if not, why.

    The same answer for a clone and for an image: both read every block.
    """
    if not disk.has_media:
        return diskops.no("There is no media in this device.")
    if volume is None or volume.kind == "whole":
        if disk.is_system:
            mounts = [m for v in disk.system_volumes for m in v.system_mounts]
            return diskops.no(LIVE_SYSTEM % ("This drive",
                                             ", ".join(sorted(set(mounts)))))
        if any(v.inner for v in disk.volumes):
            return diskops.no("Part of this drive is open as an encrypted "
                              "volume, LVM or RAID. Close that first.")
        return diskops.YES
    if volume.is_system:
        return diskops.no(LIVE_SYSTEM % ("This volume", ", ".join(
            volume.system_mounts)))
    if volume.is_extended:
        return diskops.no("An extended partition is only a container for "
                          "others; copy the partitions inside it.")
    if volume.inner:
        return diskops.no("This volume is open as an encrypted volume, LVM "
                          "or RAID. Close that first.")
    return diskops.YES


def target_availability(disk, volume=None):
    """Whether this drive or volume may be overwritten by a copy."""
    return diskops.availability(diskops.FORMAT, disk, volume)


def availability(action, disk, volume=None, tools=diskfs.find_tool):
    if action in (diskops.CLONE, diskops.IMAGE_SAVE):
        return source_availability(disk, volume)
    if action == diskops.RESCUE:
        answer = source_availability(disk, volume)
        if answer and tools("ddrescue") is None:
            return diskops.no(
                "The 'ddrescue' command is not installed.\n"
                "Fedora: sudo dnf install ddrescue\n"
                "Debian/Ubuntu: sudo apt install gddrescue\n"
                "Arch: sudo pacman -S ddrescue")
        return answer
    if action == diskops.IMAGE_CLOSE:
        if disk.kind != disks.IMAGE:
            return diskops.no("This is a drive, not an image file opened "
                              "as one.")
        if disk.is_system:
            return diskops.no("The running system uses this image.")
        return diskops.YES
    if action == diskops.IMAGE_WRITE:
        return target_availability(disk, volume)
    if action == diskops.RECOVERY_STICK:
        return stick_availability(disk, volume)
    return diskops.no("Not available.")


# ---- the recovery stick -----------------------------------------------------------

#: The smallest and largest drive the stick goes on: its system takes about
#: 150 MB, a stick sold as "1 GB" holds a little less than a GiB, and the
#: one FAT32 partition sits in an MBR table. The helper's RESCUE_MIN.
STICK_MIN = 900 * 1000 ** 2
STICK_MAX = diskfs.MBR_SECTORS * 512


def on_the_stick(marker="/etc/kss-rescue"):
    """Whether this is the recovery stick's own system."""
    return os.path.exists(marker)


def stick_availability(disk, volume=None, marker="/etc/kss-rescue"):
    if on_the_stick(marker):
        return diskops.no("A recovery stick is made from an installed "
                          "system, not from the recovery stick itself.")
    if not _whole(volume):
        return diskops.no("The recovery stick takes a whole drive: select "
                          "the drive rather than one of its volumes.")
    answer = target_availability(disk)
    if not answer:
        return answer
    if disk.size < STICK_MIN:
        return diskops.no("Too small: the recovery stick needs a drive of "
                          "1 GB or larger.")
    if disk.size > STICK_MAX * disk.logical_sector // 512:
        return diskops.no("Larger than 2 TiB, which the stick's partition "
                          "table cannot describe. Use a smaller drive.")
    return diskops.YES


def plan_recovery_stick(disk, verify=True, identities=None):
    """The request for the helper; the folder and the files it lists are
    filled in once the system has been put together."""
    plan = Plan(
        title="Make a Recovery Stick",
        target=describe_target(disk, None),
        loses=what_is_lost(disk, None),
        internal=not _portable(disk),
        confirm="Erase It and Make the Stick",
    )
    plan.steps = [
        "Put the recovery system together from this computer's kernel, "
        "Python, Qt and disk tools",
        "Erase %s and create one FAT32 partition on it" % disk.name,
        "Copy the boot loader, the kernel and the recovery system",
    ]
    plan.step_keys = ["erase", "table", "mkfs", "copy"]
    if verify:
        plan.steps.append("Read the stick back and compare it")
        plan.step_keys.append("verify")
    plan.advice = [Advice(
        INFO, "Everything on the drive is erased",
        "The stick is still usable for files afterwards: what the "
        "recovery system does not take stays free, in a FAT32 partition "
        "every computer can read.")]
    plan.request = {"op": "rescue_write", "device": "/dev/%s" % disk.name,
                    "identity": _identity(identities, disk.name),
                    "verify": bool(verify)}
    return plan


def stick_done_text(result):
    return ("The recovery stick is ready on %s%s. It starts with Secure "
            "Boot %s." % (
                result.get("device", "the drive"),
                " and was read back and checked" if result.get("verified")
                else "", "on" if result.get("secure_boot") else
                "turned off only - this system has no signed boot loader "
                "to give it"))


def needed(disk, mode):
    """How large a target has to be to take a copy of the whole drive."""
    if mode == EXACT or not disk.partitions:
        return disk.size
    end = max(v.start + v.size for v in disk.partitions
              if v.start is not None)
    reserve = (GPT_BACKUP_SECTORS * disk.logical_sector
               if disk.table == "gpt" else 0)
    # Never more than the drive itself: what fits an exact copy fits this.
    return min(disk.size, end + reserve)


def _whole(volume):
    return volume is None or volume.kind == "whole"


def targets(source_disk, source_volume, all_disks, mode=EXACT):
    """[(disk, volume or None, Availability)] - everything a copy could go
    to, each with whether it can and, if not, the reason.

    A whole drive is offered other whole drives; a partition is offered
    other partitions. What is too small or protected is listed all the
    same, greyed out, so that it is clear why it is not on offer.
    """
    found = []
    if _whole(source_volume):
        want = needed(source_disk, mode)
        for disk in all_disks:
            if disk.name == source_disk.name or not disk.has_media:
                continue
            answer = target_availability(disk)
            if answer and disk.size < want:
                answer = diskops.no(
                    "Too small: %s, and the copy needs %s%s."
                    % (disks.format_size(disk.size), disks.format_size(want),
                       " as an exact copy - 'used blocks only' would fit"
                       if mode == EXACT and disk.size >= needed(
                           source_disk, SMART) else ""))
            elif answer and disk.logical_sector != source_disk.logical_sector \
                    and source_disk.table:
                answer = diskops.no(
                    "This drive uses %d-byte sectors and the source %d: a "
                    "partition table cannot be carried from one to the "
                    "other." % (disk.logical_sector,
                                source_disk.logical_sector))
            found.append((disk, None, answer))
        return found

    for disk in all_disks:
        for volume in disk.partitions:
            if volume.name == source_volume.name:
                continue
            answer = target_availability(disk, volume)
            if answer and volume.size < source_volume.size:
                answer = diskops.no(
                    "Too small: %s, and the copy needs %s."
                    % (disks.format_size(volume.size),
                       disks.format_size(source_volume.size)))
            found.append((disk, volume, answer))
    return found


def method(volume, tools=diskfs.find_tool):
    """The tool a volume's used blocks would be copied with, or "" when it
    is copied block for block."""
    tool = SMART_TOOLS.get(volume.fstype)
    if tool is None or tools(tool) is None:
        return ""
    return tool


def methods(disk, volume=None, mode=EXACT, tools=diskfs.find_tool):
    """[(what, how)] - how each part of the source would be copied, in
    words, for the dialog to show before anything starts."""
    if mode == EXACT:
        return [("Everything", "every block, used or not")]
    parts = [volume] if not _whole(volume) else list(disk.partitions)
    if not parts:
        return [("Everything", "every block (this drive has no partitions "
                               "to copy one by one)")]
    found = []
    for part in parts:
        tool = method(part, tools)
        kind = diskfs.title_of(part.fstype) if part.fstype else "no filesystem"
        if tool:
            how = "used blocks only"
        elif part.fstype in SMART_TOOLS:
            how = "every block ('%s' is not installed)" \
                % SMART_TOOLS[part.fstype]
        elif diskfs.windows_state(part) is not None:
            how = "every block (only Windows can read inside it)"
        else:
            how = "every block (no tool copies %s by its contents)" % (
                kind if part.fstype else "an unformatted partition")
        found.append(("%s (%s, %s)" % (part.title, part.name, kind), how))
    return found


# ---- using the free space afterwards ----------------------------------------------

class Grow(NamedTuple):
    """What "use the free space" would do on a copy: enlarge `volume` (the
    source's partition, as the copy will have it) to `size` bytes - or,
    when `volume` is None, why it would not."""
    volume: Optional[disks.Volume]
    size: int = 0
    reason: str = ""


def _type_code(volume):
    kind = (volume.parttype or "").lower()
    return kind[2:] if kind.startswith("0x") else kind


def grow_candidate(source_disk, target_disk, tools=diskfs.find_tool):
    """Which partition a whole-drive copy onto a larger drive would enlarge
    into the room left at its end, and to what size. The helper works it
    out again from the copy itself; this is what the dialog says before."""
    if target_disk.size <= source_disk.size:
        return Grow(None, reason="The target is not larger than the source, "
                                 "so there is no free space to use.")
    parts = [v for v in source_disk.partitions if v.start is not None]
    if not parts:
        return Grow(None, reason="The source has no partitions to enlarge.")
    last = max(parts, key=lambda v: v.start)
    if source_disk.table == "dos" and (last.number or 0) > 4:
        return Grow(None, reason="Its last partition is a logical one, "
                                 "inside an extended partition; enlarging "
                                 "that is not offered.")
    if _type_code(last) in NO_GROW_TYPES:
        return Grow(None, reason="Its last partition is a system partition "
                                 "(%s), which is left at its size."
                                 % last.title)
    fstype = last.fstype or ""
    if fstype.lower() in NO_GROW or fstype in NO_GROW:
        return Grow(None, reason="Its last partition cannot be enlarged: %s."
                                 % NO_GROW.get(fstype, NO_GROW.get(
                                     fstype.lower())))
    if diskfs.windows_state(last) is not None:
        return Grow(None, reason="Its last partition can only be resized "
                                 "from Windows.")
    if fstype and fstype not in GROW_TOOLS:
        return Grow(None, reason="Its last partition holds %s, which cannot "
                                 "be enlarged from here."
                                 % diskfs.title_of(fstype))
    if fstype and tools(GROW_TOOLS[fstype]) is None:
        return Grow(None, reason="Enlarging %s needs '%s', which is not "
                                 "installed." % (diskfs.title_of(fstype),
                                                 GROW_TOOLS[fstype]))
    sector = target_disk.logical_sector
    end = target_disk.size
    if source_disk.table == "gpt":
        end -= GPT_BACKUP_SECTORS * sector
    elif source_disk.table == "dos":
        end = min(end, diskfs.MBR_SECTORS * sector)
    size = ((end - last.start) // MIB) * MIB
    if size - last.size < MIB:
        return Grow(None, reason=(
            "An MBR partition table cannot describe anything past 2 TiB, "
            "so the extra room cannot be used." if source_disk.table == "dos"
            and end < target_disk.size else
            "There is no free space after the last partition."))
    return Grow(last, size)


def grow_text(grow):
    """The line under the checkbox."""
    if grow.volume is None:
        return grow.reason
    volume = grow.volume
    return ("%s (%s%s) grows from %s to %s on the copy." % (
        volume.title, "partition %d" % volume.number
        if volume.number is not None else volume.name,
        ", " + diskfs.title_of(volume.fstype) if volume.fstype else "",
        disks.format_size(volume.size), disks.format_size(grow.size)))


# ---- advice ---------------------------------------------------------------------

def clone_advice(source_disk, source_volume, target_disk, target_volume,
                 mode=EXACT, grow=False):
    found = []
    whole = _whole(source_volume)
    found.append(Advice(
        WARNING, "The copy has the same identifiers as the original",
        "Both carry the same IDs afterwards, and a system finds its drives "
        "by those. Disconnect one of the two before restarting the computer "
        "- and before starting Windows, if this is a Windows drive - or the "
        "wrong one may be used."))
    source_size = source_disk.size if whole else source_volume.size
    target_size = target_disk.size if target_volume is None \
        else target_volume.size
    if target_size > source_size and not grow:
        found.append(Advice(
            INFO, "The extra room is not used by the copy",
            "The copy keeps the original's size: %s of the target stays "
            "free. Partitions can be made larger afterwards."
            % disks.format_size(target_size - source_size)))
    parts = [source_volume] if not whole else list(source_disk.partitions)
    if mode == SMART and any(p.fstype == "ntfs" for p in parts):
        found.append(Advice(
            INFO, "Windows has to be shut down completely",
            "A Windows volume left hibernated, or closed by 'fast startup', "
            "cannot be copied by its contents. If the copy is refused for "
            "that reason, the message says what to do in Windows; the exact "
            "copy works regardless."))
    if any(diskfs.windows_state(p) is diskfs.BITLOCKER for p in parts):
        found.append(Advice(
            INFO, "An encrypted Windows volume is copied as it is",
            "It stays encrypted on the copy and opens with the same key."))
    if target_disk.kind in (disks.USB, disks.CARD) and whole \
            and not _portable(source_disk):
        found.append(Advice(
            INFO, "A system copied to a USB drive may not start from it",
            "Whether a computer starts from the copy depends on how the "
            "system on it was set up. The data is all there either way."))
    return found


# ---- plans ----------------------------------------------------------------------

def _identity(identities, name):
    found = (identities or {}).get(name)
    return found if found is not None else disks.identity(name)


def plan_clone(source_disk, source_volume, target_disk, target_volume,
               mode=EXACT, verify=True, skip_errors=False, identities=None,
               tools=diskfs.find_tool, grow=False):
    whole = _whole(source_volume)
    if whole != (target_volume is None):
        raise ValueError("A whole drive is copied to a whole drive and a "
                         "partition to a partition.")
    source = source_disk.name if whole else source_volume.name
    target = target_disk.name if target_volume is None else target_volume.name
    plan = Plan(
        title="Clone",
        target="%s onto %s" % (
            describe_target(source_disk, None if whole else source_volume),
            describe_target(target_disk, target_volume)),
        loses=what_is_lost(target_disk, target_volume),
        advice=clone_advice(source_disk, source_volume, target_disk,
                            target_volume, mode, grow),
        internal=not _portable(target_disk),
        confirm="Erase the Target and Clone",
    )
    involved = (list(source_disk.volumes) if whole else [source_volume]) + (
        list(target_disk.volumes) if target_volume is None
        else [target_volume])
    if any(v.mounted for v in involved):
        plan.steps.append("Unmount the source and the target")
        plan.step_keys.append("unmount")
    how = methods(source_disk, source_volume, mode, tools)
    if mode == SMART and any("used blocks" in text for _what, text in how):
        plan.steps.append("Check the source's filesystems; a damaged one is "
                          "not copied")
        plan.step_keys.append("check")
    plan.steps.append(
        "Copy every block" if mode == EXACT else
        "Copy the partition table, then each partition: used blocks where "
        "a tool can tell them, every block otherwise"
        if whole else "Copy the filesystem's used blocks"
        if method(source_volume, tools) else "Copy every block")
    plan.step_keys.append("copy")
    if verify:
        plan.steps.append("Read the copy back and compare it with the "
                          "original")
        plan.step_keys.append("verify")
    grown = grow_candidate(source_disk, target_disk, tools) \
        if grow and whole else Grow(None)
    if grown.volume is not None:
        plan.steps.append("Enlarge %s to %s, into the free space at the end"
                          % (grown.volume.title,
                             disks.format_size(grown.size)))
        plan.step_keys.append("grow")
    plan.request = {
        "op": "clone", "mode": mode, "verify": bool(verify),
        "grow": grown.volume is not None,
        "skip_errors": bool(skip_errors and mode == EXACT),
        "source": "/dev/%s" % source, "target": "/dev/%s" % target,
        "source_identity": _identity(identities, source),
        "target_identity": _identity(identities, target),
    }
    return plan


def clone_done_text(result):
    text = "Cloned %s onto %s" % (result.get("source", "the source"),
                                  result.get("target", "the target"))
    text += (", and the copy was read back and matches."
             if result.get("verified") else "; the copy was not checked.")
    lost = result.get("skipped_bytes") or 0
    if lost:
        text += (" %s in %d place%s could not be read and is zeros on the "
                 "copy." % (disks.format_size(lost),
                            len(result.get("skipped") or ()),
                            "" if len(result.get("skipped") or ()) == 1
                            else "s"))
    grown = result.get("grown")
    if grown:
        text += " %s now uses the free space: %s instead of %s." % (
            grown.get("device", "The last partition"),
            disks.format_size(grown.get("new") or 0),
            disks.format_size(grown.get("old") or 0))
    elif result.get("grow_skipped"):
        text += " The free space was left unused: %s" % (
            result["grow_skipped"].split("\n")[0])
    return text


def skipped_report(result):
    """The unreadable ranges of a copy, one per line, for the user to keep."""
    lines = []
    for start, length in result.get("skipped") or ():
        lines.append("bytes %d to %d (%s, at %s into the source)"
                     % (start, start + length - 1, disks.format_size(length),
                        disks.format_size(start)))
    return lines


# ---- image files ----------------------------------------------------------------

def suggested_name(disk, volume=None, compress=False, now=None):
    """A file name for an image of this drive or volume."""
    base = (volume.label if volume is not None and volume.label
            else disk.title)
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in base)
    safe = "-".join(part for part in safe.split("-") if part) or "drive"
    stamp = time.strftime("%Y-%m-%d", time.localtime(now))
    name = "%s-%s-%s.img" % (
        safe, disk.name if _whole(volume) else volume.name, stamp)
    return name + (".gz" if compress else "")


def plan_image_save(disk, volume, path, compress=False, verify=True,
                    skip_errors=False, overwrite=False, identity=None):
    whole = _whole(volume)
    name = disk.name if whole else volume.name
    size = disk.size if whole else volume.size
    plan = Plan(title="Save image",
                target="%s to %s" % (describe_target(
                    disk, None if whole else volume), path))
    involved = list(disk.volumes) if whole else [volume]
    if any(v.mounted for v in involved):
        plan.steps.append("Unmount %s" % ("the drive's volumes" if whole
                                          else volume.title))
        plan.step_keys.append("unmount")
    plan.steps.append("Copy every block (%s) into the file%s" % (
        disks.format_size(size), ", compressed" if compress else ""))
    plan.step_keys.append("copy")
    if verify:
        plan.steps.append("Read the file back and compare it")
        plan.step_keys.append("verify")
    if not compress:
        plan.advice.append(Advice(
            INFO, "The file is as large as the %s" % (
                "drive" if whole else "volume"),
            "An image holds every block, used or not: %s. Compressing it "
            "makes the unused part take almost nothing, at the cost of "
            "time." % disks.format_size(size)))
    plan.remount = any(v.mounted for v in involved) and not whole
    plan.request = {
        "op": "image_create", "device": "/dev/%s" % name, "path": path,
        "compress": bool(compress), "verify": bool(verify),
        "skip_errors": bool(skip_errors), "overwrite": bool(overwrite),
        "identity": identity if identity is not None
        else disks.identity(name),
    }
    return plan


def image_saved_text(result):
    text = "Saved %s as %s (%s" % (
        result.get("source", "the drive"), result.get("path", "an image"),
        disks.format_size(result.get("stored", 0)))
    if result.get("compressed"):
        text += ", holding %s" % disks.format_size(result.get("size", 0))
    text += ")"
    text += (", read back and matching." if result.get("verified")
             else "; not checked.")
    lost = result.get("skipped_bytes") or 0
    if lost:
        text += " %s could not be read and is zeros in the image." \
            % disks.format_size(lost)
    return text


ISO_MAGIC_OFFSET = 0x8001
GZIP_MAGIC = b"\x1f\x8b"


def inspect_image(path):
    """What kind of file an image is: {"kind", "size", "advice"}.

    kind: "gzip" (a compressed image, size unknown until unpacked), "disk"
    (laid out as a drive: starts with a partition table), "iso-hybrid" (an
    installer image made to be written to a drive as it is), "iso" (a disc
    image that is not), "raw" (anything else), or "unreadable".
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(512)
            handle.seek(ISO_MAGIC_OFFSET)
            disc = handle.read(5) == b"CD001"
    except OSError as exc:
        return {"kind": "unreadable", "size": 0, "advice": [Advice(
            WARNING, "The file cannot be read", str(exc.strerror or exc))]}
    advice = []
    if head[:2] == GZIP_MAGIC:
        kind = "gzip"
        advice.append(Advice(
            INFO, "A compressed image",
            "It is unpacked once first, only to measure it, so that the "
            "drive is not touched if the image turns out not to fit."))
    else:
        table = len(head) == 512 and head[510:512] == b"\x55\xaa" and any(
            head[446 + 16 * n + 4] for n in range(4))
        if disc and table:
            kind = "iso-hybrid"
        elif disc:
            kind = "iso"
            advice.append(Advice(
                WARNING, "This disc image is not made for a USB drive",
                "It has no partition table of its own, so written to a "
                "drive as it is, most computers will not start from it. "
                "Windows installation images are like this. Use a tool made "
                "for them - Ventoy, or Microsoft's own media creation tool "
                "on Windows. Installer images of Linux systems are made to "
                "be written directly and do not show this warning."))
        elif table:
            kind = "disk"
        else:
            kind = "raw"
    return {"kind": kind, "size": size, "advice": advice}


def plan_image_write(disk, volume, path, verify=True, identity=None,
                     inspection=None):
    whole = _whole(volume)
    name = disk.name if whole else volume.name
    target = None if whole else volume
    inspection = inspection or inspect_image(path)
    size = disk.size if whole else volume.size
    if inspection["kind"] not in ("gzip", "unreadable") \
            and inspection["size"] > size:
        raise ValueError("The image holds %s and %s is only %s. It does "
                         "not fit." % (disks.format_size(inspection["size"]),
                                       name, disks.format_size(size)))
    plan = Plan(
        title="Write image",
        target="%s onto %s" % (os.path.basename(path),
                               describe_target(disk, target)),
        loses=what_is_lost(disk, target),
        advice=list(inspection["advice"]),
        internal=not _portable(disk),
        confirm="Erase and Write the Image",
    )
    if not whole and inspection["kind"] in ("iso-hybrid", "disk"):
        plan.advice.append(Advice(
            WARNING, "This image is laid out as a whole drive",
            "It carries its own partition table, so it belongs on a drive, "
            "not inside one partition. Select the drive itself instead."))
    involved = list(disk.volumes) if whole else [volume]
    if any(v.mounted for v in involved):
        plan.steps.append("Unmount %s" % ("the drive's volumes" if whole
                                          else volume.title))
        plan.step_keys.append("unmount")
    if inspection["kind"] == "gzip":
        plan.steps.append("Unpack the image once to measure it")
        plan.step_keys.append("measure")
    plan.steps.append("Erase the old signatures and write the image, block "
                      "for block")
    plan.step_keys.append("copy")
    if verify:
        plan.steps.append("Read the drive back and compare it with the "
                          "image")
        plan.step_keys.append("verify")
    plan.request = {
        "op": "image_restore", "device": "/dev/%s" % name, "path": path,
        "verify": bool(verify),
        "identity": identity if identity is not None
        else disks.identity(name),
    }
    return plan


def image_written_text(result):
    return "Wrote %s onto %s (%s)%s" % (
        os.path.basename(result.get("path", "the image")),
        result.get("target", "the drive"),
        disks.format_size(result.get("size", 0)),
        ", read back and matching." if result.get("verified")
        else "; not checked.")


# ---- a failing drive, and images opened as drives --------------------------------

RESCUE_ADVICE = Advice(
    WARNING, "Copy a failing drive before anything else",
    "Every hour a failing drive runs can be its last, and every attempt to "
    "read a damaged area wears it further. So the whole drive is copied "
    "once, easy areas first and difficult ones afterwards, and all further "
    "work - checking, searching for files - is done on the copy. Areas "
    "that cannot be read are left as zeros and recorded. If it is stopped, "
    "choosing the same file again carries on from where it was.")


def unfinished(path, isfile=os.path.isfile):
    """Whether an earlier rescue left this image and its map behind."""
    return bool(path) and isfile(path) and isfile(path + ".map")


def plan_rescue(disk, volume, path, identity=None, resume=False):
    whole = _whole(volume)
    name = disk.name if whole else volume.name
    size = disk.size if whole else volume.size
    plan = Plan(
        title="Rescue to image",
        target="%s to %s" % (describe_target(disk, None if whole
                                             else volume), path),
        advice=[RESCUE_ADVICE],
        request={"op": "rescue_image", "device": "/dev/%s" % name,
                 "path": path, "resume": bool(resume),
                 "identity": identity if identity is not None
                 else disks.identity(name)})
    involved = list(disk.volumes) if whole else [volume]
    if any(v.mounted for v in involved):
        plan.steps.append("Unmount %s" % ("the drive's volumes" if whole
                                          else volume.title))
        plan.step_keys.append("unmount")
    plan.steps += [
        "%s everything that reads without trouble (%s)" % (
            "Carry on copying" if resume else "Copy",
            disks.format_size(size)),
        "Go back to the areas that did not read, twice more"]
    plan.step_keys += ["copy", "retry"]
    return plan


def rescue_done_text(result):
    size = result.get("size") or 1
    missing = result.get("missing") or 0
    if not missing:
        return ("Rescued all of %s into %s."
                % (result.get("source", "the drive"),
                   result.get("path", "the image")))
    return ("Rescued %.2f%% of %s into %s. %s could not be read and is "
            "zeros in the image; the map file beside it records where."
            % (100.0 * (size - missing) / size,
               result.get("source", "the drive"),
               result.get("path", "the image"), disks.format_size(missing)))


def plan_attach(path):
    return Plan(title="Open image", target=os.path.basename(path),
                steps=["Present the image file as a read-only drive"],
                request={"op": "image_attach", "path": path})


def plan_detach(disk, identity=None):
    plan = Plan(title="Close image", target=disk.title,
                steps=["Stop presenting the image as a drive. The file is "
                       "not changed."],
                request={"op": "image_detach", "device": disk.path,
                         "identity": identity if identity is not None
                         else disks.identity(disk.name)})
    if any(v.mounted for v in disk.volumes):
        plan.steps.insert(0, "Unmount its volumes")
    return plan
