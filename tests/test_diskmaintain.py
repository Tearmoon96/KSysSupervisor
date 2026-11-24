"""Looking after a volume: TRIM, fragmentation, free space, checking.

The privileged half is driven with stand-in tools, as in test_diskhelper;
what the real tools print and return is pinned down by
tests/verify-disk-tools.py on loop devices.
"""

import errno
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

from ksyssupervisor import (diskfs, diskhealth, diskmaintain,  # noqa: E402
                            diskops, disks, udisks)
from ksyssupervisor import diskwindow as window_mod        # noqa: E402
from ksyssupervisor.diskmaintainui import (ActionDialog,   # noqa: E402
                                           CheckResultDialog,
                                           FragmentationDialog, WipeDialog)


def everything(*names):
    return "/usr/sbin/" + names[0]


def nothing(*names):
    return None


def machine():
    found = disks.parse(desktop(), fstab=[]) + parse(stick())
    return {d.name: d for d in found}

E4DEFRAG_REPORT = """e4defrag 1.47.3 (8-Jul-2025)
<Fragmented files>                             now/best       size/ext
1. /mnt/games/a.bin                             12/1             44 KB

 Total/best extents				%d/%d
 Average size per extent			812 KB
 Fragmentation score				%d
 [0-30 no problem: 31-55 a little bit fragmented: 56- needs defrag]
 This directory (/mnt/games) does not need defragmentation.
 Done."""


class MaintainCase(HelperCase):
    def ask(self, op, name, **fields):
        request = {"op": op, "id": 5,
                   "device": os.path.join(self.tree.dev, name),
                   "identity": self.tree.identity(name)}
        request.update(fields)
        return request

    def do(self, op, name, **fields):
        return self.helper.OPERATIONS[op](self.helper,
                                          self.ask(op, name, **fields))

    def no(self, op, name, **fields):
        return self.refused(self.ask(op, name, **fields))

    def progress(self):
        return [(e["done"], e["total"]) for e in self.events()
                if e.get("event") == "progress"]


class TrimTest(MaintainCase):
    def test_the_mounted_filesystem_is_trimmed_and_the_amount_reported(self):
        self.tools.lines["fstrim"] = [
            "/mnt/games: 1.2 GiB (1288490188 bytes) trimmed"]
        result = self.do("trim", "sdb1")
        self.assertEqual(self.tools.of("fstrim"),
                         [["--verbose", "--", "/mnt/games"]])
        self.assertEqual(result["trimmed"], 1288490188)
        self.assertEqual(result["mountpoint"], "/mnt/games")

    def test_the_system_volume_may_be_trimmed(self):
        """TRIM only reports what is already unused; it is as safe on the
        running system's own volume as anywhere."""
        self.tools.contents["nvme0n1p2"] = {"TYPE": "ext4"}
        self.tools.lines["fstrim"] = ["/: 0 B (0 bytes) trimmed"]
        self.assertEqual(self.do("trim", "nvme0n1p2")["trimmed"], 0)

    def test_an_unmounted_volume_says_to_mount_it(self):
        self.assertIn("not mounted", self.no("trim", "sdd1"))
        self.assertEqual(self.tools.of("fstrim"), [])

    def test_a_volume_mounted_read_only_is_refused(self):
        self.tree.unmount("/mnt/games")
        self.tree.mount("sdb1", "/mnt/games", options="ro,relatime")
        self.assertIn("read-only", self.no("trim", "sdb1"))

    def test_a_drive_that_takes_no_trim_says_so_in_plain_words(self):
        self.tools.lines["fstrim"] = [
            "fstrim: /mnt/games: the discard operation is not supported"]
        self.tools.codes["fstrim"] = 1
        text = self.no("trim", "sdb1")
        self.assertIn("does not accept TRIM", text)
        self.assertIn("USB", text)

    def test_the_weekly_timer_is_switched_and_read_back(self):
        self.tools.stdout["systemctl"] = "enabled\n"
        result = self.helper.op_trim_timer({"op": "trim_timer", "id": 1,
                                            "enable": True})
        self.assertEqual(result, {"enabled": True})
        self.assertEqual(self.tools.of("systemctl")[0],
                         ["enable", "--now", "fstrim.timer"])

        self.tools.calls.clear()
        self.tools.stdout["systemctl"] = "disabled\n"
        self.helper.op_trim_timer({"op": "trim_timer", "enable": False})
        self.assertEqual(self.tools.of("systemctl")[0],
                         ["disable", "--now", "fstrim.timer"])

    def test_a_timer_that_did_not_change_is_reported(self):
        self.tools.stdout["systemctl"] = "masked\n"
        with self.assertRaises(self.module.Refusal) as caught:
            self.helper.op_trim_timer({"op": "trim_timer", "enable": True})
        self.assertIn("masked", str(caught.exception))

    def test_on_or_off_has_to_be_said(self):
        with self.assertRaises(self.module.Refusal):
            self.helper.op_trim_timer({"op": "trim_timer", "enable": "yes"})


class FragmentationTest(MaintainCase):
    def report(self, score, now=1200, best=1000):
        self.tools.lines["e4defrag"] = (
            E4DEFRAG_REPORT % (now, best, score)).splitlines()

    def test_the_score_is_judged_by_the_tools_own_bands(self):
        for score, verdict in ((0, "none"), (30, "none"), (31, "some"),
                               (55, "some"), (56, "needed"), (90, "needed")):
            self.report(score)
            result = self.do("defrag_analyse", "sdb1")
            self.assertEqual((result["score"], result["verdict"]),
                             (score, verdict))
        self.assertEqual(result["extents"], [1200, 1000])
        self.assertEqual((result["low"], result["high"]), (30, 55))
        self.assertEqual(self.tools.of("e4defrag")[0], ["-c", "/mnt/games"])

    def test_bands_a_newer_tool_prints_are_followed(self):
        self.tools.lines["e4defrag"] = (E4DEFRAG_REPORT % (5, 4, 45)).replace(
            "[0-30 no problem: 31-55", "[0-40 no problem: 41-70").splitlines()
        result = self.do("defrag_analyse", "sdb1")
        self.assertEqual(result["verdict"], "some")
        self.assertEqual((result["low"], result["high"]), (40, 70))

    def test_measuring_changes_nothing_so_the_system_volume_is_allowed(self):
        self.tools.contents["nvme0n1p2"] = {"TYPE": "ext4"}
        self.report(3)
        self.assertEqual(self.do("defrag_analyse", "nvme0n1p2")["score"], 3)

    def test_output_that_is_not_a_report_is_not_guessed_at(self):
        self.tools.lines["e4defrag"] = ["e4defrag: something went wrong"]
        self.assertIn("could not be measured",
                      self.no("defrag_analyse", "sdb1"))

    def test_xfs_figures_are_passed_on_without_a_verdict(self):
        self.tools.contents["sdb1"] = {"TYPE": "xfs"}
        self.tools.lines["xfs_db"] = [
            "actual 5123, ideal 4990, fragmentation factor 2.60%",
            "Note, this number is largely meaningless."]
        result = self.do("defrag_analyse", "sdb1")
        self.assertEqual(result["verdict"], "unrated")
        self.assertEqual(result["factor"], 2.6)
        self.assertEqual(result["extents"], [5123, 4990])
        self.assertEqual(self.tools.of("xfs_db")[0][:3], ["-r", "-c", "frag"])

    def test_windows_filesystems_cannot_be_measured_or_defragmented(self):
        for fstype in ("ntfs", "vfat", "exfat"):
            self.tools.contents["sdb1"] = {"TYPE": fstype}
            self.assertIn("cannot be measured",
                          self.no("defrag_analyse", "sdb1"))
            self.assertIn("cannot be defragmented", self.no("defrag", "sdb1"))
        self.assertEqual(self.destructive(), [])

    def test_each_filesystem_is_defragmented_by_its_own_tool(self):
        for fstype, tool, argv in (
                ("ext4", "e4defrag", ["-v", "/mnt/games"]),
                ("xfs", "xfs_fsr", ["-v", "/mnt/games"]),
                ("btrfs", "btrfs", ["filesystem", "defragment", "-r",
                                    "/mnt/games"])):
            self.tools.calls.clear()
            self.tools.contents["sdb1"] = {"TYPE": fstype}
            self.do("defrag", "sdb1")
            self.assertEqual(self.tools.of(tool), [argv])

    def test_progress_follows_the_files_the_tool_counts(self):
        self.tools.lines["e4defrag"] = [
            "ext4 defragmentation for directory(/mnt/games)",
            "[1/3]/mnt/games/a: 100%  extents: 4 -> 1\t[ OK ]",
            "[2/3]/mnt/games/b: 100%  extents: 2 -> 1\t[ OK ]",
            "[3/3]/mnt/games/c:\tFailed to defrag",
            "\tSuccess:\t\t\t[ 2/3 ]",
            "\tFailure:\t\t\t[ 1/3 ]"]
        self.tools.codes["e4defrag"] = 1
        result = self.do("defrag", "sdb1")
        self.assertEqual(self.progress(), [(1, 3), (2, 3), (3, 3)])
        # Files in use are skipped on a live filesystem; that is not failure.
        self.assertEqual((result["files"], result["skipped"]), (2, 1))

    def test_a_run_that_moved_nothing_and_failed_is_a_failure(self):
        self.tools.lines["e4defrag"] = ["e4defrag: Operation not supported"]
        self.tools.codes["e4defrag"] = 1
        self.assertIn("Operation not supported", self.no("defrag", "sdb1"))

    def test_stopping_says_nothing_was_lost(self):
        self.tools.lines["e4defrag"] = ["[1/9]/a: 100% [ OK ]"] * 5
        self.tools.stop_after["e4defrag"] = 2
        self.helper.cancel.set()
        with self.assertRaises(self.module.Cancelled) as caught:
            self.do("defrag", "sdb1")
        self.assertIn("nothing was lost", str(caught.exception))

    def test_an_unmounted_volume_is_not_defragmented(self):
        self.tools.contents["sdd1"] = {"TYPE": "ext4"}
        self.assertIn("not mounted", self.no("defrag", "sdd1"))


class WipeFreeSpaceTest(MaintainCase):
    """Filling a real directory, with the amount of free space made up."""

    def setUp(self):
        super().setUp()
        self.point = os.path.join(self.root, "mnt")
        os.mkdir(self.point)
        with open(os.path.join(self.point, "keep.txt"), "w") as handle:
            handle.write("mine")
        self.tree.unmount("/mnt/games")
        self.tree.mount("sdb1", self.point)
        self.module.CHUNK = 64 * 1024
        self.module.WIPE_FILE = 256 * 1024
        self.module.SYNC_EVERY = 128 * 1024
        self.capacity = 1024 * 1024          # what is free to begin with
        self.size = 100 * GIB
        self.helper._free_bytes = self.free
        self.seen = set()
        self.most_files = 0
        self.real_write = os.write

    def used(self):
        total = 0
        for base, _dirs, files in os.walk(self.point):
            if base != self.point:
                self.most_files = max(self.most_files, len(files))
                total += sum(os.path.getsize(os.path.join(base, name))
                             for name in files)
        return total

    def free(self, _path):
        return self.capacity - self.used(), self.size

    def write(self, fd, data):
        """os.write on a filesystem with `capacity` bytes of room."""
        room = self.capacity - self.used()
        if len(data) > room:
            raise OSError(errno.ENOSPC, "No space left on device")
        self.seen.update(bytes(data[:64]))
        return self.real_write(fd, data)

    def wipe(self, name="sdb1", pattern="zero"):
        with mock.patch.object(self.module.os, "write", self.write):
            return self.do("wipe_free", name, pattern=pattern)

    def leftovers(self):
        return [n for n in os.listdir(self.point) if n != "keep.txt"]

    def test_everything_free_is_written_and_then_given_back(self):
        result = self.wipe()
        # To the last block: what did not fit as a chunk went in as blocks.
        self.assertEqual(result["written"], self.capacity)
        self.assertEqual(self.seen, {0})
        self.assertGreater(self.most_files, 1)     # several files, not one
        self.assertEqual(self.leftovers(), [])
        with open(os.path.join(self.point, "keep.txt")) as handle:
            self.assertEqual(handle.read(), "mine")
        self.assertEqual(self.progress()[-1], (self.capacity, self.capacity))
        keys = [e["key"] for e in self.events() if e.get("event") == "step"]
        self.assertEqual(keys, ["fill", "remove"])

    def test_ones_and_random_are_what_they_say(self):
        self.assertEqual(self.wipe(pattern="one")["pattern"], "one")
        self.assertEqual(self.seen, {255})
        self.seen.clear()
        self.wipe(pattern="random")
        self.assertGreater(len(self.seen), 100)

    def test_a_compressing_filesystem_gets_random_data_whatever_was_asked(self):
        """Zeros compress to nothing: the volume would never fill and the
        free blocks would never be written."""
        self.tree.unmount(self.point)
        self.tree.mount("sdb1", self.point, "btrfs",
                        "rw,relatime,compress=zstd:1")
        self.module.WIPE_RESERVE_BTRFS = (4096, 0.0)
        result = self.wipe(pattern="zero")
        self.assertEqual(result["pattern"], "random")
        self.assertTrue(result["forced"])
        self.assertGreater(len(self.seen), 100)

    def test_btrfs_is_never_filled_to_the_last_block(self):
        self.tree.unmount(self.point)
        self.tree.mount("sdb1", self.point, "btrfs")
        self.module.WIPE_RESERVE_BTRFS = (200 * 1024, 0.0)
        result = self.wipe(pattern="random")
        self.assertEqual(result["reserve"], 200 * 1024)
        self.assertLessEqual(result["written"], self.capacity - 200 * 1024)
        self.assertGreater(result["written"], self.capacity - 300 * 1024)

    def test_room_is_left_on_a_volume_the_system_runs_from(self):
        # Mounted twice: at a system location, and where the test can write.
        self.tree.unmount("/")
        self.tree.mount("nvme0n1p2", "/var/lib/" + "deep/" * 30)
        self.tree.mount("nvme0n1p2", self.point)
        self.module.WIPE_RESERVE = (300 * 1024, 0.0)
        result = self.wipe("nvme0n1p2")
        self.assertEqual(result["reserve"], 300 * 1024)
        self.assertLessEqual(result["written"], self.capacity - 300 * 1024)
        self.assertEqual(self.leftovers(), [])

    def test_too_little_room_to_leave_any_is_refused_before_writing(self):
        self.tree.unmount("/")
        self.tree.mount("nvme0n1p2", "/var/lib/" + "deep/" * 30)
        self.tree.mount("nvme0n1p2", self.point)
        self.capacity = 100 * 1024
        with mock.patch.object(self.module.os, "write", self.write):
            text = self.no("wipe_free", "nvme0n1p2", pattern="zero")
        self.assertIn("too little free space", text)
        self.assertEqual(self.leftovers(), [])

    def test_stopping_removes_what_was_written(self):
        calls = []

        def write(fd, data):
            calls.append(1)
            if len(calls) == 3:
                self.helper.cancel.set()
            return self.write(fd, data)

        with mock.patch.object(self.module.os, "write", write):
            with self.assertRaises(self.module.Cancelled):
                self.do("wipe_free", "sdb1", pattern="zero")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.helper.fill_dirs, [])

    def test_a_write_error_removes_what_was_written_and_names_the_drive(self):
        def write(fd, data):
            if self.used() >= 128 * 1024:
                raise OSError(errno.EIO, "Input/output error")
            return self.write(fd, data)

        with mock.patch.object(self.module.os, "write", write):
            text = self.no("wipe_free", "sdb1", pattern="zero")
        self.assertIn("Input/output error", text)
        self.assertIn("may be failing", text)
        self.assertEqual(self.leftovers(), [])

    def test_what_a_power_cut_left_behind_is_found_and_removed(self):
        stale = os.path.join(self.point, self.module.WIPE_PREFIX + "4242")
        os.mkdir(stale)
        with open(os.path.join(stale, "000001"), "wb") as handle:
            handle.write(b"\0" * 4096)
        other = os.path.join(self.point, "my-folder")
        os.mkdir(other)
        result = self.do("wipe_cleanup", "sdb1")
        self.assertEqual(result["removed"], 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.isdir(other))
        self.assertEqual(self.do("wipe_cleanup", "sdb1")["removed"], 0)

    def test_the_helper_going_away_takes_its_files_with_it(self):
        stale = os.path.join(self.point, self.module.WIPE_PREFIX + "1")
        os.mkdir(stale)
        with open(os.path.join(stale, "000001"), "wb") as handle:
            handle.write(b"x")
        self.helper.fill_dirs.append(stale)
        self.helper.cleanup()
        self.assertFalse(os.path.exists(stale))

    def test_an_unknown_pattern_or_an_unmounted_volume_is_refused(self):
        self.assertIn("Unknown pattern",
                      self.no("wipe_free", "sdb1", pattern="sevens"))
        self.assertIn("not mounted",
                      self.no("wipe_free", "sdd1", pattern="zero"))


class CheckTest(MaintainCase):
    def setUp(self):
        super().setUp()
        self.tools.contents["sdd1"] = {"TYPE": "ext4"}

    def steps(self):
        return [e["key"] for e in self.events() if e.get("event") == "step"]

    def test_a_clean_filesystem(self):
        self.tools.lines["e2fsck"] = ["Pass 1: Checking inodes",
                                      "/dev/sdd1: 11/65536 files"]
        result = self.do("check", "sdd1")
        self.assertEqual(result["state"], "clean")
        self.assertIn("Pass 1", result["output"])
        self.assertEqual(self.tools.of("e2fsck")[0][:2], ["-f", "-n"])
        self.assertEqual(self.steps(), ["check"])
        self.assertFalse(result["was_mounted"])

    def test_errors_are_reported_and_nothing_is_changed(self):
        self.tools.codes["e2fsck"] = 4
        result = self.do("check", "sdd1")
        self.assertEqual(result["state"], "errors")
        self.assertEqual(len(self.tools.of("e2fsck")), 1)

    def test_a_repair_is_judged_by_checking_again(self):
        self.tools.codes["e2fsck"] = [4, 1, 0]
        result = self.do("check", "sdd1", repair=True)
        self.assertEqual(result["state"], "repaired")
        calls = self.tools.of("e2fsck")
        self.assertEqual([c[:2] for c in calls],
                         [["-f", "-n"], ["-f", "-y"], ["-f", "-n"]])
        self.assertEqual(self.steps(), ["check", "repair", "verify"])
        self.assertIn("After the repair", result["output"])

    def test_a_repair_that_did_not_take_says_so(self):
        """The repairing tool exiting 0 is not the proof."""
        self.tools.codes["e2fsck"] = [4, 0, 4]
        self.assertEqual(self.do("check", "sdd1", repair=True)["state"],
                         "unrepaired")

    def test_nothing_is_repaired_that_is_not_broken(self):
        result = self.do("check", "sdd1", repair=True)
        self.assertEqual(result["state"], "clean")
        self.assertEqual(len(self.tools.of("e2fsck")), 1)

    def test_a_tool_that_could_not_run_is_not_a_verdict(self):
        self.tools.codes["e2fsck"] = 8
        self.tools.lines["e2fsck"] = ["e2fsck: Bad magic number"]
        self.assertIn("could not be carried out", self.no("check", "sdd1"))

    def test_a_mounted_volume_is_unmounted_first_and_that_is_reported(self):
        result = self.do("check", "sdb1")
        self.assertEqual(self.tools.names()[:5].count("umount"), 1)
        self.assertLess(self.tools.names().index("umount"),
                        self.tools.names().index("e2fsck"))
        self.assertTrue(result["was_mounted"])
        self.assertEqual(self.steps(), ["unmount", "check"])

    def test_the_running_system_is_never_checked(self):
        self.tools.contents["nvme0n1p2"] = {"TYPE": "ext4"}
        self.assertIn("running system", self.no("check", "nvme0n1p2"))
        self.assertEqual(self.tools.of("e2fsck"), [])

    def test_each_filesystem_has_its_tool(self):
        for fstype, tool, args in (
                ("vfat", "fsck.fat", ["-n"]), ("exfat", "fsck.exfat", ["-n"]),
                ("ntfs", "ntfsfix", ["--no-action"]),
                ("xfs", "xfs_repair", ["-n"]),
                ("btrfs", "btrfs", ["check", "--readonly"]),
                ("f2fs", "fsck.f2fs", ["--dry-run"]),
                ("ext3", "e2fsck", ["-f", "-n"])):
            self.tools.calls.clear()
            self.tools.contents["sdd1"] = {"TYPE": fstype}
            self.do("check", "sdd1")
            self.assertEqual(self.tools.of(tool)[0][:len(args)], args, fstype)

    def test_btrfs_is_checked_but_never_repaired(self):
        self.tools.contents["sdd1"] = {"TYPE": "btrfs"}
        text = self.no("check", "sdd1", repair=True)
        self.assertIn("not offered", text)
        self.assertEqual(self.tools.of("btrfs"), [])

    def test_ntfs_says_where_a_full_repair_comes_from(self):
        self.tools.contents["sdd1"] = {"TYPE": "ntfs"}
        self.assertIn("chkdsk", self.do("check", "sdd1")["note"])

    def test_a_hibernated_windows_volume_is_not_repaired(self):
        self.tools.contents["sdd1"] = {"TYPE": "ntfs"}
        self.tools.codes["ntfs-3g.probe"] = 14
        text = self.no("check", "sdd1", repair=True)
        self.assertIn("fast startup", text)
        self.assertIn("Shift", text)
        self.assertEqual(self.tools.of("ntfsfix"), [])
        # Looking is still allowed.
        self.tools.codes["ntfs-3g.probe"] = 14
        self.assertEqual(self.do("check", "sdd1")["state"], "clean")

    def test_an_unclean_windows_volume_says_to_let_windows_check_it(self):
        self.tools.contents["sdd1"] = {"TYPE": "ntfs"}
        self.tools.codes["ntfs-3g.probe"] = 15
        self.assertIn("chkdsk", self.no("check", "sdd1", repair=True))

    def test_without_the_probe_the_tools_own_check_is_relied_on(self):
        self.tools.contents["sdd1"] = {"TYPE": "ntfs"}
        self.tools.missing.add("ntfs-3g.probe")
        self.tools.codes["ntfsfix"] = [1, 0, 0]
        self.assertEqual(self.do("check", "sdd1", repair=True)["state"],
                         "repaired")

    def test_a_blank_partition_has_nothing_to_check(self):
        self.tools.contents.pop("sdd1")
        self.assertIn("no filesystem", self.no("check", "sdd1"))

    def test_a_check_can_be_stopped_and_says_nothing_changed(self):
        self.tools.lines["e2fsck"] = ["Pass 1", "Pass 2", "Pass 3"]
        self.tools.stop_after["e2fsck"] = 1
        self.helper.cancel.set()
        with self.assertRaises(self.module.Cancelled) as caught:
            self.do("check", "sdd1")
        self.assertIn("changes nothing", str(caught.exception))


class StopMessageTest(MaintainCase):
    def test_the_reply_carries_what_the_operation_said_about_stopping(self):
        self.tools.contents["sdd1"] = {"TYPE": "ext4"}
        self.tools.lines["e2fsck"] = ["Pass 1", "Pass 2"]
        self.tools.stop_after["e2fsck"] = 1
        original = self.helper.step

        def step(*args, **kwargs):
            original(*args, **kwargs)
            self.helper.cancel.set()

        self.helper.step = step
        self.helper.run_job(self.ask("check", "sdd1"))
        reply = self.events()[-1]
        self.assertTrue(reply["cancelled"])
        self.assertIn("changes nothing", reply["error"])


class AvailabilityTest(unittest.TestCase):
    """What is offered where, and what the greyed-out entry says."""

    def setUp(self):
        self.machine = machine()

    def answer(self, action, disk, volume, tools=everything):
        disk = self.machine[disk]
        return diskops.availability(action, disk,
                                    disk.volume(volume) if volume else None,
                                    tools=tools)

    def test_every_action_wants_a_volume(self):
        for action in diskops.MAINTENANCE:
            answer = self.answer(action, "sdb", None)
            self.assertFalse(answer)
            self.assertIn("Select a volume", answer.reason)

    def test_what_is_safe_on_the_running_system_is_offered_there(self):
        for action in (diskops.TRIM, diskops.DEFRAG, diskops.WIPE_FREE):
            self.assertTrue(self.answer(action, "nvme0n1", "nvme0n1p3"),
                            action)
        self.assertTrue(self.answer(diskops.ANALYSE, "nvme0n1", "nvme0n1p2"))

    def test_the_running_system_is_not_checked_and_says_why(self):
        answer = self.answer(diskops.CHECK, "nvme0n1", "nvme0n1p3")
        self.assertFalse(answer)
        self.assertIn("running system", answer.reason)
        self.assertIn("when it starts", answer.reason)

    def test_trim_needs_a_drive_that_takes_it(self):
        answer = self.answer(diskops.TRIM, "sda", "sda1")
        self.assertFalse(answer)
        self.assertIn("does not accept TRIM", answer.reason)
        self.assertTrue(self.answer(diskops.TRIM, "sdb", "sdb1"))

    def test_an_unmounted_volume_says_to_mount_it(self):
        for action in (diskops.TRIM, diskops.WIPE_FREE):
            answer = self.answer(action, "sdc", "sdc1")
            self.assertFalse(answer)
            self.assertIn("Mount the volume first", answer.reason)

    def test_windows_filesystems_say_they_cannot_be_defragmented_here(self):
        for disk, volume in (("sdc", "sdc1"), ("sde", "sde1")):
            for action in (diskops.ANALYSE, diskops.DEFRAG):
                answer = self.answer(action, disk, volume)
                self.assertFalse(answer)
                self.assertIn("cannot be defragmented from Linux",
                              answer.reason)
                self.assertIn("Windows", answer.reason)

    def test_btrfs_is_defragmented_without_a_measurement(self):
        self.assertFalse(self.answer(diskops.ANALYSE, "nvme0n1",
                                     "nvme0n1p3"))
        self.assertTrue(self.answer(diskops.DEFRAG, "nvme0n1", "nvme0n1p3"))

    def test_a_missing_tool_names_the_package(self):
        answer = self.answer(diskops.CHECK, "sdc", "sdc1", tools=nothing)
        self.assertFalse(answer)
        self.assertIn("ntfsfix", answer.reason)
        self.assertIn("dnf install ntfsprogs", answer.reason)
        self.assertIn("apt install ntfs-3g", answer.reason)

    def test_an_unmounted_volume_can_be_checked(self):
        self.assertTrue(self.answer(diskops.CHECK, "sdc", "sdc1"))
        self.assertTrue(self.answer(diskops.CHECK, "sdb", "sdb1"))

    def test_a_partition_with_nothing_on_it_has_nothing_to_look_after(self):
        for action in diskops.MAINTENANCE:
            answer = self.answer(action, "nvme1n1", "nvme1n1p2")
            self.assertFalse(answer)
            self.assertIn("no filesystem", answer.reason)

    def test_optimise_picks_by_the_kind_of_drive(self):
        ssd = self.machine["sdb"]
        action, answer = diskmaintain.optimise(ssd, ssd.volumes[0], everything)
        self.assertEqual(action, diskops.TRIM)
        hdd = self.machine["sda"]
        action, answer = diskmaintain.optimise(hdd, hdd.volumes[0], everything)
        self.assertEqual(action, diskops.ANALYSE)
        action, answer = diskmaintain.optimise(hdd, hdd.volumes[1], everything)
        self.assertIsNone(action)
        self.assertIn("cannot be defragmented", answer.reason)


class WindowsOnlyVolumesTest(unittest.TestCase):
    """Volumes only Windows can work on: said so, with what to do."""

    def volume(self, **fields):
        base = dict(name="sdx1", path="/dev/sdx1", disk="sdx", kind="part",
                    size=100 * GIB)
        base.update(fields)
        return disks.Volume(**base)

    def disk(self, volume):
        return disks.Disk("sdx", "/dev/sdx", 200 * GIB, discard=True,
                          volumes=(volume,))

    def test_bitlocker_says_how_to_turn_it_off(self):
        volume = self.volume(fstype="BitLocker")
        state = diskfs.windows_state(volume)
        self.assertIn("BitLocker", state.title)
        self.assertIn("Device encryption", state.text)
        for action in diskops.MAINTENANCE:
            answer = diskops.availability(action, self.disk(volume), volume,
                                          tools=everything)
            self.assertFalse(answer)
            self.assertIn("turn encryption off", answer.reason)

    def test_dynamic_disks_and_storage_spaces_point_at_windows(self):
        for parttype, word in (
                ("af9b60a0-1431-4f62-bc68-3311714a69ad", "Disk Management"),
                ("0x42", "Disk Management"),
                ("E75CAF8F-F680-4CEE-AFA3-B001E56EFC2D", "Storage Spaces")):
            volume = self.volume(fstype="ntfs", parttype=parttype)
            self.assertIn(word, diskfs.windows_state(volume).text)
            answer = diskops.availability(diskops.CHECK, self.disk(volume),
                                          volume, tools=everything)
            self.assertIn(word, answer.reason)

    def test_an_ordinary_windows_volume_is_just_a_volume(self):
        volume = self.volume(
            fstype="ntfs", parttype="ebd0a0a2-b9e5-4433-87c0-68b6b72699c7")
        self.assertIsNone(diskfs.windows_state(volume))
        self.assertIsNone(diskfs.windows_state(None))

    def test_the_hibernation_text_is_the_same_on_both_sides(self):
        """The helper writes its own copy; it has to say what to do."""
        from disktree import load_helper
        helper = load_helper()
        for text in (helper.NTFS_HIBERNATED, helper.NTFS_UNCLEAN,
                     helper.NTFS_DAMAGED):
            self.assertIn("What to do", text)
            self.assertIn("Windows", text)


class TextTest(unittest.TestCase):
    def setUp(self):
        self.machine = machine()

    def test_how_a_volume_is_trimmed_is_said_in_one_line(self):
        ssd, hdd = self.machine["sdb"], self.machine["sda"]
        volume = ssd.volumes[0]
        state = diskmaintain.trim_state
        self.assertIn("does not accept", state(hdd, hdd.volumes[0], True))
        self.assertIn("Weekly", state(ssd, volume, True, set()))
        self.assertIn("Continuous", state(ssd, volume, False,
                                          {"rw", "discard=async"}))
        both = state(ssd, volume, True, {"discard"})
        self.assertIn("Continuous", both)
        self.assertIn("weekly", both)
        self.assertIn("Not automatic", state(ssd, volume, False, {"rw"}))
        self.assertIn("switched off", state(ssd, volume, False, {"rw"}))
        self.assertNotIn("switched off", state(ssd, volume, None, {"rw"}))
        unmounted = self.machine["sdc"]
        self.assertIn("while mounted", state(unmounted, unmounted.volumes[0],
                                             True))

    def test_mount_options_are_read_per_mount_point(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".info") as handle:
            handle.write(
                "30 1 0:31 /root / rw,relatime shared:1 - btrfs /dev/x "
                "rw,compress=zstd:1,ssd,discard=async\n"
                "40 1 8:1 / /mnt/my\\040disk rw,noatime - ext4 /dev/sdb1 "
                "rw\n")
            handle.flush()
            root = diskmaintain.mount_options("/", handle.name)
            self.assertIn("discard=async", root)
            self.assertIn("compress=zstd:1", root)
            self.assertEqual(
                diskmaintain.mount_options("/mnt/my disk", handle.name),
                {"rw", "noatime"})
            self.assertEqual(diskmaintain.mount_options("/nope", handle.name),
                             set())

    def test_the_timer_is_on_off_or_unknown(self):
        for said, want in (("enabled", True), ("disabled", False),
                           ("masked", False), ("static", None), ("", None),
                           ("not-found", None)):
            self.assertIs(diskmaintain.timer_enabled(lambda *a: said), want)

        def broken(*_args):
            raise OSError("no systemctl")
        self.assertIsNone(diskmaintain.timer_enabled(broken))

    def test_a_measurement_is_put_into_words_by_its_verdict(self):
        hdd, ssd = self.machine["sda"], self.machine["sdb"]
        result = {"score": 4, "low": 30, "high": 55, "verdict": "none"}
        advice = diskmaintain.fragmentation_advice(result, hdd)
        self.assertEqual(advice.title, "Defragmenting is not needed")
        self.assertIn("Score 4", advice.text)
        self.assertIn("up to 30", advice.text)
        advice = diskmaintain.fragmentation_advice(
            dict(result, score=40, verdict="some"), hdd)
        self.assertIn("optional", advice.title)
        advice = diskmaintain.fragmentation_advice(
            dict(result, score=70, verdict="needed"), hdd)
        self.assertIn("worth it", advice.title)
        # Even a fragmented SSD is not told to defragment.
        advice = diskmaintain.fragmentation_advice(
            dict(result, score=70, verdict="needed"), ssd)
        self.assertNotIn("worth it", advice.title)
        self.assertIn("solid-state", advice.title)
        advice = diskmaintain.fragmentation_advice(
            {"verdict": "unrated", "factor": 2.6, "extents": [5123, 4990]},
            hdd)
        self.assertIn("XFS", advice.title)
        self.assertIn("5123", advice.text)

    def test_the_warnings_in_front_of_a_defragmentation(self):
        ssd, hdd = self.machine["sdb"], self.machine["sda"]
        system = self.machine["nvme0n1"]
        self.assertEqual(diskmaintain.defrag_warnings(hdd, hdd.volumes[0]), [])
        found = diskmaintain.defrag_warnings(ssd, ssd.volumes[0])
        self.assertEqual([a.level for a in found], [diskfs.WARNING])
        self.assertIn("nothing gained", found[0].text)
        found = diskmaintain.defrag_warnings(system, system.volumes[2])
        self.assertEqual(len(found), 2)
        self.assertIn("snapshots", found[1].text)

    def test_what_is_said_before_wiping_free_space(self):
        ssd, hdd = self.machine["sdb"], self.machine["sda"]
        system = self.machine["nvme0n1"]
        titles = lambda disk, volume, options=frozenset(): [  # noqa: E731
            a.title for a in diskmaintain.wipe_advice(disk, volume,
                                                      set(options))]
        self.assertEqual(titles(hdd, hdd.volumes[0]),
                         ["What this does not reach"])
        self.assertIn("On a solid-state drive, TRIM does this better",
                      titles(ssd, ssd.volumes[0]))
        found = titles(system, system.volumes[2])
        self.assertIn("Random data will be used", found)
        self.assertIn("A little room is left free", found)
        self.assertIn("Random data will be used",
                      titles(hdd, hdd.volumes[0], {"compress_algorithm=lz4"}))

    def test_the_requests_name_the_volume_and_what_was_seen_of_it(self):
        ssd = self.machine["sdb"]
        volume = ssd.volumes[0]
        seen = {"name": "sdb1"}
        for plan, op in (
                (diskmaintain.plan_trim(ssd, volume, seen), "trim"),
                (diskmaintain.plan_analyse(ssd, volume, seen),
                 "defrag_analyse"),
                (diskmaintain.plan_defrag(ssd, volume, seen), "defrag"),
                (diskmaintain.plan_wipe(ssd, volume, "one", seen),
                 "wipe_free"),
                (diskmaintain.plan_wipe_cleanup(ssd, volume, seen),
                 "wipe_cleanup"),
                (diskmaintain.plan_check(ssd, volume, identity=seen),
                 "check")):
            self.assertEqual(plan.request["op"], op)
            self.assertEqual(plan.request["device"], "/dev/sdb1")
            self.assertIs(plan.request["identity"], seen)
        self.assertEqual(diskmaintain.plan_timer(False).request,
                         {"op": "trim_timer", "enable": False})
        with self.assertRaises(ValueError):
            diskmaintain.plan_wipe(ssd, volume, "sevens", seen)

    def test_a_check_of_a_mounted_volume_lists_the_unmount(self):
        ssd = self.machine["sdb"]
        plan = diskmaintain.plan_check(ssd, ssd.volumes[0], identity={})
        self.assertEqual(plan.step_keys, ["unmount", "check"])
        plan = diskmaintain.plan_check(ssd, ssd.volumes[0], repair=True,
                                       identity={})
        self.assertEqual(plan.step_keys,
                         ["unmount", "check", "repair", "verify"])
        self.assertEqual(plan.advice[0].level, diskfs.WARNING)
        self.assertTrue(plan.request["repair"])

    def test_the_plans_steps_are_the_ones_the_helper_announces(self):
        """What the dialog lists and what runs must be the same sequence."""
        tree_case = CheckTest("test_a_clean_filesystem")
        tree_case.setUp()
        self.addCleanup(tree_case.doCleanups)
        tree_case.tools.codes["e2fsck"] = [4, 1, 0]
        tree_case.do("check", "sdb1", repair=True)
        ssd = self.machine["sdb"]
        plan = diskmaintain.plan_check(ssd, ssd.volumes[0], repair=True,
                                       identity={})
        self.assertEqual(tree_case.steps(), plan.step_keys)

    def test_a_checks_result_in_words(self):
        title, text, repairable = diskmaintain.check_summary(
            {"state": "clean", "fstype": "ext4"})
        self.assertEqual((title, repairable), ("No errors found", False))
        title, text, repairable = diskmaintain.check_summary(
            {"state": "errors", "fstype": "ext4"})
        self.assertTrue(repairable)
        self.assertIn("Nothing has been changed", text)
        title, text, repairable = diskmaintain.check_summary(
            {"state": "errors", "fstype": "btrfs"})
        self.assertFalse(repairable)
        self.assertIn("not offered", text)
        title, text, repairable = diskmaintain.check_summary(
            {"state": "unrepaired", "fstype": "ntfs", "note": "run chkdsk"})
        self.assertFalse(repairable)
        self.assertIn("run chkdsk", text)
        self.assertIn("Copy what you can", text)

    def test_results_in_one_line(self):
        self.assertIn("1.3 GB", diskmaintain.trim_done_text(
            {"trimmed": 1288490188, "mountpoint": "/mnt/x"}))
        self.assertIn("already knew", diskmaintain.trim_done_text(
            {"trimmed": 0, "mountpoint": "/"}))
        self.assertIn("2 files done, 1 skipped",
                      diskmaintain.defrag_done_text({"files": 2,
                                                     "skipped": 1}))
        text = diskmaintain.wipe_done_text(
            {"written": 5 * GIB, "forced": True, "reserve": 512 * MIB})
        self.assertIn("random data", text)
        self.assertIn("left free on purpose", text)

    def test_leftover_fill_directories_are_found_by_their_name(self):
        volume = self.machine["sdb"].volumes[0]
        listing = {"/mnt/giochi": ["Games", ".ksyssupervisor-wipe-77",
                                   ".Trash-1000"]}
        self.assertEqual(
            diskmaintain.leftovers(volume, lambda p: listing[p]),
            ["/mnt/giochi/.ksyssupervisor-wipe-77"])

        def unreadable(_path):
            raise OSError("denied")
        self.assertEqual(diskmaintain.leftovers(volume, unreadable), [])

    def test_the_prefix_is_the_helpers(self):
        from disktree import load_helper
        self.assertEqual(diskmaintain.WIPE_PREFIX, load_helper().WIPE_PREFIX)


class SelfTestHealthTest(unittest.TestCase):
    def test_a_failed_self_test_is_a_caution_and_a_stopped_one_is_not(self):
        for outcome, cautious in (("success", False), ("aborted", False),
                                  ("interrupted", False), ("fatal", True),
                                  ("error_read", True),
                                  ("error_electrical", True)):
            health = diskhealth.from_ata(
                {"SmartSupported": True, "SmartEnabled": True,
                 "SmartFailing": False, "SmartUpdated": 1700000000,
                 "SmartSelftestStatus": outcome}, [])
            self.assertEqual(health.status == diskhealth.CAUTION, cautious,
                             outcome)
        self.assertIn("electrical fault", health.reasons[0])

    def test_a_running_test_says_how_far_it_has_to_go(self):
        health = diskhealth.Health(selftest="inprogress")
        diskhealth._with_selftest(health, "/drive", udisks.ATA,
                                  {"SmartSelftestPercentRemaining": 70})
        self.assertEqual(health.selftest_remaining, 70)
        self.assertEqual(diskhealth.selftest_text(health),
                         "Running, 70% to go")
        self.assertEqual((health.drive_path, health.interface),
                         ("/drive", udisks.ATA))
        done = diskhealth.Health(selftest="success")
        diskhealth._with_selftest(done, "/drive", udisks.NVME,
                                  {"SmartSelftestPercentRemaining": -1})
        self.assertIsNone(done.selftest_remaining)
        self.assertEqual(diskhealth.selftest_text(done), "Passed")
        self.assertEqual(diskhealth.selftest_text(diskhealth.Health()), "")

    def test_starting_and_stopping_go_to_the_drives_own_interface(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return 0, "", ""
        udisks.selftest_start("/drive", udisks.ATA, "short", run=run)
        self.assertEqual(calls[0][-6:], [
            "/drive", udisks.ATA, "SmartSelftestStart", "sa{sv}", "short",
            "0"])
        udisks.selftest_abort("/drive", udisks.NVME, run=run)
        self.assertEqual(calls[1][-4:], [udisks.NVME, "SmartSelftestAbort",
                                         "a{sv}", "0"])
        with self.assertRaises(udisks.UDisksError):
            udisks.selftest_start("/drive", udisks.ATA, "thorough", run=run)
        with self.assertRaises(udisks.UDisksError):
            udisks.selftest_start("/drive", "", "short", run=run)


class HealthWatchTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "state", "disk-health.json")
        self.drives = [disks.Disk("sda", "/dev/sda", 100 * GIB, serial="S1",
                                  model="Old Disk"),
                       disks.Disk("sdb", "/dev/sdb", 100 * GIB, serial="S2",
                                  model="New Disk"),
                       disks.Disk("sdd", "/dev/sdd", 0, model="Reader")]
        self.health = {"sda": diskhealth.Health(status=diskhealth.GOOD),
                       "sdb": diskhealth.Health(status=diskhealth.GOOD,
                                                life_left=93)}
        self.sent = []

    def watch(self):
        return diskhealth.HealthWatch(
            inventory=lambda: self.drives,
            read=lambda targets: {d.name: self.health[d.name]
                                  for d in targets},
            notify=lambda title, text, critical=False:
            self.sent.append((title, text, critical)),
            path=self.path)

    def test_healthy_drives_say_nothing(self):
        watch = self.watch()
        watch.poll()
        self.assertEqual(self.sent, [])
        self.assertEqual(sorted(watch.snapshot()), ["sda", "sdb"])

    def test_a_drive_getting_worse_is_announced_once(self):
        watch = self.watch()
        watch.poll()
        self.health["sda"] = diskhealth.Health(
            status=diskhealth.CAUTION,
            reasons=["3 sectors went bad and were replaced by spares."])
        watch.poll()
        watch.poll()
        self.assertEqual(len(self.sent), 1)
        title, text, critical = self.sent[0]
        self.assertEqual(title, "Drive health: Caution")
        self.assertIn("Old Disk (sda)", text)
        self.assertIn("3 sectors", text)
        self.assertFalse(critical)

        self.health["sda"] = diskhealth.Health(
            status=diskhealth.BAD, reasons=["The drive reports failing."])
        watch.poll()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1][2])
        self.assertIn("copy what matters", self.sent[1][1])

    def test_what_was_announced_is_remembered_across_runs(self):
        self.health["sda"] = diskhealth.Health(status=diskhealth.CAUTION,
                                               reasons=["One bad sector."])
        self.watch().poll()
        self.assertEqual(len(self.sent), 1)
        self.watch().poll()                  # the next start of the program
        self.assertEqual(len(self.sent), 1)

    def test_a_drive_that_recovered_is_announced_again_if_it_relapses(self):
        self.health["sda"] = diskhealth.Health(status=diskhealth.CAUTION)
        watch = self.watch()
        watch.poll()
        self.health["sda"] = diskhealth.Health(status=diskhealth.GOOD)
        watch.poll()
        self.health["sda"] = diskhealth.Health(status=diskhealth.CAUTION)
        watch.poll()
        self.assertEqual(len(self.sent), 2)

    def test_no_data_is_not_a_recovery(self):
        self.health["sda"] = diskhealth.Health(status=diskhealth.CAUTION)
        watch = self.watch()
        watch.poll()
        self.health["sda"] = diskhealth.unknown("asleep")
        watch.poll()
        self.health["sda"] = diskhealth.Health(status=diskhealth.CAUTION)
        watch.poll()
        self.assertEqual(len(self.sent), 1)

    def test_an_unwritable_state_file_does_not_stop_the_watch(self):
        self.path = "/proc/nonexistent/x.json"
        self.health["sda"] = diskhealth.Health(status=diskhealth.BAD)
        watch = self.watch()
        watch.poll()
        self.assertEqual(len(self.sent), 1)

    def test_life_left_reaches_the_sensor_list(self):
        from ksyssupervisor.providers import storage
        from ksyssupervisor.providers.base import ReadContext
        watch = self.watch()
        watch.poll()
        provider = storage.StorageProvider()
        provider._drives = [("disk:sdb", "New Disk", "/nonexistent"),
                            ("disk:sda", "Old Disk", "/nonexistent")]
        with mock.patch.object(storage, "HEALTH", watch), \
                mock.patch.object(storage, "volumes_by_disk", lambda: {}):
            readings = provider.read(ReadContext({}, {}, "sysfs"))
        self.assertEqual([(r.device, r.label, r.value, r.kind)
                          for r in readings],
                         [("disk:sdb", "Life Left", 93.0, "utilization")])
        with mock.patch.object(storage, "volumes_by_disk", lambda: {}):
            self.assertEqual(provider.read(ReadContext({}, {}, "sysfs")), [])


class MaintainMenuTest(DiskWindowCase):
    """The menu, and each flow from the click to the helper and back."""

    def tip_of(self, action):
        return action.toolTip()

    def test_entries_are_greyed_with_the_reason_never_hidden(self):
        window = self.window()
        window.select_device("sda2")         # NTFS on a hard disk, unmounted
        self.assertFalse(window.defrag_action.isEnabled())
        self.assertIn("cannot be defragmented from Linux",
                      self.tip_of(window.defrag_action))
        self.assertFalse(window.trim_action.isEnabled())
        self.assertIn("does not accept TRIM", self.tip_of(window.trim_action))
        self.assertFalse(window.wipe_action.isEnabled())
        self.assertIn("Mount the volume first",
                      self.tip_of(window.wipe_action))
        self.assertTrue(window.defrag_action.isVisible()
                        or not window.isVisible())

    def test_the_system_volume_offers_what_is_safe_there(self):
        window = self.window()
        window.select_device("nvme0n1p3")
        self.assertTrue(window.trim_action.isEnabled())
        self.assertTrue(window.wipe_action.isEnabled())
        self.assertFalse(window.check_action.isEnabled())
        self.assertIn("running system", self.tip_of(window.check_action))

    def test_trim_goes_to_the_helper_and_reports_the_amount(self):
        window = self.window()
        window.select_device("sdb1")
        self.client.result = {"trimmed": 2 * GIB, "mountpoint": "/mnt/giochi"}
        window.trim_action.trigger()
        request = self.client.requests[-1]
        self.assertEqual((request["op"], request["device"]),
                         ("trim", "/dev/sdb1"))
        self.assertIn("2.1 GB", window.status.text())

    def test_optimise_trims_an_ssd_and_measures_a_hard_disk(self):
        window = self.window()
        window.select_device("sdb1")
        self.client.result = {"trimmed": 0}
        window.optimise_action.trigger()
        self.assertEqual(self.client.requests[-1]["op"], "trim")

        window.select_device("sda1")
        self.client.result = {"score": 2, "low": 30, "high": 55,
                              "verdict": "none", "fstype": "ext4"}
        window.optimise_action.trigger()
        self.assertEqual(self.client.requests[-1]["op"], "defrag_analyse")
        # The measurement is shown; declined, nothing else is asked.
        self.assertIsInstance(self.dialogs[-1], FragmentationDialog)
        self.assertEqual(self.dialogs[-1].verdict.title,
                         "Defragmenting is not needed")
        self.assertEqual(len(self.client.requests), 2)

    def test_the_weekly_trim_follows_the_system_and_is_switched_there(self):
        self.timer = True
        window = self.window()
        self.assertTrue(window.timer_action.isChecked())
        self.client.result = {"enabled": False}
        self.timer = False               # what the system says from now on
        window.timer_action.trigger()
        self.assertEqual(self.client.requests[-1],
                         {"op": "trim_timer", "enable": False})
        self.assertFalse(window.timer_action.isChecked())
        self.assertIn("now off", window.status.text())

    def test_no_timer_on_this_system_greys_the_entry(self):
        self.timer = None
        window = self.window()
        self.assertFalse(window.timer_action.isEnabled())
        self.assertIn("fstrim.timer", self.tip_of(window.timer_action))

    def test_how_the_volume_is_trimmed_is_among_the_details(self):
        window = self.window()
        window.select_device("sdb1")
        facts = dict(window.drive_grid.facts())
        self.assertIn("Weekly", facts["TRIM"])
        window.select_device("sda1")
        self.assertNotIn("TRIM", dict(window.drive_grid.facts()))

    def test_defragmenting_is_measured_first_then_asked(self):
        window = self.window()
        window.select_device("sda1")
        self.client.result = {"score": 70, "low": 30, "high": 55,
                              "verdict": "needed", "fstype": "ext4"}
        self.answers = [True]
        window.defrag_action.trigger()
        self.assertEqual([r["op"] for r in self.client.requests],
                         ["defrag_analyse", "defrag"])
        dialog = self.dialogs[0]
        self.assertTrue(dialog.understood.isHidden())      # a hard disk
        self.assertTrue(dialog.go.isEnabled())

    def test_an_ssd_gets_the_large_warning_and_a_tick(self):
        window = self.window()
        window.select_device("sdb1")
        self.client.result = {"score": 70, "low": 30, "high": 55,
                              "verdict": "needed", "fstype": "ext4"}
        window.defrag_action.trigger()
        dialog = self.dialogs[-1]
        self.assertEqual(dialog.warnings[0].title,
                         "Do not defragment a solid-state drive")
        self.assertFalse(dialog.understood.isHidden())
        self.assertFalse(dialog.go.isEnabled())
        dialog.understood.setChecked(True)
        self.assertTrue(dialog.go.isEnabled())
        self.assertEqual(len(self.client.requests), 1)     # only measured

    def test_btrfs_goes_straight_to_the_question(self):
        window = self.window()
        window.select_device("nvme0n1p3")
        window.defrag_action.trigger()
        self.assertEqual(self.client.requests, [])
        dialog = self.dialogs[-1]
        self.assertIsNone(dialog.verdict)
        self.assertEqual(len(dialog.warnings), 2)

    def test_wiping_free_space_asks_what_with(self):
        window = self.window()
        window.select_device("sda1")
        self.answers = [True]
        original = window.show_dialog

        def choose(dialog):
            dialog.patterns["random"].setChecked(True)
            dialog._accept()
            return original(dialog)
        window.show_dialog = choose
        self.client.result = {"written": 3 * GIB}
        window.wipe_action.trigger()
        request = self.client.requests[-1]
        self.assertEqual((request["op"], request["pattern"]),
                         ("wipe_free", "random"))
        self.assertIsInstance(self.dialogs[-1], WipeDialog)
        self.assertIn("3.2 GB", window.status.text())

    def test_a_cancelled_wipe_asks_nothing_of_the_helper(self):
        window = self.window()
        window.select_device("sda1")
        window.wipe_action.trigger()
        self.assertEqual(self.client.requests, [])

    def test_leftover_files_bring_up_the_entry_that_removes_them(self):
        window = self.window()
        window.select_device("sda1")
        self.assertFalse(window.leftover_action.isVisible())
        with mock.patch.object(window_mod.diskmaintain, "leftovers",
                               lambda volume, listdir=None: ["/mnt/x/.k"]):
            window._update_actions()
            self.assertTrue(window.leftover_action.isVisible())
            window.leftover_action.trigger()
        self.assertEqual(self.client.requests[-1]["op"], "wipe_cleanup")

    def test_a_clean_check_of_a_mounted_volume_mounts_it_again(self):
        window = self.window()
        window.select_device("sdb1")
        self.answers = [True, False]         # go ahead; close the result
        self.client.result = {"state": "clean", "fstype": "ext4",
                              "device": "/dev/sdb1", "was_mounted": True,
                              "output": "Pass 1"}
        window.check_action.trigger()
        self.assertIsInstance(self.dialogs[0], ActionDialog)
        self.assertEqual(self.dialogs[0].go.text(), "Unmount and Check")
        self.assertEqual(self.client.requests[-1]["repair"], False)
        self.assertEqual(self.mounted, ["/dev/sdb1"])
        self.assertIsInstance(self.dialogs[1], CheckResultDialog)
        self.assertIsNone(self.dialogs[1].repair)

    def test_an_unmounted_volume_is_checked_without_a_question(self):
        window = self.window()
        window.select_device("sdc1")
        self.client.result = {"state": "clean", "fstype": "ntfs",
                              "was_mounted": False, "note": "run chkdsk"}
        window.check_action.trigger()
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.mounted, [])

    def test_errors_offer_a_repair_which_is_confirmed_and_shown_again(self):
        window = self.window()
        window.select_device("sdb1")
        # go ahead; Repair...; confirm the repair; close the last result
        self.answers = [True, True, True, False]
        results = [{"state": "errors", "fstype": "ext4", "was_mounted": True,
                    "device": "/dev/sdb1"},
                   {"state": "repaired", "fstype": "ext4",
                    "was_mounted": False, "device": "/dev/sdb1"}]
        run = self.client.run

        def answer(request, on_event=None):
            self.client.result = results.pop(0)
            return run(request, on_event)
        self.client.run = answer
        window.check_action.trigger()
        self.assertEqual([r["repair"] for r in self.client.requests],
                         [False, True])
        kinds = [type(d).__name__ for d in self.dialogs]
        self.assertEqual(kinds, ["ActionDialog", "CheckResultDialog",
                                 "ActionDialog", "CheckResultDialog"])
        # A damaged filesystem is not mounted again; a repaired one is.
        self.assertEqual(self.mounted, ["/dev/sdb1"])
        self.assertEqual(self.dialogs[2].go.text(), "Repair")
        self.assertNotIn("unmount", self.dialogs[2].plan.step_keys)

    def test_errors_left_alone_leave_the_volume_unmounted(self):
        window = self.window()
        window.select_device("sdb1")
        self.answers = [True, False]
        self.client.result = {"state": "errors", "fstype": "ext4",
                              "was_mounted": True, "device": "/dev/sdb1"}
        window.check_action.trigger()
        self.assertEqual(self.mounted, [])
        self.assertEqual(len(self.client.requests), 1)

    def test_a_failure_is_shown_and_the_follow_up_is_not_run(self):
        window = self.window()
        window.select_device("sdc1")
        self.client.error = window_mod.diskctl.HelperError(
            "Windows did not shut down completely on this volume")
        window.check_action.trigger()
        self.assertEqual(self.dialogs, [])
        self.assertIn("did not shut down", self.messages[-1][1])

    def test_a_self_test_is_asked_of_the_drive_and_health_is_read_again(self):
        self.health["sdb"] = diskhealth.Health(
            status=diskhealth.GOOD, kind="ata", drive_path="/drive/sdb",
            interface=udisks.ATA, selftest="success")
        started = []
        with mock.patch.object(window_mod.udisks, "selftest_start",
                               lambda *args: started.append(args),
                               create=True), \
                mock.patch.object(udisks, "selftest_start",
                                  lambda *args: started.append(args)):
            window = self.window()
            window.select_device("sdb")
            self.assertTrue(window.short_test_action.isEnabled())
            self.assertFalse(window.stop_test_action.isEnabled())
            window.long_test_action.trigger()
        self.assertEqual(started, [("/drive/sdb", udisks.ATA, "extended")])
        self.assertIn("extended self-test", window.status.text())

    def test_a_drive_without_a_self_test_says_so(self):
        window = self.window()
        window.select_device("sde1")
        self.assertFalse(window.short_test_action.isEnabled())
        self.assertIn("does not offer a self-test",
                      self.tip_of(window.short_test_action))

    def test_a_running_self_test_can_only_be_stopped(self):
        self.health["sdb"] = diskhealth.Health(
            status=diskhealth.GOOD, kind="ata", drive_path="/drive/sdb",
            interface=udisks.ATA, selftest="inprogress",
            selftest_remaining=40)
        window = self.window()
        window.select_device("sdb")
        self.assertFalse(window.short_test_action.isEnabled())
        self.assertTrue(window.stop_test_action.isEnabled())
        self.assertIn("Running, 40% to go",
                      dict(window.health_grid.facts()).values())

    def test_nothing_is_offered_while_an_operation_runs(self):
        window = self.window()
        window.select_device("sdb1")
        window._job = object()
        window._update_actions()
        for name in ("trim_action", "check_action", "wipe_action",
                     "optimise_action", "timer_action"):
            self.assertFalse(getattr(window, name).isEnabled(), name)
        window._job = None


if __name__ == "__main__":
    unittest.main()
