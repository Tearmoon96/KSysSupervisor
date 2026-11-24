"""The folders of an NTFS drive after it was formatted.

NTFS keeps a record for every file: its name, the folder it is in, its size
and dates, and where on the disk its contents are. A format writes a new,
empty set of records at the start and leaves most of the old ones lying
where they were. Found again - each begins with the letters FILE - they give
back what a search by content cannot: the names, and the whole tree of
folders.

The privileged helper collects those records off the raw device into a file
in the recovery folder; this module reads that file. Every record is taken
as a claim about a disk that has since been written to, so nothing in one is
trusted further than it checks out, and what a record says about where a
file's contents are is only ever used to read.

Qt-free.
"""

from __future__ import annotations

import struct

RECORD = 1024
#: The helper's file: for each record, its place on the device as eight
#: bytes, then the record.
ITEM = 8 + RECORD
ROOT = 5                         # the record number of the top folder
FIRST_USER = 24                  # below this are the filesystem's own files
#: Seconds between 1601, where NTFS counts from, and 1970.
EPOCH_GAP = 11644473600

ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF
DOS_NAME = 2                     # the 8.3 alias of a longer name
#: Where the record's own number is kept. Records in the older layout - the
#: one Linux's ntfs3 driver writes - put their check values at 0x2A, over
#: the place of the number, and do not say which record they are.
NUMBER_AT = 44
NUMBERED_FROM = 0x30
#: How far a record without a number may lie from a numbered one in front of
#: it and still be counted from it, when the table's own list of places is
#: not to be found.
NEAREST = 256


def _fixed_up(record):
    """A record with its sector ends put right, or None if it is torn.

    The last two bytes of each sector of a record are swapped for a
    check value when it is written, the real bytes being kept in a list at
    the front. A sector whose end does not carry the check value was not
    written together with the rest.
    """
    data = bytearray(record)
    offset, count = struct.unpack_from("<HH", data, 4)
    if count < 2 or count > 9 or offset < 40 or offset + count * 2 > 512:
        return None
    check = data[offset:offset + 2]
    for sector in range(1, count):
        end = sector * 512
        if end > len(data) or data[end - 2:end] != check:
            return None
        data[end - 2:end] = data[offset + sector * 2:offset + sector * 2 + 2]
    return bytes(data)


def _runs(data):
    """[(first cluster or None, clusters)] from a run list. None for a
    stretch that was never written (a hole). Raises ValueError when the
    list does not make sense."""
    found, place, cluster = [], 0, 0
    while place < len(data) and data[place]:
        head = data[place]
        length_size, offset_size = head & 0x0F, head >> 4
        place += 1
        if length_size == 0 or length_size > 8 or offset_size > 8 \
                or place + length_size + offset_size > len(data):
            raise ValueError("bad run")
        length = int.from_bytes(data[place:place + length_size], "little")
        place += length_size
        if length <= 0:
            raise ValueError("empty run")
        if offset_size == 0:
            found.append((None, length))
        else:
            cluster += int.from_bytes(data[place:place + offset_size],
                                      "little", signed=True)
            if cluster < 0:
                raise ValueError("run before the start")
            found.append((cluster, length))
        place += offset_size
    return found


def parse_record(record, where=0):
    """What one file record says, as a dictionary, or None when it is not a
    usable record: torn, an overflow record of another, or nameless."""
    if len(record) < RECORD or record[:4] != b"FILE":
        return None
    data = _fixed_up(record[:RECORD])
    if data is None:
        return None
    (sequence, _links, first, flags, used, _size,
     base) = struct.unpack_from("<HHHHIIQ", data, 16)
    if base & 0xFFFFFFFFFFFF or first < 42 or used > RECORD or used < first:
        return None
    check_at = struct.unpack_from("<H", data, 4)[0]
    number = struct.unpack_from("<I", data, NUMBER_AT)[0] \
        if check_at >= NUMBERED_FROM else None
    found = {"number": number, "sequence": sequence, "where": where,
             "in_use": bool(flags & 1), "is_dir": bool(flags & 2),
             "name": "", "parent": None, "size": 0, "mtime": 0,
             "inline": None, "runs": None, "packed": False,
             "encrypted": False}
    place = first
    best = -1
    while place + 16 <= used:
        kind, length = struct.unpack_from("<II", data, place)
        if kind == ATTR_END or length < 16 or place + length > used:
            break
        resident = data[place + 8] == 0
        named = data[place + 9] != 0
        attr_flags = struct.unpack_from("<H", data, place + 12)[0]
        if kind == ATTR_FILE_NAME and resident:
            size, offset = struct.unpack_from("<IH", data, place + 16)
            body = data[place + offset:place + offset + size]
            if len(body) >= 66:
                count, space = body[64], body[65]
                name = body[66:66 + count * 2].decode("utf-16-le", "replace")
                # The long name where there are two.
                rank = 0 if space == DOS_NAME else 1
                if name and rank > best:
                    best = rank
                    found["name"] = name
                    found["parent"] = struct.unpack_from(
                        "<Q", body, 0)[0] & 0xFFFFFFFFFFFF
                    changed = struct.unpack_from("<Q", body, 16)[0]
                    stamp = changed // 10_000_000 - EPOCH_GAP
                    found["mtime"] = stamp if 0 < stamp < 2 ** 33 else 0
        elif kind == ATTR_DATA and not named:
            found["packed"] = bool(attr_flags & 0x0001)
            found["encrypted"] = bool(attr_flags & 0x4000)
            if resident:
                size, offset = struct.unpack_from("<IH", data, place + 16)
                if place + offset + size <= used:
                    found["inline"] = data[place + offset:
                                           place + offset + size]
                    found["size"] = size
            elif struct.unpack_from("<Q", data, place + 16)[0] == 0:
                # The first stretch of the file's list of places; a list
                # that goes on in another record is not followed.
                offset = struct.unpack_from("<H", data, place + 32)[0]
                real = struct.unpack_from("<Q", data, place + 48)[0]
                try:
                    found["runs"] = _runs(data[place + offset:
                                               place + length])
                    found["size"] = real
                except ValueError:
                    found["runs"] = None
        place += length
    if not found["name"]:
        return None
    return found


def _table_maps(records):
    """[(start, end, first record number)] of the pieces of each record
    table found, from its own record (number 0, "$MFT"), whose list of
    places says where on the disk each stretch of the table is.

    Its cluster size is not known by itself; it is the one that puts the
    table's first stretch where that record was found. A copy of the record
    kept elsewhere (the mirror) fits no cluster size and is left out.
    """
    maps = []
    for record in records:
        if record["number"] != 0 or record["name"] != "$MFT" \
                or not record["runs"] or record["runs"][0][0] is None:
            continue
        first = record["runs"][0][0]
        if first <= 0 or record["where"] % first:
            continue
        cluster = record["where"] // first
        if cluster < 512 or cluster > 2 * 1024 * 1024 \
                or cluster & (cluster - 1):
            continue
        number = 0
        for run, count in record["runs"]:
            length = count * cluster
            if run is not None:
                maps.append((run * cluster, run * cluster + length, number))
            number += length // RECORD
    return maps


def _number_of(record, maps, numbered):
    """The record number of a record that does not state its own: from the
    table's list of places, else counted on from the nearest numbered record
    in front of it. None when neither tells."""
    where = record["where"]
    for start, end, first in maps:
        if start <= where < end and not (where - start) % RECORD:
            return first + (where - start) // RECORD
    places = [w for w in numbered if w < where]
    if places:
        anchor = max(places)
        gap = where - anchor
        if gap % RECORD == 0 and gap // RECORD <= NEAREST:
            return numbered[anchor] + gap // RECORD
    return None


def read_records(path):
    """Every usable record in the helper's file, by record number.

    Where a number turns up twice - an old filesystem under a newer one -
    the one further along the disk is kept: the records of a new, empty
    filesystem sit at the front.
    """
    found = {}
    every = []
    try:
        handle = open(path, "rb")
    except OSError:
        return found
    with handle:
        while True:
            item = handle.read(ITEM)
            if len(item) < ITEM:
                break
            where = struct.unpack_from("<Q", item, 0)[0]
            record = parse_record(item[8:], where)
            if record is not None:
                every.append(record)
    if any(record["number"] is None for record in every):
        maps = _table_maps(every)
        numbered = {record["where"]: record["number"] for record in every
                    if record["number"] is not None}
        for record in every:
            if record["number"] is None:
                record["number"] = _number_of(record, maps, numbered)
    for record in every:
        if record["number"] is None:
            continue
        known = found.get(record["number"])
        if known is None or record["where"] > known["where"]:
            found[record["number"]] = record
    return found


def path_of(record, records, limit=64):
    """The folders a record sits in, outermost first, and whether the chain
    led all the way to the top folder."""
    parts, seen = [], set()
    parent = record["parent"]
    while parent is not None and parent != ROOT and limit > 0:
        if parent in seen:
            return parts, False
        seen.add(parent)
        folder = records.get(parent)
        if folder is None or not folder["is_dir"]:
            return parts, False
        parts.insert(0, folder["name"])
        parent = folder["parent"]
        limit -= 1
    return parts, parent == ROOT


def extents(record, cluster, size, device_size, start=0):
    """[[byte offset or None, byte length]] of a file's contents on the
    device, cut to its real size, or None when they cannot be read: packed
    or encrypted by NTFS, or pointing outside the device."""
    if record["packed"] or record["encrypted"] or record["runs"] is None:
        return None
    left = size
    found = []
    for first, count in record["runs"]:
        if left <= 0:
            break
        length = min(count * cluster, left)
        if first is None:
            found.append([None, length])
        else:
            offset = start + first * cluster
            if offset < 0 or offset + length > device_size:
                return None
            found.append([offset, length])
        left -= length
    if left > 0:
        return None
    return found


def files(records, cluster=4096, device_size=0, skip=()):
    """What can be offered for recovery, from a set of records.

    Yields (path, record, extents or None). Left out: folders, the
    filesystem's own files, empty files, and records lying inside `skip` -
    [(start, end)] of the filesystem that is on the device now, whose files
    are not lost.
    """
    for number in sorted(records):
        record = records[number]
        if record["is_dir"] or number < FIRST_USER or record["size"] <= 0 \
                or record["name"].startswith("$"):
            continue
        if any(begin <= record["where"] < end for begin, end in skip):
            continue
        folders, whole = path_of(record, records)
        if not whole:
            folders = ["Folder unknown"] + folders
        path = "/".join(folders + [record["name"]])
        if record["inline"] is not None:
            yield path, record, []
        else:
            yield path, record, extents(record, cluster, record["size"],
                                        device_size)


def current_filesystem(boot, read):
    """(cluster size, [(start, end)] of the present filesystem's own record
    table) from a device's first sector.

    `read(offset, length)` reads the device. Where the device does not hold
    NTFS now, the usual cluster size and nothing to skip.
    """
    if len(boot) < 512 or boot[3:11] != b"NTFS    ":
        return 4096, []
    sector = struct.unpack_from("<H", boot, 0x0B)[0]
    per_cluster = boot[0x0D]
    if sector not in (512, 1024, 2048, 4096) or per_cluster == 0:
        return 4096, []
    cluster = sector * (per_cluster if per_cluster <= 0x80
                        else 1 << (256 - per_cluster))
    table = struct.unpack_from("<Q", boot, 0x30)[0] * cluster
    first = parse_record(read(table, RECORD), table)
    if first is None or first["runs"] is None:
        return cluster, [(table, table + 64 * RECORD)]
    spans = []
    for run, count in first["runs"]:
        if run is not None:
            spans.append((run * cluster, (run + count) * cluster))
    return cluster, spans
