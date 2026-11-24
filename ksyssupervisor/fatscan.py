"""The folders of a FAT32 or exFAT drive after it was formatted.

Neither filesystem keeps a central list of its files the way NTFS does: each
folder is a run of 32-byte entries stored in the data area like a file, and
a format only writes a new, empty top folder and new allocation tables. The
other folders stay where they were until something is written over them.
Found again, they give back the names of the files in them, their sizes and
dates, and the cluster each one starts at.

What a format does take is the old top folder: the files that were in it,
and the names of the folders that were. Those folders come back as "Folder
without a name"; what is inside them keeps its names. The old allocation
table is gone too, so a file is read as one unbroken stretch from its first
cluster - which is how almost every file on a stick or card is stored, and
what the check after recovery confirms or not.

The privileged helper collects every run of sectors that looks like folder
entries into a file in the recovery folder; this module reads that file.
Like ntfsscan, it treats every entry as a claim about a disk that has since
been written to, and only ever uses one to read.

Qt-free.
"""

from __future__ import annotations

import collections
import struct

ENTRY = 32
SECTOR = 512
#: The helper's file: for each run, its place on the device and its length
#: as eight bytes each, then the run.
HEAD = 16

ATTR_DIR = 0x10
ATTR_VOLUME = 0x08
ATTR_LFN = 0x0F
#: Cluster sizes either filesystem allows.
CLUSTERS = tuple(1 << n for n in range(9, 26))

UNNAMED = "Folder without a name"


# ---- reading the helper's file ----------------------------------------------------

def read_runs(path):
    """[(place on the device, bytes)] of every run in the helper's file."""
    found = []
    try:
        handle = open(path, "rb")
    except OSError:
        return found
    with handle:
        while True:
            head = handle.read(HEAD)
            if len(head) < HEAD:
                break
            where, length = struct.unpack("<QQ", head)
            if length > 64 * 1024 * 1024:
                break
            data = handle.read(length)
            if len(data) < length:
                break
            found.append((where, data))
    return found


def _dos_time(date, time):
    """Seconds since 1970 from a FAT date and time, as local time is not
    known: read as UTC. 0 when it is not a real date."""
    import calendar
    year, month, day = 1980 + (date >> 9), (date >> 5) & 15, date & 31
    hour, minute, second = time >> 11, (time >> 5) & 63, (time & 31) * 2
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24
            and minute < 60 and second < 60):
        return 0
    return calendar.timegm((year, month, day, hour, minute, second))


# ---- FAT ---------------------------------------------------------------------------

def _short_name(raw, case=0):
    """A short (8.3) name. `case`: the flags Windows keeps in byte 12 to
    show a name it stores in capitals as all small letters."""
    base = raw[:8].rstrip(b" ")
    ext = raw[8:11].rstrip(b" ")
    if base[:1] == b"\x05":
        base = b"\xe5" + base[1:]
    name = base.decode("cp437", "replace")
    if case & 0x08:
        name = name.lower()
    if ext:
        ext = ext.decode("cp437", "replace")
        name += "." + (ext.lower() if case & 0x10 else ext)
    return name


def _lfn_checksum(raw):
    total = 0
    for byte in raw[:11]:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def parse_fat(data):
    """What a FAT folder says: (its own cluster, its parent's, [entries]),
    or None when it does not begin as a folder does, with "." and "..".

    Each entry: dict(name, is_dir, cluster, size, mtime). Deleted entries,
    the volume label, "." and ".." are left out.
    """
    if len(data) < 2 * ENTRY or data[0:11] != b".          " \
            or not data[11] & ATTR_DIR or data[32:43] != b"..         " \
            or not data[43] & ATTR_DIR:
        return None

    def cluster_of(entry):
        return (struct.unpack_from("<H", entry, 20)[0] << 16) \
            | struct.unpack_from("<H", entry, 26)[0]

    own, parent = cluster_of(data[0:32]), cluster_of(data[32:64])
    entries = []
    long_parts = {}
    long_sum = None
    for place in range(2 * ENTRY, len(data) - ENTRY + 1, ENTRY):
        entry = data[place:place + ENTRY]
        first = entry[0]
        if first == 0:
            break
        attr = entry[11]
        if first == 0xE5:
            long_parts, long_sum = {}, None
            continue
        if attr == ATTR_LFN:
            order = first & 0x3F
            if first & 0x40:
                long_parts, long_sum = {}, entry[13]
            if long_sum != entry[13] or not 1 <= order <= 20:
                long_parts, long_sum = {}, None
                continue
            chars = entry[1:11] + entry[14:26] + entry[28:32]
            long_parts[order] = chars
            continue
        if attr & ATTR_VOLUME or attr & 0xC0:
            long_parts, long_sum = {}, None
            continue
        raw = entry[0:11]
        name = _short_name(raw, entry[12])
        if long_parts and long_sum == _lfn_checksum(raw) \
                and sorted(long_parts) == list(range(1, len(long_parts) + 1)):
            text = b"".join(long_parts[n] for n in sorted(long_parts))
            text = text.decode("utf-16-le", "replace")
            text = text.split("\0", 1)[0].rstrip("￿")
            if text:
                name = text
        long_parts, long_sum = {}, None
        if name in (".", ".."):
            continue
        date, time = struct.unpack_from("<H", entry, 24)[0], \
            struct.unpack_from("<H", entry, 22)[0]
        entries.append({
            "name": name, "is_dir": bool(attr & ATTR_DIR),
            "cluster": cluster_of(entry),
            "size": struct.unpack_from("<I", entry, 28)[0],
            "mtime": _dos_time(date, time), "contiguous": False})
    return own, parent, entries


# ---- exFAT -------------------------------------------------------------------------

def _set_checksum(entries):
    total = 0
    for index, byte in enumerate(entries):
        if index in (2, 3):
            continue
        total = (((total & 1) << 15) + (total >> 1) + byte) & 0xFFFF
    return total


def parse_exfat(data):
    """What an exFAT folder says: ([entries], whether it is a top folder),
    or None when nothing in it is a whole, checked file entry.

    exFAT keeps every file as a set of entries - the file, its stream, and
    its name in pieces - with a checksum over the set, so a set that
    passes is no accident of the data.
    """
    entries = []
    top = False
    place = 0
    while place + ENTRY <= len(data):
        kind = data[place]
        if kind == 0:
            break
        if kind in (0x81, 0x82, 0x83):
            top = True
        if kind != 0x85:
            place += ENTRY
            continue
        count = data[place + 1]
        end = place + (count + 1) * ENTRY
        if not 2 <= count <= 18 or end > len(data):
            place += ENTRY
            continue
        block = data[place:end]
        if struct.unpack_from("<H", block, 2)[0] != _set_checksum(block) \
                or block[ENTRY] != 0xC0:
            place += ENTRY
            continue
        attrs = struct.unpack_from("<H", block, 4)[0]
        stamp = struct.unpack_from("<I", block, 12)[0]
        stream = block[ENTRY:2 * ENTRY]
        length = stream[3]
        text = b"".join(block[n * ENTRY + 2:n * ENTRY + 32]
                        for n in range(2, count + 1)
                        if block[n * ENTRY] == 0xC1)
        name = text.decode("utf-16-le", "replace")[:length]
        if name:
            entries.append({
                "name": name, "is_dir": bool(attrs & ATTR_DIR),
                "cluster": struct.unpack_from("<I", stream, 20)[0],
                "size": struct.unpack_from("<Q", stream, 24)[0],
                "mtime": _dos_time(stamp >> 16, stamp & 0xFFFF),
                "contiguous": bool(stream[1] & 2)})
        place = end
    if not entries:
        return None
    return entries, top


# ---- the old filesystem's layout ---------------------------------------------------

def _vote(counter):
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def current_layout(boot):
    """(cluster size, place of cluster 2, "fat" or "exfat", first cluster of
    the top folder - 0 where FAT keeps it outside the clusters) of the
    filesystem on the device now, from its first sector; None when it is
    neither FAT nor exFAT."""
    if len(boot) < SECTOR or boot[510:512] != b"\x55\xaa":
        return None
    if boot[3:11] == b"EXFAT   ":
        sector = 1 << boot[108]
        cluster = sector << boot[109]
        heap = struct.unpack_from("<I", boot, 88)[0] * sector
        root = struct.unpack_from("<I", boot, 96)[0]
        return cluster, heap, "exfat", root
    sector = struct.unpack_from("<H", boot, 11)[0]
    per = boot[13]
    if sector not in (512, 1024, 2048, 4096) or per == 0 or per & (per - 1):
        return None
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    root_entries = struct.unpack_from("<H", boot, 17)[0]
    fat_size = struct.unpack_from("<H", boot, 22)[0] \
        or struct.unpack_from("<I", boot, 36)[0]
    if not fats or not fat_size:
        return None
    root_sectors = (root_entries * 32 + sector - 1) // sector
    heap = (reserved + fats * fat_size + root_sectors) * sector
    root = struct.unpack_from("<I", boot, 44)[0] if root_entries == 0 else 0
    return sector * per, heap, "fat", root


def fat_layout(folders):
    """(cluster size, place of cluster 2) of an old FAT filesystem, from
    where its folders lie and the cluster each says it is at."""
    sizes = collections.Counter()
    known = sorted((own, where) for where, own, _p, _e in folders if own >= 2)
    for (c1, w1), (c2, w2) in zip(known, known[1:]):
        if c2 > c1 and (w2 - w1) % (c2 - c1) == 0:
            size = (w2 - w1) // (c2 - c1)
            if size in CLUSTERS:
                sizes[size] += 1
    size = _vote(sizes)
    if size is None:
        return None
    bases = collections.Counter(where - (own - 2) * size
                                for own, where in known)
    base = _vote(bases)
    return None if base is None or base < 0 else (size, base)


#: How many folder links take part in the first round of the vote; every
#: link then counts for the few layouts that round puts in front.
SAMPLE = 48
SHORTLIST = 8


def exfat_layout(folders, hint=None):
    """(cluster size, place of cluster 2) of an old exFAT filesystem, found
    by which cluster size and start make its folders' links to each other
    land on folders that were found."""
    places = sorted(where for where, _entries, _top in folders)
    found = set(places)
    pairs = sorted({(where, entry["cluster"]) for where, entries, _t in folders
                    for entry in entries
                    if entry["is_dir"] and entry["cluster"] >= 2})
    links = sorted({cluster for _where, cluster in pairs})
    votes = collections.Counter()
    step = max(1, len(links) // SAMPLE)
    for cluster in links[::step]:
        for size in CLUSTERS:
            for where in places:
                base = where - (cluster - 2) * size
                if base >= 0:
                    votes[(size, base)] += 1
    shortlist = [layout for layout, _n in votes.most_common(SHORTLIST)]
    if hint is not None and hint not in shortlist:
        shortlist.append(hint)

    def landing(layout):
        # How many folders found a link leads to - each once, and never a
        # folder from itself, which no real link does.
        size, base = layout
        return len({base + (cluster - 2) * size for where, cluster in pairs
                    if base + (cluster - 2) * size in found
                    and base + (cluster - 2) * size != where})

    if not shortlist:
        return hint
    best = max(shortlist, key=lambda layout: (landing(layout),
                                              layout == hint))
    # A layout agreed on by one link alone is no better than chance, unless
    # it is also the one on the device now.
    if landing(best) < 2 and best != hint:
        return hint
    return best


# ---- putting the tree together ---------------------------------------------------

def _extent(entry, layout, device_size):
    """[[byte offset, length]] of a file read as one stretch, or None."""
    if layout is None or entry["cluster"] < 2:
        return None
    size, base = layout
    offset = base + (entry["cluster"] - 2) * size
    if offset + entry["size"] > device_size:
        return None
    return [[offset, entry["size"]]]


def files(runs, device_size, boot=b"", current_root=b""):
    """What can be offered for recovery from the runs the helper found.

    Yields (path, entry, extents or None, kind) for every file in a folder
    of an earlier FAT or exFAT filesystem. Folders that the filesystem on
    the device now still reaches from its top folder are its own, not lost,
    and are left out with everything in them.
    """
    now = current_layout(boot)
    fat = []
    exfat = []
    for where, data in runs:
        parsed = parse_fat(data)
        if parsed is not None:
            fat.append((where,) + parsed)
            continue
        parsed = parse_exfat(data)
        if parsed is not None:
            exfat.append((where,) + parsed)
    if fat:
        yield from _fat_files(fat, device_size, now, current_root)
    if exfat:
        yield from _exfat_files(exfat, device_size, now, current_root)


def _reached(start_entries, layout, folder_at):
    """Places of the folders reached from a top folder's entries."""
    seen = set()
    todo = [e for e in start_entries if e["is_dir"]]
    while todo and layout is not None:
        entry = todo.pop()
        size, base = layout
        place = base + (entry["cluster"] - 2) * size
        if place in seen or place not in folder_at:
            continue
        seen.add(place)
        todo += [e for e in folder_at[place] if e["is_dir"]]
    return seen


def _fat_files(folders, device_size, now, current_root):
    layout = fat_layout(folders)
    if layout is None and now is not None and now[2] == "fat":
        layout = now[:2]
    by_place = {where: entries for where, _o, _p, entries in folders}
    current = set()
    if now is not None and now[2] == "fat" and current_root:
        # The top folder has no "." and ".." of its own; given a pair, it
        # reads like any other.
        parsed = parse_fat(b".          \x10" + b"\0" * 20
                           + b"..         \x10" + b"\0" * 20 + current_root)
        if parsed is not None:
            current = _reached(parsed[2], now[:2], by_place)
    # Which folder each cluster is, and the name and parent it is given in
    # the folder that holds it.
    by_cluster = {}
    for where, own, parent, entries in folders:
        if where in current:
            continue
        known = by_cluster.get(own)
        if known is None or where > known[0]:
            by_cluster[own] = (where, parent, entries)
    names = {}
    for own, (_where, _parent, entries) in by_cluster.items():
        for entry in entries:
            if entry["is_dir"] and entry["cluster"] in by_cluster:
                names[entry["cluster"]] = entry["name"]
    unnamed = {}

    def path_of(cluster, limit=64):
        parts = []
        seen = set()
        while cluster in by_cluster and limit > 0 and cluster not in seen:
            seen.add(cluster)
            name = names.get(cluster)
            if name is None:
                number = unnamed.setdefault(cluster, len(unnamed) + 1)
                parts.insert(0, "%s %d" % (UNNAMED, number))
                break
            parts.insert(0, name)
            cluster = by_cluster[cluster][1]
            limit -= 1
        return parts

    for own in sorted(by_cluster):
        _where, _parent, entries = by_cluster[own]
        folder = path_of(own)
        for entry in entries:
            if entry["is_dir"] or entry["size"] <= 0:
                continue
            yield ("/".join(folder + [entry["name"]]), entry,
                   _extent(entry, layout, device_size), "FAT")


def _exfat_files(folders, device_size, now, current_root):
    hint = now[:2] if now is not None and now[2] == "exfat" else None
    layout = exfat_layout(folders, hint)
    folder_at = {where: entries for where, entries, _top in folders}
    # The filesystem there now: its top folder, where its boot sector says,
    # and every folder reached from it, are not lost.
    current = set()
    if hint is not None:
        top_place = now[1] + (now[3] - 2) * now[0]
        top = folder_at.get(top_place)
        if top is None and current_root:
            parsed = parse_exfat(current_root)
            top = parsed[0] if parsed is not None else None
        current = {top_place} | _reached(top or [], hint, folder_at)
    # Which folder names which: a link from a folder's entry to a folder
    # found where the layout says it lies.
    parent = {}
    names = {}
    if layout is not None:
        size, base = layout
        for where, entries, _top in folders:
            for entry in entries:
                if entry["is_dir"] and entry["cluster"] >= 2:
                    place = base + (entry["cluster"] - 2) * size
                    if place in folder_at and place != where:
                        parent[place] = where
                        names[place] = entry["name"]
    tops = {where for where, _entries, top in folders if top}
    unnamed = {}

    def path_of(place, limit=64):
        parts = []
        seen = set()
        while limit > 0 and place not in seen:
            seen.add(place)
            if place not in names:
                if place not in tops:
                    number = unnamed.setdefault(place, len(unnamed) + 1)
                    parts.insert(0, "%s %d" % (UNNAMED, number))
                break
            parts.insert(0, names[place])
            place = parent[place]
            limit -= 1
        return parts

    for where, entries, _top in sorted(folders):
        if where in current:
            continue
        folder = path_of(where)
        for entry in entries:
            if entry["is_dir"] or entry["size"] <= 0:
                continue
            yield ("/".join(folder + [entry["name"]]), entry,
                   _extent(entry, layout, device_size), "exFAT")
