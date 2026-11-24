"""Prepared changes: the recovery stick's "nothing happens until Apply".

On the stick every operation that writes to a drive is held back and
listed. The drives are shown as they will be once the list is carried out -
a partition deleted is gone from the picture, one created is there, marked
as pending - so that the next change can be prepared on top of it. Nothing
touches a drive until Apply Changes; then the list runs in order, and stops
at the first thing that goes wrong.

A prepared request names its devices by where they are, not by the name the
kernel gave them: the drive (its name and the kernel's never-reused number
for it) and, for a partition, the byte it starts at in the picture at that
point. Just before each change runs, the drives are read again and those
places are turned back into device names and a fresh identity, which the
helper checks as it always does. A partition that an earlier change made or
moved is found where that change put it.

Qt-free.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import diskfs, disks

MIB = 1024 * 1024

#: Operations that write to a drive - the ones held back. Reading (checks,
#: scans, searches, the limits of a partition, opening an array or image
#: read-only) runs straight away: it is what preparing needs to know.
PREPARED = frozenset((
    "format", "label", "table_new", "part_create", "part_delete",
    "part_resize", "part_move", "partition_restore", "clone",
    "image_restore", "image_create", "rescue_image", "recover_files",
    "trim", "defrag", "wipe_free", "wipe_cleanup",
))

#: Where a request names a device, and where the identity for it goes.
DEVICE_KEYS = (("device", "identity"), ("source", "source_identity"),
               ("target", "target_identity"))

#: What a volume in the picture is waiting for, in words.
PENDING_TITLES = {
    "new": "New", "formatted": "Formatted", "resized": "Resized",
    "moved": "Moved", "renamed": "Renamed", "restored": "Restored",
    "copied": "Copied", "image": "Image",
}


def is_prepared(request):
    """Whether this request is held back on the stick."""
    op = request.get("op")
    if op == "check":
        return bool(request.get("repair"))
    return op in PREPARED


class PlanError(Exception):
    """Why a change cannot be prepared or carried out, in words."""


@dataclass
class Locator:
    """A device by where it is: a drive (name and diskseq) and, for a
    partition, its first byte."""
    disk: str
    diskseq: Optional[int]
    start: Optional[int] = None


@dataclass
class Change:
    """One prepared change."""
    plan: object                       # diskops.Plan
    places: dict                       # request key -> Locator
    drives: tuple                      # names of the drives it changes
    #: What else this change needs to know to show its result before it
    #: happens: the partition found by a search, the filesystem it holds.
    extra: dict = field(default_factory=dict)
    done: Optional[Callable] = None
    failed: Optional[Callable] = None
    error: str = ""

    @property
    def title(self):
        plan = self.plan
        return "%s: %s" % (plan.title, plan.target) if plan.target \
            else plan.title


def partition_name(disk_name, number):
    """The name the kernel gives partition `number` of a drive."""
    return "%s%s%d" % (disk_name, "p" if disk_name[-1:].isdigit() else "",
                       number)


def _with_free(disk, volumes, table=None):
    table = disk.table if table is None else table
    spans = disks.free_space(disk.size, table, volumes, disk.logical_sector)
    return dataclasses.replace(
        disk, table=table, volumes=tuple(sorted(
            volumes, key=lambda v: (v.start is None, v.start or 0))),
        free=tuple(disks.FreeSpace(disk.name, start, size)
                   for start, size in spans))


def _find(view, name):
    """(disk, volume or None) named `name` in the picture."""
    for disk in view:
        if disk.name == name:
            return disk, None
        for volume in disk.volumes:
            if volume.name == name:
                return disk, volume
    return None, None


def _name_of(path):
    return path[len("/dev/"):] if isinstance(path, str) \
        and path.startswith("/dev/") else None


def _number_for(disk, start):
    """The number a partition created at `start` will get: the lowest one
    free - above 4, inside an MBR's extended partition."""
    used = {v.number for v in disk.volumes if v.number is not None}
    logical = disk.table == "dos" and any(
        v.is_extended and v.start <= start < v.start + v.size
        for v in disk.volumes if v.start is not None)
    number = 5 if logical else 1
    while number in used:
        number += 1
    return number


class Queue:
    """The prepared changes, in order, and the picture they make."""

    def __init__(self):
        self.changes: list[Change] = []

    def __len__(self):
        return len(self.changes)

    def __bool__(self):
        return True

    def touches(self, disk_name):
        return any(disk_name in c.drives for c in self.changes)

    # ---- preparing ---------------------------------------------------------

    def add(self, plan, view, identities, done=None, failed=None,
            extra=None):
        """Hold a plan back. `view`: the drives as the window shows them
        (the picture with everything prepared so far). Raises PlanError
        when the request names something the picture does not have, or a
        drive whose contents are not known until an earlier change runs."""
        request = plan.request
        places, drives = {}, []
        for key, _identity_key in DEVICE_KEYS:
            name = _name_of(request.get(key))
            if name is None:
                continue
            disk, volume = _find(view, name)
            if disk is None:
                raise PlanError("%s is not in the list of drives." % name)
            if disk.pending == "image":
                raise PlanError(
                    "What %s will hold is only known once the image "
                    "prepared for it has been written. Apply the changes "
                    "first." % disk.title)
            seq = (identities or {}).get(disk.name, {}) or {}
            places[key] = Locator(disk.name, seq.get("diskseq"),
                                  None if volume is None or volume.kind
                                  == "whole" else volume.start)
            # A drive read from counts too: what is copied off it depends on
            # what was prepared for it before.
            if disk.name not in drives:
                drives.append(disk.name)
        change = Change(plan=plan, places=places, drives=tuple(drives),
                        extra=dict(extra or getattr(plan, "preview", None)
                                   or {}), done=done, failed=failed)
        # Make sure the picture can be drawn with it before keeping it.
        self.apply_change(list(view), change)
        self.changes.append(change)
        return change

    def remove(self, change):
        """Take a change back, and every later one that touches the same
        drives (they were prepared on top of it). Returns what went."""
        index = self.changes.index(change)
        drives = set(change.drives)
        gone = [change]
        for later in self.changes[index + 1:]:
            if drives & set(later.drives):
                gone.append(later)
                drives |= set(later.drives)
        self.changes = [c for c in self.changes if c not in gone]
        return gone

    def dependents(self, change):
        """What would go with `change` if it were taken back."""
        index = self.changes.index(change)
        drives = set(change.drives)
        found = []
        for later in self.changes[index + 1:]:
            if drives & set(later.drives):
                found.append(later)
                drives |= set(later.drives)
        return found

    def clear(self):
        self.changes = []

    # ---- the picture -------------------------------------------------------

    def view(self, real):
        """The drives as they will be once every change has run. A change
        that no longer fits the drives (one was unplugged) is marked with
        the reason and left out of the picture; Apply stops at it."""
        picture = list(real)
        for change in self.changes:
            try:
                picture = self.apply_change(picture, change)
            except PlanError as exc:
                change.error = str(exc)
                continue
            # Every drive the change writes to is marked, deletions too.
            written = {place.disk for key, place in change.places.items()
                       if key != "source"}
            picture = [dataclasses.replace(d, pending=d.pending or "changed")
                       if d.name in written else d for d in picture]
        return picture

    def apply_change(self, picture, change):
        request = change.plan.request
        op = request.get("op")
        handler = getattr(self, "_" + op, None)
        if handler is None:
            return picture
        return handler(picture, change, request)

    def _replace_disk(self, picture, disk):
        return [disk if d.name == disk.name else d for d in picture]

    def _target(self, picture, change, key="device"):
        place = change.places.get(key)
        if place is None:
            raise PlanError("The change names no drive.")
        disk = next((d for d in picture if d.name == place.disk), None)
        if disk is None:
            raise PlanError("%s is no longer connected." % place.disk)
        if place.start is None:
            return disk, None
        volume = next((v for v in disk.volumes if v.start == place.start
                       and v.kind == "part"), None)
        if volume is None:
            raise PlanError("A partition the change is for is no longer "
                            "there.")
        return disk, volume

    @staticmethod
    def _mark(volume, what, **changes):
        return dataclasses.replace(
            volume, pending=volume.pending or what, mountpoints=(),
            used=None, free=None, fs_size=None, **changes)

    def _format(self, picture, change, request):
        disk, volume = self._target(picture, change)
        fstype = request.get("fstype", "")
        label = request.get("label", "")
        if volume is not None:
            new = self._mark(volume, "formatted", fstype=fstype, label=label,
                             uuid="", inner=())
            volumes = [new if v is volume else v for v in disk.volumes]
            return self._replace_disk(picture, _with_free(disk, volumes))
        table = request.get("table") or "gpt"
        end = disk.size - (33 * disk.logical_sector if table == "gpt" else 0)
        size = (end - MIB) // MIB * MIB
        if table == "dos":
            size = min(size, diskfs.MBR_SECTORS * disk.logical_sector - MIB)
        name = partition_name(disk.name, 1)
        new = disks.Volume(name, "/dev/" + name, disk.name, "part", size,
                           start=MIB, number=1, fstype=fstype, label=label,
                           pending="formatted")
        return self._replace_disk(picture, _with_free(dataclasses.replace(
            disk, fstype=""), [new], table))

    def _label(self, picture, change, request):
        disk, volume = self._target(picture, change)
        if volume is None:
            return picture
        new = dataclasses.replace(volume, label=request.get("label", ""),
                                  pending=volume.pending or "renamed")
        volumes = [new if v is volume else v for v in disk.volumes]
        return self._replace_disk(picture, _with_free(disk, volumes))

    def _table_new(self, picture, change, request):
        disk, _volume = self._target(picture, change)
        return self._replace_disk(picture, _with_free(dataclasses.replace(
            disk, fstype=""), [], request.get("table") or "gpt"))

    def _part_create(self, picture, change, request):
        disk, _volume = self._target(picture, change)
        start, size = int(request["start"]), int(request["size"])
        if any(v.start is not None and not v.is_extended
               and v.start < start + size and start < v.start + v.size
               for v in disk.volumes):
            raise PlanError("That space is taken by a partition prepared "
                            "earlier.")
        number = _number_for(disk, start)
        name = partition_name(disk.name, number)
        new = disks.Volume(name, "/dev/" + name, disk.name, "part", size,
                           start=start, number=number,
                           fstype=request.get("fstype", ""),
                           label=request.get("label", ""), pending="new")
        return self._replace_disk(picture, _with_free(
            disk, list(disk.volumes) + [new]))

    def _part_delete(self, picture, change, request):
        disk, volume = self._target(picture, change)
        return self._replace_disk(picture, _with_free(
            disk, [v for v in disk.volumes if v is not volume]))

    def _part_resize(self, picture, change, request):
        disk, volume = self._target(picture, change)
        new = self._mark(volume, "resized", size=int(request["size"]))
        return self._replace_disk(picture, _with_free(
            disk, [new if v is volume else v for v in disk.volumes]))

    def _part_move(self, picture, change, request):
        disk, volume = self._target(picture, change)
        new = self._mark(volume, "moved", start=int(request["start"]))
        return self._replace_disk(picture, _with_free(
            disk, [new if v is volume else v for v in disk.volumes]))

    def _partition_restore(self, picture, change, request):
        disk, _volume = self._target(picture, change)
        start, size = int(request["start"]), int(request["size"])
        number = _number_for(disk, start)
        name = partition_name(disk.name, number)
        new = disks.Volume(name, "/dev/" + name, disk.name, "part", size,
                           start=start, number=number,
                           fstype=change.extra.get("fstype", ""),
                           label=change.extra.get("label", ""),
                           pending="restored")
        return self._replace_disk(picture, _with_free(
            disk, list(disk.volumes) + [new]))

    def _clone(self, picture, change, request):
        source, source_volume = self._target(picture, change, "source")
        target, target_volume = self._target(picture, change, "target")
        if source_volume is None:
            volumes = []
            for volume in source.volumes:
                if volume.kind != "part" or volume.number is None:
                    continue
                name = partition_name(target.name, volume.number)
                volumes.append(dataclasses.replace(
                    volume, name=name, path="/dev/" + name,
                    disk=target.name, mountpoints=(), inner=(),
                    pending="copied"))
            if request.get("grow") and volumes:
                from . import diskcopy
                grow = diskcopy.grow_candidate(source, target)
                if grow.volume is not None:
                    volumes = [dataclasses.replace(v, size=grow.size)
                               if v.start == grow.volume.start else v
                               for v in volumes]
            new = _with_free(dataclasses.replace(
                target, table_uuid=source.table_uuid, fstype=source.fstype),
                volumes, source.table)
            return self._replace_disk(picture, new)
        new = self._mark(target_volume, "copied",
                         fstype=source_volume.fstype,
                         label=source_volume.label, uuid=source_volume.uuid)
        return self._replace_disk(picture, _with_free(
            target, [new if v is target_volume else v
                     for v in target.volumes]))

    def _image_restore(self, picture, change, request):
        disk, volume = self._target(picture, change)
        if volume is not None:
            new = self._mark(volume, "image", fstype="", label="")
            return self._replace_disk(picture, _with_free(
                disk, [new if v is volume else v for v in disk.volumes]))
        # What the image holds is only known once it is written.
        return self._replace_disk(picture, dataclasses.replace(
            disk, table="", volumes=(), free=(), pending="image"))

    # ---- carrying it out ---------------------------------------------------

    def request_for(self, change, real, identity=disks.identity):
        """The change's request with its devices named as they are now:
        `real` is the drive list read just before it runs."""
        request = copy.deepcopy(change.plan.request)
        for key, identity_key in DEVICE_KEYS:
            place = change.places.get(key)
            if place is None:
                continue
            disk = next((d for d in real if d.name == place.disk), None)
            seq = identity(place.disk).get("diskseq") if disk else None
            if disk is None or (place.diskseq is not None
                                and seq != place.diskseq):
                raise PlanError("%s was unplugged since this change was "
                                "prepared." % place.disk)
            if place.start is None:
                name = disk.name
            else:
                volume = next((v for v in disk.volumes
                               if v.start == place.start and v.kind == "part"),
                              None)
                if volume is None:
                    raise PlanError(
                        "The partition this change is for is not on %s "
                        "where the earlier changes should have put it. "
                        "Nothing more is done." % disk.title)
                name = volume.name
            request[key] = "/dev/%s" % name
            request[identity_key] = identity(name)
        return request


def limits(disk, volume, real_limits=None):
    """What a partition can be resized or moved to on the picture: the room
    the prepared changes leave around it. `real_limits` is the helper's
    answer for the partition as it is on the drive - what its filesystem
    allows - or None for one that does not exist yet."""
    from . import diskcopy, diskparts
    before, after = diskparts.room(disk, volume)
    start, size = volume.start, volume.size
    found = dict(real_limits or {})
    smallest = found.get("smallest")
    if real_limits is None or volume.pending in ("new", "formatted",
                                                 "copied", "restored"):
        fs = diskfs.FILESYSTEMS.get(volume.fstype) if volume.fstype else None
        smallest = fs.min_size if fs is not None else MIB
        reason = diskcopy.NO_GROW.get(volume.fstype, "")
        found["grow"] = found["shrink"] = (
            "%s." % reason[:1].upper() + reason[1:] if reason else "")
        if volume.fstype in ("xfs", "f2fs"):
            found["shrink"] = ("%s can be made larger but never smaller."
                               % diskfs.title_of(volume.fstype))
    found.update({
        "start": start, "size": size,
        "lowest_start": start - before, "highest_end": start + size + after,
        "smallest": max(MIB, smallest or MIB),
        "largest": (size + after) // MIB * MIB,
    })
    return found


def reading_problem(request, view, queue):
    """Why an operation that only reads should not run now, or "": it would
    read the drive as it is, while the picture shows it as prepared. (The
    limits of a partition are worked out from the picture instead.)"""
    if request.get("op") == "part_limits":
        return ""
    for key, _identity_key in DEVICE_KEYS:
        name = _name_of(request.get(key))
        if name is None:
            continue
        disk, volume = _find(view, name)
        if volume is not None and volume.pending:
            return ("This reads %s as it is on the drive now, but changes "
                    "are prepared for it. Apply them first, or remove them "
                    "from the list." % volume.title)
        if disk is not None and volume is None and queue.touches(disk.name):
            return ("This reads %s as it is now, but changes are prepared "
                    "for it. Apply them first, or remove them from the "
                    "list." % disk.title)
    return ""


def copy_plan(plan, request):
    """The plan with the request as it is to be sent now."""
    new = copy.copy(plan)
    new.request = request
    return new


def summary(changes):
    """One plan describing a whole list, for the last confirmation."""
    from .diskops import Plan
    loses = []
    for change in changes:
        for item in change.plan.loses:
            if item not in loses:
                loses.append(item)
    drives = []
    for change in changes:
        # The drives written to: a clone's source is only read.
        for key, place in change.places.items():
            if key != "source" and place.disk not in drives:
                drives.append(place.disk)
    return Plan(
        title="Apply %d change%s" % (len(changes),
                                     "" if len(changes) == 1 else "s"),
        target="to %s" % ", ".join(drives),
        steps=[change.title for change in changes],
        loses=loses,
        internal=any(change.plan.internal for change in changes),
        confirm="Apply Changes")
