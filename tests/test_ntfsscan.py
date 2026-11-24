"""The folders of an NTFS drive after a format, from its old file records.

The fixture is what the helper collects: every file record found on a real
NTFS volume that held folders and files and was then formatted as NTFS
again. That the files named there come back byte for byte is checked on
real devices by tests/verify-disk-tools.py.
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ksyssupervisor import diskrecover, ntfsscan           # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")
RECORDS = os.path.join(FIXTURES, "ntfs_records.bin")
INFO = os.path.join(FIXTURES, "ntfs_records.json")

EXPECTED = ["Docs/budget.xlsx", "Docs/notes.txt", "Docs/report.pdf",
            "Photos/2024/holiday.png", "Photos/cat.png", "Photos/keep.png",
            "big.bin"]


def record(number, name, parent=5, flags=1, data=None, runs=None, size=0,
           space=1, usn=b"\x07\x00", older=False):
    """A file record as NTFS lays one out, for the cases no real volume
    happens to hold. `older`: the layout Linux's ntfs3 driver writes, with
    the check values over the place of the record's number."""
    out = bytearray(1024)
    out[0:4] = b"FILE"
    check = 42 if older else 48
    struct.pack_into("<HH", out, 4, check, 3)
    struct.pack_into("<HHHHIIQ", out, 16, 1, 1, 56, flags, 0, 1024, 0)
    if not older:
        struct.pack_into("<I", out, 44, number)
    place = 56
    text = name.encode("utf-16-le")
    body = bytearray(66 + len(text))
    struct.pack_into("<Q", body, 0, parent | (1 << 48))
    struct.pack_into("<Q", body, 16, (1700000000 + 11644473600) * 10_000_000)
    body[64], body[65] = len(name), space
    body[66:] = text
    head = bytearray(24)
    struct.pack_into("<II", head, 0, 0x30, 24 + len(body) + (-len(body) % 8))
    struct.pack_into("<IH", head, 16, len(body), 24)
    out[place:place + 24] = head
    out[place + 24:place + 24 + len(body)] = body
    place += 24 + len(body) + (-len(body) % 8)
    if data is not None:
        head = bytearray(24)
        struct.pack_into("<II", head, 0, 0x80, 24 + len(data)
                         + (-len(data) % 8))
        struct.pack_into("<IH", head, 16, len(data), 24)
        out[place:place + 24] = head
        out[place + 24:place + 24 + len(data)] = data
        place += 24 + len(data) + (-len(data) % 8)
    elif runs is not None:
        head = bytearray(64)
        struct.pack_into("<II", head, 0, 0x80, 64 + len(runs)
                         + (-len(runs) % 8))
        head[8] = 1
        struct.pack_into("<H", head, 32, 64)
        struct.pack_into("<QQQ", head, 40, size, size, size)
        out[place:place + 64] = head
        out[place + 64:place + 64 + len(runs)] = runs
        place += 64 + len(runs) + (-len(runs) % 8)
    struct.pack_into("<I", out, place, 0xFFFFFFFF)
    struct.pack_into("<I", out, 24, place + 8)
    # The sector ends carry the check value; the real bytes go up front.
    out[check:check + 2] = usn
    for sector in (1, 2):
        end = sector * 512
        out[check + sector * 2:check + 2 + sector * 2] = out[end - 2:end]
        out[end - 2:end] = usn
    return bytes(out)


def records_file(items):
    """The helper's file from [(place on the device, record)]."""
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "records.bin")
    with open(path, "wb") as handle:
        for where, data in items:
            handle.write(struct.pack("<Q", where) + data)
    return folder, path


class RealRecordsTest(unittest.TestCase):
    def setUp(self):
        self.records = ntfsscan.read_records(RECORDS)
        with open(INFO) as handle:
            self.info = json.load(handle)

    def listing(self, skip=()):
        return [(path, record, extents) for path, record, extents
                in ntfsscan.files(self.records, 4096, self.info["size"],
                                  skip)]

    def test_every_file_is_back_in_its_folder(self):
        self.assertEqual(sorted(p for p, _r, _e in self.listing()), EXPECTED)

    def test_sizes_dates_and_places_are_read(self):
        found = {path: (record, extents)
                 for path, record, extents in self.listing()}
        record, extents = found["Docs/report.pdf"]
        self.assertEqual(record["size"], 40016)
        self.assertGreater(record["mtime"], 1_700_000_000)
        self.assertEqual(sum(length for _o, length in extents), 40016)
        self.assertTrue(all(offset % 4096 == 0 for offset, _l in extents))
        big, extents = found["big.bin"]
        self.assertEqual(sum(length for _o, length in extents), 3145728)

    def test_the_filesystem_there_now_is_told_apart_from_the_old_one(self):
        boot = bytes.fromhex(self.info["boot"])
        table = bytes.fromhex(self.info["table"])
        cluster, skip = ntfsscan.current_filesystem(
            boot, lambda offset, length: table)
        self.assertEqual(cluster, 4096)
        self.assertEqual(len(skip), 1)
        # The old records lie beyond the new, short table.
        self.assertEqual(sorted(p for p, _r, _e in self.listing(skip)),
                         EXPECTED)
        everything = [(0, self.info["size"])]
        self.assertEqual(self.listing(everything), [])

    def test_a_device_that_is_not_ntfs_now_skips_nothing(self):
        self.assertEqual(ntfsscan.current_filesystem(b"\0" * 512, None),
                         (4096, []))
        self.assertEqual(ntfsscan.current_filesystem(b"", None), (4096, []))

    def test_the_results_show_them_under_their_own_heading(self):
        entries = diskrecover.lost_files(RECORDS, INFO, "/dev/sdx1")
        self.assertEqual(sorted(e.path for e in entries),
                         ["Before the format/" + p for p in EXPECTED])
        entry = [e for e in entries if e.name == "holiday.png"][0]
        self.assertEqual(entry.source, diskrecover.LOST)
        self.assertEqual(entry.folder, "Before the format/Photos/2024")
        self.assertIn("Fair", entry.chance)
        root = diskrecover.by_folder(entries)
        self.assertEqual(list(root.children), ["Before the format"])
        self.assertEqual(sorted(root.children["Before the format"].children),
                         ["Docs", "Photos", "big.bin"])


class RecordTest(unittest.TestCase):
    def test_a_small_file_kept_inside_its_record(self):
        found = ntfsscan.parse_record(record(40, "note.txt",
                                             data=b"hello there"), 4096)
        self.assertEqual((found["name"], found["size"], found["inline"]),
                         ("note.txt", 11, b"hello there"))
        self.assertEqual(found["mtime"], 1700000000)
        self.assertEqual(found["where"], 4096)

    def test_a_list_of_places_with_a_hole_in_it(self):
        # 16 clusters at cluster 100, a hole of 8, 4 clusters 50 before.
        runs = bytes([0x11, 16, 100, 0x01, 8, 0x11, 4, 0xCE, 0x00])
        found = ntfsscan.parse_record(record(41, "a.bin", runs=runs,
                                             size=28 * 4096 - 100))
        self.assertEqual(found["runs"], [(100, 16), (None, 8), (50, 4)])
        self.assertEqual(
            ntfsscan.extents(found, 4096, found["size"], 10 ** 9),
            [[409600, 65536], [None, 32768], [204800, 16384 - 100]])

    def test_places_outside_the_device_are_not_followed(self):
        runs = bytes([0x11, 16, 100, 0x00])
        found = ntfsscan.parse_record(record(41, "a.bin", runs=runs,
                                             size=65536))
        self.assertIsNone(ntfsscan.extents(found, 4096, 65536, 400000))
        self.assertIsNone(ntfsscan.extents(found, 4096, 99999999, 10 ** 9))

    def test_a_torn_or_foreign_record_is_left_out(self):
        good = record(40, "x.txt", data=b"abc")
        torn = bytearray(good)
        torn[510:512] = b"\xff\xff"
        self.assertIsNone(ntfsscan.parse_record(bytes(torn)))
        self.assertIsNone(ntfsscan.parse_record(b"FILE" + b"\0" * 1020))
        self.assertIsNone(ntfsscan.parse_record(b"BAAD" + good[4:]))
        self.assertIsNone(ntfsscan.parse_record(good[:500]))

    def test_nonsense_in_a_list_of_places_is_refused_not_guessed(self):
        for runs in (bytes([0x19, 1, 1, 0]), bytes([0x11, 0, 5, 0]),
                     bytes([0x11, 4, 0x80, 0])):
            found = ntfsscan.parse_record(record(41, "a.bin", runs=runs,
                                                 size=4096))
            self.assertIsNone(found["runs"], runs)

    def test_the_long_name_wins_over_its_short_alias(self):
        found = ntfsscan.parse_record(record(40, "LONGFI~1.TXT", space=2))
        self.assertEqual(found["name"], "LONGFI~1.TXT")
        both = bytearray(record(40, "A long file name.txt", space=1))
        self.assertEqual(ntfsscan.parse_record(bytes(both))["name"],
                         "A long file name.txt")

    def test_folders_missing_from_the_chain_are_said_to_be(self):
        records = {
            30: ntfsscan.parse_record(record(30, "Docs", flags=3)),
            31: ntfsscan.parse_record(record(31, "Work", parent=30,
                                             flags=3)),
            40: ntfsscan.parse_record(record(40, "a.txt", parent=31,
                                             data=b"x")),
            41: ntfsscan.parse_record(record(41, "b.txt", parent=77,
                                             data=b"x")),
            42: ntfsscan.parse_record(record(42, "$Secret", data=b"x")),
            43: ntfsscan.parse_record(record(43, "empty.txt", data=b"")),
        }
        found = sorted(path for path, _r, _e
                       in ntfsscan.files(records, 4096, 10 ** 9))
        self.assertEqual(found, ["Docs/Work/a.txt",
                                 "Folder unknown/b.txt"])

    def test_folders_that_point_at_each_other_do_not_hang(self):
        records = {
            30: ntfsscan.parse_record(record(30, "A", parent=31, flags=3)),
            31: ntfsscan.parse_record(record(31, "B", parent=30, flags=3)),
            40: ntfsscan.parse_record(record(40, "f.txt", parent=30,
                                             data=b"x"))}
        paths = [p for p, _r, _e in ntfsscan.files(records, 4096, 10 ** 9)]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].startswith("Folder unknown/"))

    def test_of_two_records_of_one_number_the_later_on_disk_is_kept(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "records.bin")
        with open(path, "wb") as handle:
            handle.write(struct.pack("<Q", 9000) + record(40, "old.txt",
                                                          data=b"old"))
            handle.write(struct.pack("<Q", 1000) + record(40, "new.txt",
                                                          data=b"new"))
            handle.write(b"short")
        found = ntfsscan.read_records(path)
        self.assertEqual(found[40]["name"], "old.txt")
        self.assertEqual(ntfsscan.read_records("/nonexistent"), {})



class OlderLayoutTest(unittest.TestCase):
    """Records written by Linux's ntfs3 driver do not state their number;
    it is worked out from where they lie."""

    CLUSTER = 4096

    def read(self, items):
        folder, path = records_file(items)
        self.addCleanup(shutil.rmtree, folder, True)
        return ntfsscan.read_records(path)

    def table(self):
        # The table in two pieces: 8 clusters (32 records) at cluster 4,
        # then 8 more at cluster 100.
        runs = bytes([0x11, 8, 4, 0x11, 8, 96, 0])
        return (4 * self.CLUSTER, record(0, "$MFT", runs=runs,
                                         size=64 * 1024))

    def test_such_a_record_is_read_without_a_number(self):
        found = ntfsscan.parse_record(record(40, "a.txt", data=b"x",
                                             older=True))
        self.assertIsNone(found["number"])
        self.assertEqual((found["name"], found["inline"]), ("a.txt", b"x"))

    def test_the_tables_own_list_of_places_numbers_them(self):
        second = 100 * self.CLUSTER
        found = self.read([
            self.table(),
            (second, record(0, "Docs", flags=3, older=True)),
            (second + 2 * 1024, record(0, "b.txt", parent=32, data=b"b",
                                       older=True))])
        self.assertEqual(found[32]["name"], "Docs")
        self.assertEqual(found[34]["name"], "b.txt")
        paths = [p for p, _r, _e in ntfsscan.files(found, self.CLUSTER,
                                                   10 ** 9)]
        self.assertEqual(paths, ["Docs/b.txt"])

    def test_the_mirror_of_the_table_record_is_not_taken_for_it(self):
        where, data = self.table()
        found = self.read([(where + 4 * 1024 * 1024, data),
                           (100 * self.CLUSTER + 1024,
                            record(0, "c.txt", data=b"c", older=True))])
        self.assertNotIn(33, found)

    def test_without_the_table_record_the_nearest_numbered_one_counts(self):
        found = self.read([
            (50 * 1024, record(30, "Docs", flags=3)),
            (53 * 1024, record(0, "d.txt", parent=30, data=b"d",
                               older=True)),
            ((50 + ntfsscan.NEAREST + 5) * 1024,
             record(0, "far.txt", data=b"f", older=True))])
        self.assertEqual(found[33]["name"], "d.txt")
        self.assertNotIn("far.txt", [r["name"] for r in found.values()])


if __name__ == "__main__":
    unittest.main()
