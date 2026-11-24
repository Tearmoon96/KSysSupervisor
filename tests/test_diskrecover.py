"""Recovering files: reading a scan, judging a file, the results window.

The listings parsed here are what The Sleuth Kit really printed for small
FAT32, exFAT and NTFS volumes with deleted files (tests/fixtures/fls_*.body).
That the files come back byte for byte from real devices is pinned down by
tests/verify-disk-tools.py.
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, MIB                              # noqa: E402
from test_diskhelper import HelperCase                     # noqa: E402
from test_disks import desktop, parse, stick               # noqa: E402
from test_diskwindow import DiskWindowCase                 # noqa: E402

from ksyssupervisor import diskfs, diskops, diskrecover, disks  # noqa: E402
from ksyssupervisor.diskrecover import Entry               # noqa: E402
from ksyssupervisor.diskrecoverui import (RecoverDialog,   # noqa: E402
                                          RecoveryResults)
from PyQt6.QtCore import Qt                                # noqa: E402
from PyQt6.QtWidgets import QLabel                         # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")


def everything(*names):
    return "/usr/sbin/" + names[0]


def body(fstype):
    return os.path.join(FIXTURES, "fls_%s.body" % fstype)


def png(path, side=16, cut=None, spoil=None):
    raw = b"".join(b"\0" + os.urandom(side * 3) for _ in range(side))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
        ">IIBBBBB", side, side, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    if cut:
        data = data[:cut]
    if spoil:
        data = data[:spoil] + b"\0\0\0\0" + data[spoil + 4:]
    with open(path, "wb") as handle:
        handle.write(data)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def file(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path


class ListingTest(unittest.TestCase):
    def test_deleted_files_come_with_their_folders_sizes_and_dates(self):
        for fstype in ("vfat", "exfat", "ntfs"):
            found = {e.path: e for e in diskrecover.parse_body(
                body(fstype), "/dev/sdx1")}
            self.assertIn("Docs/report.pdf", found, fstype)
            self.assertIn("Photos/cat.png", found, fstype)
            report = found["Docs/report.pdf"]
            self.assertEqual(report.size, 40016)
            self.assertGreater(report.mtime, 1_700_000_000)
            self.assertEqual(report.device, "/dev/sdx1")
            self.assertEqual(report.source, diskrecover.DELETED)
            self.assertEqual((report.name, report.folder),
                             ("report.pdf", "Docs"))

    def test_folders_empty_records_and_second_names_are_left_out(self):
        for fstype in ("vfat", "exfat", "ntfs"):
            found = diskrecover.parse_body(body(fstype), "/dev/sdx1")
            names = [e.path for e in found]
            self.assertEqual(len(names), 5, "%s: %r" % (fstype, names))
            self.assertFalse([n for n in names if "$" in n])
            self.assertFalse([e for e in found if e.size <= 0])

    def test_the_address_is_kept_as_the_tool_gave_it(self):
        ntfs = {e.name: e.inode for e in diskrecover.parse_body(
            body("ntfs"), "d")}
        self.assertEqual(ntfs["report.pdf"], "67-128-2")
        fat = {e.name: e.inode for e in diskrecover.parse_body(
            body("vfat"), "d")}
        self.assertEqual(fat["report.pdf"], "98328")

    def test_odd_lines_are_survived(self):
        with tempfile.NamedTemporaryFile("w", suffix=".body") as handle:
            handle.write(
                "garbage\n"
                "0|/a|b.txt (deleted)|9|r/rrwx|0|0|10|1|2|3|4\n"
                "0|/x.jpg:Zone.Identifier (deleted)|8-128-4|-/r|0|0|26|1|2|"
                "3|4\n"
                "0|/gone.doc (deleted-realloc)|7|r/r|0|0|900|1|2|3|4\n"
                "0|/$OrphanFiles/lost.mp3 (deleted)|6|-/r|0|0|5000|1|2|3|4\n"
                "0|/live.txt|5|r/r|0|0|10|1|2|3|4\n"
                "0|/bad (deleted)|4|r/r|0|0|notanumber|1|2|3|4\n")
            handle.flush()
            found = {e.path: e for e in diskrecover.parse_body(handle.name,
                                                               "d")}
        self.assertEqual(sorted(found), ["Folder unknown/lost.mp3",
                                         "a|b.txt", "gone.doc"])
        self.assertTrue(found["gone.doc"].reallocated)
        self.assertIn("Poor", found["gone.doc"].chance)
        self.assertIn("Good", found["a|b.txt"].chance)
        self.assertEqual(diskrecover.parse_body("/nonexistent", "d"), [])

    def test_files_are_sorted_into_kinds_by_their_names(self):
        for name, kind in (("a.JPG", "Pictures"), ("b.mov", "Video"),
                           ("c.flac", "Audio"), ("d.xlsx", "Documents"),
                           ("e.7z", "Archives"), ("f.xyz", "Other"),
                           ("noextension", "Other")):
            self.assertEqual(diskrecover.category(name), kind)


class InspectTest(TempCase):
    def state(self, name, data=None):
        path = self.file(name, data) if data is not None else \
            os.path.join(self.dir, name)
        return diskrecover.inspect_file(path)

    def test_a_png_is_checked_part_by_part(self):
        png(os.path.join(self.dir, "ok.png"))
        self.assertEqual(self.state("ok.png")[0], diskrecover.INTACT)
        png(os.path.join(self.dir, "cut.png"), cut=200)
        status, detail = self.state("cut.png")
        self.assertEqual(status, diskrecover.DAMAGED)
        self.assertIn("Cut short", detail)
        png(os.path.join(self.dir, "spoilt.png"), side=64, spoil=400)
        status, detail = self.state("spoilt.png")
        self.assertEqual(status, diskrecover.DAMAGED)
        self.assertIn("overwritten", detail)

    def test_a_name_that_lies_about_the_contents_is_unreadable(self):
        junk = os.urandom(5000)
        for name in ("a.png", "a.jpg", "a.pdf", "a.docx", "a.doc", "a.gif"):
            self.assertEqual(self.state(name, junk)[0],
                             diskrecover.UNREADABLE, name)

    def test_zeros_are_what_an_erased_file_looks_like(self):
        status, detail = self.state("photo.jpg", b"\0" * 50000)
        self.assertEqual(status, diskrecover.UNREADABLE)
        self.assertIn("only zeros", detail)
        self.assertEqual(self.state("empty.pdf", b"")[0],
                         diskrecover.UNREADABLE)

    def test_jpeg_and_pdf_need_their_beginning_and_end(self):
        jpeg = b"\xff\xd8\xff\xe0" + os.urandom(3000)
        self.assertEqual(self.state("a.jpg", jpeg + b"\xff\xd9")[0],
                         diskrecover.INTACT)
        self.assertEqual(self.state("b.jpg", jpeg.replace(b"\xff\xd9",
                                                          b""))[0],
                         diskrecover.DAMAGED)
        pdf = b"%PDF-1.7\n" + b"x" * 9000
        self.assertEqual(self.state(
            "a.pdf", pdf + b"\nstartxref\n5\n%%EOF\n")[0],
            diskrecover.INTACT)
        status, detail = self.state("b.pdf", pdf)
        self.assertEqual(status, diskrecover.DAMAGED)
        self.assertIn("repair", detail)

    def test_office_and_archive_files_are_tested_as_archives(self):
        path = os.path.join(self.dir, "ok.xlsx")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", "<x/>" * 500)
        status, detail = diskrecover.inspect_file(path)
        self.assertEqual(status, diskrecover.INTACT)
        self.assertIn("1 parts", detail)
        with open(path, "rb") as handle:
            data = handle.read()
        status, detail = self.state("cut.docx", data[:len(data) // 2])
        self.assertEqual(status, diskrecover.DAMAGED)
        self.assertIn("index", detail)

    def test_a_video_is_whole_when_its_boxes_add_up(self):
        def box(kind, data):
            return struct.pack(">I4s", 8 + len(data), kind) + data
        video = box(b"ftyp", b"isom" * 4) + box(b"moov", b"m" * 200) \
            + box(b"mdat", os.urandom(4000))
        self.assertEqual(self.state("a.mp4", video)[0], diskrecover.INTACT)
        self.assertEqual(self.state("b.mp4", video[:-900])[0],
                         diskrecover.DAMAGED)
        self.assertEqual(self.state("c.mp4", os.urandom(4000))[0],
                         diskrecover.UNREADABLE)

    def test_what_cannot_be_judged_says_so(self):
        status, detail = self.state("data.bin", os.urandom(4000))
        self.assertEqual(status, diskrecover.UNKNOWN)
        self.assertIn("open it to see", detail)
        self.assertEqual(self.state("missing.png")[0],
                         diskrecover.UNREADABLE)

    def test_a_batch_is_counted_by_outcome(self):
        png(os.path.join(self.dir, "ok.png"))
        entries = [Entry("ok.png", file=os.path.join(self.dir, "ok.png")),
                   Entry("x.png", file=self.file("x.png", b"junk" * 50)),
                   Entry("y.bin", file=self.file("y.bin", b"data" * 50)),
                   Entry("not-yet.png")]
        seen = []
        counts = diskrecover.inspect_all(entries,
                                         lambda a, b: seen.append((a, b)))
        self.assertEqual(seen[-1], (3, 3))
        self.assertEqual(diskrecover.summary_text(counts),
                         "1 intact, 1 unreadable, 1 of a kind that cannot "
                         "be checked")
        self.assertEqual(entries[3].status, diskrecover.UNKNOWN)


class SessionTest(TempCase):
    """Where a search works, the search file, and where files go."""

    def setUp(self):
        super().setUp()
        self.disk = parse(stick())[0]

    def found_folder(self, names):
        work = diskrecover.work_folder(self.dir, now=1700000000)
        found = os.path.join(work, "found", "part.1")
        os.makedirs(found)
        for name in names:
            png(os.path.join(found, name))
        return work

    def test_a_search_is_one_file_that_opens_again(self):
        record = diskrecover.new_search(self.disk, self.disk.volumes[0],
                                        now=1700000000)
        self.assertEqual(record["device"], "/dev/sde1")
        self.assertEqual(record["drive"]["serial"], self.disk.serial)
        self.assertEqual(record["drive"]["volume"]["start"],
                         self.disk.volumes[0].start)
        work = self.found_folder(["f0001.png"])
        record["work"] = work
        entries = diskrecover.parse_body(body("ntfs"), "/dev/sde1") \
            + diskrecover.found_files(work)
        entries[0].chosen = True
        path = os.path.join(self.dir, diskrecover.search_name(record))
        self.assertTrue(path.endswith(".kssearch"))
        diskrecover.save_search(path, record, entries)

        again, back = diskrecover.load_search(path)
        self.assertEqual(again["what"], record["what"])
        self.assertEqual([e.path for e in back], [e.path for e in entries])
        self.assertTrue(back[0].chosen)
        self.assertEqual(back[-1].file, entries[-1].file)

        # Moved, with its work folder, to another place or computer.
        moved = os.path.join(self.dir, "elsewhere")
        shutil.move(work, moved)
        _record, back = diskrecover.load_search(path)
        self.assertEqual(diskrecover.missing_found(back), 1)
        again, back = diskrecover.load_search(path, work=moved)
        self.assertEqual(diskrecover.missing_found(back), 0)
        self.assertEqual(again["work"], moved)

    def test_a_file_that_is_not_a_search_is_refused(self):
        other = os.path.join(self.dir, "x.kssearch")
        with open(other, "w") as handle:
            handle.write("not one")
        with self.assertRaises(ValueError):
            diskrecover.load_search(other)
        with self.assertRaises(ValueError):
            diskrecover.load_search(os.path.join(self.dir, "none"))

    def test_the_drive_is_found_again_under_another_name(self):
        record = diskrecover.drive_record(self.disk, self.disk.volumes[0])
        renamed = parse(stick())[0]
        object.__setattr__(renamed, "name", "sdk")
        disk, volume = diskrecover.find_drive(record, [renamed])
        self.assertIs(disk, renamed)
        self.assertEqual(volume.start, self.disk.volumes[0].start)
        stranger = disks.Disk("sdz", "/dev/sdz", self.disk.size,
                              model="Other")
        self.assertEqual(diskrecover.find_drive(record, [stranger]),
                         (None, None))
        # This stick reports no serial number: of two alike, neither.
        twin = parse(stick())[0]
        self.assertEqual(diskrecover.find_drive(record, [renamed, twin]),
                         (None, None))
        # One that does report it is found by it alone.
        record = dict(record, serial="AA11")
        record.pop("volume")
        numbered = disks.Disk("sdy", "/dev/sdy", self.disk.size,
                              serial="AA11", model="Renamed by udev")
        self.assertEqual(diskrecover.find_drive(record, [twin, numbered])[0],
                         numbered)

    def test_private_folders_are_in_memory_and_go_away(self):
        folder = diskrecover.scratch_folder(self.dir)
        self.assertTrue(os.path.isfile(os.path.join(folder, "scan.json")))
        diskrecover.remove_scratch(folder)
        self.assertFalse(os.path.exists(folder))
        # Only those: nothing else is ever removed that way.
        diskrecover.remove_scratch(self.dir)
        self.assertTrue(os.path.isdir(self.dir))

    def test_a_search_is_not_kept_on_the_drive_searched(self):
        record = diskrecover.new_search(self.disk)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.dir}):
            place = diskrecover.autosave_place(record, self.disk)
            self.assertTrue(place.startswith(os.path.join(
                self.dir, "KSysSupervisor", "Searches")))
            mounted = disks.Disk("sdq", "/dev/sdq", GIB, volumes=(
                disks.Volume("sdq1", "/dev/sdq1", "sdq", "part", GIB,
                             mountpoints=(self.dir,)),))
            self.assertIsNone(diskrecover.autosave_place(record, mounted))

    def test_the_place_recovered_to_must_be_another_drive(self):
        mounted = disks.Disk("sdq", "/dev/sdq", GIB, volumes=(
            disks.Volume("sdq1", "/dev/sdq1", "sdq", "part", GIB,
                         mountpoints=(self.dir,)),))
        self.assertTrue(diskrecover.on_drive(self.dir, mounted))
        text = diskrecover.folder_problem(self.dir, mounted)
        self.assertIn("on the drive being searched", text)
        self.assertEqual(diskrecover.folder_problem(self.dir, self.disk), "")
        self.assertIn("does not exist", diskrecover.folder_problem(
            os.path.join(self.dir, "nope"), self.disk))
        self.assertIn("another drive",
                      diskrecover.folder_problem("", self.disk))
        self.assertIn("free there", diskrecover.folder_problem(
            self.dir, self.disk, needed=10 ** 18))

    def test_the_ticked_files_are_handed_to_the_helper(self):
        entries = diskrecover.parse_body(body("ntfs"), "/dev/sde1")
        entries[0].chosen = entries[2].chosen = True
        entries.append(Entry("Found by content/Other/f1.bin",
                             source=diskrecover.FOUND, chosen=True))
        self.assertEqual(diskrecover.write_selection(self.dir, entries), 2)
        with open(os.path.join(self.dir, "selection.json")) as handle:
            written = json.load(handle)
        self.assertEqual([w["path"] for w in written],
                         [entries[0].path, entries[2].path])
        self.assertEqual(set(written[0]), {"device", "inode", "path",
                                           "size"})

    def test_found_files_ticked_move_sorted_the_rest_stay(self):
        work = self.found_folder(["f1.png", "f2.png"])
        entries = diskrecover.found_files(work)
        entries[0].chosen = True
        target = diskrecover.recovery_folder(self.dir, now=1700000000)
        self.assertEqual(diskrecover.move_found(entries, target), 1)
        self.assertEqual(os.listdir(os.path.join(
            target, "Found by content", "Pictures")), ["f1.png"])
        # Not ticked is not thrown away: the search can still offer it.
        self.assertTrue(os.path.exists(entries[1].file))
        self.assertTrue(entries[0].file.startswith(target))

    def test_kinds_not_asked_for_are_dropped_and_deleted(self):
        work = self.found_folder(["f1.png"])
        wav = os.path.join(work, "found", "part.1", "f2.wav")
        with open(wav, "wb") as handle:
            handle.write(b"RIFF")
        entries = diskrecover.found_files(work)
        kept = diskrecover.keep_kinds(entries, ["Pictures"])
        self.assertEqual([e.name for e in kept], ["f1.png"])
        self.assertFalse(os.path.exists(wav))
        self.assertEqual(diskrecover.families(["Audio"])[:2], ["mp3",
                                                               "flac"])

    def test_a_file_found_where_a_named_one_starts_is_that_one(self):
        work = self.found_folder(["f0000208.png", "f0000999.png"])
        found = diskrecover.found_files(work)
        for entry in found:
            entry.device = "/dev/sde"
        named = Entry("Before the format/a/cat.png", source=diskrecover.LOST,
                      device="/dev/sde1", extents=[[100 * 512, 9000]])
        merged = diskrecover.merge([named], found,
                                   {"/dev/sde": 0, "/dev/sde1": 108 * 512})
        self.assertEqual([e.name for e in merged], ["cat.png",
                                                    "f0000999.png"])
        self.assertFalse(os.path.exists(found[0].file))
        # And what is already there is not added twice.
        self.assertEqual(len(diskrecover.merge(merged, merged)), 2)

    def test_recovered_files_are_matched_back_to_their_entries(self):
        entries = [Entry("a/x.png", chosen=True), Entry("a/y.png",
                                                        chosen=True)]
        with open(os.path.join(self.dir, "recovered.json"), "w") as handle:
            json.dump({"recovered": [{"path": "a/x.png", "file": "/r/x"}],
                       "failed": [{"path": "a/y.png", "error": "gone"}]},
                      handle)
        failed = diskrecover.attach_recovered(self.dir, entries)
        self.assertEqual(entries[0].file, "/r/x")
        self.assertEqual(entries[1].file, "")
        self.assertEqual(failed[0]["path"], "a/y.png")


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.entries = diskrecover.parse_body(body("exfat"), "d") + [
            Entry("Found by content/Pictures/f1.png", size=10,
                  source=diskrecover.FOUND)]

    def test_by_folder_is_how_the_files_were_arranged(self):
        root = diskrecover.by_folder(self.entries)
        self.assertEqual(sorted(root.children),
                         ["Docs", "Found by content", "Photos", "big.bin"])
        photos = root.children["Photos"]
        self.assertEqual(sorted(photos.children), ["2024", "cat.png"])
        self.assertEqual(photos.files, 2)
        self.assertEqual(photos.size, 270448 + 120303)
        self.assertTrue(photos.is_folder)
        self.assertFalse(photos.children["cat.png"].is_folder)

    def test_by_type_ignores_the_folders(self):
        root = diskrecover.by_type(self.entries)
        self.assertEqual(sorted(root.children),
                         ["Documents", "Other", "Pictures"])
        self.assertEqual(root.children["Pictures"].files, 3)

    def test_two_files_of_one_name_both_show(self):
        root = diskrecover.by_type([Entry("a/x.png", size=1),
                                    Entry("b/x.png", size=1)])
        self.assertEqual(sorted(root.children["Pictures"].children),
                         ["x.png", "x.png (2)"])

    def test_ticking_a_folder_ticks_what_is_in_it(self):
        root = diskrecover.by_folder(self.entries)
        photos = root.children["Photos"]
        self.assertEqual(photos.state(), 0)
        photos.children["cat.png"].choose(True)
        self.assertEqual(photos.state(), 1)
        photos.choose(True)
        self.assertEqual(photos.state(), 2)
        self.assertEqual(sum(e.chosen for e in self.entries), 2)
        # The ticks are the entries': the other view shows the same ones.
        other = diskrecover.by_type(self.entries)
        self.assertEqual(other.children["Pictures"].state(), 1)

    def test_finding_by_name(self):
        self.assertEqual(len(diskrecover.matching(self.entries, "PNG")), 3)
        self.assertEqual(len(diskrecover.matching(self.entries, "docs/")), 2)
        self.assertEqual(len(diskrecover.matching(self.entries, "",
                                                  smallest=10 ** 6)), 1)


class AdviceTest(unittest.TestCase):
    def setUp(self):
        self.machine = {d.name: d for d in
                        disks.parse(desktop(), fstab=[]) + parse(stick())}

    def titles(self, name, volume=None):
        disk = self.machine[name]
        return [a.title for a in diskrecover.scan_advice(
            disk, disk.volume(volume) if volume else None)]

    def test_an_ssd_with_trim_is_told_the_truth_up_front(self):
        self.assertIn("Files deleted from this SSD are probably gone",
                      self.titles("sdc", "sdc1"))
        self.assertNotIn("Files deleted from this SSD are probably gone",
                         self.titles("sda", "sda2"))

    def test_linux_filesystems_and_the_running_system_are_explained(self):
        found = self.titles("nvme0n1")
        self.assertIn("Linux filesystems keep no record of deleted files",
                      found)
        self.assertIn("The running system is on this drive", found)

    def test_ntfs_says_a_linux_delete_may_have_taken_the_names(self):
        note = "Files deleted from Linux may come back without names"
        self.assertIn(note, self.titles("sda"))           # has an NTFS
        self.assertNotIn(note, self.titles("sda", "sda1"))
        self.assertNotIn(note, self.titles("nvme0n1"))

    def test_names_can_be_listed_only_on_windows_filesystems(self):
        self.assertTrue(diskrecover.can_list(self.machine["sde"]))
        sda = self.machine["sda"]
        self.assertFalse(diskrecover.can_list(sda, sda.volume("sda1")))
        self.assertTrue(diskrecover.can_list(sda))       # has an NTFS too

    def test_availability(self):
        sde = self.machine["sde"]
        self.assertTrue(diskops.availability(diskops.RECOVER, sde, None,
                                             tools=everything))
        answer = diskops.availability(diskops.RECOVER, sde, None,
                                      tools=lambda *n: None)
        self.assertIn("dnf install sleuthkit testdisk", answer.reason)
        locked = disks.Volume("x1", "/dev/x1", "x", "part", GIB,
                              fstype="BitLocker")
        answer = diskrecover.availability(sde, locked, everything)
        self.assertIn("turn encryption off", answer.reason)
        self.assertIn("no media", diskrecover.availability(
            self.machine["sdd"], None, everything).reason)

    def test_the_requests(self):
        sde = self.machine["sde"]
        plan = diskrecover.plan_scan(sde, sde.volumes[0], "/r/s",
                                     identity={"i": 1})
        self.assertEqual(plan.request, {
            "op": "recover_scan", "device": "/dev/sde1", "session": "/r/s",
            "deleted": True, "content": False, "records": False,
            "kinds": [], "keep_damaged": False, "loose": False,
            "identity": {"i": 1}})
        self.assertEqual(plan.step_keys, ["deleted"])
        plan = diskrecover.plan_scan(sde, None, "/r/s", quick=False,
                                     deep=True, kinds=["Pictures"],
                                     keep_damaged=True, loose=True,
                                     identity={})
        self.assertEqual(plan.step_keys, ["records", "content"])
        self.assertEqual((plan.request["records"], plan.request["content"],
                          plan.request["keep_damaged"],
                          plan.request["loose"]), (True, True, True, False))
        self.assertIn("jpg", plan.request["kinds"])
        self.assertIn("photos", plan.steps[-1])
        plan = diskrecover.plan_recover(sde, None, "/r/s", "/out", 3, 5000,
                                        identity={})
        self.assertEqual((plan.request["op"], plan.request["device"],
                          plan.request["target"]),
                         ("recover_files", "/dev/sde", "/out"))
        self.assertIn("3 deleted files on record and 2 files found",
                      diskrecover.scan_done_text({"deleted": 3, "found": 2}))
        self.assertIn("nothing was found", diskrecover.scan_done_text({}))
        self.assertIn("4 folders of an earlier FAT32 or exFAT",
                      diskrecover.scan_done_text(
                          {"old": [{"count": 0, "folders": 4}]}))

    def test_the_names_used_in_the_folder_are_the_helpers(self):
        from disktree import load_helper
        helper = load_helper()
        for name in ("SESSION_MARK", "FOUND_DIR", "RECOVERED_DIR",
                     "SELECTION_NAME"):
            self.assertEqual(getattr(helper, name),
                             getattr(diskrecover, name))


class HelperRecoveryTest(HelperCase):
    def setUp(self):
        super().setUp()
        self.session = os.path.join(self.root, "rec")
        os.mkdir(self.session)
        with open(os.path.join(self.session, "scan.json"), "w") as handle:
            handle.write("{}")
        # Where recovered files go: chosen at the time, any folder of the
        # user's not on the drive.
        self.into = os.path.join(self.session, "recovered")
        os.mkdir(self.into)
        with open(body("ntfs"), "rb") as handle:
            self.listing = handle.read()
        self.tools.stdout["fsstat"] = "ntfs\n"
        self.tools.writes["fls"] = self.listing

    def ask(self, op, name="sdd1", **fields):
        request = {"op": op, "id": 2,
                   "device": os.path.join(self.tree.dev, name),
                   "identity": self.tree.identity(name),
                   "session": self.session}
        if op == "recover_files":
            request["target"] = self.into
        request.update(fields)
        return request

    def scan(self, name="sdd1", **fields):
        return self.helper.op_recover_scan(self.ask("recover_scan", name,
                                                    **fields))

    def test_the_listing_is_written_into_the_folder_and_counted(self):
        result = self.scan(deleted=True)
        path = os.path.join(self.session, "deleted-sdd1.body")
        self.assertEqual(result["volumes"][0]["body"], path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), self.listing)
        self.assertEqual(result["deleted"], self.listing.count(b"(deleted"))
        self.assertEqual(self.tools.of("fls")[0][:4],
                         ["-r", "-d", "-m", "/"])
        # What the application reads back from it.
        self.assertEqual(len(diskrecover.parse_body(path, "/dev/sdd1")), 5)

    def test_nothing_is_ever_written_to_the_drive_searched(self):
        before = self.tools.names()
        self.scan(deleted=True)
        used = set(self.tools.names()) - set(before)
        self.assertEqual(used, {"fsstat", "fls"})

    def test_a_filesystem_without_such_a_record_is_reported_not_failed(self):
        self.tools.stdout["fsstat"] = "ext4\n"
        result = self.scan(deleted=True)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["unlisted"][0]["fstype"], "ext4")
        self.assertEqual(self.tools.of("fls"), [])

    def test_a_whole_drive_is_read_partition_by_partition(self):
        self.tree.add_partition("sdd", "sdd2", 2, 9000000, 100 * MIB)
        result = self.scan("sdd", deleted=True)
        self.assertEqual([os.path.basename(v["device"])
                          for v in result["volumes"]], ["sdd1", "sdd2"])

    def test_searching_by_content_saves_into_the_folder(self):
        found = os.path.join(self.session, "found")

        def photorec(args):
            target = os.path.join(found, "part.1")
            os.makedirs(target)
            for name in ("f1.png", "f2.pdf", "photorec.log"):
                with open(os.path.join(target, name), "w") as handle:
                    handle.write("x")
        self.tools.makes["photorec"] = photorec
        self.tools.lines["photorec"] = [
            "Pass 1 - Reading sector    1000/16000, 2 files found"]
        result = self.scan(content=True)
        self.assertEqual(result["found"], 2)
        args = self.tools.of("photorec")[0]
        self.assertEqual(args[:3], ["/log", "/d", os.path.join(found,
                                                               "part")])
        self.assertEqual(args[-1], self.module.PHOTOREC_COMMAND)
        self.assertEqual(self.tools.options["cwd"], found)
        self.assertEqual(self.tools.options["env"]["TERM"], "xterm")
        events = self.events()
        self.assertIn((1000 * 512, 16000 * 512),
                      [(e["done"], e["total"]) for e in events
                       if e.get("event") == "progress"])
        self.assertIn("2 files found so far",
                      [e["text"] for e in events if e.get("event") == "note"])

    def test_a_deep_scan_can_be_limited_and_told_what_to_keep(self):
        self.tools.makes["photorec"] = lambda args: os.makedirs(
            os.path.join(self.session, "found", "part.1"))
        self.scan(content=True, kinds=["png", "jpg"], keep_damaged=True)
        self.assertEqual(self.tools.of("photorec")[0][-1],
                         "partition_none,fileopt,everything,disable,jpg,"
                         "enable,png,enable,options,keep_corrupted_file,"
                         "wholespace,search")
        shutil.rmtree(os.path.join(self.session, "found"))
        self.scan(content=True, loose=True)
        self.assertEqual(self.tools.of("photorec")[-1][-1],
                         "partition_none,fileopt,everything,enable,"
                         "wholespace,search")
        for kinds in (["exe"], ["jpg,everything,enable"], "jpg", [3]):
            self.assertIn("not a kind of file", self.refused(
                self.ask("recover_scan", content=True, kinds=kinds)))

    def test_where_files_are_recovered_to_is_checked(self):
        self.choose(self.entry("a.txt"))
        for target, said in ((os.path.join(self.root, "none"),
                              "does not exist"),
                             ("relative", "not a folder"),
                             (os.path.join(self.session, "scan.json"),
                              "not a folder")):
            self.assertIn(said, self.refused(self.ask(
                "recover_files", target=target)))
        self.tree.mount("sdd1", self.into)
        self.assertIn("itself", self.refused(self.ask("recover_files")))
        self.assertEqual(self.tools.of("icat"), [])

    def test_the_folder_has_to_be_a_recovery_folder_of_the_users(self):
        for session in (self.root, "relative", os.path.join(self.root, "no")):
            text = self.refused(self.ask("recover_scan", session=session,
                                         deleted=True))
            self.assertTrue("not a recovery folder" in text
                            or "not a folder" in text)
        self.assertIn("what to look for",
                      self.refused(self.ask("recover_scan")))

    def test_recovering_onto_the_drive_itself_is_refused(self):
        self.tree.mount("sdd1", self.session)
        text = self.refused(self.ask("recover_scan", deleted=True))
        self.assertIn("itself", text)

    def choose(self, *entries):
        with open(os.path.join(self.session, "selection.json"), "w") as h:
            json.dump(list(entries), h)

    def recover(self):
        return self.helper.op_recover_files(self.ask("recover_files"))

    def entry(self, path, inode="67-128-2", size=5, device=None):
        return {"device": device or os.path.join(self.tree.dev, "sdd1"),
                "inode": inode, "path": path, "size": size}

    def test_files_are_copied_out_under_their_names_and_folders(self):
        self.tools.writes["icat"] = b"hello"
        self.choose(self.entry("Docs/report.pdf"),
                    self.entry("Photos/2024/a.png", "71-128-2"))
        result = self.recover()
        self.assertEqual((result["recovered"], result["failed"]), (2, 0))
        with open(os.path.join(self.session, "recovered", "Docs",
                               "report.pdf"), "rb") as handle:
            self.assertEqual(handle.read(), b"hello")
        self.assertEqual(self.tools.of("icat")[0],
                         ["-r", os.path.join(self.tree.dev, "sdd1"),
                          "67-128-2"])
        with open(result["report"]) as handle:
            self.assertEqual(len(json.load(handle)["recovered"]), 2)

    def test_two_files_of_one_name_do_not_overwrite_each_other(self):
        self.tools.writes["icat"] = b"x"
        self.choose(self.entry("a.txt", "5"), self.entry("a.txt", "6"),
                    self.entry("noext", "7"), self.entry("noext", "8"))
        self.recover()
        self.assertEqual(sorted(os.listdir(os.path.join(self.session,
                                                        "recovered"))),
                         ["a (2).txt", "a.txt", "noext", "noext (2)"])

    def test_a_recorded_path_cannot_lead_out_of_the_folder(self):
        self.tools.writes["icat"] = b"x"
        self.choose(self.entry("../../../etc/evil", "5"),
                    self.entry("/abs/olute.txt", "6"),
                    self.entry("a/./b/../c.txt", "7"))
        self.recover()
        base = os.path.join(self.session, "recovered")
        self.assertTrue(os.path.exists(os.path.join(base, "etc", "evil")))
        self.assertTrue(os.path.exists(os.path.join(base, "abs",
                                                    "olute.txt")))
        self.assertTrue(os.path.exists(os.path.join(base, "a", "b",
                                                    "c.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc")))

    def test_a_list_naming_another_device_or_an_odd_address_is_refused(self):
        for entry in (self.entry("a", device="/dev/sdb1"),
                      self.entry("a", inode="5; rm -rf /"),
                      self.entry("a", inode="-r"), "text"):
            self.choose(entry)
            self.assertIn("not valid",
                          self.refused(self.ask("recover_files")))
        self.assertEqual(self.tools.of("icat"), [])
        self.choose()
        self.assertIn("No files were chosen",
                      self.refused(self.ask("recover_files")))

    def test_old_file_records_are_collected_off_the_device(self):
        import struct
        from test_ntfsscan import record
        device = os.path.join(self.tree.dev, "sdd1")
        with open(device, "r+b") as handle:
            handle.seek(8 * MIB)
            handle.write(record(40, "one.txt", data=b"first"))
            handle.seek(8 * MIB + 3 * 1024)
            handle.write(record(41, "two.txt", data=b"second"))
            handle.seek(12 * MIB + 100)          # not at a sector's start
            handle.write(b"FILE is just a word here")
        result = self.scan(records=True)
        found = result["old"][0]
        self.assertEqual(found["count"], 2)
        with open(found["records"], "rb") as handle:
            data = handle.read()
        self.assertEqual(len(data), 2 * 1032)
        self.assertEqual(struct.unpack_from("<Q", data, 0)[0], 8 * MIB)
        entries = diskrecover.lost_files(found["records"], found["info"],
                                         "/dev/sdd1")
        self.assertEqual(sorted(e.name for e in entries),
                         ["one.txt", "two.txt"])
        self.assertEqual(self.tools.names().count("fls"), 0)

    def test_old_fat_folders_are_collected_and_their_files_copied(self):
        from test_fatscan import MTOOLS, fat_image, mtools
        if not MTOOLS:
            self.skipTest("mtools is not installed")
        import tempfile
        folder = tempfile.mkdtemp(dir=self.root)
        files = {"Photos/Trip 2024/beach.png": os.urandom(70000),
                 "Photos/cat.png": os.urandom(9000), "top.txt": b"gone"}
        image = fat_image(folder, files)
        mtools("mformat", "-i", image, "-F", "::")
        device = os.path.join(self.tree.dev, "sdd1")
        with open(image, "rb") as source, open(device, "r+b") as target:
            target.write(source.read())
        result = self.scan(records=True)
        found = result["old"][0]
        self.assertEqual((found["count"], found["records"]), (0, ""))
        self.assertGreaterEqual(found["folders"], 2)
        entries = diskrecover.lost_files("", found["info"], device,
                                         found["dirs"])
        names = sorted(e.path.split("/", 2)[2] for e in entries)
        self.assertEqual(names, ["Trip 2024/beach.png", "cat.png"])
        for entry in entries:
            entry.chosen = True
        self.assertEqual(diskrecover.write_selection(self.session,
                                                     entries), 2)
        outcome = self.recover()
        self.assertEqual((outcome["recovered"], outcome["failed"]), (2, 0))
        for entry in entries:
            tail = entry.path.split("/", 2)[2]
            with open(os.path.join(self.session, "recovered",
                                   entry.path), "rb") as handle:
                self.assertEqual(handle.read(), files["Photos/" + tail])
        # And a scan's result reads back the same way.
        again = diskrecover.scan_entries(result, self.session)
        self.assertEqual(sorted(e.path for e in again),
                         sorted(e.path for e in entries))

    def test_a_drive_with_no_old_records_reports_none(self):
        result = self.scan(records=True)
        self.assertEqual(result["old"], [])
        self.assertEqual([n for n in os.listdir(self.session)
                          if n.startswith("records")], [])

    def test_files_named_by_old_records_are_copied_from_where_they_say(self):
        device = os.path.join(self.tree.dev, "sdd1")
        with open(device, "r+b") as handle:
            handle.seek(4 * MIB)
            handle.write(b"A" * 5000)
            handle.seek(9 * MIB)
            handle.write(b"B" * 3000)
        self.choose(
            {"device": device, "path": "Before the format/a/joined.bin",
             "size": 9000, "inline": "",
             "extents": [[4 * MIB, 5000], [None, 1000], [9 * MIB, 3000]]},
            {"device": device, "path": "Before the format/small.txt",
             "size": 5, "extents": [], "inline": b"hello".hex()})
        result = self.recover()
        self.assertEqual((result["recovered"], result["failed"]), (2, 0))
        base = os.path.join(self.session, "recovered", "Before the format")
        with open(os.path.join(base, "a", "joined.bin"), "rb") as handle:
            self.assertEqual(handle.read(),
                             b"A" * 5000 + b"\0" * 1000 + b"B" * 3000)
        with open(os.path.join(base, "small.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"hello")
        self.assertEqual(self.tools.of("icat"), [])

    def test_places_outside_the_device_are_refused_before_any_reading(self):
        device = os.path.join(self.tree.dev, "sdd1")
        size = 8 * 1024 * MIB
        for extents in ([[size, 4096]], [[-4096, 4096]], [[0, 0]],
                        [[0, "many"]], "everything", [[0, 4096, 1]]):
            self.choose({"device": device, "path": "x", "size": 4096,
                         "extents": extents, "inline": ""})
            self.assertIn("not valid",
                          self.refused(self.ask("recover_files")))
        self.choose({"device": device, "path": "x", "size": 0,
                     "extents": [[0, 4096]], "inline": ""})
        self.assertIn("not valid", self.refused(self.ask("recover_files")))

    def test_a_file_that_cannot_be_read_is_reported_not_left_empty(self):
        self.tools.codes["icat"] = 1
        self.tools.lines["icat"] = ["Invalid address"]
        self.choose(self.entry("gone.jpg"))
        result = self.recover()
        self.assertEqual((result["recovered"], result["failed"]), (0, 1))
        self.assertFalse(os.path.exists(os.path.join(
            self.session, "recovered", "gone.jpg")))


class ArrayTest(DiskWindowCase):
    ARRAYS = [
        {"uuid": "aaaa:1111", "level": "raid5", "devices": 3, "present": 2,
         "name": "nas:volume1", "members": ["/dev/sdx", "/dev/sdy"],
         "device": ""},
        {"uuid": "bbbb:2222", "level": "raid5", "devices": 4, "present": 2,
         "name": "", "members": ["/dev/sdv", "/dev/sdw"], "device": ""},
        {"uuid": "cccc:3333", "level": "raid1", "devices": 2, "present": 1,
         "name": "", "members": ["/dev/sdu"], "device": ""},
        {"uuid": "dddd:4444", "level": "raid0", "devices": 2, "present": 1,
         "name": "", "members": ["/dev/sdt"], "device": ""},
    ]

    def test_what_each_array_can_do_without_is_said(self):
        said = [diskrecover.describe_array(a) for a in self.ARRAYS]
        self.assertIn("RAID 5 of 3 drives, 2 connected ('volume1')",
                      said[0][0])
        self.assertTrue(said[0][1])
        self.assertIn("one short", said[0][0])
        self.assertFalse(said[1][1])
        self.assertIn("2 of its drives are missing", said[1][2])
        self.assertTrue(said[2][1])               # a mirror: one is enough
        self.assertFalse(said[3][1])
        self.assertIn("can only do without 0", said[3][2])

    def test_an_array_is_found_chosen_and_opened_read_only(self):
        from ksyssupervisor.diskrecoverui import ArraysDialog
        window = self.window()
        results = [{"arrays": self.ARRAYS}, {"device": "/dev/md127"}]
        run = self.client.run

        def answer(request, on_event=None):
            self.client.result = results.pop(0)
            return run(request, on_event)
        self.client.run = answer
        self.answers = [True]
        window.raid_action.trigger()
        dialog = self.dialogs[-1]
        self.assertIsInstance(dialog, ArraysDialog)
        self.assertEqual([b.isEnabled() for b in dialog.buttons],
                         [True, False, True, False])
        self.assertEqual(self.client.requests[-1],
                         {"op": "raid_open", "uuid": "aaaa:1111"})
        self.assertIn("open read-only as /dev/md127", window.status.text())

    def test_no_array_says_which_kinds_cannot_be_seen(self):
        window = self.window()
        self.client.result = {"arrays": []}
        window.raid_action.trigger()
        self.assertIn("hardware controller", self.messages[-1][1])

    def test_an_open_array_is_a_drive_that_can_be_closed(self):
        array = disks.Disk("md127", "/dev/md127", 400 * GIB,
                           model="RAID 5 array", read_only=True)
        self.assertEqual(array.kind, disks.RAID)
        self.machine = self.machine + [array]
        window = self.window()
        window.select_device("md127")
        self.assertTrue(window.raid_close_action.isVisible())
        window.raid_close_action.trigger()
        self.assertEqual(self.client.requests[-1]["op"], "raid_close")
        window.select_device("sdb")
        self.assertFalse(window.raid_close_action.isVisible())

    def test_an_array_listed_under_its_members_becomes_a_drive(self):
        member = {"name": "sdx", "kname": "sdx", "path": "/dev/sdx",
                  "type": "disk", "size": 200 * GIB,
                  "fstype": "linux_raid_member", "children": [
                      {"name": "md127", "kname": "md127",
                       "path": "/dev/md127", "type": "raid5",
                       "size": 400 * GIB, "fstype": "ext4", "ro": True,
                       "mountpoints": [None]}]}
        other = dict(member, name="sdy", kname="sdy", path="/dev/sdy")
        found = {d.name: d for d in parse(member, other)}
        self.assertEqual(sorted(found), ["md127", "sdx", "sdy"])
        self.assertEqual(found["md127"].title, "RAID 5 array")
        self.assertEqual(found["md127"].kind_title, "RAID array")


class GuessArrayTest(DiskWindowCase):
    """Drives whose own record of their array is gone."""

    GUESS = {"sure": True, "why": "", "members": 3, "candidates": [
        {"layout": "raid5_ls", "chunk": 65536, "offset": 1048576,
         "order": ["/dev/sdb", None, "/dev/sdc"], "score": 1.0,
         "checks": 2400, "kind": "NTFS", "size": 2 * 1000 * GIB},
        {"layout": "raid0", "chunk": 4096, "offset": 0,
         "order": ["/dev/sdc", "/dev/sdb"], "score": 0.41, "checks": 2400,
         "kind": "NTFS", "size": 2000 * GIB}]}

    def test_which_drives_can_be_named(self):
        by_name = {d.name: d for d in self.machine}
        self.assertIn("running system",
                      diskrecover.member_problem(by_name["nvme0n1"]))
        self.assertIn("no media", diskrecover.member_problem(by_name["sdd"]))
        self.assertIn("mounted", diskrecover.member_problem(by_name["sde"]))
        self.assertEqual(diskrecover.member_problem(by_name["sdc"]), "")

    def test_found_chosen_and_opened_read_only(self):
        from ksyssupervisor.diskrecoverui import (GuessDialog,
                                                  GuessResultDialog)
        window = self.window()
        results = [self.GUESS, {"device": "/dev/dm-4"}]
        run = self.client.run

        def answer(request, on_event=None):
            self.client.result = results.pop(0)
            return run(request, on_event)
        self.client.run = answer
        seen = []

        def show(dialog):
            seen.append(dialog)
            if isinstance(dialog, GuessDialog):
                enabled = [b for _d, b in dialog.drives if b.isEnabled()]
                self.assertFalse(dialog.start.isEnabled())
                for box in enabled[:2]:
                    box.setChecked(True)
                dialog.missing.setChecked(True)
                self.assertTrue(dialog.start.isEnabled())
            return True
        window.show_dialog = show
        window.guess_action.trigger()
        guess = self.client.requests[-2]
        self.assertEqual(guess["op"], "raid_guess")
        self.assertEqual(len(guess["members"]), 3)
        self.assertIsNone(guess["members"][-1])
        self.assertIsInstance(seen[1], GuessResultDialog)
        self.assertEqual(len(seen[1].buttons), 1)       # sure: just the one
        built = self.client.requests[-1]
        self.assertEqual(built["op"], "raid_build")
        self.assertEqual([m and m["device"] for m in built["members"]],
                         ["/dev/sdb", None, "/dev/sdc"])
        self.assertEqual((built["layout"], built["chunk"], built["offset"]),
                         ("raid5_ls", 65536, 1048576))
        self.assertIn("open read-only as /dev/dm-4", window.status.text())

    def test_a_guess_that_is_not_sure_says_so(self):
        from ksyssupervisor.diskrecoverui import GuessResultDialog
        dialog = GuessResultDialog(dict(self.GUESS, sure=False,
                                        why="Too little on it."), {})
        text = " ".join(w.text() for w in dialog.findChildren(QLabel))
        self.assertEqual(len(dialog.buttons), 2)        # the others too
        self.assertIn("Not sure", text)
        self.assertIn("scrambled", text)
        line, details = diskrecover.describe_guess(self.GUESS["candidates"][0],
                                                   {"/dev/sdb": "sdb (X)"})
        self.assertEqual(line, "RAID 5, left-symmetric (Linux's usual), "
                               "pieces of 64 KiB")
        self.assertIn("Drives in order: sdb (X), (missing), /dev/sdc.",
                      details)
        self.assertIn("Data starts 1 MiB into each drive.", details)

    def test_the_choices_that_cannot_work_are_refused_in_the_dialog(self):
        from ksyssupervisor.diskrecoverui import GuessDialog
        drives = [d for d in self.machine
                  if not diskrecover.member_problem(d)][:2]
        dialog = GuessDialog(drives)
        for _disk, box in dialog.drives:
            box.setChecked(True)
        self.assertEqual(dialog.problem(), "")
        dialog.levels.buttons()[1].setChecked(True)          # RAID 0
        dialog.missing.setChecked(True)
        self.assertIn("RAID 0 cannot", dialog.problem())
        dialog.levels.buttons()[2].setChecked(True)          # RAID 5
        dialog.missing.setChecked(False)
        self.assertIn("at least three", dialog.problem())

    def test_an_array_put_together_here_is_listed_as_a_drive(self):
        member = {"name": "sdx", "kname": "sdx", "path": "/dev/sdx",
                  "type": "disk", "size": 200 * GIB, "children": [
                      {"name": "kss-raid5-1", "kname": "dm-4",
                       "path": "/dev/mapper/kss-raid5-1", "type": "dm",
                       "size": 400 * GIB, "fstype": "ntfs", "ro": True,
                       "mountpoints": [None]},
                      {"name": "luks-abc", "kname": "dm-5",
                       "path": "/dev/mapper/luks-abc", "type": "crypt",
                       "size": 100 * GIB}]}
        found = {d.name: d for d in parse(member)}
        self.assertEqual(sorted(found), ["dm-4", "sdx"])
        self.assertEqual(found["dm-4"].title, "RAID 5 array")
        self.assertEqual(found["dm-4"].kind, disks.RAID)


class RecoverDialogTest(DiskWindowCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.by_name = {d.name: d for d in self.machine}

    def dialog(self, name="sde", volume=None, **options):
        disk = self.by_name[name]
        return RecoverDialog(disk, disk.volume(volume) if volume else None,
                             identity={"n": 1}, folder=self.dir,
                             scratch_base=self.dir, **options)

    def test_quick_where_it_can_be_deep_where_it_cannot(self):
        dialog = self.dialog("sde", "sde1")
        self.assertTrue(dialog.quick.isChecked())
        self.assertFalse(dialog.options.isEnabled())
        self.assertTrue(dialog.folder.isHidden())        # needs none
        self.assertEqual(dialog.start.text(), "Start Quick Scan")
        dialog = self.dialog("sda", "sda1")             # ext4
        self.assertFalse(dialog.quick.isEnabled())
        self.assertTrue(dialog.deep.isChecked())
        self.assertFalse(dialog.folder.isHidden())
        self.assertEqual(dialog.start.text(), "Start Deep Scan")

    def test_how_deep_is_chosen_only_for_a_deep_scan(self):
        dialog = self.dialog()
        dialog.deep.setChecked(True)
        self.assertTrue(dialog.options.isEnabled())
        self.assertTrue(dialog.skip_damaged.isChecked())
        self.assertFalse(dialog.loose.isChecked())
        self.assertFalse(dialog.kinds["Pictures"].isEnabled())
        dialog.some_kinds.setChecked(True)
        self.assertTrue(dialog.kinds["Pictures"].isEnabled())
        self.assertFalse(dialog.loose.isEnabled())    # no sense with kinds
        for box in dialog.kinds.values():
            box.setChecked(False)
        self.assertFalse(dialog.start.isEnabled())
        dialog.kinds["Audio"].setChecked(True)
        self.assertTrue(dialog.start.isEnabled())
        self.assertEqual(dialog.chosen_kinds(), ["Audio"])

    def test_the_folder_is_checked_before_a_deep_scan_starts(self):
        dialog = self.dialog(problem=lambda folder, disk:
                             "on the drive being searched"
                             if folder == "/bad" else "")
        dialog.deep.setChecked(True)
        dialog.folder.setText("/bad")
        self.assertFalse(dialog.start.isEnabled())
        self.assertIn("being searched", dialog.folder_note.text())
        dialog.folder.setText(self.dir)
        self.assertTrue(dialog.start.isEnabled())

    def test_a_quick_scan_works_in_a_private_folder(self):
        dialog = self.dialog("sde", "sde1")
        dialog._accept()
        self.assertTrue(os.path.basename(dialog.folder_path).startswith(
            "ksyssupervisor-search-"))
        request = dialog.plan().request
        self.assertEqual((request["device"], request["deleted"],
                          request["content"]), ("/dev/sde1", True, False))

    def test_a_deep_scan_makes_its_folder_and_says_how_deep(self):
        dialog = self.dialog()
        dialog.deep.setChecked(True)
        dialog.some_kinds.setChecked(True)
        dialog.skip_damaged.setChecked(False)
        dialog._accept()
        self.assertTrue(os.path.basename(dialog.folder_path).startswith(
            diskrecover.WORK_PREFIX))
        request = dialog.plan().request
        self.assertEqual((request["deleted"], request["records"],
                          request["content"], request["keep_damaged"]),
                         (True, True, True, True))
        self.assertIn("jpg", request["kinds"])
        self.assertNotIn("rar", request["kinds"])     # archives not ticked

    def test_from_the_results_it_only_goes_deeper(self):
        dialog = self.dialog(deep_only=True)
        self.assertFalse(dialog.quick.isEnabled())
        self.assertTrue(dialog.deep.isChecked())
        dialog._accept()
        self.assertFalse(dialog.plan().request["deleted"])

    def test_advice_shows_its_titles_warnings_first(self):
        from ksyssupervisor.diskdialogs import AdviceCard
        dialog = self.dialog("nvme0n1")
        cards = dialog.findChildren(AdviceCard)
        levels = [c.advice.level for c in cards]
        self.assertEqual(levels, sorted(levels, key=lambda level:
                                        level != diskfs.WARNING))
        self.assertIn(diskfs.WARNING, levels)
        card = cards[0]
        self.assertTrue(card.details.isHidden())
        card.flip()
        self.assertFalse(card.details.isHidden())


class ResultsTest(DiskWindowCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.entries = diskrecover.parse_body(body("exfat"), "/dev/sde1")
        path = os.path.join(self.dir, "f1.png")
        png(path, side=32)
        self.entries.append(Entry("Found by content/Pictures/f1.png",
                                  size=os.path.getsize(path),
                                  source=diskrecover.FOUND, file=path))
        self.asked = []
        self.record = {"what": "a stick", "scans": ["quick"]}

    def results(self):
        return RecoveryResults(self.record, self.entries,
                               recover=self.asked.append,
                               deeper=lambda r: self.asked.append("deep"),
                               save=lambda r: self.asked.append("save"))

    def top(self, results):
        return [results.tree.topLevelItem(i).text(0)
                for i in range(results.tree.topLevelItemCount())]

    def find(self, results, *names):
        item = results.tree.invisibleRootItem()
        for name in names:
            if item is not results.tree.invisibleRootItem():
                item.setExpanded(True)
            item = [item.child(i) for i in range(item.childCount())
                    if item.child(i).text(0) == name][0]
        return item

    def test_it_opens_on_the_folders_the_files_were_in(self):
        results = self.results()
        self.assertTrue(results.folders_view.isChecked())
        self.assertEqual(self.top(results),
                         ["Docs", "Found by content", "Photos", "big.bin"])
        self.assertIn("5 deleted files still on record and 1 files found",
                      results.summary.text())
        self.assertFalse(results.go.isEnabled())

    def test_the_other_view_groups_by_kind_and_keeps_the_ticks(self):
        results = self.results()
        self.find(results, "Photos").setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(sum(e.chosen for e in self.entries), 2)
        self.assertIn("2 of 6 files ticked", results.chosen.text())
        self.assertTrue(results.go.isEnabled())
        results.types_view.setChecked(True)
        self.assertEqual(self.top(results),
                         ["Documents", "Other", "Pictures"])
        pictures = self.find(results, "Pictures")
        self.assertEqual(pictures.checkState(0),
                         Qt.CheckState.PartiallyChecked)
        self.assertIn("2 of 6 files ticked", results.chosen.text())

    def test_ticking_one_file_part_ticks_its_folders(self):
        results = self.results()
        cat = self.find(results, "Photos", "cat.png")
        cat.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(self.find(results, "Photos").checkState(0),
                         Qt.CheckState.PartiallyChecked)
        self.find(results, "Photos").setCheckState(
            0, Qt.CheckState.Unchecked)
        self.assertEqual(cat.checkState(0), Qt.CheckState.Unchecked)
        self.assertEqual(sum(e.chosen for e in self.entries), 0)

    def test_finding_by_name_narrows_the_list(self):
        results = self.results()
        results.search.setText("report")
        self.assertEqual(self.top(results), ["Docs"])
        results.search.setText("")
        self.assertEqual(len(self.top(results)), 4)

    def test_a_found_file_is_shown_and_judged_when_selected(self):
        results = self.results()
        item = self.find(results, "Found by content", "Pictures", "f1.png")
        results.tree.setCurrentItem(item)
        self.assertIn("Intact", results.about.text())
        self.assertFalse(results.picture.pixmap().isNull())
        self.assertEqual(item.text(3), "Intact")
        deleted = self.find(results, "Photos", "cat.png")
        results.tree.setCurrentItem(deleted)
        self.assertEqual(results.picture.text(), "Recover it to see it")
        self.assertIn("Good", results.about.text())

    def test_a_folder_of_thousands_opens_a_batch_at_a_time(self):
        self.entries = [Entry("Found by content/Other/f%05d.bin" % n, size=1,
                              source=diskrecover.FOUND)
                        for n in range(1300)]
        results = self.results()
        # Nothing here has a folder of its own, so it opens by type.
        self.assertTrue(results.types_view.isChecked())
        other = self.find(results, "Other")
        other.setExpanded(True)
        self.assertEqual(other.childCount(), 501)
        more = other.child(500)
        self.assertIn("800 more", more.text(0))
        results._clicked(more, 0)
        self.assertEqual(other.childCount(), 1001)
        other.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(sum(e.chosen for e in self.entries), 1300)

    def test_nothing_found_says_what_that_means(self):
        self.entries = []
        results = self.results()
        self.assertIn("Nothing was found", results.summary.text())
        self.assertIn("Deep Scan", results.summary.text())

    def test_damaged_files_can_be_repaired_from_the_results(self):
        cut = os.path.join(self.dir, "cut.jpg")
        with open(cut, "wb") as handle:
            handle.write(b"\xff\xd8\xff\xe0" + b"\x11" * 3000)
        self.entries.append(Entry("Found by content/Pictures/cut.jpg",
                                  size=3004, source=diskrecover.FOUND,
                                  file=cut))
        results = self.results()
        self.assertTrue(results.repair_button.isHidden())   # not judged yet
        diskrecover.inspect_all(self.entries)
        results.refresh()
        self.assertFalse(results.repair_button.isHidden())
        said = []
        results.tell = said.append
        results.repair()
        self.assertIn("1 of 1 damaged files could be repaired", said[0])
        self.assertTrue(os.path.exists(os.path.join(
            self.dir, "cut (repaired).jpg")))

    def test_its_buttons_hand_the_window_to_whoever_does_the_work(self):
        results = self.results()
        self.find(results, "Docs").setCheckState(0, Qt.CheckState.Checked)
        results.go.click()
        results.deep_button.click()
        results.save_button.click()
        self.assertEqual(self.asked, [results, "deep", "save"])

    def test_a_deep_scan_offers_what_it_finds_as_it_goes(self):
        results = self.results()
        self.assertFalse(results.deep_button.isHidden())
        work = os.path.join(self.dir, "work")
        part = os.path.join(work, "found", "part.1")
        os.makedirs(part)
        results.set_busy(True, work)
        self.assertFalse(results.deep_button.isEnabled())
        self.find(results, "Docs").setCheckState(0, Qt.CheckState.Checked)
        self.assertFalse(results.go.isEnabled())     # the drive is busy
        png(os.path.join(part, "f0000777.png"))
        results._look()
        self.assertFalse(results.more.isHidden())
        self.assertEqual(results.more.text(), "Show 1 new file")
        results.show_new()
        self.assertEqual(len(results.entries), 7)
        record = dict(self.record, scans=["quick", "deep"])
        results.set_entries(record, results.entries)
        self.assertTrue(results.deep_button.isHidden())   # done already
        self.assertTrue(results.go.isEnabled())


class RecoverFlowTest(DiskWindowCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for target, name, value in (
                (diskrecover, "availability",
                 lambda disk, volume=None, tools=None: diskops.YES),
                (diskrecover, "scratch_base", lambda: self.dir)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(
            self.dir, "data")})
        patch.start()
        self.addCleanup(patch.stop)

    def quick(self, window):
        original = window.show_dialog

        def drive(dialog):
            if isinstance(dialog, RecoverDialog):
                dialog._accept()
                shutil.copy(body("vfat"), os.path.join(
                    dialog.folder_path, "deleted-sde1.body"))
                self.client.result = {
                    "deleted": 6, "found": 0, "unlisted": [],
                    "volumes": [{"device": "/dev/sde1",
                                 "body": "deleted-sde1.body"}]}
            return original(dialog)
        window.show_dialog = drive
        self.answers = [True]
        window.recover_action.trigger()

    def test_a_quick_scan_opens_its_results_and_keeps_the_search(self):
        window = self.window()
        window.select_device("sde1")
        self.quick(window)
        self.assertEqual(self.client.requests[-1]["op"], "recover_scan")
        self.assertIn("6 deleted files on record", window.status.text())
        results = window._results
        self.assertEqual(len(results.entries), 5)
        # Its private folder is gone; the search is kept in a file.
        self.assertEqual([n for n in os.listdir(self.dir)
                          if n.startswith("ksyssupervisor-search-")], [])
        self.assertTrue(results.path.endswith(".kssearch"))
        record, entries = diskrecover.load_search(results.path)
        self.assertEqual(record["scans"], ["quick"])
        self.assertEqual(len(entries), 5)

    def test_files_are_recovered_into_a_folder_chosen_then(self):
        window = self.window()
        window.select_device("sde1")
        self.quick(window)
        results = window._results
        for entry in results.entries[:2]:
            entry.chosen = True
        out = os.path.join(self.dir, "out")
        os.mkdir(out)
        window.pick_folder = lambda start, title="": out

        def answer(request, on_event=None):
            # The helper writes the files and its report.
            files = []
            for entry in results.entries[:2]:
                path = os.path.join(request["target"], entry.name)
                png(path)
                files.append({"path": entry.path, "file": path})
            with open(os.path.join(request["session"], "recovered.json"),
                      "w") as handle:
                json.dump({"recovered": files, "failed": []}, handle)
            with open(os.path.join(request["session"],
                                   "selection.json")) as handle:
                self.assertEqual(len(json.load(handle)), 2)
            self.client.requests.append(request)
            return {"recovered": 2, "failed": 0}
        self.client.run = answer
        results.recover()
        request = self.client.requests[-1]
        self.assertEqual((request["op"], request["device"]),
                         ("recover_files", "/dev/sde1"))
        self.assertTrue(os.path.basename(request["target"]).startswith(
            diskrecover.FOLDER_PREFIX))
        self.assertEqual(os.path.dirname(request["target"]), out)
        self.assertIn("Recovered 2 files", self.messages[-1][1])
        # A copy of the search goes with the files.
        self.assertTrue(any(n.endswith(".kssearch")
                            for n in os.listdir(request["target"])))

    def test_a_deep_scan_from_the_results_adds_to_them(self):
        window = self.window()
        window.select_device("sde1")
        self.quick(window)
        results = window._results
        original = window.show_dialog

        def deep(dialog):
            dialog.folder.setText(self.dir)
            dialog._accept()
            part = os.path.join(dialog.folder_path, "found", "part.1")
            os.makedirs(part)
            png(os.path.join(part, "f0000500.png"))
            self.client.result = {"deleted": 0, "found": 1, "volumes": [],
                                  "old": []}
            return original(dialog)
        window.show_dialog = deep
        self.answers = [True]
        results.deeper()
        request = self.client.requests[-1]
        self.assertEqual((request["deleted"], request["content"]),
                         (False, True))
        self.assertEqual(len(results.entries), 6)
        self.assertEqual(results.record["scans"], ["quick", "deep"])
        self.assertTrue(results.deep_button.isHidden())
        _record, kept = diskrecover.load_search(results.path)
        self.assertEqual(len(kept), 6)

    def test_a_saved_search_opens_again(self):
        window = self.window()
        window.pick_search = lambda start: os.path.join(self.dir, "none")
        window.reopen_action.trigger()
        self.assertIn("not a search file", self.messages[-1][1])
        disk = parse(stick())[0]
        record = diskrecover.new_search(disk)
        path = diskrecover.save_search(os.path.join(self.dir, "s.kssearch"),
                                       record, [Entry("a/b.txt", size=3)])
        window.pick_search = lambda start: path
        window.reopen_action.trigger()
        self.assertEqual(window._results.path, path)
        self.assertEqual([e.path for e in window._results.entries],
                         ["a/b.txt"])

    def test_a_search_can_be_saved_anywhere(self):
        window = self.window()
        results = window.show_search({"what": "x", "created": 1700000000,
                                      "format": diskrecover.SEARCH_FORMAT,
                                      "version": 2},
                                     [Entry("a/b.txt", size=3)])
        target = os.path.join(self.dir, "mine")
        window.pick_save = lambda parent, start: target
        results.save()
        self.assertEqual(results.path, target + ".kssearch")
        self.assertTrue(os.path.exists(target + ".kssearch"))


if __name__ == "__main__":
    unittest.main()
