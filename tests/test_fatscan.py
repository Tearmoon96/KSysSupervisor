"""The folders of a FAT32 or exFAT drive after a format.

FAT images are real ones, made with mtools where it is installed; exFAT
entries are built here, checksums and all. That files come back byte for
byte from real devices is checked by tests/verify-disk-tools.py.
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksyssupervisor import fatscan                         # noqa: E402

MIB = 1024 * 1024
MTOOLS = all(shutil.which(t) for t in ("mformat", "mmd", "mcopy"))


def mtools(*argv):
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1", MTOOLSRC="/dev/null")
    subprocess.run(argv, check=True, env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def fat_image(folder, files, size=64 * MIB, fat32=True):
    """A FAT image holding `files` ({path: bytes}), made by mtools."""
    path = os.path.join(folder, "fat.img")
    with open(path, "wb") as handle:
        handle.truncate(size)
    mtools("mformat", "-i", path, *(["-F"] if fat32 else []), "::")
    made = set()
    for name, data in files.items():
        parts = name.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            folder_name = "/".join(parts[:depth])
            if folder_name not in made:
                mtools("mmd", "-i", path, "::/" + folder_name)
                made.add(folder_name)
        source = os.path.join(folder, "source")
        with open(source, "wb") as handle:
            handle.write(data)
        mtools("mcopy", "-i", path, source, "::/" + name)
    return path


def collect(path):
    """What the helper would collect: every FAT folder run, found by its
    "." and ".." entries, as (place, bytes)."""
    with open(path, "rb") as handle:
        data = handle.read()
    runs = []
    place = data.find(b".          ")
    while place >= 0:
        if place % 512 == 0 and data[place + 32:place + 43] == \
                b"..         ":
            end = data.find(b"\0", place)
            # Up to the end marker: an entry whose first byte is zero.
            cut = place
            while cut < len(data) and data[cut] != 0:
                cut += 32
            runs.append((place, data[place:-(-cut // 512) * 512]))
        place = data.find(b".          ", place + 1)
    return runs, data


def exfat_set(name, cluster, size, folder=False, contiguous=True,
              stamp=0x5A214C00):
    """An exFAT file entry set: file, stream and name entries, with the
    checksum exFAT keeps over them."""
    chars = name.encode("utf-16-le")
    pieces = [chars[n:n + 30] for n in range(0, len(chars), 30)] or [b""]
    block = bytearray(32 * (2 + len(pieces)))
    block[0], block[1] = 0x85, 1 + len(pieces)
    struct.pack_into("<H", block, 4, 0x10 if folder else 0x20)
    struct.pack_into("<II", block, 8, stamp, stamp)
    block[32], block[33], block[35] = 0xC0, 3 if contiguous else 1, len(name)
    struct.pack_into("<Q", block, 40, size)
    struct.pack_into("<IQ", block, 52, cluster, size)
    for index, piece in enumerate(pieces):
        start = 64 + 32 * index
        block[start] = 0xC1
        block[start + 2:start + 2 + len(piece)] = piece
    struct.pack_into("<H", block, 2, fatscan._set_checksum(bytes(block)))
    return bytes(block)


def exfat_folder(*sets, top=False):
    data = (b"\x83" + b"\0" * 31 if top else b"") + b"".join(sets)
    return data + b"\0" * (-len(data) % 512 or 512)


@unittest.skipUnless(MTOOLS, "mtools is not installed")
class FatAfterFormatTest(unittest.TestCase):
    FILES = {"Photos/Trip 2024/beach.png": os.urandom(70000),
             "Photos/cat.png": os.urandom(9000),
             "Docs/report.pdf": os.urandom(40000),
             "Docs/Sub/deep.txt": b"deep" * 100,
             "video.bin": os.urandom(30000)}

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def listing(self, path):
        runs, data = collect(path)
        found = {}
        for name, entry, extents, kind in fatscan.files(
                runs, len(data), data[:512]):
            content = b"".join(data[o:o + n] for o, n in extents) \
                if extents else None
            found[name] = (entry, content, kind)
        return found

    def test_files_in_folders_come_back_by_name_after_a_format(self):
        path = fat_image(self.folder, self.FILES)
        mtools("mformat", "-i", path, "-F", "::")
        found = self.listing(path)
        tails = {name.split("/", 1)[1]: content
                 for name, (_e, content, _k) in found.items()}
        self.assertEqual(tails["Trip 2024/beach.png"],
                         self.FILES["Photos/Trip 2024/beach.png"])
        self.assertEqual(tails["cat.png"], self.FILES["Photos/cat.png"])
        self.assertEqual(tails["report.pdf"], self.FILES["Docs/report.pdf"])
        self.assertEqual(tails["Sub/deep.txt"],
                         self.FILES["Docs/Sub/deep.txt"])
        # The old top folder went with the format: what was in it, and the
        # names of the folders it held.
        self.assertNotIn("video.bin", [n.rsplit("/", 1)[-1] for n in found])
        tops = {name.split("/", 1)[0] for name in found}
        self.assertEqual(tops, {"%s 1" % fatscan.UNNAMED,
                                "%s 2" % fatscan.UNNAMED})
        self.assertEqual({k for _e, _c, k in found.values()}, {"FAT"})

    def test_the_layout_is_found_from_the_folders_alone(self):
        path = fat_image(self.folder, self.FILES)
        with open(path, "rb") as handle:
            boot = handle.read(512)
        size, base, _kind, _root = fatscan.current_layout(boot)
        mtools("mformat", "-i", path, "-F", "-c", "8", "::")
        runs, _data = collect(path)
        folders = [(w,) + fatscan.parse_fat(d) for w, d in runs]
        self.assertEqual(fatscan.fat_layout(folders), (size, base))

    def test_folders_of_the_filesystem_there_now_are_not_lost(self):
        path = fat_image(self.folder, {"Old/a.txt": b"a" * 3000})
        mtools("mformat", "-i", path, "-F", "::")
        with open(os.path.join(self.folder, "n"), "wb") as handle:
            handle.write(b"new" * 1000)
        mtools("mmd", "-i", path, "::/Fresh")
        mtools("mmd", "-i", path, "::/Fresh/Deeper")
        mtools("mcopy", "-i", path, os.path.join(self.folder, "n"),
               "::/Fresh/Deeper/new.txt")
        runs, data = collect(path)
        size, base, _kind, root = fatscan.current_layout(data[:512])
        top = data[base + (root - 2) * size:base + (root - 1) * size]
        names = [n for n, _e, _x, _k in fatscan.files(runs, len(data),
                                                      data[:512], top)]
        self.assertFalse([n for n in names if "new.txt" in n], names)

    def test_a_fat16_drive_too(self):
        path = fat_image(self.folder, {"A/b/c.txt": b"c" * 5000},
                         size=32 * MIB, fat32=False)
        mtools("mformat", "-i", path, "::")
        found = self.listing(path)
        self.assertEqual([n.split("/", 1)[1] for n in found], ["b/c.txt"])
        self.assertEqual(next(iter(found.values()))[1], b"c" * 5000)


class FatEntriesTest(unittest.TestCase):
    def test_a_sector_that_does_not_start_a_folder_is_not_one(self):
        self.assertIsNone(fatscan.parse_fat(b"\0" * 512))
        self.assertIsNone(fatscan.parse_fat(os.urandom(512)))

    def test_long_names_need_the_checksum_of_their_short_name(self):
        def short(name, attr=0x20, cluster=5, size=10):
            entry = bytearray(32)
            entry[0:11] = name
            entry[11] = attr
            struct.pack_into("<HI", entry, 26, cluster, size)
            return bytes(entry)

        def long_entry(text, checksum, order=0x41):
            entry = bytearray(32)
            entry[0], entry[11], entry[13] = order, 0x0F, checksum
            chars = (text.encode("utf-16-le") + b"\0\0").ljust(26, b"\xff")
            entry[1:11], entry[14:26], entry[28:32] = (
                chars[:10], chars[10:22], chars[22:26])
            return bytes(entry)

        raw = b"HOLIDA~1JPG"
        data = (short(b".          ", 0x10, 7)
                + short(b"..         ", 0x10, 0)
                + long_entry("holiday.jpg", fatscan._lfn_checksum(raw))
                + short(raw)
                + long_entry("stray name.jpg", 0x00) + short(b"OTHER   TXT")
                + b"\0" * 32)
        own, parent, entries = fatscan.parse_fat(data)
        self.assertEqual((own, parent), (7, 0))
        self.assertEqual([e["name"] for e in entries],
                         ["holiday.jpg", "OTHER.TXT"])


class ExfatTest(unittest.TestCase):
    CLUSTER = 4096
    BASE = 1 * MIB

    def place(self, cluster):
        return self.BASE + (cluster - 2) * self.CLUSTER

    def test_a_set_whose_checksum_is_wrong_is_not_taken(self):
        good = exfat_set("photo.jpg", 9, 100)
        self.assertEqual(fatscan.parse_exfat(exfat_folder(good))[0][0]
                         ["name"], "photo.jpg")
        bad = bytearray(good)
        bad[70] ^= 1
        self.assertIsNone(fatscan.parse_exfat(exfat_folder(bytes(bad))))

    def test_folders_are_linked_by_the_layout_their_links_agree_on(self):
        runs = [
            (self.place(10), exfat_folder(
                exfat_set("Trip", 20, 4096, folder=True),
                exfat_set("Work", 30, 4096, folder=True),
                exfat_set("cat.png", 100, 5000))),
            (self.place(20), exfat_folder(exfat_set("beach.png", 200, 7000))),
            (self.place(30), exfat_folder(
                exfat_set("Old", 40, 4096, folder=True),
                exfat_set("a.txt", 300, 10))),
            (self.place(40), exfat_folder(exfat_set("b.txt", 400, 10)))]
        found = {n: (e, x) for n, e, x, _k in fatscan.files(runs, 64 * MIB)}
        unnamed = "%s 1" % fatscan.UNNAMED
        self.assertEqual(sorted(found), sorted(
            "%s/%s" % (unnamed, p) for p in (
                "cat.png", "Trip/beach.png", "Work/a.txt", "Work/Old/b.txt")))
        _entry, extents = found[unnamed + "/Trip/beach.png"]
        self.assertEqual(extents, [[self.place(200), 7000]])

    def test_an_old_top_folder_that_survived_names_its_folders(self):
        runs = [
            (self.place(5), exfat_folder(
                exfat_set("Photos", 20, 4096, folder=True),
                exfat_set("top.txt", 50, 10), top=True)),
            (self.place(20), exfat_folder(
                exfat_set("Sub", 21, 4096, folder=True),
                exfat_set("cat.png", 100, 50))),
            (self.place(21), exfat_folder(exfat_set("deep.png", 101, 50)))]
        names = sorted(n for n, _e, _x, _k in fatscan.files(runs, 64 * MIB))
        self.assertEqual(names, ["Photos/Sub/deep.png", "Photos/cat.png",
                                 "top.txt"])

    def test_the_top_folder_there_now_and_what_it_reaches_are_left_out(self):
        boot = bytearray(512)
        boot[3:11] = b"EXFAT   "
        boot[108], boot[109] = 9, 3                   # 512 * 8 = 4096
        struct.pack_into("<I", boot, 88, self.BASE // 512)
        struct.pack_into("<I", boot, 96, 5)           # top folder: cluster 5
        boot[510:512] = b"\x55\xaa"
        runs = [
            (self.place(5), exfat_folder(
                exfat_set("Fresh", 6, 4096, folder=True), top=True)),
            (self.place(6), exfat_folder(exfat_set("new.txt", 60, 10))),
            (self.place(30), exfat_folder(exfat_set("old.txt", 70, 10)))]
        names = [n for n, _e, _x, _k in fatscan.files(runs, 64 * MIB,
                                                      bytes(boot))]
        self.assertEqual(names, ["%s 1/old.txt" % fatscan.UNNAMED])

    def test_a_file_reaching_past_the_device_is_not_offered_to_read(self):
        runs = [(self.place(10), exfat_folder(
            exfat_set("A", 20, 4096, folder=True))),
            (self.place(20), exfat_folder(
                exfat_set("B", 21, 4096, folder=True),
                exfat_set("huge.bin", 50, 10 ** 12))),
            (self.place(21), exfat_folder(exfat_set("c.txt", 51, 10)))]
        found = {n.rsplit("/", 1)[-1]: x
                 for n, _e, x, _k in fatscan.files(runs, 64 * MIB)}
        self.assertIsNone(found["huge.bin"])
        self.assertEqual(found["c.txt"], [[self.place(51), 10]])


class RunsFileTest(unittest.TestCase):
    def test_the_helpers_file_is_read_back_and_a_torn_end_ignored(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "dirs.bin")
        with open(path, "wb") as handle:
            for where, data in ((4096, b"a" * 512), (9000, b"b" * 1024)):
                handle.write(struct.pack("<QQ", where, len(data)) + data)
            handle.write(struct.pack("<QQ", 1, 512) + b"short")
        self.assertEqual([(w, len(d)) for w, d in fatscan.read_runs(path)],
                         [(4096, 512), (9000, 1024)])
        self.assertEqual(fatscan.read_runs("/nonexistent"), [])


if __name__ == "__main__":
    unittest.main()
