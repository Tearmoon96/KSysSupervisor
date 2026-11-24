"""The partition editor: new table, create, delete, resize, move.

The privileged half works here on a plain file standing in for a disk, with
a stand-in sfdisk that keeps a table, so the moving of blocks is the real
code moving real bytes. What the real tools do to real filesystems is pinned
down by tests/verify-disk-tools.py on loop devices.
"""

import errno
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, MIB                              # noqa: E402
from test_diskhelper import HelperCase                     # noqa: E402
from test_disks import desktop, parse, stick               # noqa: E402
from test_diskwindow import DiskWindowCase                 # noqa: E402

from ksyssupervisor import diskfs, diskops, diskparts, disks  # noqa: E402
from ksyssupervisor.diskdialogs import ConfirmDialog       # noqa: E402
from ksyssupervisor.diskpartsui import (MoveDialog,        # noqa: E402
                                        NewPartitionDialog, NewTableDialog,
                                        ResizeDialog, SizeEdit)


def everything(*names):
    return "/usr/sbin/" + names[0]


def nothing(*names):
    return None


def roomy():
    """A 100 GB internal drive: | data 40 | free 20 | windows 30 | free |"""
    return disks.Disk(
        "sdq", "/dev/sdq", 100 * GIB, table="gpt", model="Roomy",
        volumes=(
            disks.Volume("sdq1", "/dev/sdq1", "sdq", "part", 40 * GIB,
                         start=MIB, number=1, fstype="ext4", label="Data",
                         mountpoints=("/mnt/data",)),
            disks.Volume("sdq2", "/dev/sdq2", "sdq", "part", 30 * GIB,
                         start=MIB + 60 * GIB, number=2, fstype="ntfs",
                         label="Win")),
        free=(disks.FreeSpace("sdq", MIB + 40 * GIB, 20 * GIB),
              disks.FreeSpace("sdq", MIB + 90 * GIB, 9 * GIB)))

KIB = 1024
LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"


class PartCase(HelperCase):
    """A 64 MiB disk: | 1 MiB | sdx1 8 MiB | 11 free | sdx2 8 MiB | free |"""

    SIZE = 64 * MIB

    def setUp(self):
        super().setUp()
        self.module.CHUNK = 256 * KIB
        self.module.SYNC_EVERY = MIB
        self.tree.add_disk("sdx", self.SIZE, diskseq=50)
        # Random data where the two partitions are; the rest of the file
        # stays a hole, so a test costs megabytes and not the whole disk.
        with open(self.path("sdx"), "r+b") as handle:
            for start in (1, 20):
                handle.seek(start * MIB)
                handle.write(os.urandom(8 * MIB))
        self.tools.contents["sdx"] = {"PTTYPE": "gpt"}
        self.tools.tables["sdx"] = {
            "label": "gpt", "sectorsize": 512, "firstlba": 2048,
            "lastlba": self.SIZE // 512 - 34, "partitions": []}
        self.add("sdx", 1, 1, 8, "ext4")
        self.add("sdx", 2, 20, 8, "ext4")

    def path(self, name):
        return os.path.join(self.tree.dev, name)

    def again(self):
        """A fresh disk within one test, the previous one thrown away."""
        self._remove_tree()
        self.setUp()

    def add(self, disk, number, start_mib, size_mib, fstype="",
            kind=LINUX):
        name = "%s%d" % (disk, number)
        self.tree.add_partition(disk, name, number, start_mib * 2048,
                                size_mib * MIB)
        self.tools.tables[disk]["partitions"].append(
            {"node": "/dev/" + name, "start": start_mib * 2048,
             "size": size_mib * 2048, "type": kind})
        if fstype:
            self.tools.contents[name] = {"TYPE": fstype, "UUID": "u-" + name}
        return name

    def ask(self, op, name, **fields):
        request = {"op": op, "id": 3, "device": self.path(name),
                   "identity": self.tree.identity(name)}
        request.update(fields)
        return request

    def do(self, op, name, **fields):
        return self.helper.OPERATIONS[op](self.helper,
                                          self.ask(op, name, **fields))

    def no(self, op, name, **fields):
        return self.refused(self.ask(op, name, **fields))

    def steps(self):
        return [e["key"] for e in self.events() if e.get("event") == "step"]

    def entry(self, name):
        return [e for e in self.tools.tables["sdx"]["partitions"]
                if e["node"] == "/dev/" + name][0]

    def edits(self):
        """sfdisk calls that changed the table."""
        return [args for args in self.tools.of("sfdisk")
                if "--json" not in args and "--dump" not in args]

    def disk_bytes(self, start, length):
        with open(self.path("sdx"), "rb") as handle:
            handle.seek(start)
            return handle.read(length)

    def saved_tables(self):
        folder = os.path.join(self.module.STATE_DIR, "tables")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


class LayoutTest(PartCase):
    def test_unallocated_space_is_found_on_mib_boundaries(self):
        layout = self.helper.layout(self.module.Device("sdx"))
        gaps = self.helper.gaps(layout)
        self.assertEqual([(a // MIB, b // MIB, logical)
                          for a, b, logical in gaps],
                         [(9, 20, False), (28, 63, False)])

    def test_the_room_around_a_partition_stops_at_its_neighbours(self):
        layout = self.helper.layout(self.module.Device("sdx"))
        second = layout["entries"][1]
        low, high = self.helper.room_around(layout, second)
        self.assertEqual((low // MIB, high // MIB), (9, 63))
        first = layout["entries"][0]
        low, high = self.helper.room_around(layout, first)
        self.assertEqual((low // MIB, high // MIB), (1, 20))

    def test_room_inside_an_extended_partition_is_marked_logical(self):
        self.tools.tables["sdx"] = {"label": "dos", "sectorsize": 512,
                                    "partitions": []}
        for name in ("sdx1", "sdx2"):
            self.tree.remove(name)
        self.add("sdx", 1, 1, 8, "ext4", kind="83")
        self.add("sdx", 2, 10, 40, kind="5")
        self.add("sdx", 5, 11, 8, "ext4", kind="83")
        layout = self.helper.layout(self.module.Device("sdx"))
        gaps = [(a // MIB, b // MIB, logical)
                for a, b, logical in self.helper.gaps(layout)]
        self.assertIn((50, 64, False), gaps)
        inside = [g for g in gaps if g[2]]
        self.assertEqual(len(inside), 1)
        # A megabyte on: the logical partition's own record goes in front.
        self.assertEqual(inside[0][:2], (20, 50))


class CreateTest(PartCase):
    def test_a_partition_and_its_filesystem_are_made_in_free_space(self):
        result = self.do("part_create", "sdx", start=9 * MIB, size=10 * MIB,
                         fstype="ext4", label="New")
        self.assertEqual(result["device"], self.path("sdx3"))
        self.assertEqual(self.steps(), ["table", "mkfs", "verify"])
        edit = self.edits()[0]
        self.assertIn("--append", edit)
        self.assertIn("--no-reread", edit)
        script = [stdin for name, args, stdin in self.tools.calls
                  if name == "sfdisk" and "--append" in args][0]
        self.assertEqual(script, "start=%d, size=%d, type=%s\n"
                         % (9 * 2048, 10 * 2048, LINUX))
        self.assertEqual(self.tools.contents["sdx3"]["LABEL"], "New")
        # The others are as they were.
        self.assertEqual(self.entry("sdx1")["start"], 2048)

    def test_the_cluster_size_reaches_mkfs_and_is_checked(self):
        self.do("part_create", "sdx", start=9 * MIB, size=10 * MIB,
                fstype="exfat", label="", cluster=4096)
        argv = self.tools.of("mkfs.exfat")[0]
        self.assertEqual(argv[argv.index("-c") + 1], "4096")

    def test_a_cluster_size_the_partition_cannot_have_changes_nothing(self):
        text = self.no("part_create", "sdx", start=9 * MIB, size=10 * MIB,
                       fstype="exfat", label="", cluster=MIB)
        self.assertIn("cannot have clusters", text)
        self.assertEqual(self.edits(), [])

    def test_the_table_is_saved_before_it_is_changed(self):
        self.do("part_create", "sdx", start=9 * MIB, size=10 * MIB,
                fstype="ext4", label="")
        saved = self.saved_tables()
        self.assertEqual(len(saved), 1)
        with open(os.path.join(self.module.STATE_DIR, "tables",
                               saved[0])) as handle:
            text = handle.read()
        self.assertIn("/dev/sdx2", text)
        self.assertNotIn("/dev/sdx3", text)

    def test_no_saved_table_no_edit(self):
        self.module.STATE_DIR = "/proc/nonexistent"
        text = self.no("part_create", "sdx", start=9 * MIB, size=10 * MIB,
                       fstype="ext4", label="")
        self.assertIn("could not be saved first", text)
        self.assertEqual(self.edits(), [])

    def test_what_is_asked_is_put_on_mib_boundaries(self):
        self.do("part_create", "sdx", start=9 * MIB + 5, size=10 * MIB - 7,
                fstype="", label="")
        entry = self.entry("sdx3")
        self.assertEqual((entry["start"], entry["size"]),
                         (10 * 2048, 9 * 2048))

    def test_an_unformatted_partition_is_only_a_table_entry(self):
        result = self.do("part_create", "sdx", start=28 * MIB,
                         size=30 * MIB, fstype="", label="")
        self.assertEqual(self.steps(), ["table"])
        self.assertEqual(result["fstype"], "")
        self.assertFalse([n for n in self.tools.names()
                          if n.startswith("mkfs")])

    def test_space_that_is_not_free_is_refused_untouched(self):
        for start, size in ((5 * MIB, 4 * MIB), (15 * MIB, 10 * MIB),
                            (28 * MIB, 100 * MIB), (9 * MIB, 0)):
            text = self.no("part_create", "sdx", start=start, size=size,
                           fstype="ext4", label="")
            self.assertIn("not unallocated space", text)
        self.assertEqual(self.edits(), [])

    def test_too_small_for_the_filesystem_is_refused(self):
        text = self.no("part_create", "sdx", start=9 * MIB, size=10 * MIB,
                       fstype="btrfs", label="")
        self.assertIn("too small", text)
        self.assertEqual(self.edits(), [])

    def test_a_drive_without_a_table_says_to_make_one(self):
        self.tools.tables["sdx"]["label"] = ""
        self.assertIn("no partition table",
                      self.no("part_create", "sdx", start=9 * MIB,
                              size=MIB, fstype="", label=""))

    def test_a_fifth_primary_partition_is_refused_with_the_way_out(self):
        self.tools.tables["sdx"] = {"label": "dos", "sectorsize": 512,
                                    "partitions": []}
        for name in ("sdx1", "sdx2"):
            self.tree.remove(name)
        for number in range(1, 5):
            self.add("sdx", number, number * 4, 2, kind="83")
        text = self.no("part_create", "sdx", start=30 * MIB, size=8 * MIB,
                       fstype="ext4", label="")
        self.assertIn("four partitions", text)
        self.assertIn("GPT", text)

    def test_free_space_on_the_system_drive_can_be_used(self):
        """Nothing that is in use is touched by adding a partition."""
        self.tools.tables["nvme0n1"] = {
            "label": "gpt", "sectorsize": 512, "firstlba": 2048,
            "lastlba": 500 * GIB // 512 - 34, "partitions": [
                {"node": "/dev/nvme0n1p1", "start": 2048,
                 "size": 600 * 2048, "type": LINUX},
                {"node": "/dev/nvme0n1p2", "start": 1230848,
                 "size": 400 * GIB // 512, "type": LINUX}]}
        result = self.do("part_create", "nvme0n1", start=420 * GIB,
                         size=10 * GIB, fstype="ext4", label="")
        self.assertEqual(result["device"], self.path("nvme0n1p3"))

    def test_a_failed_edit_says_the_table_was_not_changed(self):
        self.tools.fail["sfdisk"] = "sfdisk: cannot write"
        self.tools.fail.pop("sfdisk")
        real = self.tools.run

        def run(argv, stdin=None, timeout=None):
            if os.path.basename(argv[0]) == "sfdisk" and "--append" in argv:
                return 1, "", "sfdisk: failed to add partition"
            return real(argv, stdin, timeout)
        self.helper.run_tool = run
        text = self.no("part_create", "sdx", start=9 * MIB, size=10 * MIB,
                       fstype="ext4", label="")
        self.assertIn("failed to add partition", text)
        self.assertIn("was not changed", text)


class DeleteAndNewTableTest(PartCase):
    def test_the_entry_goes_and_the_others_stay(self):
        result = self.do("part_delete", "sdx2")
        self.assertEqual(result["disk"], self.path("sdx"))
        self.assertEqual([e["node"] for e
                          in self.tools.tables["sdx"]["partitions"]],
                         ["/dev/sdx1"])
        self.assertEqual(len(self.saved_tables()), 1)
        # The contents are left where they were: only the entry is gone.
        self.assertNotIn("wipefs", self.tools.names())

    def test_a_mounted_partition_is_unmounted_first(self):
        self.tree.mount("sdx2", "/mnt/two")
        self.do("part_delete", "sdx2")
        self.assertEqual(self.steps(), ["unmount", "table"])
        self.assertNotIn("/mnt/two", self.tree.mounted())

    def test_a_system_partition_is_never_deleted(self):
        text = self.no("part_delete", "nvme0n1p2")
        self.assertIn("running system", text)

    def test_a_whole_drive_is_not_a_partition(self):
        self.assertIn("not a partition", self.no("part_delete", "sdx"))

    def test_a_new_table_empties_the_drive_and_keeps_the_old_one_on_file(
            self):
        result = self.do("table_new", "sdx", table="dos")
        self.assertEqual(self.steps(), ["table", "verify"])
        self.assertEqual(self.tools.contents["sdx"], {"PTTYPE": "dos"})
        self.assertTrue(os.path.exists(result["saved"]))
        self.assertEqual(self.tools.names().count("wipefs"), 3)

    def test_a_new_table_is_never_written_on_the_system_drive(self):
        self.assertIn("running system",
                      self.no("table_new", "nvme0n1", table="gpt"))
        self.assertIn("Unknown partition table",
                      self.no("table_new", "sdx", table="apm"))


class ResizeTest(PartCase):
    def setUp(self):
        super().setUp()
        self.tools.stdout["resize2fs"] = (
            "Estimated minimum size of the filesystem: 512\n")
        self.tools.stdout["dumpe2fs"] = "Block size:               4096\n"
        self.module.SHRINK_MARGIN = (MIB, 0.0)

    def tools_run(self):
        return [n for n in self.tools.names()
                if n not in ("blkid", "udevadm", "partprobe", "dumpe2fs")]

    def test_growing_goes_partition_first_then_filesystem(self):
        result = self.do("part_resize", "sdx1", size=16 * MIB)
        self.assertEqual(self.steps(), ["check", "table", "filesystem",
                                        "verify"])
        self.assertEqual(self.entry("sdx1")["size"], 16 * 2048)
        names = self.tools_run()
        self.assertLess(names.index("sfdisk"), names.index("resize2fs"))
        # Checked before, stamped for the resize, checked after.
        self.assertEqual([a[:2] for a in self.tools.of("e2fsck")],
                         [["-f", "-n"], ["-f", "-p"], ["-f", "-n"]])
        self.assertEqual(self.tools.of("resize2fs")[-1], [self.path("sdx1")])
        self.assertEqual(result["size"], 16 * MIB)

    def test_shrinking_goes_filesystem_first_then_partition(self):
        self.do("part_resize", "sdx1", size=4 * MIB)
        self.assertEqual(self.steps(), ["check", "filesystem", "table",
                                        "verify"])
        calls = [(n, a) for n, a, _s in self.tools.calls
                 if n in ("resize2fs", "sfdisk") and "-P" not in a
                 and "--json" not in a and "--dump" not in a]
        self.assertEqual([n for n, _a in calls], ["resize2fs", "sfdisk"])
        self.assertEqual(calls[0][1], [self.path("sdx1"), "4096K"])
        self.assertEqual(self.entry("sdx1")["size"], 4 * 2048)

    def test_nothing_is_shrunk_below_what_its_tool_says_it_needs(self):
        # 512 blocks of 4096 bytes is 2 MiB, plus the margin of one.
        text = self.no("part_resize", "sdx1", size=2 * MIB)
        self.assertIn("cannot be made smaller than", text)
        self.assertEqual(self.edits(), [])
        self.assertNotIn(["%s" % self.path("sdx1"), "2048K"],
                         self.tools.of("resize2fs"))

    def test_a_minimum_that_cannot_be_read_stops_a_shrink(self):
        self.tools.stdout["resize2fs"] = "resize2fs: something else\n"
        text = self.no("part_resize", "sdx1", size=4 * MIB)
        self.assertIn("could not be worked out", text)
        self.assertEqual(self.edits(), [])

    def test_growing_stops_at_the_next_partition(self):
        text = self.no("part_resize", "sdx1", size=30 * MIB)
        self.assertIn("only room to grow", text)
        self.assertEqual(self.edits(), [])
        self.do("part_resize", "sdx1", size=19 * MIB)

    def test_a_damaged_filesystem_is_not_resized(self):
        self.tools.codes["e2fsck"] = 4
        text = self.no("part_resize", "sdx1", size=16 * MIB)
        self.assertIn("Repair it first", text)
        self.assertEqual(self.edits(), [])

    def test_a_mounted_ext4_is_grown_in_place(self):
        self.tree.mount("sdx1", "/mnt/one")
        result = self.do("part_resize", "sdx1", size=16 * MIB)
        self.assertEqual(self.steps(), ["table", "filesystem", "verify"])
        self.assertEqual(self.tools.of("e2fsck"), [])
        self.assertIn("/mnt/one", self.tree.mounted())
        self.assertFalse(result["was_mounted"])

    def test_a_mounted_ext4_is_unmounted_to_shrink(self):
        self.tree.mount("sdx1", "/mnt/one")
        result = self.do("part_resize", "sdx1", size=4 * MIB)
        self.assertEqual(self.steps()[0], "unmount")
        self.assertTrue(result["was_mounted"])

    def test_ntfs_is_tried_dry_first_and_says_windows_will_check_it(self):
        self.tools.contents["sdx1"] = {"TYPE": "ntfs"}
        self.tools.stdout["ntfsresize"] = "You might resize at 2097152 bytes"
        result = self.do("part_resize", "sdx1", size=5 * MIB)
        runs = [a for a in self.tools.of("ntfsresize") if "--info" not in a]
        self.assertIn("--no-action", runs[0])
        self.assertNotIn("--no-action", runs[1])
        self.assertEqual(runs[1][-3:], ["--size", str(5 * MIB),
                                        self.path("sdx1")])
        self.assertIn("Windows will check", result["note"])

    def test_a_hibernated_windows_volume_is_not_resized(self):
        self.tools.contents["sdx1"] = {"TYPE": "ntfs"}
        self.tools.codes["ntfs-3g.probe"] = 14
        text = self.no("part_resize", "sdx1", size=16 * MIB)
        self.assertIn("fast startup", text)
        self.assertEqual(self.edits(), [])

    def test_btrfs_is_resized_through_a_mount_of_its_own(self):
        self.tools.contents["sdx1"] = {"TYPE": "btrfs"}
        self.do("part_resize", "sdx1", size=16 * MIB)
        names = self.tools_run()
        self.assertLess(names.index("mount"), names.index("btrfs"))
        self.assertLess(names.index("btrfs"), names.index("umount"))
        call = self.tools.of("btrfs")[-1]
        self.assertEqual(call[:3], ["filesystem", "resize", "max"])
        self.assertEqual(self.helper.temp_mounts, [])

    def test_what_cannot_be_resized_says_why_and_what_to_do(self):
        for fstype, word in (("vfat", "Copy the files off"),
                             ("exfat", "no tool for it exists"),
                             ("crypto_LUKS", "from inside"),
                             ("BitLocker", "Windows")):
            self.tools.contents["sdx1"] = {"TYPE": fstype}
            self.assertIn(word, self.no("part_resize", "sdx1",
                                        size=16 * MIB), fstype)
        self.tools.contents["sdx1"] = {"TYPE": "xfs"}
        self.assertIn("never smaller",
                      self.no("part_resize", "sdx1", size=4 * MIB))
        self.assertEqual(self.edits(), [])

    def test_an_unformatted_partition_only_changes_in_the_table(self):
        self.tools.contents.pop("sdx1")
        self.do("part_resize", "sdx1", size=4 * MIB)
        self.assertEqual(self.steps(), ["table", "verify"])
        self.assertEqual(self.entry("sdx1")["size"], 4 * 2048)

    def test_the_system_volume_and_the_same_size_are_refused(self):
        self.assertIn("running system",
                      self.no("part_resize", "nvme0n1p2", size=100 * GIB))
        self.assertIn("already has",
                      self.no("part_resize", "sdx1", size=8 * MIB))

    def test_the_limits_are_reported_without_changing_anything(self):
        result = self.do("part_limits", "sdx1")
        self.assertEqual(result["smallest"], 3 * MIB)
        self.assertEqual(result["largest"], 19 * MIB)
        self.assertEqual((result["lowest_start"], result["highest_end"]),
                         (MIB, 20 * MIB))
        self.assertEqual((result["grow"], result["shrink"]), ("", ""))
        self.assertEqual(self.edits(), [])
        self.tools.contents["sdx1"] = {"TYPE": "xfs"}
        result = self.do("part_limits", "sdx1")
        self.assertIn("never smaller", result["shrink"])
        self.assertEqual(result["grow"], "")
        self.tools.contents["sdx1"] = {"TYPE": "exfat"}
        self.assertIn("cannot be resized",
                      self.do("part_limits", "sdx1")["grow"])


class GrowLastTest(PartCase):
    """After a clone onto a larger drive: the last partition takes the free
    space behind it. sdx2 (20-28 MiB) is last, the disk is 64 MiB."""

    def setUp(self):
        super().setUp()
        self.disk = self.module.Device("sdx")
        # The end of the usable area, as GPT leaves it, rounded to a MiB.
        self.new = (self.SIZE - 33 * 512 - 20 * MIB) // MIB * MIB

    def grow(self):
        return self.helper.grow_last(self.disk)

    def refused_with(self, word):
        with self.assertRaises(self.module.Refusal) as caught:
            self.grow()
        self.assertIn(word, str(caught.exception))
        self.assertEqual(self.edits(), [])

    def test_ext4_is_grown_partition_first_and_checked_both_sides(self):
        result = self.grow()
        self.assertEqual(self.entry("sdx2")["size"], self.new // 512)
        self.assertEqual((result["old"], result["new"]), (8 * MIB, self.new))
        names = [n for n in self.tools.names() if n in ("sfdisk", "resize2fs")
                 and n]
        self.assertEqual(names[-1], "resize2fs")
        self.assertEqual([a[:2] for a in self.tools.of("e2fsck")],
                         [["-f", "-n"], ["-f", "-p"], ["-f", "-n"]])
        self.assertTrue(self.saved_tables())

    def test_xfs_is_mounted_without_its_id_check(self):
        self.tools.contents["sdx2"] = {"TYPE": "xfs", "UUID": "u"}
        self.grow()
        mount = self.tools.of("mount")[-1]
        self.assertIn("nouuid", mount[mount.index("-o") + 1])
        self.assertEqual(self.tools.of("xfs_growfs")[-1][0][:len(
            self.module.RUN_DIR)], self.module.RUN_DIR)

    def test_btrfs_with_the_original_mounted_is_left_alone(self):
        self.tools.contents["sdx2"] = {"TYPE": "btrfs", "UUID": "same"}
        os.makedirs(os.path.join(self.module.SYS_BTRFS, "same"))
        self.refused_with("the original - is mounted")
        os.rmdir(os.path.join(self.module.SYS_BTRFS, "same"))
        self.grow()
        self.assertEqual(self.tools.of("btrfs")[-1][:3],
                         ["filesystem", "resize", "max"])

    def test_what_cannot_grow_is_refused_before_the_table_changes(self):
        self.tools.contents["sdx2"] = {"TYPE": "vfat"}
        self.refused_with("FAT cannot")
        self.tools.contents["sdx2"] = {"TYPE": "ext4"}
        self.entry("sdx2")["type"] = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
        self.refused_with("system partition")

    def test_no_room_after_the_last_partition(self):
        self.add("sdx", 3, 30, 33, "ext4")
        self.refused_with("no free space")

    def test_a_backup_table_left_in_the_middle_is_named(self):
        self.tools.tables["sdx"]["lastlba"] = 28 * 2048 - 1
        self.refused_with("second copy")

    def test_a_logical_partition_is_not_grown(self):
        table = self.tools.tables["sdx"]
        table["label"] = "dos"
        self.add("sdx", 3, 30, 20, kind="5")
        self.add("sdx", 5, 31, 8, "ext4", kind="83")
        self.refused_with("logical")


class MbrLimitTest(PartCase):
    SIZE = 3 * 1024 ** 4

    def test_an_mbr_disk_ends_for_partitions_at_two_tebibytes(self):
        self.tools.tables["sdx"]["label"] = "dos"
        layout = self.helper.layout(self.module.Device("sdx"))
        self.assertEqual(layout["end"], 2 ** 32 * 512)
        text = self.no("part_resize", "sdx2", size=2 * 1024 ** 4)
        self.assertIn("only room to grow", text)


class MoveTest(PartCase):
    """The real copying, on a file."""

    def setUp(self):
        super().setUp()
        self.original = self.disk_bytes(20 * MIB, 8 * MIB)
        self.neighbour = self.disk_bytes(1 * MIB, 8 * MIB)

    def journal(self):
        return os.path.join(self.module.STATE_DIR, "moves", "sdx-2.json")

    def moved_right(self, megabyte):
        self.assertEqual(self.disk_bytes(megabyte * MIB, 8 * MIB),
                         self.original)
        self.assertEqual(self.entry("sdx2")["start"], megabyte * 2048)
        self.assertEqual(self.disk_bytes(1 * MIB, 8 * MIB), self.neighbour)

    def test_a_short_move_towards_the_end_overlapping_where_it_was(self):
        result = self.do("part_move", "sdx2", start=21 * MIB)
        self.moved_right(21)
        self.assertEqual(self.steps(), ["check", "move", "table", "verify"])
        self.assertEqual(result["start"], 21 * MIB)
        self.assertFalse(os.path.exists(self.journal()))

    def test_a_short_move_towards_the_start(self):
        self.do("part_move", "sdx2", start=19 * MIB)
        self.moved_right(19)

    def test_moves_of_every_distance_in_both_directions(self):
        place = 20
        for target in (23, 22, 9, 55, 30, 31, 12, 54):
            self.do("part_move", "sdx2", start=target * MIB)
            self.assertEqual(self.disk_bytes(target * MIB, 8 * MIB),
                             self.original, "%d -> %d" % (place, target))
            place = target
        self.assertEqual(self.disk_bytes(1 * MIB, 8 * MIB), self.neighbour)

    def test_where_there_is_no_room_nothing_is_moved(self):
        for target in (5, 8, 56, 60):
            self.assertIn("no room",
                          self.no("part_move", "sdx2", start=target * MIB))
        self.assertEqual(self.disk_bytes(20 * MIB, 8 * MIB), self.original)
        self.assertEqual(self.edits(), [])

    def test_a_damaged_filesystem_is_not_moved(self):
        self.tools.codes["e2fsck"] = 4
        text = self.no("part_move", "sdx2", start=30 * MIB)
        self.assertIn("Repair it first", text)
        self.assertEqual(self.disk_bytes(20 * MIB, 8 * MIB), self.original)

    def test_the_table_is_changed_only_after_the_last_block(self):
        seen = []
        real = self.tools.run

        def run(argv, stdin=None, timeout=None):
            if os.path.basename(argv[0]) == "sfdisk" and "-N" in argv:
                seen.append(self.disk_bytes(30 * MIB, 8 * MIB)
                            == self.original)
            return real(argv, stdin, timeout)
        self.helper.run_tool = run
        self.do("part_move", "sdx2", start=30 * MIB)
        self.assertEqual(seen, [True])

    def failing_writes(self, after):
        """os.pwrite to the disk failing from the `after`-th call on."""
        real = os.pwrite
        disk = os.stat(self.path("sdx")).st_ino
        count = [0]

        def pwrite(fd, data, offset):
            if os.fstat(fd).st_ino == disk:
                count[0] += 1
                if count[0] > after:
                    raise OSError(errno.EIO, "Input/output error")
            return real(fd, data, offset)
        return mock.patch.object(self.module.os, "pwrite", pwrite)

    def interrupted(self, target, after=9):
        with self.failing_writes(after):
            text = self.no("part_move", "sdx2", start=target * MIB)
        self.assertIn("partly moved", text)
        self.assertIn("Finish Interrupted Move", text)
        self.assertTrue(os.path.exists(self.journal()))
        # The table still points at the old place.
        self.assertEqual(self.entry("sdx2")["start"], 20 * 2048)

    def resume(self):
        return self.helper.op_move_resume({"op": "move_resume", "id": 8,
                                           "disk": "sdx", "number": 2})

    def test_a_move_cut_short_is_taken_up_where_it_stopped(self):
        for target in (21, 19, 22, 17, 40):
            self.again()
            self.interrupted(target)
            self.assertNotEqual(self.disk_bytes(target * MIB, 8 * MIB),
                                self.original)
            result = self.resume()
            self.assertTrue(result["resumed"])
            self.assertEqual(self.disk_bytes(target * MIB, 8 * MIB),
                             self.original, "to %d MiB" % target)
            self.assertEqual(self.entry("sdx2")["start"], target * 2048)
            self.assertEqual(self.disk_bytes(1 * MIB, 8 * MIB),
                             self.neighbour)
            self.assertFalse(os.path.exists(self.journal()))

    def test_cut_short_at_any_point_it_still_ends_right(self):
        """Every piece boundary of an overlapping move, in both
        directions: the record never claims more than is on the disk."""
        for target in (21, 19):
            for after in range(0, 34, 3):
                self.again()
                with self.failing_writes(after):
                    try:
                        self.do("part_move", "sdx2", start=target * MIB)
                    except self.module.Refusal:
                        pass
                if os.path.exists(self.journal()):
                    self.resume()
                self.assertEqual(self.disk_bytes(target * MIB, 8 * MIB),
                                 self.original,
                                 "to %d, cut after %d writes"
                                 % (target, after))

    def test_a_crash_between_the_copy_and_the_table_is_finished_too(self):
        real = self.helper.write_table

        def crash(*args, **kwargs):
            raise RuntimeError("the process died here")
        self.helper.write_table = crash
        with self.assertRaises(RuntimeError):
            self.do("part_move", "sdx2", start=30 * MIB)
        self.helper.write_table = real
        with open(self.journal()) as handle:
            self.assertEqual(json.loads(handle.read(4096))["stage"], "table")
        self.resume()
        self.assertEqual(self.entry("sdx2")["start"], 30 * 2048)
        self.assertEqual(self.disk_bytes(30 * MIB, 8 * MIB), self.original)

    def test_a_partly_moved_partition_is_not_moved_or_mounted_meanwhile(
            self):
        self.interrupted(21)
        self.assertIn("interrupted",
                      self.no("part_move", "sdx2", start=30 * MIB))
        self.tree.mount("sdx2", "/mnt/two")
        with self.assertRaises(self.module.Refusal) as caught:
            self.resume()
        self.assertIn("must not be used", str(caught.exception))

    def test_a_record_is_not_applied_to_a_different_drive_or_table(self):
        self.interrupted(21)
        with open(os.path.join(self.tree.block, "sdx", "size"), "w") as f:
            f.write("%d\n" % (32 * MIB // 512))
        with self.assertRaises(self.module.Refusal) as caught:
            self.resume()
        self.assertIn("not the drive", str(caught.exception))
        with open(os.path.join(self.tree.block, "sdx", "size"), "w") as f:
            f.write("%d\n" % (self.SIZE // 512))
        self.entry("sdx2")["size"] = 4 * 2048
        with self.assertRaises(self.module.Refusal) as caught:
            self.resume()
        self.assertIn("has changed", str(caught.exception))

    def test_nothing_on_record_nothing_to_finish(self):
        with self.assertRaises(self.module.Refusal) as caught:
            self.resume()
        self.assertIn("no interrupted move", str(caught.exception))

    def boot_sector(self, start, sectors):
        boot = bytearray(512)
        boot[3:11] = b"NTFS    "
        boot[0x0B:0x0D] = (512).to_bytes(2, "little")
        boot[0x1C:0x20] = (start // 512).to_bytes(4, "little")
        boot[0x28:0x30] = sectors.to_bytes(8, "little")
        boot[510:512] = b"\x55\xaa"
        return bytes(boot)

    def test_a_windows_volume_is_told_where_it_now_starts(self):
        sectors = 8 * 2048 - 1
        boot = self.boot_sector(20 * MIB, sectors)
        with open(self.path("sdx"), "r+b") as handle:
            handle.seek(20 * MIB)
            handle.write(boot)
            handle.seek(20 * MIB + sectors * 512)
            handle.write(boot)               # its spare copy, at the end
        self.tools.contents["sdx2"] = {"TYPE": "ntfs"}
        self.do("part_move", "sdx2", start=30 * MIB)
        for place in (30 * MIB, 30 * MIB + sectors * 512):
            found = self.disk_bytes(place, 512)
            self.assertEqual(int.from_bytes(found[0x1C:0x20], "little"),
                             30 * 2048)
            self.assertEqual(found[:0x1C], boot[:0x1C])
            self.assertEqual(found[0x20:], boot[0x20:])

    def test_a_linux_filesystem_is_moved_without_a_byte_changed(self):
        with open(self.path("sdx"), "r+b") as handle:
            handle.seek(20 * MIB)
            handle.write(self.boot_sector(20 * MIB, 100))
        original = self.disk_bytes(20 * MIB, 8 * MIB)
        self.do("part_move", "sdx2", start=30 * MIB)     # typed as ext4
        self.assertEqual(self.disk_bytes(30 * MIB, 8 * MIB), original)


class WhatCanBeEditedTest(unittest.TestCase):
    def setUp(self):
        self.machine = {d.name: d for d in
                        disks.parse(desktop(), fstab=[]) + parse(stick())}
        self.roomy = roomy()

    def answer(self, action, disk, volume=None, tools=everything):
        return diskops.availability(
            action, disk, disk.volume(volume) if volume else None,
            tools=tools)

    def test_a_system_partition_is_greyed_for_every_edit(self):
        system = self.machine["nvme0n1"]
        for action in (diskops.PART_DELETE, diskops.PART_RESIZE,
                       diskops.PART_MOVE):
            answer = self.answer(action, system, "nvme0n1p3")
            self.assertFalse(answer)
            self.assertIn("running system", answer.reason)
            self.assertIn("live USB", answer.reason)
        self.assertFalse(self.answer(diskops.NEW_TABLE, system))

    def test_a_new_partition_needs_a_table_and_free_space(self):
        self.assertTrue(self.answer(diskops.PART_NEW, self.roomy))
        full = self.machine["sdb"]
        answer = self.answer(diskops.PART_NEW, full)
        self.assertIn("no unallocated space", answer.reason)
        self.assertIn("Shrink or delete", answer.reason)
        bare = disks.Disk("sdz", "/dev/sdz", GIB)
        self.assertIn("no partition table",
                      self.answer(diskops.PART_NEW, bare).reason)

    def test_edits_want_a_partition_selected(self):
        for action in (diskops.PART_DELETE, diskops.PART_RESIZE,
                       diskops.PART_MOVE):
            self.assertIn("Select a partition",
                          self.answer(action, self.roomy).reason)

    def test_what_cannot_be_resized_says_why(self):
        stick_ = self.machine["sde"]
        answer = self.answer(diskops.PART_RESIZE, stick_, "sde1")
        self.assertIn("FAT cannot be resized", answer.reason)
        self.assertIn("Copy the files off", answer.reason)
        self.assertTrue(self.answer(diskops.PART_RESIZE, self.roomy, "sdq1"))
        answer = self.answer(diskops.PART_RESIZE, self.roomy, "sdq2",
                             tools=nothing)
        self.assertIn("ntfsresize", answer.reason)
        self.assertIn("dnf install ntfsprogs", answer.reason)
        locked = disks.Volume("sdq3", "/dev/sdq3", "sdq", "part", GIB,
                              fstype="BitLocker")
        answer = diskops.availability(diskops.PART_RESIZE, self.roomy,
                                      locked, tools=everything)
        self.assertIn("turn encryption off", answer.reason)

    def test_moving_needs_free_space_beside_the_partition(self):
        self.assertTrue(self.answer(diskops.PART_MOVE, self.roomy, "sdq1"))
        self.assertEqual(diskparts.room(self.roomy, self.roomy.volumes[0]),
                         (0, 20 * GIB))
        self.assertEqual(diskparts.room(self.roomy, self.roomy.volumes[1]),
                         (20 * GIB, 9 * GIB))
        answer = self.answer(diskops.PART_MOVE, self.machine["sda"], "sda2")
        self.assertIn("no unallocated space next to", answer.reason)

    def test_what_each_filesystem_allows_in_one_line(self):
        said = lambda fstype: diskparts.capability(  # noqa: E731
            disks.Volume("x", "/dev/x", "d", "part", GIB, fstype=fstype))
        self.assertIn("larger and smaller", said("ext4"))
        self.assertIn("never smaller", said("xfs"))
        self.assertIn("no tool", said("exfat"))
        self.assertIn("only the partition", said(""))

    def test_the_tables_here_are_the_helpers(self):
        from disktree import load_helper
        helper = load_helper()
        self.assertEqual(sorted(helper.RESIZE), sorted(diskparts.RESIZE))
        for fstype, (tool, grow, shrink) in diskparts.RESIZE.items():
            spec = helper.RESIZE[fstype]
            self.assertEqual(spec["tool"], tool)
            self.assertEqual(bool(spec["grow"]), grow)
            self.assertEqual(bool(spec["shrink"]), shrink)
        for fstype, text in diskparts.NO_RESIZE.items():
            self.assertEqual(helper.NO_RESIZE[fstype], text)
        self.assertEqual(diskparts.MOVES_DIR,
                         os.path.join(helper.STATE_DIR, "moves"))


class PartPlanTest(unittest.TestCase):
    def setUp(self):
        self.disk = roomy()
        self.data, self.win = self.disk.volumes

    def test_creating_names_the_place_and_the_filesystem(self):
        plan = diskparts.plan_create(self.disk, MIB + 40 * GIB, 10 * GIB,
                                     "exfat", "media", identity={"i": 1})
        self.assertEqual(plan.request, {
            "op": "part_create", "device": "/dev/sdq",
            "start": MIB + 40 * GIB, "size": 10 * GIB, "fstype": "exfat",
            "label": "media", "owner": False, "cluster": None,
            "identity": {"i": 1}})
        self.assertEqual(plan.step_keys, ["table", "mkfs", "verify"])
        bare = diskparts.plan_create(self.disk, MIB + 40 * GIB, GIB, "",
                                     "ignored", identity={})
        self.assertEqual(bare.step_keys, ["table"])
        self.assertEqual(bare.request["label"], "")
        with self.assertRaises(ValueError):
            diskparts.plan_create(self.disk, 0, GIB, "vfat",
                                  "a name far too long", identity={})

    def test_deleting_lists_what_goes_and_warns_about_boot_partitions(self):
        plan = diskparts.plan_delete(self.disk, self.data, identity={})
        self.assertEqual(plan.step_keys, ["unmount", "table"])
        self.assertIn("Data", plan.loses[0])
        self.assertTrue(plan.internal)
        self.assertIn("The contents are not overwritten",
                      [a.title for a in plan.advice])
        efi = disks.Volume("sdq9", "/dev/sdq9", "sdq", "part", 100 * MIB,
                           fstype="vfat", parttype=disks.GPT_EFI)
        advice = diskparts.delete_advice(self.disk, efi)
        self.assertEqual(advice[0].level, diskfs.WARNING)
        self.assertIn("Windows included", advice[0].text)

    def test_the_resize_steps_are_the_ones_the_helper_announces(self):
        def keys(volume, size):
            return diskparts.plan_resize(self.disk, volume, size,
                                         identity={}).step_keys
        self.assertEqual(keys(self.data, 50 * GIB),
                         ["table", "filesystem", "verify"])     # online
        self.assertEqual(keys(self.data, 30 * GIB),
                         ["unmount", "check", "filesystem", "table",
                          "verify"])
        self.assertEqual(keys(self.win, 35 * GIB),
                         ["check", "table", "filesystem", "verify"])
        plan = diskparts.plan_resize(self.disk, self.win, 20 * GIB + 5,
                                     identity={})
        self.assertEqual(plan.request["size"], 20 * GIB)
        titles = [a.title for a in plan.advice]
        self.assertIn("Have a copy of what matters first", titles)
        self.assertIn("Windows will check this drive when it next starts",
                      titles)

    def test_moving_warns_that_it_cannot_be_interrupted(self):
        plan = diskparts.plan_move(self.disk, self.win, MIB + 45 * GIB,
                                   identity={})
        self.assertEqual(plan.request["start"], MIB + 45 * GIB)
        self.assertEqual(plan.step_keys, ["check", "move", "table",
                                          "verify"])
        self.assertIn("towards the start", plan.target)
        self.assertEqual(plan.advice[0].title,
                         "This must not be interrupted")
        self.assertIn("can be finished afterwards", plan.advice[0].text)

    def test_a_new_table_lists_everything_as_lost(self):
        plan = diskparts.plan_table_new(self.disk, "gpt", identity={})
        self.assertEqual(len(plan.loses), 2)
        self.assertEqual(plan.step_keys, ["unmount", "table", "verify"])
        self.assertEqual(plan.request["table"], "gpt")

    def test_interrupted_moves_are_found_and_described(self):
        import tempfile
        folder = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        record = {"version": 1, "disk": "sdq", "number": 2,
                  "size": 30 * GIB, "stage": "copy"}
        with open(os.path.join(folder, "sdq-2.json"), "w") as handle:
            handle.write(json.dumps(record) + " " * 100)
        with open(os.path.join(folder, "junk.json"), "w") as handle:
            handle.write("not json")
        found = diskparts.pending_moves(folder)
        self.assertEqual(len(found), 1)
        self.assertIn("must not be used", diskparts.describe_move(found[0]))
        plan = diskparts.plan_resume(found[0])
        self.assertEqual(plan.request, {"op": "move_resume", "disk": "sdq",
                                        "number": 2})
        self.assertEqual(diskparts.pending_moves("/nonexistent"), [])

    def test_outcomes_in_one_line(self):
        plan = diskparts.plan_resize(self.disk, self.win, 20 * GIB,
                                     identity={})
        text = diskparts.done_text(plan, {"device": "/dev/sdq2",
                                          "size": 20 * GIB,
                                          "note": "Windows will check."})
        self.assertIn("Resized /dev/sdq2 to 21.5 GB. Windows will check.",
                      text)
        plan = diskparts.plan_create(self.disk, MIB, GIB, "", identity={})
        self.assertIn("not formatted", diskparts.done_text(
            plan, {"device": "/dev/sdq3", "size": GIB, "fstype": ""}))


class SizeEditTest(DiskWindowCase):
    def test_the_ends_are_exact_and_values_sit_on_mib_boundaries(self):
        edit = SizeEdit(3 * GIB + 5, 77 * GIB + 123456, 10 * GIB)
        self.assertEqual(edit.low, 3 * GIB + MIB)
        self.assertEqual(edit.high, 77 * GIB)
        edit.slider.setValue(edit.STEPS)
        self.assertEqual(edit.value(), 77 * GIB)
        edit.slider.setValue(0)
        self.assertEqual(edit.value(), 3 * GIB + MIB)
        edit.slider.setValue(500)
        self.assertEqual(edit.value() % MIB, 0)
        edit.spin.setValue(20.0)             # 20 GB, as drives are sold
        self.assertEqual(edit.value(), (20 * 1000 ** 3 // MIB) * MIB)
        edit.set_value(10 ** 15)
        self.assertEqual(edit.value(), 77 * GIB)

    def test_small_sizes_are_shown_in_megabytes(self):
        edit = SizeEdit(MIB, 500 * MIB, 100 * MIB)
        self.assertEqual(edit.spin.suffix(), " MB")
        big = SizeEdit(MIB, 500 * GIB, GIB)
        self.assertEqual(big.spin.suffix(), " GB")


class PartitionDialogTest(DiskWindowCase):
    def setUp(self):
        super().setUp()
        self.disk = roomy()

    def test_a_new_partition_opens_on_the_largest_free_stretch_in_full(self):
        dialog = NewPartitionDialog(self.disk, identity={}, tools=everything)
        self.assertFalse(dialog.where_holder.isHidden())
        self.assertEqual(dialog.gap().size, 20 * GIB)
        self.assertEqual(dialog.size.value(), 20 * GIB)
        plan = dialog.build_plan()
        self.assertEqual((plan.request["start"], plan.request["size"]),
                         (MIB + 40 * GIB, 20 * GIB))
        dialog.where.setCurrentIndex(1)
        self.assertEqual(dialog.size.value(), 9 * GIB)
        self.assertEqual(dialog.build_plan().request["start"],
                         MIB + 90 * GIB)

    def test_the_filesystems_offered_follow_the_size(self):
        dialog = NewPartitionDialog(self.disk, identity={}, tools=everything)
        row = dialog.filesystem.findData("btrfs")
        self.assertTrue(dialog.filesystem.model().item(row).isEnabled())
        dialog.size.set_value(50 * MIB)
        row = dialog.filesystem.findData("btrfs")
        self.assertFalse(dialog.filesystem.model().item(row).isEnabled())

    def test_advice_added_on_screen_gets_its_room(self):
        dialog = NewPartitionDialog(self.disk, identity={}, tools=everything)
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData(""))
        dialog.show()
        self.addCleanup(dialog.close)
        self.app.processEvents()
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData("vfat"))
        self.assertTrue(dialog.advice_cards)
        self.assert_dialog_fits(dialog)

    def test_the_cluster_size_is_offered_with_the_filesystem(self):
        dialog = NewPartitionDialog(self.disk, identity={}, tools=everything)
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData("exfat"))
        self.assertFalse(dialog.cluster.row.isHidden())
        self.assertEqual(dialog.cluster.value(), 32768)    # 20 GB
        dialog.cluster.combo.setCurrentIndex(
            dialog.cluster.combo.findData(4096))
        self.assertEqual(dialog.build_plan().request["cluster"], 4096)
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData(""))
        self.assertTrue(dialog.cluster.row.isHidden())
        self.assertTrue(dialog.cluster.note.isHidden())
        self.assertIsNone(dialog.build_plan().request["cluster"])

    def test_unformatted_needs_no_name_and_makes_no_filesystem(self):
        dialog = NewPartitionDialog(self.disk, identity={}, tools=everything)
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData(""))
        self.assertFalse(dialog.label.isEnabled())
        self.assertEqual(dialog.build_plan().request["fstype"], "")
        self.assertTrue(dialog.start.isEnabled())

    def test_a_bad_name_blocks_the_button(self):
        dialog = NewPartitionDialog(self.disk, identity={}, tools=everything)
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData("vfat"))
        dialog.label.setText("far too long a name")
        self.assertFalse(dialog.start.isEnabled())
        dialog.label.setText("ok")
        self.assertTrue(dialog.start.isEnabled())

    def limits(self, **fields):
        base = {"size": 30 * GIB, "start": MIB + 60 * GIB,
                "smallest": 12 * GIB, "largest": 39 * GIB,
                "lowest_start": MIB + 40 * GIB,
                "highest_end": 99 * GIB + MIB, "grow": "", "shrink": ""}
        base.update(fields)
        return base

    def test_resizing_runs_between_what_the_helper_reported(self):
        dialog = ResizeDialog(self.disk, self.disk.volumes[1], self.limits(),
                              identity={})
        self.assertEqual((dialog.size.low, dialog.size.high),
                         (12 * GIB, 39 * GIB))
        self.assertEqual(dialog.size.value(), 30 * GIB)
        self.assertFalse(dialog.start.isEnabled())       # no change yet
        dialog.size.set_value(39 * GIB)
        self.assertTrue(dialog.start.isEnabled())
        self.assertIn("Grows by", dialog.outcome.text())
        self.assertEqual(dialog.build_plan().request["size"], 39 * GIB)
        dialog.size.set_value(12 * GIB)
        self.assertIn("Shrinks by", dialog.outcome.text())

    def test_a_filesystem_that_cannot_shrink_only_offers_growing(self):
        dialog = ResizeDialog(
            self.disk, self.disk.volumes[1],
            self.limits(shrink="XFS can be made larger but never smaller.",
                        smallest=None), identity={})
        self.assertEqual(dialog.size.low, 30 * GIB)
        self.assertEqual(dialog.size.high, 39 * GIB)

    def test_moving_runs_between_the_neighbours(self):
        dialog = MoveDialog(self.disk, self.disk.volumes[1], self.limits(),
                            identity={})
        self.assertEqual((dialog.low, dialog.high),
                         (MIB + 40 * GIB, MIB + 69 * GIB))
        self.assertFalse(dialog.start.isEnabled())
        dialog.to_start.click()
        self.assertTrue(dialog.start.isEnabled())
        self.assertIn("towards the start", dialog.outcome.text())
        self.assertEqual(dialog.build_plan().request["start"],
                         MIB + 40 * GIB)
        dialog.to_end.click()
        self.assertEqual(dialog.build_plan().request["start"],
                         MIB + 69 * GIB)

    def test_a_new_table_offers_mbr_only_where_it_can_describe_the_drive(
            self):
        dialog = NewTableDialog(self.disk, identity={})
        self.assertTrue(dialog.gpt.isChecked())
        self.assertTrue(dialog.mbr.isEnabled())
        self.assertEqual(dialog.build_plan().request["table"], "gpt")
        huge = disks.Disk("sdh", "/dev/sdh", 8000 * GIB, table="gpt")
        dialog = NewTableDialog(huge, identity={})
        self.assertFalse(dialog.mbr.isEnabled())


class PartitionMenuTest(DiskWindowCase):
    def setUp(self):
        super().setUp()
        self.machine = self.machine + [roomy()]

    def test_entries_are_greyed_with_the_reason(self):
        window = self.window()
        window.select_device("nvme0n1p3")
        for action in (window.resize_action, window.move_action,
                       window.delete_partition_action):
            self.assertFalse(action.isEnabled())
            self.assertIn("running system", action.toolTip())
        window.select_device("sdb1")
        self.assertFalse(window.new_partition_action.isEnabled())
        self.assertIn("no unallocated space",
                      window.new_partition_action.toolTip())
        self.assertFalse(window.resume_move_action.isVisible())

    def test_a_partition_is_created_from_the_dialog(self):
        window = self.window()
        window.select_device("sdq")
        self.answers = [True]
        original = window.show_dialog

        def drive(dialog):
            self.assertIsInstance(dialog, NewPartitionDialog)
            dialog._accept()
            return original(dialog)
        window.show_dialog = drive
        self.client.result = {"device": "/dev/sdq3", "size": 20 * GIB,
                              "fstype": "ext4"}
        window.new_partition_action.trigger()
        request = self.client.requests[-1]
        self.assertEqual((request["op"], request["size"]),
                         ("part_create", 20 * GIB))
        self.assertIn("Created /dev/sdq3", window.status.text())

    def test_deleting_is_confirmed_first(self):
        window = self.window()
        window.select_device("sdq2")
        window.delete_partition_action.trigger()
        self.assertIsInstance(self.dialogs[-1], ConfirmDialog)
        self.assertEqual(self.dialogs[-1].go.text(), "Delete the Partition")
        self.assertEqual(self.client.requests, [])
        self.answers = [True]
        self.client.result = {"device": "/dev/sdq2"}
        window.delete_partition_action.trigger()
        self.assertEqual(self.client.requests[-1]["op"], "part_delete")
        self.assertEqual(self.mounted, [])           # nothing to mount again

    def test_resizing_asks_the_helper_for_the_limits_first(self):
        window = self.window()
        window.select_device("sdq2")
        results = [{"size": 30 * GIB, "start": MIB + 60 * GIB,
                    "smallest": 12 * GIB, "largest": 39 * GIB, "grow": "",
                    "shrink": ""},
                   {"device": "/dev/sdq2", "size": 39 * GIB}]
        run = self.client.run

        def answer(request, on_event=None):
            self.client.result = results.pop(0)
            return run(request, on_event)
        self.client.run = answer
        original = window.show_dialog

        def drive(dialog):
            self.assertIsInstance(dialog, ResizeDialog)
            dialog.size.set_value(39 * GIB)
            dialog.confirm = lambda plan: True
            dialog._accept()
            return original(dialog)
        window.show_dialog = drive
        self.answers = [True]
        window.resize_action.trigger()
        self.assertEqual([r["op"] for r in self.client.requests],
                         ["part_limits", "part_resize"])
        self.assertEqual(self.client.requests[-1]["size"], 39 * GIB)
        self.assertIn("Resized /dev/sdq2", window.status.text())

    def test_a_volume_that_cannot_change_says_so_instead_of_a_dialog(self):
        window = self.window()
        window.select_device("sdq2")
        self.client.result = {"size": 30 * GIB, "smallest": 30 * GIB,
                              "largest": 30 * GIB, "grow": "", "shrink": ""}
        window.resize_action.trigger()
        self.assertEqual(self.dialogs, [])
        self.assertIn("nothing to change", self.messages[-1][1])
        self.client.result = {"grow": "Windows did not shut down",
                              "shrink": "Windows did not shut down"}
        window.resize_action.trigger()
        self.assertIn("Windows did not shut down", self.messages[-1][1])

    def test_lost_partitions_are_searched_for_listed_and_put_back(self):
        from ksyssupervisor import diskpartsui
        patch = mock.patch.object(
            diskpartsui.diskops, "availability",
            lambda action, disk, volume=None, tools=None: diskops.YES)
        patch.start()
        self.addCleanup(patch.stop)
        window = self.window()
        window.select_device("sdq")
        found = [{"start": MIB, "size": 10 * GIB, "fstype": "ext4",
                  "label": "Old"},
                 {"start": 20 * GIB, "size": 5 * GIB, "fstype": "ntfs",
                  "label": ""}]
        results = [{"found": found}, {"device": "/dev/sdq3"},
                   {"device": "/dev/sdq4"}]
        run = self.client.run

        def answer(request, on_event=None):
            self.client.result = results.pop(0)
            return run(request, on_event)
        self.client.run = answer
        self.answers = [True]
        window.find_lost_action.trigger()
        dialog = self.dialogs[-1]
        self.assertIsInstance(dialog, diskpartsui.LostPartitionsDialog)
        self.assertIn("ext4 'Old', 10.7 GB", dialog.boxes[0].text())
        self.assertEqual([r["op"] for r in self.client.requests],
                         ["partition_search", "partition_restore",
                          "partition_restore"])
        self.assertEqual(self.client.requests[1]["start"], MIB)
        self.assertEqual(self.client.requests[2]["size"], 5 * GIB)
        self.assertIn("Put back 2 partitions: /dev/sdq3, /dev/sdq4",
                      window.status.text())

    def test_nothing_lost_points_at_searching_for_files_instead(self):
        from ksyssupervisor import diskpartsui
        patch = mock.patch.object(
            diskpartsui.diskops, "availability",
            lambda action, disk, volume=None, tools=None: diskops.YES)
        patch.start()
        self.addCleanup(patch.stop)
        window = self.window()
        window.select_device("sdq")
        self.client.result = {"found": []}
        window.find_lost_action.trigger()
        self.assertIn("Recover Files", self.messages[-1][1])
        self.assertEqual(self.dialogs, [])

    def test_an_interrupted_move_is_announced_and_can_be_finished(self):
        record = {"version": 1, "disk": "sdq", "number": 2,
                  "size": 30 * GIB, "stage": "copy"}
        from ksyssupervisor import diskpartsui
        with mock.patch.object(diskpartsui.diskparts, "pending_moves",
                               lambda folder=None: [record]):
            window = self.window()
            self.assertIn("Finish Interrupted Move", window.status.text())
            window.select_device("sdq2")
            self.assertTrue(window.resume_move_action.isVisible())
            self.answers = [True]
            self.client.result = {"device": "/dev/sdq2",
                                  "start": 50 * GIB, "resumed": True}
            window.resume_move_action.trigger()
        self.assertEqual(self.client.requests[-1],
                         {"op": "move_resume", "disk": "sdq", "number": 2})
        self.assertIn("Moved /dev/sdq2", window.status.text())


if __name__ == "__main__":
    unittest.main()
