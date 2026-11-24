"""Recovering files: what a scan found, what state it is in, what to copy out.

Two kinds of finding. Deleted files the filesystem still has on record come
with their names, folders, sizes and dates; they are listed by The Sleuth Kit
and copied out on request. Files found by content - after a format, or on a
filesystem that keeps no such record - are recognised by what is in them and
come without names; PhotoRec saves those as it finds them.

Everything is kept in a recovery folder on another drive: the scan's
listings, the files found and the files recovered. That folder is the saved
scan; opening it again shows the same results without reading the drive.

Qt-free. Reading a raw device needs root, so the scanning and the copying
out are the privileged helper's; this module reads what it wrote.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import struct
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass, field

from . import diskfs, diskops, disks, fatscan, ntfsscan
from .diskfs import INFO, WARNING, Advice
from .diskops import Plan, describe_target

SESSION_MARK = "scan.json"
FOUND_DIR = "found"
RECOVERED_DIR = "recovered"
SELECTION_NAME = "selection.json"
FOLDER_PREFIX = "KSysSupervisor Recovery"

#: Filesystems whose deleted files can be listed with their names.
LISTABLE = ("vfat", "exfat", "ntfs")
NO_RECORD = ("ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs")

DELETED = "deleted"
FOUND = "found"
#: Known from the records of a filesystem that has been formatted over.
LOST = "lost"
#: The folder such files are shown under, to keep them apart from the ones
#: the present filesystem still has on record.
BEFORE = "Before the format"

# How a file that has been looked at turned out.
INTACT = "intact"
DAMAGED = "damaged"
UNREADABLE = "unreadable"
UNKNOWN = "unknown"
STATUS_TITLES = {INTACT: "Intact", DAMAGED: "Partly damaged",
                 UNREADABLE: "Unreadable", UNKNOWN: "Not checked"}

CATEGORIES = (
    ("Pictures", ("jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "heic",
                  "heif", "webp", "svg", "psd", "ico", "raw", "cr2", "cr3",
                  "nef", "arw", "dng", "orf", "rw2", "raf", "x3f", "pef")),
    ("Video", ("mp4", "mov", "avi", "mkv", "wmv", "flv", "m4v", "mpg", "mpeg",
               "webm", "3gp", "mts", "m2ts", "vob", "asf")),
    ("Audio", ("mp3", "wav", "flac", "ogg", "m4a", "aac", "wma", "opus",
               "aiff", "aif", "mid", "ape")),
    ("Documents", ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt",
                   "ods", "odp", "rtf", "txt", "md", "csv", "epub", "pages",
                   "numbers", "key", "tex", "xml", "html", "htm", "json")),
    ("Archives", ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "iso",
                  "cab", "jar", "apk", "deb", "rpm", "dmg")),
)
OTHER = "Other"
ZIP_FAMILY = ("zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub",
              "jar", "apk", "pages", "numbers", "key")
OLE_FAMILY = ("doc", "xls", "ppt", "msi", "msg")


def category(name):
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for title, extensions in CATEGORIES:
        if extension in extensions:
            return title
    return OTHER


@dataclass
class Entry:
    """One file a scan turned up."""
    path: str                    # as it was: "Photos/2024/holiday.png"
    size: int = 0
    mtime: int = 0               # epoch seconds, 0 when not known
    source: str = DELETED        # DELETED or FOUND
    device: str = ""             # for a deleted file: where it is read from
    inode: str = ""              # and by what address
    file: str = ""               # for a found file: where it was saved
    reallocated: bool = False    # its record has been reused since
    extents: list = None         # for a lost file: where its contents are
    inline: str = ""             # or the contents themselves, as hex
    status: str = UNKNOWN
    detail: str = ""
    chosen: bool = False

    @property
    def name(self):
        return self.path.rsplit("/", 1)[-1]

    @property
    def folder(self):
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def category(self):
        return category(self.name)

    @property
    def chance(self):
        """What can be said before the file has been copied out."""
        if self.source == FOUND:
            return STATUS_TITLES[self.status] if self.status != UNKNOWN \
                else "Found whole or in part"
        if self.source == LOST:
            if self.extents is None:
                return "Poor: its contents cannot be located"
            return "Fair: named in the old records; judged once recovered"
        if self.reallocated:
            return "Poor: its place has been reused"
        return "Good: still on record"


# ---- reading a scan's listings -------------------------------------------------

def parse_body(path, device):
    """[Entry] from The Sleuth Kit's listing of one volume.

    One line per name, fields divided by "|"; the name is second and may
    itself hold that character, so the line is taken apart from both ends.
    Left out: folders, NTFS's second listing of each name, the named
    streams Windows attaches to a file, and records with nothing in them.
    """
    found = []
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return found
    with handle:
        for line in handle:
            fields = line.rstrip("\n").split("|")
            if len(fields) < 11:
                continue
            name = "|".join(fields[1:-9])
            inode, mode, size = fields[-9], fields[-8], fields[-5]
            if "(deleted" not in name or "($FILE_NAME)" in name:
                continue
            reallocated = name.endswith("(deleted-realloc)")
            name = name.rsplit(" (deleted", 1)[0].strip("/")
            kind = mode.split("/", 1)[1][:1] if "/" in mode else mode[:1]
            if kind == "d" or not name:
                continue
            try:
                size, mtime = int(size), int(fields[-3])
            except ValueError:
                continue
            base = name.rsplit("/", 1)[-1]
            if size <= 0 or ":" in base:
                continue
            if name.startswith("$OrphanFiles/"):
                name = "Folder unknown/" + base
            elif name.startswith("$"):
                continue                 # the filesystem's own files
            found.append(Entry(path=name, size=size, mtime=mtime,
                               device=device, inode=inode,
                               reallocated=reallocated))
    return found


#: Files PhotoRec writes beside what it finds.
PHOTOREC_OWN = ("photorec.log", "photorec.ses", "report.xml")


def lost_files(records_path, info_path, device, dirs_path=""):
    """[Entry] for the files of an earlier filesystem, from what the helper
    collected off the device: NTFS file records, and FAT / exFAT folders."""
    try:
        with open(info_path, encoding="utf-8") as handle:
            info = json.load(handle)
        boot = bytes.fromhex(info.get("boot", ""))
        table = bytes.fromhex(info.get("table", ""))
        root = bytes.fromhex(info.get("root", ""))
        size = int(info.get("size") or 0)
    except (OSError, ValueError, AttributeError):
        boot, table, root, size = b"", b"", b"", 0
    found = []
    if dirs_path:
        for path, entry, extents, _kind in fatscan.files(
                fatscan.read_runs(dirs_path), size, boot, root):
            found.append(Entry(
                path="%s/%s" % (BEFORE, path), size=entry["size"],
                mtime=entry["mtime"], source=LOST, device=device,
                extents=extents, inline=""))
    if not records_path:
        return found
    cluster, skip = ntfsscan.current_filesystem(
        boot, lambda offset, length: table)
    records = ntfsscan.read_records(records_path)
    for path, record, extents in ntfsscan.files(records, cluster, size,
                                                skip):
        found.append(Entry(
            path="%s/%s" % (BEFORE, path), size=record["size"],
            mtime=record["mtime"], source=LOST, device=device,
            extents=extents,
            inline=record["inline"].hex() if record["inline"] is not None
            else ""))
    return found


def found_files(folder):
    """[Entry] for what the search by content saved in a work folder."""
    found = []
    top = os.path.join(folder, FOUND_DIR)
    for base, _dirs, files in os.walk(top):
        for name in sorted(files):
            if name in PHOTOREC_OWN or name.startswith("photorec."):
                continue
            path = os.path.join(base, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            found.append(Entry(
                path="Found by content/%s/%s" % (category(name), name),
                size=info.st_size, mtime=0, source=FOUND, file=path))
    return found


# ---- where a search works, and where files go --------------------------------
#
# A Quick Scan writes only small listings, kept in a private folder in memory
# (the user's runtime folder is a RAM disk), so it needs no place on any
# drive. A Deep Scan saves every file it finds by content as it finds it, so
# it works in a folder on another drive. Where recovered files go is chosen
# only when they are recovered. What a search found is kept in one search
# file, which opens again without reading the drive - on this computer or
# another one.

#: The kinds a Deep Scan can be limited to: (what results call it, what the
#: user is shown, PhotoRec's own names for the families that hold them).
KINDS = (
    ("Pictures", "Photos", ("jpg", "png", "gif", "bmp", "tif", "psd", "raw",
                            "crw", "orf", "raf", "rw2", "x3f", "ico", "mov",
                            "riff")),
    ("Video", "Video", ("mov", "mpg", "riff", "mkv", "asf", "flv", "m2ts")),
    ("Audio", "Music", ("mp3", "flac", "ogg", "aif", "ape", "mid", "riff",
                        "asf", "mov")),
    ("Documents", "Documents", ("doc", "pdf", "zip", "txt", "xml", "ps",
                                "indd", "wpd", "pst", "mobi", "chm")),
    ("Archives", "Archives", ("zip", "rar", "7z", "gz", "bz2", "xz", "tar",
                              "cab", "iso")),
)
DEFAULT_KINDS = ("Pictures", "Video", "Audio", "Documents")
SEARCH_SUFFIX = ".kssearch"
SEARCH_FORMAT = "ksyssupervisor-search"
SEARCH_VERSION = 2
WORK_PREFIX = "KSysSupervisor Search"


def families(kinds):
    """PhotoRec's family names for the kinds chosen, each once."""
    found = []
    for key, _title, names in KINDS:
        if key in kinds:
            found += [n for n in names if n not in found]
    return found


def on_drive(path, disk):
    """Whether a folder is on the drive itself, as far as its mounted
    volumes show: writing there can overwrite what is sought."""
    try:
        where = os.stat(path).st_dev
    except OSError:
        return False
    for volume in disk.volumes:
        for point in volume.all_mountpoints():
            try:
                if point != disks.SWAP and os.stat(point).st_dev == where:
                    return True
            except OSError:
                continue
    return False


def folder_problem(parent, disk, needed=0):
    """Why a place cannot be written to during a recovery, or ""."""
    if not parent or not os.path.isabs(parent):
        return "Choose a folder on another drive."
    if not os.path.isdir(parent):
        return "The folder %s does not exist." % parent
    if not os.access(parent, os.W_OK):
        return "You cannot write to %s." % parent
    if disk is not None and on_drive(parent, disk):
        return ("That folder is on the drive being searched. Everything "
                "written there can overwrite the very files you want "
                "back: choose a place on another drive.")
    try:
        room = os.statvfs(parent)
        free = room.f_bavail * room.f_frsize
    except OSError:
        return ""
    if needed and free < needed:
        return ("Only %s is free there; this may need up to %s."
                % (disks.format_size(free), disks.format_size(needed)))
    return ""


def _mark(path):
    """Make `path` a folder the helper will work in."""
    with open(os.path.join(path, SESSION_MARK), "w",
              encoding="utf-8") as handle:
        json.dump({"version": 1}, handle)
    return path


def scratch_base():
    """Where private working folders go: the user's runtime folder, which
    is in memory, else the system's temporary folder."""
    return os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()


def scratch_folder(base=None):
    """A new private working folder, for listings and the list of files to
    recover. Removed with remove_scratch() when done."""
    path = tempfile.mkdtemp(prefix="ksyssupervisor-search-",
                            dir=base or scratch_base())
    return _mark(path)


def remove_scratch(path):
    if path and os.path.basename(path).startswith("ksyssupervisor-search-"):
        shutil.rmtree(path, ignore_errors=True)


def work_folder(parent, now=None):
    """A new folder on another drive for what a Deep Scan finds."""
    stamp = time.strftime("%Y-%m-%d %H.%M.%S", time.localtime(now))
    path = os.path.join(parent, "%s %s" % (WORK_PREFIX, stamp))
    os.makedirs(path)
    return _mark(path)


def recovery_folder(parent, now=None):
    """A new folder in `parent` for the files being recovered."""
    stamp = time.strftime("%Y-%m-%d %H.%M.%S", time.localtime(now))
    path = os.path.join(parent, "%s %s" % (FOLDER_PREFIX, stamp))
    serial = 1
    while os.path.lexists(path):
        serial += 1
        path = os.path.join(parent, "%s %s (%d)" % (FOLDER_PREFIX, stamp,
                                                     serial))
    os.makedirs(path)
    return path


# ---- the search file ------------------------------------------------------------

def drive_record(disk, volume=None):
    """What tells this drive and volume apart from others, as far as it
    stays the same on another day or another computer - not its name."""
    found = {"model": disk.model, "serial": disk.serial, "wwn": disk.wwn,
             "size": disk.size, "table_uuid": disk.table_uuid,
             "name": disk.name, "title": disk.title}
    if volume is not None and volume.kind != "whole":
        found["volume"] = {"name": volume.name, "number": volume.number,
                           "start": volume.start, "size": volume.size,
                           "uuid": volume.uuid}
    return found


def find_drive(record, drives):
    """(disk, volume or None) connected now that a search was made on, or
    (None, None). The name the system gave it does not count: it changes
    from one time to the next.

    Matched by serial number, WWN or partition table identifier, with the
    size. A drive that reports none of these - many cheap memory sticks -
    is matched by model and size, but only where exactly one connected
    drive fits: of two alike, neither is guessed."""
    strong = [key for key in ("serial", "wwn", "table_uuid")
              if record.get(key)]
    if strong:
        fits = [d for d in drives if d.size == record.get("size")
                and any(getattr(d, key) == record[key] for key in strong)]
    else:
        fits = [d for d in drives if d.size == record.get("size")
                and d.model == record.get("model")]
    if len(fits) != 1:
        return None, None
    disk = fits[0]
    wanted = record.get("volume")
    if not wanted:
        return disk, None
    for volume in disk.volumes:
        if volume.start == wanted.get("start") \
                and volume.size == wanted.get("size"):
            return disk, volume
    return None, None


def new_search(disk, volume=None, now=None):
    whole = volume is None or volume.kind == "whole"
    return {"format": SEARCH_FORMAT, "version": SEARCH_VERSION,
            "created": int(now or time.time()),
            "what": describe_target(disk, None if whole else volume),
            "device": disk.path if whole else volume.path,
            "drive": drive_record(disk, None if whole else volume),
            "scans": [], "work": "", "recovered": []}


_FIELDS = ("path", "size", "mtime", "source", "device", "inode", "file",
           "reallocated", "extents", "inline", "status", "detail", "chosen")


def save_search(path, record, entries):
    """Write a search and what it found to one file, atomically. Files a
    Deep Scan saved are named relative to its work folder, so the two can
    be moved together."""
    work = record.get("work") or ""
    items = []
    for entry in entries:
        item = {name: getattr(entry, name) for name in _FIELDS}
        if work and entry.file.startswith(work.rstrip("/") + "/"):
            item["file"] = "@work/" + entry.file[len(work.rstrip("/")) + 1:]
        items.append(item)
    data = dict(record, entries=items, saved=int(time.time()))
    temporary = path + ".new"
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)
    return path


def load_search(path, work=None):
    """(record, [Entry]) of a search file, or raises ValueError. `work`:
    where its work folder is now, when it was moved."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, EOFError):
        raise ValueError("%s is not a search file." % path)
    if not isinstance(data, dict) or data.get("format") != SEARCH_FORMAT:
        raise ValueError("%s is not a search file." % path)
    if data.get("version") != SEARCH_VERSION:
        raise ValueError("%s was made by a different version." % path)
    if work:
        data["work"] = work
    base = data.get("work") or ""
    entries = []
    for item in data.pop("entries", None) or ():
        if not isinstance(item, dict):
            continue
        fields = {name: item[name] for name in _FIELDS if name in item}
        if str(fields.get("file", "")).startswith("@work/"):
            fields["file"] = os.path.join(base, fields["file"][6:]) \
                if base else ""
        try:
            entries.append(Entry(**fields))
        except TypeError:
            continue
    return data, entries


def missing_found(entries):
    """How many files found by content are not where the search says - its
    work folder was moved, or is on a drive not connected."""
    return sum(1 for e in entries if e.source == FOUND
               and (not e.file or not os.path.exists(e.file)))


def searches_folder():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    return os.path.join(base, "KSysSupervisor", "Searches")


def search_name(record):
    stamp = time.strftime("%Y-%m-%d %H.%M", time.localtime(
        record.get("created") or time.time()))
    what = "".join(c if c.isalnum() or c in " ._-()" else " "
                   for c in record.get("what") or "drive").strip()
    return "%s %s%s" % (what[:60], stamp, SEARCH_SUFFIX)


def autosave_place(record, disk):
    """Where a search is kept by itself, or None when that place is on the
    drive searched - writing there could overwrite what is sought."""
    folder = searches_folder()
    probe = folder
    while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    if disk is not None and on_drive(probe, disk):
        return None
    return os.path.join(folder, search_name(record))


# ---- what a scan found ----------------------------------------------------------

def scan_entries(result, folder):
    """[Entry] of one scan's result: the listings in `folder` and what the
    Deep Scan saved there. The listings are not needed once read."""
    entries = []

    def inside(volume, name):
        # Only ever a file in the folder itself.
        name = os.path.basename(volume.get(name) or "")
        return os.path.join(folder, name) if name else ""

    for volume in result.get("volumes") or ():
        entries += parse_body(inside(volume, "body"),
                              volume.get("device", ""))
    for volume in result.get("old") or ():
        entries += lost_files(inside(volume, "records"),
                              inside(volume, "info"),
                              volume.get("device", ""),
                              inside(volume, "dirs"))
        for name in ("records", "info", "dirs"):
            if inside(volume, name):
                try:
                    os.unlink(inside(volume, name))
                except OSError:
                    pass
    entries += found_files(folder)
    return entries


def keep_kinds(entries, kinds):
    """Drop, and delete from the work folder, what a Deep Scan limited to
    `kinds` found of other kinds: PhotoRec's families hold more than one
    kind each (the one for AVI video also holds WAV sound)."""
    kept = []
    for entry in entries:
        if entry.source == FOUND and entry.category not in kinds:
            try:
                os.unlink(entry.file)
            except OSError:
                pass
            continue
        kept.append(entry)
    return kept


def _start_sector(entry):
    """The sector a file found by content starts at, from the name PhotoRec
    gives it (f<sector>.<ext>), or None."""
    name = entry.name
    if not name.startswith("f") or "." not in name:
        return None
    digits = name[1:].split(".", 1)[0].split("_", 1)[0]
    return int(digits) if digits.isdigit() else None


def merge(old, new, starts=None):
    """`old` with what is new in `new`. A file found by content where an
    old filesystem's record says a named file starts is that file, and is
    left out: it is in the list already, with its name.

    `starts`: {device path: byte offset of that device on the drive}, to
    compare places on a partition with places on the whole drive."""
    starts = starts or {}
    seen = {(e.source, e.device, e.inode, e.path, e.file) for e in old}
    named = set()
    for entry in list(old) + list(new):
        if entry.source == LOST and entry.extents \
                and entry.extents[0][0] is not None:
            named.add(starts.get(entry.device, 0) + entry.extents[0][0])
    merged = list(old)
    for entry in new:
        key = (entry.source, entry.device, entry.inode, entry.path,
               entry.file)
        if key in seen:
            continue
        if entry.source == FOUND:
            sector = _start_sector(entry)
            if sector is not None and starts.get(entry.device, 0) \
                    + sector * 512 in named:
                try:
                    os.unlink(entry.file)
                except OSError:
                    pass
                continue
        seen.add(key)
        merged.append(entry)
    return merged


def write_selection(folder, entries):
    """Hand the helper the files to read off the drive. Returns how many."""
    chosen = [{"device": e.device, "inode": e.inode, "path": e.path,
               "size": e.size}
              for e in entries if e.source == DELETED and e.chosen]
    chosen += [{"device": e.device, "path": e.path, "size": e.size,
                "extents": e.extents or [], "inline": e.inline}
               for e in entries if e.source == LOST and e.chosen
               and (e.extents is not None or e.inline)]
    with open(os.path.join(folder, SELECTION_NAME), "w",
              encoding="utf-8") as handle:
        json.dump(chosen, handle)
    return len(chosen)


def move_found(entries, target):
    """Move the chosen files found by content into the recovery folder,
    sorted by kind. Returns how many."""
    moved = 0
    for entry in entries:
        if entry.source != FOUND or not entry.chosen or not entry.file \
                or not os.path.exists(entry.file):
            continue
        folder = os.path.join(target, "Found by content", entry.category)
        os.makedirs(folder, exist_ok=True)
        destination = os.path.join(folder, entry.name)
        serial = 1
        while os.path.lexists(destination):
            serial += 1
            stem, dot, ext = entry.name.rpartition(".")
            destination = os.path.join(folder, "%s (%d)%s%s" % (
                stem or ext, serial, dot, ext if stem else ""))
        try:
            shutil.move(entry.file, destination)
            entry.file = destination
            moved += 1
        except OSError:
            pass
    return moved


# ---- looking at a file -----------------------------------------------------------

def _png(handle, size):
    if handle.read(8) != b"\x89PNG\r\n\x1a\n":
        return UNREADABLE, "Not a PNG image: its contents are something else."
    place = 8
    while True:
        head = handle.read(8)
        if len(head) < 8:
            return DAMAGED, ("Cut short after %d of %d bytes: the top of "
                             "the picture may show." % (place, size))
        length, kind = struct.unpack(">I4s", head)
        data = handle.read(min(length, size))
        crc = handle.read(4)
        if len(data) < length or len(crc) < 4:
            return DAMAGED, ("Cut short after %d of %d bytes: the top of "
                             "the picture may show." % (place, size))
        if zlib.crc32(kind + data) != struct.unpack(">I", crc)[0]:
            return DAMAGED, ("Part of it has been overwritten (%d bytes "
                             "in)." % place)
        place += 12 + length
        if kind == b"IEND":
            return INTACT, "Every part checks out."


def _jpeg(handle, size):
    if handle.read(3) != b"\xff\xd8\xff":
        return UNREADABLE, "Not a JPEG image: its contents are something " \
                           "else."
    handle.seek(max(0, size - 65536))
    tail = handle.read()
    if b"\xff\xd9" not in tail:
        return DAMAGED, "Its end is missing: the top of the picture may show."
    return INTACT, "It has its beginning and its end."


def _pdf(handle, size):
    if not handle.read(1024).lstrip().startswith(b"%PDF-"):
        return UNREADABLE, "Not a PDF: its contents are something else."
    handle.seek(max(0, size - 4096))
    tail = handle.read()
    if b"%%EOF" not in tail or b"startxref" not in tail:
        return DAMAGED, ("Its end, which holds the index of its pages, is "
                         "missing. A repair may get the pages back.")
    return INTACT, "It has its beginning and its index."


def _zip(path):
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            count = len(archive.namelist())
    except zipfile.BadZipFile:
        return DAMAGED, ("Its index is missing or broken. A repair may get "
                         "the parts that are whole back.")
    except (OSError, RuntimeError, ValueError, NotImplementedError) as exc:
        return DAMAGED, "It could not be read through: %s." % exc
    if bad:
        return DAMAGED, "One of its %d parts is damaged (%s)." % (count, bad)
    return INTACT, "All %d parts check out." % count


def _boxes(handle, size):
    """An MP4 or QuickTime file is a chain of boxes that says how long each
    is; a whole file's boxes add up to its size."""
    place = 0
    seen = set()
    while place < size:
        handle.seek(place)
        head = handle.read(16)
        if len(head) < 8:
            break
        length, kind = struct.unpack(">I4s", head[:8])
        if length == 1 and len(head) == 16:
            length = struct.unpack(">Q", head[8:])[0]
        elif length == 0:
            length = size - place
        if length < 8:
            return DAMAGED, "Its structure breaks off partway through."
        seen.add(kind)
        place += length
    if b"ftyp" not in seen:
        return UNREADABLE, "Not a video of this kind: its contents are " \
                           "something else."
    if place != size or b"moov" not in seen:
        return DAMAGED, ("It is cut short, or the part that says how to "
                         "play it is missing.")
    return INTACT, "Its parts add up."


def inspect_file(path):
    """(status, one sentence) for a file that has been copied out.

    By its structure, for the kinds whose structure says whether they are
    whole; for the rest by whether the contents are what the name claims.
    """
    try:
        size = os.path.getsize(path)
        if size == 0:
            return UNREADABLE, "It is empty."
        extension = path.rsplit(".", 1)[-1].lower() if "." in \
            os.path.basename(path) else ""
        with open(path, "rb") as handle:
            head = handle.read(4096)
            if not head.strip(b"\0") and size > len(head) // 2:
                handle.seek(max(0, size // 2))
                if not handle.read(4096).strip(b"\0"):
                    return UNREADABLE, ("It holds only zeros: what was "
                                        "there has been erased.")
            handle.seek(0)
            if extension == "png":
                return _png(handle, size)
            if extension in ("jpg", "jpeg"):
                return _jpeg(handle, size)
            if extension == "pdf":
                return _pdf(handle, size)
            if extension in ("mp4", "mov", "m4v", "m4a", "3gp"):
                return _boxes(handle, size)
            if extension == "gif":
                if head[:6] not in (b"GIF87a", b"GIF89a"):
                    return UNREADABLE, "Not a GIF image."
                handle.seek(size - 1)
                return (INTACT, "It has its beginning and its end.") \
                    if handle.read(1) == b";" else \
                    (DAMAGED, "Its end is missing.")
            if extension in OLE_FAMILY:
                if head[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                    return UNREADABLE, ("Not an Office document: its "
                                        "contents are something else.")
                return INTACT, ("It starts as an Office document should; "
                                "open it to be sure of the rest.")
        if extension in ZIP_FAMILY:
            if head[:2] != b"PK":
                return UNREADABLE, "Not what its name says: its contents " \
                                   "are something else."
            return _zip(path)
    except OSError as exc:
        return UNREADABLE, "It cannot be read: %s." % (exc.strerror or exc)
    return UNKNOWN, "Its kind cannot be checked from here; open it to see."


def inspect_all(entries, progress=None):
    """Look at every entry that has a file; fills in status and detail."""
    todo = [e for e in entries if e.file]
    for index, entry in enumerate(todo):
        entry.status, entry.detail = inspect_file(entry.file)
        if progress is not None:
            progress(index + 1, len(todo))
    return summary(todo)


def summary(entries):
    counts = {INTACT: 0, DAMAGED: 0, UNREADABLE: 0, UNKNOWN: 0}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return counts


def summary_text(counts):
    parts = []
    for key, one, many in ((INTACT, "%d intact", "%d intact"),
                           (DAMAGED, "%d partly damaged",
                            "%d partly damaged"),
                           (UNREADABLE, "%d unreadable", "%d unreadable"),
                           (UNKNOWN, "%d of a kind that cannot be checked",
                            "%d of a kind that cannot be checked")):
        if counts.get(key):
            parts.append((one if counts[key] == 1 else many) % counts[key])
    return ", ".join(parts) or "nothing"


def attach_recovered(folder, entries):
    """After the helper copied files out: give each entry its file, from
    the report it left in the working folder. Returns the paths that
    failed, with why."""
    try:
        with open(os.path.join(folder, "recovered.json"),
                  encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        return []
    by_path = {}
    for item in report.get("recovered") or ():
        by_path.setdefault(item.get("path"), []).append(item)
    for entry in entries:
        if entry.source in (DELETED, LOST) and entry.chosen \
                and by_path.get(entry.path):
            entry.file = by_path[entry.path].pop(0).get("file", "")
    return list(report.get("failed") or ())


# ---- the tree --------------------------------------------------------------------

@dataclass
class Node:
    """A folder of the results, or a file in one."""
    name: str
    entry: Entry = None
    children: dict = field(default_factory=dict)
    files: int = 0               # files beneath, counted once
    size: int = 0

    @property
    def is_folder(self):
        return self.entry is None

    def leaves(self):
        if self.entry is not None:
            yield self.entry
        for child in self.children.values():
            yield from child.leaves()

    def state(self):
        """0 none chosen, 2 all, 1 some."""
        if self.entry is not None:
            return 2 if self.entry.chosen else 0
        some = every = None
        for entry in self.leaves():
            some = some or entry.chosen
            every = entry.chosen if every is None else (every
                                                        and entry.chosen)
            if some and not every:
                return 1
        return 2 if every else (1 if some else 0)

    def choose(self, on):
        for entry in self.leaves():
            entry.chosen = bool(on)


def _add(root, parts, entry):
    node = root
    for part in parts[:-1]:
        node.files += 1
        node.size += entry.size
        node = node.children.setdefault(part, Node(part))
    node.files += 1
    node.size += entry.size
    name, serial = parts[-1], 1
    while name in node.children:
        serial += 1
        name = "%s (%d)" % (parts[-1], serial)
    node.children[name] = Node(name, entry=entry, files=1, size=entry.size)


def by_folder(entries):
    """The results as the folders the files were in."""
    root = Node("")
    for entry in entries:
        _add(root, entry.path.split("/"), entry)
    return root


def by_type(entries):
    """The results by kind of file, whatever folder each was in."""
    root = Node("")
    for title in [title for title, _ext in CATEGORIES] + [OTHER]:
        root.children[title] = Node(title)
    for entry in entries:
        _add(root, [entry.category, entry.name], entry)
    for title in list(root.children):
        if not root.children[title].children:
            del root.children[title]
    return root


def matching(entries, text="", smallest=0):
    """The entries whose name holds `text` and that are at least so big."""
    text = text.strip().lower()
    return [e for e in entries
            if (not text or text in e.path.lower()) and e.size >= smallest]


# ---- availability, advice, plans ----------------------------------------------------

def availability(disk, volume=None, tools=diskfs.find_tool):
    if not disk.has_media:
        return diskops.no("There is no media in this device.")
    if volume is not None and volume.is_extended:
        return diskops.no("An extended partition is only a container; "
                          "select a partition inside it, or the drive.")
    if volume is not None and diskfs.windows_state(volume) is \
            diskfs.BITLOCKER:
        return diskops.no(diskfs.BITLOCKER.text)
    if tools("fls") is None and tools("photorec") is None:
        return diskops.no(
            "The tools that search a drive are not installed.\n"
            "Fedora: sudo dnf install sleuthkit testdisk\n"
            "Debian/Ubuntu: sudo apt install sleuthkit testdisk\n"
            "Arch: sudo pacman -S sleuthkit testdisk")
    return diskops.YES


def can_list(disk, volume=None):
    """Whether deleted files can be listed with their names here."""
    parts = [volume] if volume is not None and volume.kind == "part" \
        else list(disk.volumes)
    return any(p.fstype in LISTABLE for p in parts)


def scan_advice(disk, volume=None):
    found = [Advice(
        INFO, "Use the drive as little as possible until this is done",
        "A deleted file stays on the drive only until something else is "
        "written where it was. Do not save or install anything on this "
        "drive, and recover onto a different one.")]
    parts = [volume] if volume is not None and volume.kind == "part" \
        else list(disk.volumes)
    if disk.is_solid_state and disk.discard:
        found.append(Advice(
            WARNING, "Files deleted from this SSD are probably gone",
            "A solid-state drive that takes TRIM erases a deleted file's "
            "blocks for good, within seconds or at the next weekly TRIM. "
            "No program can bring those back. Files lost to a format or a "
            "damaged filesystem can often still be found, if the drive "
            "has not been trimmed since."))
    if any(p.fstype in NO_RECORD for p in parts):
        found.append(Advice(
            INFO, "Linux filesystems keep no record of deleted files",
            "On ext4, Btrfs and XFS a deleted file's name and place are "
            "erased with it. Such files can only be found by their "
            "content, which gives them back without names or folders."))
    if any(p.fstype == "ntfs" for p in parts):
        # Read from what the drivers write, not assumed: ntfs-3g and Windows
        # leave a deleted file's record whole; the kernel's ntfs3 driver
        # takes the name out of it and keeps only where the contents were.
        found.append(Advice(
            INFO, "Files deleted from Linux may come back without names",
            "Windows keeps a deleted file's name, and so does Linux's "
            "older NTFS driver (ntfs-3g). The newer one that most "
            "distributions now use (ntfs3) erases the name and folder of "
            "a file it deletes. Such files are not in the list of deleted "
            "files; a Deep Scan finds them by their content."))
    if any(p.is_system for p in parts):
        found.append(Advice(
            WARNING, "The running system is on this drive",
            "It writes to the drive all the time, which is what "
            "overwrites deleted files. For the best chance, shut down, "
            "start from a live USB stick and search from there."))
    return found


def plan_scan(disk, volume, folder, quick=True, deep=False, kinds=None,
              keep_damaged=False, loose=False, identity=None):
    """A Quick Scan (`quick`), a Deep Scan (`deep`), or both.

    `kinds`: limit what is found by content to these (keys of KINDS); none
    means every kind that can be recognised reliably. `keep_damaged`: keep
    files that fail PhotoRec's own check instead of skipping them. `loose`:
    also keep what only loosely looks like a file.
    """
    whole = volume is None or volume.kind == "whole"
    name = disk.name if whole else volume.name
    size = disk.size if whole else volume.size
    plan = Plan(title="Deep Scan" if deep else "Quick Scan",
                target=describe_target(disk, None if whole else volume),
                advice=scan_advice(disk, volume))
    if quick:
        plan.steps.append("Read the filesystem's record of deleted files")
        plan.step_keys.append("deleted")
    if deep:
        plan.steps.append("Read all %s for the names and folders of a "
                          "filesystem that was formatted over"
                          % disks.format_size(size))
        plan.step_keys.append("records")
        what = ", ".join(title.lower() for key, title, _f in KINDS
                         if key in kinds) if kinds else "files"
        plan.steps.append("Read all %s again for %s recognisable by their "
                          "content, saving each as it is found"
                          % (disks.format_size(size), what))
        plan.step_keys.append("content")
    plan.request = {
        "op": "recover_scan", "device": "/dev/%s" % name,
        "session": folder, "deleted": bool(quick),
        "records": bool(deep), "content": bool(deep),
        "kinds": families(kinds) if deep and kinds else [],
        "keep_damaged": bool(deep and keep_damaged),
        "loose": bool(deep and loose and not kinds),
        "identity": identity if identity is not None
        else disks.identity(name)}
    return plan


def plan_recover(disk, volume, scratch, target, count, size, identity=None):
    whole = volume is None or volume.kind == "whole"
    name = disk.name if whole else volume.name
    return Plan(
        title="Recover files",
        target="%d files, %s" % (count, disks.format_size(size)),
        steps=["Copy each file out under the name and folder it had, into "
               "%s" % target],
        step_keys=["recover"],
        request={"op": "recover_files", "device": "/dev/%s" % name,
                 "session": scratch, "target": target,
                 "identity": identity if identity is not None
                 else disks.identity(name)})


def scan_done_text(result):
    parts = []
    if result.get("deleted"):
        parts.append("%d deleted files on record" % result["deleted"])
    old = sum(v.get("count", 0) for v in result.get("old") or ())
    if old:
        parts.append("%d records of an earlier filesystem" % old)
    folders = sum(v.get("folders", 0) for v in result.get("old") or ())
    if folders:
        parts.append("%d folders of an earlier FAT32 or exFAT filesystem"
                     % folders)
    if result.get("found"):
        parts.append("%d files found by content" % result["found"])
    return ("Search finished: %s." % " and ".join(parts)) if parts else \
        "Search finished: nothing was found."


# ---- RAID arrays -----------------------------------------------------------------

#: How many of its drives each level can lose and still be read.
RAID_SPARE = {"raid0": 0, "linear": 0, "raid1": None, "raid4": 1,
              "raid5": 1, "raid6": 2, "raid10": 1}
RAID_TITLES = {"raid0": "RAID 0", "raid1": "RAID 1", "raid4": "RAID 4",
               "raid5": "RAID 5", "raid6": "RAID 6", "raid10": "RAID 10",
               "linear": "Linear (drives joined end to end)"}


def describe_array(array):
    """(one line, can it be put together, why not) for a found array."""
    level = array.get("level", "")
    title = RAID_TITLES.get(level, level or "RAID")
    want, have = array.get("devices") or 0, array.get("present") or 0
    line = "%s of %d drive%s, %d connected" % (
        title, want, "" if want == 1 else "s", have)
    if array.get("name"):
        line += " ('%s')" % array["name"].split(":")[-1]
    if array.get("device"):
        return line + " - already open", True, ""
    missing = max(0, want - have)
    spare = RAID_SPARE.get(level, 0)
    if level == "raid1":
        spare = want - 1                 # one copy is enough
    if missing > (spare or 0):
        return line, False, (
            "%d of its drives are missing, and a %s can only do without "
            "%d. Connect the missing drive%s and search again."
            % (missing, title, spare or 0, "" if missing == 1 else "s"))
    if missing:
        line += " - one short, which this kind of array can do without" \
            if missing == 1 else " - %d short, still readable" % missing
    return line, True, ""


def plan_raid_scan():
    return Plan(title="Looking for RAID arrays", target="every drive",
                steps=["Read each drive's own record of the array it "
                       "belongs to. Nothing is changed."],
                request={"op": "raid_scan"})


def plan_raid_open(array):
    return Plan(title="Open RAID array",
                target=describe_array(array)[0],
                steps=["Put the array together read-only, from the drives "
                       "that are connected. Nothing is written to any of "
                       "them."],
                request={"op": "raid_open", "uuid": array["uuid"]})


# ---- arrays whose own record is lost ------------------------------------------

#: How each way of laying out a RAID is named to the user.
LAYOUT_TITLES = {
    "raid0": "RAID 0",
    "raid5_ls": "RAID 5, left-symmetric (Linux's usual)",
    "raid5_la": "RAID 5, left-asymmetric",
    "raid5_rs": "RAID 5, right-symmetric",
    "raid5_ra": "RAID 5, right-asymmetric",
}
#: How many drives the helper can try every order of.
GUESS_MOST = 6


def member_problem(disk):
    """Why a drive cannot be named as part of an array put together without
    its record, or "" when it can."""
    if not disk.has_media:
        return "There is no media in it."
    if disk.is_system:
        return "The running system is on this drive."
    if disk.kind == disks.RAID:
        return "It is an array itself."
    if any(v.all_mountpoints() for v in disk.volumes):
        return "Something on it is mounted; unmount it first."
    return ""


def plan_raid_guess(members, level="auto", missing=False, identities=None):
    """Work out how drives whose own record of their array is gone fit
    together. `members`: [Disk], in any order."""
    identities = identities or {}
    names = ", ".join(d.name for d in members)
    steps = ["Read each drive and find where the array starts",
             "Try every order, piece size and layout against the "
             "filesystem on it. Nothing is written to any drive."]
    request = {"op": "raid_guess", "level": level,
               "members": [{"device": d.path,
                            "identity": identities.get(d.name)
                            if d.name in identities
                            else disks.identity(d.name)} for d in members]
               + ([None] if missing else [])}
    return Plan(title="Work out how the drives fit together",
                target=names + (" and one missing" if missing else ""),
                steps=steps, request=request)


def chunk_text(size):
    """A RAID piece size as RAID tools state it: in binary units."""
    if size >= 1024 * 1024 and size % (1024 * 1024) == 0:
        return "%d MiB" % (size // (1024 * 1024))
    return "%d KiB" % (size // 1024)


def describe_guess(candidate, titles=None):
    """(one short line, details) for a way of putting drives together."""
    titles = titles or {}
    order = [titles.get(path, path) if path else "(missing)"
             for path in candidate.get("order") or ()]
    line = "%s, pieces of %s" % (
        LAYOUT_TITLES.get(candidate.get("layout"), candidate.get("layout")),
        chunk_text(candidate.get("chunk") or 0))
    details = ["Drives in order: %s." % ", ".join(order),
               "Data starts %s into each drive."
               % chunk_text(candidate["offset"])
               if candidate.get("offset") else
               "Data starts at the beginning of each drive.",
               "%s on it: %.1f%% of %d checks read back as they should."
               % (candidate.get("kind") or "A filesystem",
                  100 * (candidate.get("score") or 0),
                  candidate.get("checks") or 0),
               "As one drive: %s." % disks.format_size(
                   candidate.get("size") or 0)]
    return line, details


def plan_raid_build(candidate, identities):
    """Put drives together read-only as `candidate` says. `identities`:
    {device path: identity}."""
    members = [None if path is None else
               {"device": path, "identity": identities.get(path)}
               for path in candidate["order"]]
    return Plan(
        title="Open the array read-only",
        target=describe_guess(candidate)[0],
        steps=["Put the drives together read-only. Nothing is written to "
               "any of them; the array appears in the list as one drive."],
        request={"op": "raid_build", "members": members,
                 "layout": candidate["layout"], "chunk": candidate["chunk"],
                 "offset": candidate["offset"]})


def plan_raid_close(disk, identity=None):
    return Plan(title="Close RAID array", target=disk.title,
                steps=["Take the array apart again. Its drives are not "
                       "changed."],
                request={"op": "raid_close", "device": disk.path,
                         "identity": identity if identity is not None
                         else disks.identity(disk.name)})
