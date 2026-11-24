"""Copying: a drive to a drive, a partition to a partition, to and from an
image file.

The privileged half is driven against plain files standing in for devices,
so what is asserted is the real copying, comparing and refusing; what the
filesystem tools do with real partitions is pinned down by
tests/verify-disk-tools.py on loop devices.
"""

import contextlib
import dataclasses
import errno
import gc
import gzip
import hashlib
import os
import sys
import unittest
import warnings
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, MIB                              # noqa: E402
from test_diskhelper import HelperCase                     # noqa: E402
from test_disks import desktop, parse, stick               # noqa: E402
from test_diskwindow import DiskWindowCase                 # noqa: E402

from ksyssupervisor import diskcopy, diskfs, diskops, disks  # noqa: E402
from ksyssupervisor.diskcopyui import (CloneDialog,        # noqa: E402
                                       SaveImageDialog, WriteImageDialog)


def everything(*names):
    return "/usr/sbin/" + names[0]


def nothing(*names):
    return None


def machine():
    found = disks.parse(desktop(), fstab=[]) + parse(stick())
    return {d.name: d for d in found}

KIB = 1024


class CopyCase(HelperCase):
    """Two small drives with something on them, and a larger blank one."""

    def setUp(self):
        super().setUp()
        self.module.CHUNK = 256 * KIB
        self.module.SYNC_EVERY = MIB
        self.tree.add_disk("sdx", 4 * MIB, diskseq=20)
        self.tree.add_disk("sdy", 4 * MIB, diskseq=21)
        self.tree.add_disk("sdz", 6 * MIB, diskseq=22)
        self.fill("sdx", os.urandom(4 * MIB))
        self.fill("sdy", b"\xaa" * (4 * MIB))
        self.fill("sdz", b"\xaa" * (6 * MIB))

    def path(self, name):
        return os.path.join(self.tree.dev, name)

    def fill(self, name, data):
        with open(self.path(name), "wb") as handle:
            handle.write(data)

    def data(self, name):
        with open(self.path(name), "rb") as handle:
            return handle.read()

    def clone(self, source, target, mode="exact", **fields):
        request = {"op": "clone", "id": 9, "mode": mode,
                   "source": self.path(source), "target": self.path(target),
                   "source_identity": self.tree.identity(source),
                   "target_identity": self.tree.identity(target)}
        request.update(fields)
        return request

    def run_clone(self, *args, **fields):
        return self.helper.op_clone(self.clone(*args, **fields))

    def steps(self):
        return [e["key"] for e in self.events() if e.get("event") == "step"]

    def bad_reads(self, name, start, length):
        """os.pread failing for part of one device, as a bad sector does."""
        real, real_vector = os.pread, os.preadv
        wanted = os.stat(self.path(name)).st_ino

        def bad(fd, count, offset):
            return (os.fstat(fd).st_ino == wanted
                    and offset < start + length and start < offset + count)

        def pread(fd, count, offset):
            if bad(fd, count, offset):
                raise OSError(errno.EIO, "Input/output error")
            return real(fd, count, offset)

        def preadv(fd, buffers, offset):
            # The reads that bypass the cache, where the filesystem the
            # test runs on allows those.
            if bad(fd, sum(len(b) for b in buffers), offset):
                raise OSError(errno.EIO, "Input/output error")
            return real_vector(fd, buffers, offset)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.module.os, "pread", pread))
        stack.enter_context(mock.patch.object(self.module.os, "preadv",
                                              preadv))
        return stack


class ExactCloneTest(CopyCase):
    def test_every_byte_is_copied_and_read_back(self):
        result = self.run_clone("sdx", "sdy")
        self.assertEqual(self.data("sdy"), self.data("sdx"))
        self.assertEqual(self.steps(), ["copy", "verify"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["methods"][0]["method"], "exact")
        self.assertFalse(result["larger"])
        # The old signatures go before the first byte is written.
        self.assertEqual(self.tools.names()[0], "wipefs")

    def test_a_larger_target_keeps_the_copy_at_its_start(self):
        result = self.run_clone("sdx", "sdz")
        self.assertEqual(self.data("sdz")[:4 * MIB], self.data("sdx"))
        self.assertEqual(self.data("sdz")[4 * MIB:], b"\xaa" * (2 * MIB))
        self.assertTrue(result["larger"])

    def test_a_gpt_copied_onto_a_larger_drive_gets_its_second_copy_moved(self):
        self.tools.lie["sdz"] = {"PTTYPE": "gpt"}
        self.run_clone("sdx", "sdz")
        self.assertIn(["--relocate", "gpt-bak-std", self.path("sdz")],
                      self.tools.of("sfdisk"))

    def test_an_mbr_drive_has_nothing_to_move(self):
        self.tools.lie["sdz"] = {"PTTYPE": "dos"}
        self.run_clone("sdx", "sdz")
        self.assertEqual(self.tools.of("sfdisk"), [])

    def test_a_smaller_target_is_refused_before_anything_is_written(self):
        text = self.refused(self.clone("sdz", "sdy"))
        self.assertIn("smaller", text)
        self.assertEqual(self.data("sdy"), b"\xaa" * (4 * MIB))
        self.assertEqual(self.tools.names(), [])

    def test_a_drive_is_not_copied_onto_itself_or_its_own_partition(self):
        self.tree.add_partition("sdx", "sdx1", 1, 2048, 2 * MIB)
        for target in ("sdx", "sdx1"):
            self.assertIn("cannot go onto",
                          self.refused(self.clone("sdx", target)))

    def test_a_drive_goes_to_a_drive_and_a_partition_to_a_partition(self):
        self.tree.add_partition("sdy", "sdy1", 1, 2048, 2 * MIB)
        self.assertIn("whole drive", self.refused(self.clone("sdx", "sdy1")))
        self.assertIn("whole drive", self.refused(self.clone("sdy1", "sdx")))

    def test_the_running_systems_drive_is_never_the_source(self):
        """It cannot be copied consistently while it is in use."""
        self.tree.add_disk("sdw", 600 * 1024 * MIB, diskseq=30)
        text = self.refused(self.clone("nvme0n1", "sdw"))
        self.assertIn("running system", text)
        self.assertEqual(self.tools.names(), [])

    def test_nor_the_target(self):
        text = self.refused(self.clone("sdx", "nvme0n1"))
        self.assertIn("running system", text)

    def test_a_swapped_drive_is_noticed_on_either_side(self):
        for side in ("source_identity", "target_identity"):
            request = self.clone("sdx", "sdy")
            request[side] = dict(request[side], diskseq=99)
            self.assertIn("unplugged", self.refused(request))
        self.assertEqual(self.data("sdy"), b"\xaa" * (4 * MIB))

    def test_what_is_mounted_is_unmounted_first(self):
        self.tree.add_partition("sdx", "sdx1", 1, 2048, 2 * MIB)
        self.tree.mount("sdx1", "/mnt/source")
        self.run_clone("sdx", "sdy")
        self.assertEqual(self.steps(), ["unmount", "copy", "verify"])
        self.assertNotIn("/mnt/source", self.tree.mounted())
        self.assertLess(self.tools.names().index("umount"),
                        self.tools.names().index("wipefs"))

    def test_verification_can_be_declined(self):
        result = self.run_clone("sdx", "sdy", verify=False)
        self.assertEqual(self.steps(), ["copy"])
        self.assertFalse(result["verified"])

    def test_a_copy_that_reads_back_differently_is_reported(self):
        """The write succeeding is not the proof."""
        real = self.helper.raw_copy

        def copy_then_rot(*args, **kwargs):
            real(*args, **kwargs)
            with open(self.path("sdy"), "r+b") as handle:
                handle.seek(3 * MIB + 17)
                handle.write(b"\x00\x01")
        self.helper.raw_copy = copy_then_rot
        text = self.refused(self.clone("sdx", "sdy"))
        self.assertIn("does not match the original", text)
        self.assertIn("Do not rely on this copy", text)

    def test_stopping_says_the_target_is_incomplete(self):
        real = self.helper.progress

        def progress(*args):
            real(*args)
            self.helper.cancel.set()
        self.helper.progress = progress
        with self.assertRaises(self.module.Cancelled) as caught:
            self.run_clone("sdx", "sdy")
        self.assertIn("incomplete copy", str(caught.exception))
        self.assertIn("source was not changed", str(caught.exception))
        self.assertNotEqual(self.data("sdy"), self.data("sdx"))

    def test_an_unknown_kind_of_copy_is_refused(self):
        self.assertIn("Unknown kind",
                      self.refused(self.clone("sdx", "sdy", mode="clever")))


class CloneThenGrowTest(CopyCase):
    """The grow itself is GrowLastTest's (test_diskparts); here, only how
    the clone hands over to it and what comes back."""

    def test_a_larger_target_grows_after_the_copy_is_checked(self):
        with mock.patch.object(self.helper, "grow_last",
                               return_value={"device": "/dev/sdz2"}) as grow:
            result = self.run_clone("sdx", "sdz", grow=True)
        self.assertEqual(self.steps(), ["copy", "verify", "grow"])
        self.assertEqual(grow.call_args[0][0].name, "sdz")
        self.assertEqual(result["grown"], {"device": "/dev/sdz2"})

    def test_a_refused_grow_leaves_the_copy_and_says_why(self):
        with mock.patch.object(self.helper, "grow_last",
                               side_effect=self.module.Refusal("no room")):
            result = self.run_clone("sdx", "sdz", grow=True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["grow_skipped"], "no room")
        self.assertNotIn("grown", result)
        self.assertIn("left unused: no room",
                      diskcopy.clone_done_text(result))

    def test_a_same_size_target_or_no_request_has_no_grow_step(self):
        with mock.patch.object(self.helper, "grow_last") as grow:
            self.run_clone("sdx", "sdy", grow=True)
            self.run_clone("sdx", "sdz")
        grow.assert_not_called()
        self.assertNotIn("grow", self.steps())


class UnreadableSourceTest(CopyCase):
    BAD = (MIB + 8 * KIB, 4 * KIB)

    def test_an_unreadable_block_stops_the_copy_and_says_how_to_go_on(self):
        with self.bad_reads("sdx", *self.BAD):
            text = self.refused(self.clone("sdx", "sdy"))
        self.assertIn("unreadable areas", text)
        self.assertIn("Skip unreadable areas", text)

    def test_skipping_copies_everything_else_and_reports_the_range(self):
        with self.bad_reads("sdx", *self.BAD):
            result = self.run_clone("sdx", "sdy", skip_errors=True)
        start, length = self.BAD
        self.assertEqual(result["skipped"], [(start, length)])
        self.assertEqual(result["skipped_bytes"], length)
        source, copy = self.data("sdx"), self.data("sdy")
        self.assertEqual(copy[:start], source[:start])
        self.assertEqual(copy[start:start + length], b"\0" * length)
        self.assertEqual(copy[start + length:], source[start + length:])
        self.assertTrue(result["verified"])

    def test_neighbouring_bad_blocks_are_one_range(self):
        start = 2 * MIB
        with self.bad_reads("sdx", start, 12 * KIB):
            result = self.run_clone("sdx", "sdy", skip_errors=True)
        self.assertEqual(result["skipped"], [(start, 12 * KIB)])

    def test_a_difference_outside_the_skipped_blocks_still_fails(self):
        real = self.helper.raw_copy

        def copy_then_rot(*args, **kwargs):
            real(*args, **kwargs)
            with open(self.path("sdy"), "r+b") as handle:
                handle.seek(3 * MIB)
                handle.write(b"\xff\xfe")
        self.helper.raw_copy = copy_then_rot
        with self.bad_reads("sdx", *self.BAD):
            # The source reads fine again for the comparison: only the
            # zero-filled block may differ, nothing else.
            text = self.refused(self.clone("sdx", "sdy", skip_errors=True))
        self.assertIn("does not match", text)

    def test_a_drive_that_went_away_is_not_a_bad_block(self):
        real = os.pread

        def gone(fd, count, offset):
            if offset >= MIB:
                raise OSError(errno.ENODEV, "No such device")
            return real(fd, count, offset)
        with mock.patch.object(self.module.os, "pread", gone):
            text = self.refused(self.clone("sdx", "sdy", skip_errors=True))
        self.assertIn("No such device", text)


class SmartCloneTest(CopyCase):
    def setUp(self):
        super().setUp()
        self.tree.add_partition("sdx", "sdx1", 1, 2048, 2 * MIB)
        self.tree.add_partition("sdz", "sdz1", 1, 2048, 3 * MIB)
        self.fill("sdx1", os.urandom(2 * MIB))
        self.tools.contents["sdx1"] = {"TYPE": "ext4", "UUID": "u-1",
                                       "LABEL": "Data"}

    def test_a_filesystem_is_checked_copied_by_its_tool_and_checked_again(
            self):
        result = self.run_clone("sdx1", "sdz1", mode="smart")
        self.assertEqual(self.steps(), ["check", "copy", "verify"])
        self.assertEqual(result["methods"],
                         [{"device": self.path("sdx1"), "fstype": "ext4",
                           "method": "e2image"}])
        self.assertEqual(self.tools.of("e2image"),
                         [["-ra", "-p", self.path("sdx1"),
                           self.path("sdz1")]])
        checks = self.tools.of("e2fsck")
        self.assertEqual([c[-1] for c in checks],
                         [self.path("sdx1"), self.path("sdz1")])
        self.assertTrue(all(c[:2] == ["-f", "-n"] for c in checks))

    def test_each_tool_gets_source_and_target_the_right_way_round(self):
        for fstype, tool, argv in (
                ("ntfs", "ntfsclone", ["--overwrite", self.path("sdz1"),
                                       self.path("sdx1")]),
                ("xfs", "xfs_copy", ["-d", self.path("sdx1"),
                                     self.path("sdz1")])):
            self.tools.calls.clear()
            self.tools.contents["sdx1"] = {"TYPE": fstype, "UUID": "u"}
            self.run_clone("sdx1", "sdz1", mode="smart")
            self.assertEqual(self.tools.of(tool), [argv])

    def test_a_filesystem_with_no_tool_is_copied_block_for_block(self):
        self.tools.contents["sdx1"] = {"TYPE": "vfat", "UUID": "AB-CD"}
        result = self.run_clone("sdx1", "sdz1", mode="smart")
        self.assertEqual(result["methods"][0]["method"], "exact")
        self.assertEqual(self.data("sdz1")[:2 * MIB], self.data("sdx1"))
        self.assertEqual(self.steps(), ["copy", "verify"])

    def test_a_missing_tool_falls_back_to_block_for_block(self):
        self.tools.missing.add("e2image")
        result = self.run_clone("sdx1", "sdz1", mode="smart")
        self.assertEqual(result["methods"][0]["method"], "exact")
        self.assertEqual(self.data("sdz1")[:2 * MIB], self.data("sdx1"))

    def test_a_damaged_filesystem_is_not_copied_by_its_structure(self):
        self.tools.codes["e2fsck"] = 4
        self.tools.lines["e2fsck"] = ["/dev/sdx1: UNEXPECTED INCONSISTENCY"]
        before = self.data("sdz1")
        text = self.refused(self.clone("sdx1", "sdz1", mode="smart"))
        self.assertIn("has errors", text)
        self.assertIn("Repair it first", text)
        self.assertIn("exact copy", text)
        self.assertEqual(self.data("sdz1"), before)
        self.assertNotIn("wipefs", self.tools.names())

    def test_a_hibernated_windows_volume_says_what_to_do_in_windows(self):
        self.tools.contents["sdx1"] = {"TYPE": "ntfs", "UUID": "u"}
        self.tools.codes["ntfs-3g.probe"] = 14
        text = self.refused(self.clone("sdx1", "sdz1", mode="smart"))
        self.assertIn("fast startup", text)
        self.assertNotIn("wipefs", self.tools.names())
        # Block for block it may still be copied: that reads, never mounts.
        self.tools.calls.clear()
        self.run_clone("sdx1", "sdz1")
        self.assertEqual(self.data("sdz1")[:2 * MIB], self.data("sdx1"))

    def test_a_copy_with_a_different_identity_fails_the_check(self):
        real = self.tools.stream

        def stream(argv, on_line, should_stop, timeout=None, **options):
            result = real(argv, on_line, should_stop, timeout, **options)
            if os.path.basename(argv[0]) == "e2image":
                self.tools.contents["sdz1"]["UUID"] = "something-else"
            return result
        self.helper.stream_tool = stream
        text = self.refused(self.clone("sdx1", "sdz1", mode="smart"))
        self.assertIn("different uuid", text)

    def test_progress_follows_the_percentages_the_tool_prints(self):
        self.tools.lines["e2image"] = ["Copying 12.50 % complete",
                                       "Copying 50 % complete"]
        self.run_clone("sdx1", "sdz1", mode="smart", verify=False)
        size = 2 * MIB
        found = [(e["done"], e["total"]) for e in self.events()
                 if e.get("event") == "progress"]
        self.assertEqual(found[:2], [(size // 8, size), (size // 2, size)])
        self.assertEqual(found[-1], (size, size))


class SmartDiskCloneTest(CopyCase):
    def setUp(self):
        super().setUp()
        self.tree.add_partition("sdx", "sdx1", 1, 2048, MIB)
        self.tree.add_partition("sdx", "sdx2", 2, 4096, MIB)
        self.fill("sdx1", os.urandom(MIB))
        self.fill("sdx2", os.urandom(MIB))
        self.tools.contents["sdx"] = {"PTTYPE": "gpt"}
        self.tools.contents["sdx1"] = {"TYPE": "ext4", "UUID": "one"}
        self.tools.contents["sdx2"] = {"TYPE": "vfat", "UUID": "TW-O0"}
        self.tools.tables["sdx"] = {
            "label": "gpt", "sectorsize": 512, "partitions": [
                {"node": "/dev/sdx1", "start": 2048, "size": 2048,
                 "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4"},
                {"node": "/dev/sdx2", "start": 4096, "size": 2048,
                 "type": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"}]}

    def test_the_table_is_carried_over_and_each_partition_by_its_method(self):
        result = self.run_clone("sdx", "sdz", mode="smart")
        self.assertEqual([m["method"] for m in result["methods"]],
                         ["e2image", "exact"])
        # What comes before the first partition and is not the table:
        # the boot code, and what a boot loader keeps after the table.
        self.assertEqual(self.data("sdz")[:440], self.data("sdx")[:440])
        self.assertEqual(self.data("sdz")[34 * 512:MIB],
                         self.data("sdx")[34 * 512:MIB])
        self.assertEqual(self.data("sdz")[440:34 * 512],
                         b"\xaa" * (34 * 512 - 440))
        self.assertEqual(self.tools.of("e2image")[0][-2:],
                         [self.path("sdx1"), self.path("sdz1")])
        self.assertEqual(self.data("sdz2"), self.data("sdx2"))
        self.assertEqual(self.steps(), ["check", "copy", "verify"])

    def test_the_table_is_written_for_the_targets_own_size(self):
        """A dump names the source's last sector; on a smaller or larger
        drive that line has to go."""
        self.run_clone("sdx", "sdz", mode="smart")
        script = self.tools.written_tables["sdz"]
        self.assertNotIn("last-lba", script)
        self.assertIn("label: gpt", script)
        self.assertIn("start=2048", script)

    def test_a_target_too_small_for_the_partitions_is_refused(self):
        self.tree.add_disk("sdv", 2 * MIB, diskseq=40)
        text = self.refused(self.clone("sdx", "sdv", mode="smart"))
        self.assertIn("too small", text)
        self.assertNotIn("wipefs", self.tools.names())

    def test_a_smaller_target_that_holds_every_partition_is_accepted(self):
        self.tree.add_disk("sdv", 3 * MIB + 512 * KIB, diskseq=40)
        self.fill("sdv", b"\xaa" * (3 * MIB + 512 * KIB))
        self.run_clone("sdx", "sdv", mode="smart")
        self.assertEqual(self.data("sdv2"), self.data("sdx2"))

    def test_different_sector_sizes_are_not_papered_over(self):
        self.tree.add_disk("sdv", 8 * MIB, diskseq=40, logical=4096)
        text = self.refused(self.clone("sdx", "sdv", mode="smart"))
        self.assertIn("different sector sizes", text)

    def test_a_drive_without_partitions_points_at_the_exact_copy(self):
        self.tools.tables["sdx"]["partitions"] = []
        self.assertIn("exact copy",
                      self.refused(self.clone("sdx", "sdz", mode="smart")))


class ImageCase(CopyCase):
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.root, "images")
        os.mkdir(self.folder)
        self.image = os.path.join(self.folder, "drive.img")

    def ask(self, op, name, path=None, **fields):
        request = {"op": op, "id": 4, "device": self.path(name),
                   "identity": self.tree.identity(name),
                   "path": path or self.image}
        request.update(fields)
        return request

    def create(self, name="sdx", **fields):
        return self.helper.op_image_create(self.ask("image_create", name,
                                                    **fields))

    def restore(self, name="sdy", **fields):
        return self.helper.op_image_restore(self.ask("image_restore", name,
                                                     **fields))


class ImageCreateTest(ImageCase):
    def test_the_image_is_the_drive_and_is_read_back(self):
        result = self.create()
        with open(self.image, "rb") as handle:
            self.assertEqual(handle.read(), self.data("sdx"))
        self.assertEqual(result["sha256"],
                         hashlib.sha256(self.data("sdx")).hexdigest())
        self.assertEqual((result["size"], result["stored"]),
                         (4 * MIB, 4 * MIB))
        self.assertEqual(self.steps(), ["copy", "verify"])

    def test_a_compressed_image_unpacks_to_the_drive(self):
        self.fill("sdx", b"\0" * (3 * MIB) + os.urandom(MIB))
        result = self.create(path=self.image + ".gz", compress=True)
        with gzip.open(self.image + ".gz", "rb") as handle:
            self.assertEqual(handle.read(), self.data("sdx"))
        self.assertLess(result["stored"], 2 * MIB)
        self.assertTrue(result["verified"])

    def test_a_compressed_image_is_closed_before_it_is_flushed(self):
        # Left to the garbage collector, the last compressed bytes would
        # reach the file after its fsync, and a write error there was lost.
        self.fill("sdx", os.urandom(4 * MIB))
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            self.create(path=self.image + ".gz", compress=True)
            gc.collect()
        self.assertEqual([w for w in seen
                          if issubclass(w.category, ResourceWarning)], [])

    def test_an_existing_file_is_kept_unless_replacing_was_asked_for(self):
        with open(self.image, "w") as handle:
            handle.write("mine")
        self.assertIn("already exists",
                      self.refused(self.ask("image_create", "sdx")))
        with open(self.image) as handle:
            self.assertEqual(handle.read(), "mine")
        self.create(overwrite=True)
        self.assertEqual(os.path.getsize(self.image), 4 * MIB)

    def test_a_path_that_is_not_a_plain_absolute_file_is_refused(self):
        for path in ("relative.img", "", None, "/dev/\0x"):
            self.assertIn("not a file path",
                          self.refused(self.ask("image_create", "sdx",
                                                path=path or "x")
                                       if path else
                                       dict(self.ask("image_create", "sdx"),
                                            path=path)))
        link = os.path.join(self.folder, "link.img")
        os.symlink(self.path("sdy"), link)
        text = self.refused(self.ask("image_create", "sdx", path=link,
                                     overwrite=True))
        self.assertIn("Cannot create", text)
        self.assertEqual(self.data("sdy"), b"\xaa" * (4 * MIB))

    def test_no_room_is_found_out_before_writing(self):
        class Room:
            f_bavail, f_frsize = 10, 4096
        with mock.patch.object(self.module.os, "statvfs",
                               lambda path: Room):
            text = self.refused(self.ask("image_create", "sdx"))
        self.assertIn("not enough room", text)
        self.assertFalse(os.path.exists(self.image))

    def test_an_image_is_not_saved_on_the_drive_it_is_of(self):
        self.tree.add_partition("sdx", "sdx1", 1, 2048, 2 * MIB)
        self.tree.mount("sdx1", self.folder)
        text = self.refused(self.ask("image_create", "sdx"))
        self.assertIn("itself", text)
        self.assertFalse(os.path.exists(self.image))

    def test_a_failure_leaves_no_half_written_file(self):
        with self.bad_reads("sdx", 2 * MIB, 4 * KIB):
            self.refused(self.ask("image_create", "sdx"))
        self.assertFalse(os.path.exists(self.image))

    def test_stopping_leaves_no_file_and_says_so(self):
        real = self.helper.progress

        def progress(*args):
            real(*args)
            self.helper.cancel.set()
        self.helper.progress = progress
        with self.assertRaises(self.module.Cancelled) as caught:
            self.create()
        self.assertIn("removed", str(caught.exception))
        self.assertFalse(os.path.exists(self.image))

    def test_an_image_that_reads_back_differently_is_reported_and_removed(
            self):
        real = self.helper.image_digest
        self.helper.image_digest = lambda *args: "0" * 64
        text = self.refused(self.ask("image_create", "sdx"))
        self.helper.image_digest = real
        self.assertIn("does not read back the same", text)
        self.assertFalse(os.path.exists(self.image))

    def test_unreadable_blocks_can_be_skipped_into_an_image_too(self):
        with self.bad_reads("sdx", 2 * MIB, 4 * KIB):
            result = self.create(skip_errors=True)
        self.assertEqual(result["skipped"], [(2 * MIB, 4 * KIB)])
        with open(self.image, "rb") as handle:
            handle.seek(2 * MIB)
            self.assertEqual(handle.read(4 * KIB), b"\0" * 4 * KIB)

    def test_the_running_system_is_not_imaged(self):
        self.assertIn("running system",
                      self.refused(self.ask("image_create", "nvme0n1")))


class ImageRestoreTest(ImageCase):
    def setUp(self):
        super().setUp()
        self.content = os.urandom(3 * MIB)
        with open(self.image, "wb") as handle:
            handle.write(self.content)

    def test_the_drive_becomes_the_image_and_is_read_back(self):
        result = self.restore()
        self.assertEqual(self.data("sdy")[:3 * MIB], self.content)
        self.assertEqual(result["size"], 3 * MIB)
        self.assertEqual(result["sha256"],
                         hashlib.sha256(self.content).hexdigest())
        self.assertEqual(self.steps(), ["copy", "verify"])
        self.assertEqual(self.tools.names()[0], "wipefs")

    def test_a_compressed_image_is_measured_before_anything_is_written(self):
        packed = self.image + ".gz"
        with gzip.open(packed, "wb") as handle:
            handle.write(self.content)
        self.restore(path=packed)
        self.assertEqual(self.data("sdy")[:3 * MIB], self.content)
        self.assertEqual(self.steps(), ["measure", "copy", "verify"])

    def test_an_image_larger_than_the_drive_is_refused_untouched(self):
        for packed in (False, True):
            path = self.image + (".gz" if packed else ".big")
            opener = gzip.open if packed else open
            with opener(path, "wb") as handle:
                handle.write(os.urandom(5 * MIB))
            text = self.refused(self.ask("image_restore", "sdy", path=path))
            self.assertIn("does not fit", text)
            self.assertEqual(self.data("sdy"), b"\xaa" * (4 * MIB))
            self.assertNotIn("wipefs", self.tools.names())

    def test_a_damaged_compressed_image_is_refused_untouched(self):
        packed = self.image + ".gz"
        with gzip.open(packed, "wb") as handle:
            handle.write(self.content)
        with open(packed, "r+b") as handle:
            handle.truncate(os.path.getsize(packed) // 2)
        text = self.refused(self.ask("image_restore", "sdy", path=packed))
        self.assertIn("damaged", text)
        self.assertEqual(self.data("sdy"), b"\xaa" * (4 * MIB))

    def test_a_missing_or_empty_image_is_refused(self):
        self.assertIn("Cannot open", self.refused(
            self.ask("image_restore", "sdy", path=self.image + ".none")))
        open(self.image, "w").close()
        self.assertIn("empty", self.refused(self.ask("image_restore", "sdy")))
        self.assertIn("not an ordinary file", self.refused(
            self.ask("image_restore", "sdy", path=self.folder)))

    def test_a_gpt_image_on_a_larger_drive_gets_its_second_copy_moved(self):
        self.tools.lie["sdz"] = {"PTTYPE": "gpt"}
        result = self.restore("sdz")
        self.assertIn(["--relocate", "gpt-bak-std", self.path("sdz")],
                      self.tools.of("sfdisk"))
        self.assertEqual(result["table"], "gpt")

    def test_a_drive_that_reads_back_differently_is_reported(self):
        self.helper.digest_of = lambda *args: "0" * 64
        text = self.refused(self.ask("image_restore", "sdy"))
        self.assertIn("does not read back the same", text)

    def test_stopping_says_the_drive_holds_part_of_the_image(self):
        real = self.helper.progress

        def progress(*args):
            real(*args)
            self.helper.cancel.set()
        self.helper.progress = progress
        with self.assertRaises(self.module.Cancelled) as caught:
            self.restore()
        self.assertIn("part of the image", str(caught.exception))

    def test_the_system_drive_is_never_written(self):
        self.assertIn("running system",
                      self.refused(self.ask("image_restore", "nvme0n1")))


class WhatCanBeCopiedTest(unittest.TestCase):
    def setUp(self):
        self.machine = machine()
        self.all = list(self.machine.values())

    def test_the_running_systems_drive_is_greyed_and_says_how_to_do_it(self):
        system = self.machine["nvme0n1"]
        for action in (diskops.CLONE, diskops.IMAGE_SAVE):
            for volume in (None, system.volumes[2]):
                answer = diskops.availability(action, system, volume)
                self.assertFalse(answer)
                self.assertIn("running system", answer.reason)
                self.assertIn("live USB", answer.reason)
        self.assertFalse(diskops.availability(diskops.IMAGE_WRITE, system))

    def test_other_drives_and_volumes_can_be_copied_mounted_or_not(self):
        for name in ("sda", "sdb", "sdc", "nvme1n1", "sde"):
            disk = self.machine[name]
            self.assertTrue(diskops.availability(diskops.CLONE, disk), name)
            self.assertTrue(diskops.availability(diskops.IMAGE_SAVE, disk,
                                                 disk.volumes[0]), name)

    def test_an_empty_reader_has_nothing_to_copy(self):
        answer = diskops.availability(diskops.CLONE, self.machine["sdd"])
        self.assertIn("no media", answer.reason)

    def test_a_drive_is_offered_drives_and_each_refusal_says_why(self):
        source = self.machine["sdb"]
        found = {disk.name: answer for disk, volume, answer
                 in diskcopy.targets(source, None, self.all)}
        self.assertNotIn("sdb", found)           # not onto itself
        self.assertNotIn("sdd", found)           # nothing in the reader
        self.assertTrue(found["sdc"])            # the same size
        self.assertTrue(found["sda"])            # larger
        self.assertIn("running system", found["nvme0n1"].reason)
        self.assertIn("Too small", found["sde"].reason)
        self.assertIn("Too small", found["nvme1n1"].reason)

    def test_used_blocks_only_fits_where_the_partitions_fit(self):
        source = disks.Disk("sdq", "/dev/sdq", 1000 * GIB, table="gpt",
                            volumes=(disks.Volume(
                                "sdq1", "/dev/sdq1", "sdq", "part",
                                100 * GIB, start=MIB, fstype="ext4"),))
        smaller = self.machine["nvme1n1"]        # 500 GB
        exact = dict((d.name, a) for d, v, a in diskcopy.targets(
            source, None, [smaller], diskcopy.EXACT))
        self.assertIn("used blocks only' would fit", exact["nvme1n1"].reason)
        smart = dict((d.name, a) for d, v, a in diskcopy.targets(
            source, None, [smaller], diskcopy.SMART))
        self.assertTrue(smart["nvme1n1"])
        self.assertEqual(diskcopy.needed(source, diskcopy.SMART),
                         100 * GIB + MIB + 34 * 512)
        # A drive filled to its end needs no more than its own size.
        full = self.machine["sdb"]
        self.assertLessEqual(diskcopy.needed(full, diskcopy.SMART), full.size)

    def test_a_partition_is_offered_partitions(self):
        source = self.machine["sdb"]
        found = {volume.name: answer for disk, volume, answer
                 in diskcopy.targets(source, source.volumes[0], self.all)}
        self.assertNotIn("sdb1", found)
        self.assertTrue(found["sda1"])
        self.assertIn("running system", found["nvme0n1p3"].reason)
        self.assertIn("Too small", found["sde1"].reason)

    def test_how_each_partition_would_be_copied_is_said_beforehand(self):
        windows = self.machine["nvme1n1"]
        found = diskcopy.methods(windows, None, diskcopy.SMART, everything)
        how = [text for _what, text in found]
        self.assertEqual(len(found), 4)
        self.assertIn("no tool copies FAT", how[0])
        self.assertIn("unformatted partition", how[1])
        self.assertEqual(how[2], "used blocks only")
        self.assertIn("NTFS", found[2][0])
        found = diskcopy.methods(windows, None, diskcopy.SMART, nothing)
        self.assertIn("'ntfsclone' is not installed", found[2][1])
        self.assertEqual(diskcopy.methods(windows, None, diskcopy.EXACT),
                         [("Everything", "every block, used or not")])

    def test_the_tools_named_here_are_the_helpers(self):
        from disktree import load_helper
        helper = load_helper()
        self.assertEqual({k: v[0] for k, v in helper.SMART_TOOLS.items()},
                         diskcopy.SMART_TOOLS)


class ClonePlanTest(unittest.TestCase):
    def setUp(self):
        self.machine = machine()

    def plan(self, source, target, volume=None, target_volume=None, **kw):
        kw.setdefault("identities", {})
        kw.setdefault("tools", everything)
        with mock.patch.object(disks, "identity", lambda name: {"n": name}):
            return diskcopy.plan_clone(self.machine[source], volume,
                                       self.machine[target], target_volume,
                                       **kw)

    def test_both_devices_are_named_with_what_was_seen_of_them(self):
        plan = self.plan("sdb", "sdc", identities={"sdb": {"a": 1},
                                                   "sdc": {"b": 2}})
        self.assertEqual(plan.request["source"], "/dev/sdb")
        self.assertEqual(plan.request["target"], "/dev/sdc")
        self.assertEqual(plan.request["source_identity"], {"a": 1})
        self.assertEqual(plan.request["target_identity"], {"b": 2})
        self.assertTrue(plan.request["verify"])

    def test_what_is_on_the_target_is_listed_as_lost(self):
        plan = self.plan("sdb", "sdc")
        self.assertEqual(len(plan.loses), 1)
        self.assertIn("NTFS", plan.loses[0])
        self.assertTrue(plan.internal)
        self.assertEqual(plan.confirm, "Erase the Target and Clone")

    def test_the_steps_are_the_ones_the_helper_announces(self):
        self.assertEqual(self.plan("sdb", "sdc").step_keys,
                         ["unmount", "copy", "verify"])
        self.assertEqual(self.plan("sdc", "nvme1n1").step_keys,
                         ["copy", "verify"])
        self.assertEqual(self.plan("sdb", "sdc", mode=diskcopy.SMART,
                                   verify=False).step_keys,
                         ["unmount", "check", "copy"])
        # Nothing to check where nothing is copied by its contents.
        self.assertEqual(self.plan("sde", "sdc", mode=diskcopy.SMART)
                         .step_keys, ["unmount", "copy", "verify"])

    def test_skipping_errors_belongs_to_the_exact_copy(self):
        self.assertTrue(self.plan("sdb", "sdc", skip_errors=True)
                        .request["skip_errors"])
        self.assertFalse(self.plan("sdb", "sdc", skip_errors=True,
                                   mode=diskcopy.SMART)
                         .request["skip_errors"])

    def test_the_same_identifiers_are_always_warned_about(self):
        titles = [a.title for a in self.plan("sdb", "sdc").advice]
        self.assertIn("The copy has the same identifiers as the original",
                      titles)
        advice = self.plan("sdb", "sda").advice
        self.assertIn("The extra room is not used by the copy",
                      [a.title for a in advice])

    def test_a_windows_drive_copied_by_contents_mentions_shutting_down(self):
        titles = [a.title for a in self.plan(
            "nvme1n1", "sdc", mode=diskcopy.SMART).advice]
        self.assertIn("Windows has to be shut down completely", titles)
        titles = [a.title for a in self.plan("nvme1n1", "sdc").advice]
        self.assertNotIn("Windows has to be shut down completely", titles)

    def test_a_drive_and_a_partition_do_not_mix(self):
        with self.assertRaises(ValueError):
            self.plan("sdb", "sdc",
                      target_volume=self.machine["sdc"].volumes[0])

    def test_the_outcome_in_one_line(self):
        text = diskcopy.clone_done_text(
            {"source": "/dev/sdb", "target": "/dev/sdc", "verified": True})
        self.assertIn("read back and matches", text)
        result = {"verified": True, "skipped": [[1048576, 4096],
                                                [9 * MIB, 8192]],
                  "skipped_bytes": 12288}
        self.assertIn("in 2 places could not be read",
                      diskcopy.clone_done_text(result))
        report = diskcopy.skipped_report(result)
        self.assertEqual(len(report), 2)
        self.assertIn("bytes 1048576 to 1052671", report[0])
        self.assertIn("not checked",
                      diskcopy.clone_done_text({"verified": False}))


class GrowRuleTest(unittest.TestCase):
    """Which partition "use the free space" would enlarge, said before."""

    def setUp(self):
        self.machine = machine()

    def grow(self, source, target, tools=everything):
        return diskcopy.grow_candidate(self.machine[source],
                                       self.machine[target], tools)

    def linux_last(self):
        """nvme0n1 with its btrfs last and nothing after it."""
        return self.machine["nvme0n1"]

    def test_the_last_partition_takes_everything_up_to_the_backup_table(self):
        source = self.linux_last()
        target = dataclasses.replace(self.machine["sdb"], table="")
        answer = diskcopy.grow_candidate(source, target, everything)
        last = source.volume("nvme0n1p3")
        self.assertEqual(answer.volume, last)
        end = target.size - 34 * 512
        self.assertEqual(answer.size,
                         (end - last.start) // MIB * MIB)
        self.assertIn("grows from", diskcopy.grow_text(answer))
        self.assertIn("Btrfs", diskcopy.grow_text(answer))

    def test_an_mbr_copy_stops_at_two_tebibytes(self):
        answer = self.grow("sdb", "sda")
        self.assertEqual(answer.volume.name, "sdb1")
        self.assertEqual(MIB + answer.size, 2 * 1024 ** 4)
        full = dataclasses.replace(
            self.machine["sdb"], size=2 * 1024 ** 4 + 10 * MIB,
            volumes=(dataclasses.replace(
                self.machine["sdb"].volume("sdb1"),
                size=2 * 1024 ** 4 - MIB),))
        answer = diskcopy.grow_candidate(full, self.machine["sda"],
                                         everything)
        self.assertIsNone(answer.volume)
        self.assertIn("2 TiB", answer.reason)

    def test_what_is_left_alone_says_why(self):
        # Windows recovery last.
        self.assertIn("system partition",
                      self.grow("nvme1n1", "sdb").reason)
        # FAT last.
        self.assertIn("FAT cannot", self.grow("sde", "sdb").reason)
        # Not larger.
        self.assertIn("not larger", self.grow("sdb", "sdc").reason)
        self.assertIn("not larger", self.grow("sdb", "nvme0n1").reason)
        # The tool missing.
        self.assertIn("'btrfs'", diskcopy.grow_candidate(
            self.linux_last(), self.machine["sdb"], nothing).reason)

    def test_a_logical_partition_is_not_grown(self):
        sdb = self.machine["sdb"]
        logical = dataclasses.replace(sdb.volume("sdb1"), number=5)
        answer = diskcopy.grow_candidate(
            dataclasses.replace(sdb, volumes=(logical,)),
            self.machine["sda"], everything)
        self.assertIn("logical", answer.reason)

    def test_the_tools_and_types_are_the_helpers(self):
        from disktree import load_helper
        module = load_helper()
        self.assertEqual(diskcopy.NO_GROW_TYPES, module.NO_GROW_TYPES)
        self.assertEqual(diskcopy.GROW_TOOLS, {
            fs: spec["tool"] for fs, spec in module.RESIZE.items()
            if spec["grow"]})
        self.assertEqual({k.lower() for k in diskcopy.NO_GROW},
                         {k.lower() for k in module.NO_RESIZE})


class ImagePlanTest(unittest.TestCase):
    def setUp(self):
        self.machine = machine()
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def file(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_a_name_is_suggested_from_what_is_being_saved(self):
        stick_ = self.machine["sde"]
        now = 1700000000
        name = diskcopy.suggested_name(stick_, None, now=now)
        self.assertTrue(name.startswith("SanDisk-Ultra-sde-2023-11-"))
        self.assertTrue(name.endswith(".img"))
        name = diskcopy.suggested_name(stick_, stick_.volumes[0], True, now)
        self.assertTrue(name.startswith("STICK-sde1-"))
        self.assertTrue(name.endswith(".img.gz"))
        odd = disks.Disk("sdq", "/dev/sdq", GIB, model="a/b c:d")
        self.assertNotIn("/", diskcopy.suggested_name(odd, now=now))

    def test_saving_asks_the_helper_for_what_was_chosen(self):
        ssd = self.machine["sdb"]
        plan = diskcopy.plan_image_save(ssd, None, "/x/y.img.gz",
                                        compress=True, skip_errors=True,
                                        identity={"k": 1})
        self.assertEqual(plan.request, {
            "op": "image_create", "device": "/dev/sdb", "path": "/x/y.img.gz",
            "compress": True, "verify": True, "skip_errors": True,
            "overwrite": False, "identity": {"k": 1}})
        self.assertEqual(plan.step_keys, ["unmount", "copy", "verify"])
        self.assertEqual(plan.advice, [])
        plain = diskcopy.plan_image_save(ssd, ssd.volumes[0], "/x/y.img",
                                         identity={})
        self.assertIn("as large as the volume", plain.advice[0].title)
        self.assertEqual(plain.request["device"], "/dev/sdb1")
        self.assertTrue(plain.remount)

    def test_an_image_is_recognised_by_what_is_in_it(self):
        table = bytearray(512)
        table[510:512] = b"\x55\xaa"
        table[446 + 4] = 0x83
        disc = b"\0" * 0x8001 + b"CD001" + b"\0" * 100
        cases = (
            ("a.img.gz", b"\x1f\x8b" + b"x" * 100, "gzip"),
            ("disk.img", bytes(table) + b"\0" * 4096, "disk"),
            ("live.iso", bytes(table) + disc[512:], "iso-hybrid"),
            ("win.iso", disc, "iso"),
            ("raw.bin", b"\0" * 2048, "raw"))
        for name, data, kind in cases:
            found = diskcopy.inspect_image(self.file(name, data))
            self.assertEqual(found["kind"], kind, name)
            self.assertEqual(found["size"], len(data))
        found = diskcopy.inspect_image(os.path.join(self.dir, "none.img"))
        self.assertEqual(found["kind"], "unreadable")

    def test_a_disc_image_not_made_for_usb_says_so(self):
        path = self.file("win.iso", b"\0" * 0x8001 + b"CD001")
        found = diskcopy.inspect_image(path)
        self.assertEqual(found["advice"][0].level, diskfs.WARNING)
        self.assertIn("Windows installation images", found["advice"][0].text)
        self.assertIn("Ventoy", found["advice"][0].text)

    def test_writing_lists_what_is_lost_and_checks_the_size_first(self):
        stick_ = self.machine["sde"]
        path = self.file("small.img", b"\0" * 4096)
        plan = diskcopy.plan_image_write(stick_, None, path, identity={})
        self.assertEqual(plan.request["op"], "image_restore")
        self.assertEqual(plan.request["device"], "/dev/sde")
        self.assertEqual(plan.step_keys, ["unmount", "copy", "verify"])
        self.assertEqual(len(plan.loses), 1)
        self.assertFalse(plan.internal)
        with self.assertRaises(ValueError) as caught:
            diskcopy.plan_image_write(
                stick_, None, path, identity={},
                inspection={"kind": "raw", "size": 64 * GIB, "advice": []})
        self.assertIn("does not fit", str(caught.exception))

    def test_a_compressed_image_adds_the_measuring_step(self):
        path = self.file("a.img.gz", b"\x1f\x8b" + b"x" * 10)
        plan = diskcopy.plan_image_write(self.machine["sde"], None, path,
                                         identity={})
        self.assertEqual(plan.step_keys,
                         ["unmount", "measure", "copy", "verify"])

    def test_a_drive_image_aimed_at_a_partition_is_warned_about(self):
        table = bytearray(512)
        table[510:512] = b"\x55\xaa"
        table[446 + 4] = 0x83
        path = self.file("disk.img", bytes(table))
        stick_ = self.machine["sde"]
        plan = diskcopy.plan_image_write(stick_, stick_.volumes[0], path,
                                         identity={})
        self.assertIn("This image is laid out as a whole drive",
                      [a.title for a in plan.advice])

    def test_the_outcomes_in_one_line(self):
        self.assertIn("read back and matching", diskcopy.image_saved_text(
            {"source": "/dev/sdb", "path": "/x.img", "stored": GIB,
             "verified": True}))
        self.assertIn("holding 2.1 GB", diskcopy.image_saved_text(
            {"stored": GIB, "size": 2 * GIB, "compressed": True}))
        self.assertIn("live.iso onto /dev/sde", diskcopy.image_written_text(
            {"path": "/x/live.iso", "target": "/dev/sde", "size": GIB,
             "verified": True}))


class CopyMenuTest(DiskWindowCase):
    def test_the_system_drive_has_its_copying_greyed_with_the_reason(self):
        window = self.window()
        window.select_device("nvme0n1")
        for action in (window.clone_action, window.save_image_action,
                       window.write_image_action):
            self.assertFalse(action.isEnabled())
            self.assertIn("running system", action.toolTip())
        self.assertIn("live USB", window.clone_action.toolTip())

    def test_another_drive_offers_all_three(self):
        window = self.window()
        window.select_device("sdc")
        for action in (window.clone_action, window.save_image_action,
                       window.write_image_action):
            self.assertTrue(action.isEnabled())

    def test_a_clone_is_set_up_confirmed_and_handed_to_the_helper(self):
        window = self.window()
        window.select_device("sdc")
        original = window.show_dialog

        def drive(dialog):
            self.assertIsInstance(dialog, CloneDialog)
            dialog.target.setCurrentIndex(dialog.target.findData("sdb"))
            dialog.smart.setChecked(True)
            dialog.confirm = lambda plan: True
            dialog._accept()
            return original(dialog)
        window.show_dialog = drive
        self.answers = [True]
        self.client.result = {"source": "/dev/sdc", "target": "/dev/sdb",
                              "verified": True, "skipped": []}
        window.clone_action.trigger()
        request = self.client.requests[-1]
        self.assertEqual((request["op"], request["mode"], request["source"],
                          request["target"]),
                         ("clone", "smart", "/dev/sdc", "/dev/sdb"))
        self.assertEqual(request["source_identity"],
                         window.identities.get("sdc"))
        self.assertIn("read back and matches", window.status.text())

    def test_areas_that_could_not_be_read_are_listed_for_the_user(self):
        window = self.window()
        window._clone_done({"source": "/dev/sdc", "target": "/dev/sdb",
                            "verified": True, "skipped": [[4096, 4096]],
                            "skipped_bytes": 4096})
        self.assertIn("bytes 4096 to 8191", self.messages[-1][1])
        self.assertIn("zeros on the copy", self.messages[-1][1])

    def test_a_declined_confirmation_asks_nothing_of_the_helper(self):
        window = self.window()
        window.select_device("sdc")
        window.clone_action.trigger()
        self.assertEqual(self.client.requests, [])


class CloneDialogTest(DiskWindowCase):
    def dialog(self, source="sdb", volume=None):
        disks_ = {d.name: d for d in self.machine}
        disk = disks_[source]
        return CloneDialog(disk, disk.volume(volume) if volume else None,
                           self.machine, identities={}, tools=everything)

    def test_what_cannot_take_the_copy_is_listed_greyed(self):
        dialog = self.dialog()
        names = [dialog.target.itemData(i)
                 for i in range(dialog.target.count())]
        self.assertIn("nvme0n1", names)
        row = names.index("nvme0n1")
        item = dialog.target.model().item(row)
        self.assertFalse(item.isEnabled())
        self.assertIn("running system", item.toolTip())

    def test_no_target_is_ever_chosen_on_the_users_behalf(self):
        """The target is what gets erased."""
        dialog = self.dialog()
        self.assertIsNone(dialog.target.currentData())
        self.assertFalse(dialog.start.isEnabled())
        self.assertIsNone(dialog.build_plan())
        self.assertIn("is erased", dialog.target_note.text())
        dialog.target.setCurrentIndex(dialog.target.findData("sdc"))
        self.assertTrue(dialog.start.isEnabled())
        self.assertIn("Will be erased", dialog.target_note.text())
        # Changing how the copy is made keeps what was chosen...
        dialog.smart.setChecked(True)
        self.assertEqual(dialog.target.currentData(), "sdc")
        self.assertEqual(dialog.build_plan().request["target"], "/dev/sdc")

    def test_what_is_added_on_screen_gets_its_room(self):
        dialog = self.dialog("nvme1n1")
        dialog.show()
        self.addCleanup(dialog.close)
        self.app.processEvents()
        dialog.smart.setChecked(True)
        dialog.target.setCurrentIndex(dialog.target.findData("sdc"))
        self.assertTrue(dialog._method_labels)
        self.assert_dialog_fits(dialog)

    def test_the_method_table_appears_with_used_blocks_only(self):
        dialog = self.dialog("nvme1n1")
        self.assertEqual(dialog._method_labels, [])
        self.assertFalse(dialog.skip.isHidden())
        dialog.smart.setChecked(True)
        texts = [label.text() for label in dialog._method_labels]
        self.assertIn("used blocks only", texts)
        self.assertTrue(dialog.skip.isHidden())
        dialog.target.setCurrentIndex(dialog.target.findData("sdc"))
        plan = dialog.build_plan()
        self.assertEqual(plan.request["mode"], "smart")

    def test_nothing_to_copy_onto_is_said_in_words(self):
        only = [d for d in self.machine if d.name in ("sdb", "nvme0n1")]
        disk = [d for d in only if d.name == "sdb"][0]
        dialog = CloneDialog(disk, None, only, identities={})
        self.assertFalse(dialog.start.isEnabled())
        self.assertIn("running system", dialog.target_note.text())
        dialog = CloneDialog(disk, None, [disk], identities={})
        self.assertIn("no other drive", dialog.target_note.text())
        self.assertIsNone(dialog.build_plan())

    def test_using_the_free_space_is_offered_onto_a_larger_drive(self):
        dialog = self.dialog("sdb")
        self.assertFalse(dialog.grow.isEnabled())
        self.assertIn("Choose the target", dialog.grow_note.text())
        dialog.target.setCurrentIndex(dialog.target.findData("sda"))
        self.assertTrue(dialog.grow.isEnabled())
        self.assertIn("grows from", dialog.grow_note.text())
        self.assertFalse(dialog.build_plan().request["grow"])
        dialog.grow.setChecked(True)
        plan = dialog.build_plan()
        self.assertTrue(plan.request["grow"])
        self.assertEqual(plan.step_keys[-1], "grow")
        # Ticked, the advice about unused room goes: it will be used.
        self.assertNotIn("The extra room is not used by the copy",
                         [a.title for a in plan.advice])
        # Onto a drive of the same size it is greyed with the reason.
        dialog.target.setCurrentIndex(dialog.target.findData("sdc"))
        self.assertFalse(dialog.grow.isEnabled())
        self.assertIn("not larger", dialog.grow_note.text())
        self.assertFalse(dialog.build_plan().request["grow"])

    def test_a_partition_copy_has_no_grow_option(self):
        dialog = self.dialog("sdc", "sdc1")
        self.assertTrue(dialog.grow.isHidden())
        self.assertTrue(dialog.grow_note.isHidden())

    def test_a_partition_is_copied_onto_a_partition(self):
        dialog = self.dialog("sdc", "sdc1")
        names = [dialog.target.itemData(i)
                 for i in range(dialog.target.count())]
        self.assertIn("sdb1", names)
        self.assertNotIn("sdb", names)
        dialog.target.setCurrentIndex(names.index("sdb1"))
        plan = dialog.build_plan()
        self.assertEqual((plan.request["source"], plan.request["target"]),
                         ("/dev/sdc1", "/dev/sdb1"))


class ImageDialogTest(DiskWindowCase):
    def setUp(self):
        super().setUp()
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.stick = [d for d in self.machine if d.name == "sde"][0]

    def test_saving_suggests_a_name_and_follows_the_compress_tick(self):
        dialog = SaveImageDialog(self.stick, None, identity={},
                                 folder=self.dir)
        path = dialog.path.edit.text()
        self.assertTrue(path.startswith(self.dir))
        self.assertTrue(path.endswith(".img"))
        dialog.compress.setChecked(True)
        self.assertTrue(dialog.path.edit.text().endswith(".img.gz"))
        dialog.compress.setChecked(False)
        self.assertTrue(dialog.path.edit.text().endswith(".img"))
        plan = dialog.build_plan()
        self.assertEqual(plan.request["path"], path)

    def test_a_folder_that_is_not_there_blocks_the_button(self):
        dialog = SaveImageDialog(self.stick, None, identity={},
                                 folder=self.dir)
        dialog.path.edit.setText(os.path.join(self.dir, "nope", "x.img"))
        self.assertFalse(dialog.start.isEnabled())
        self.assertIn("does not exist", dialog.note.text())
        dialog.path.edit.setText("relative.img")
        self.assertIn("full path", dialog.note.text())

    def test_no_room_is_said_before_starting(self):
        huge = disks.Disk("sdq", "/dev/sdq", 10 ** 18, model="Huge")
        dialog = SaveImageDialog(huge, None, identity={}, folder=self.dir)
        self.assertFalse(dialog.start.isEnabled())
        self.assertIn("Not enough room", dialog.note.text())
        dialog.compress.setChecked(True)
        self.assertTrue(dialog.start.isEnabled())

    def test_an_existing_file_is_replaced_only_when_agreed(self):
        dialog = SaveImageDialog(self.stick, None, identity={},
                                 folder=self.dir, exists=lambda path: True)
        dialog.ask_replace = lambda path: False
        dialog._accept()
        self.assertIsNone(dialog.plan())
        dialog.ask_replace = lambda path: True
        dialog._accept()
        self.assertTrue(dialog.plan().request["overwrite"])

    def test_writing_describes_the_file_and_shows_its_warnings(self):
        path = os.path.join(self.dir, "win.iso")
        with open(path, "wb") as handle:
            handle.write(b"\0" * 0x8001 + b"CD001")
        dialog = WriteImageDialog(self.stick, None, identity={},
                                  folder=self.dir)
        self.assertFalse(dialog.start.isEnabled())
        dialog.path.edit.setText(path)
        self.assertTrue(dialog.start.isEnabled())
        self.assertIn("A disc image", dialog.note.text())
        self.assertIn("This disc image is not made for a USB drive",
                      [card.advice.title for card in dialog.advice_cards])
        dialog.confirm = lambda plan: True
        dialog._accept()
        self.assertEqual(dialog.plan().request["path"], path)

    def test_an_image_too_large_or_missing_blocks_the_button(self):
        dialog = WriteImageDialog(
            self.stick, None, identity={},
            inspect=lambda path: {"kind": "raw", "size": 64 * GIB,
                                  "advice": []})
        dialog.path.edit.setText("/some/image.img")
        self.assertFalse(dialog.start.isEnabled())
        self.assertIn("does not fit", dialog.note.text())
        dialog = WriteImageDialog(self.stick, None, identity={})
        dialog.path.edit.setText(os.path.join(self.dir, "missing.img"))
        self.assertFalse(dialog.start.isEnabled())


if __name__ == "__main__":
    unittest.main()
