"""What the drives under a running operation are doing, and when one stalls."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, MIB, FakeBlockTree, load_helper  # noqa: E402

from ksyssupervisor import diskactivity, disks  # noqa: E402
from ksyssupervisor.diskactivity import Counters  # noqa: E402

#: A stat line as the kernel writes it: 17 fields on a 7.x kernel.
STAT = ("  183063    76062 49464666  1091861     8285    31131 48735824  "
        "4448838        2   373354  5581731       10        0        0"
        "        0     1247    41031\n")


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tree = FakeBlockTree(self.root)


class ReadStatTest(Tree):
    def stat(self, name, text):
        os.makedirs(os.path.join(self.root, "block", name), exist_ok=True)
        with open(os.path.join(self.root, "block", name, "stat"), "w") as f:
            f.write(text)

    def test_the_kernels_fields_are_read(self):
        self.stat("sda", STAT)
        counters = diskactivity.read_stat(
            "sda", os.path.join(self.root, "block"), clock=lambda: 5.0)
        self.assertEqual(counters, Counters(
            5.0, 49464666 * 512, 48735824 * 512,
            183063 + 8285 + 10 + 1247, 2))

    def test_an_older_kernel_without_discards_and_flushes(self):
        self.stat("sda", " ".join(STAT.split()[:11]))
        counters = diskactivity.read_stat(
            "sda", os.path.join(self.root, "block"))
        self.assertEqual(counters.completed, 183063 + 8285)

    def test_a_missing_or_garbled_file_is_none(self):
        block = os.path.join(self.root, "block")
        self.assertIsNone(diskactivity.read_stat("sdz", block))
        self.stat("sdy", "not numbers")
        self.assertIsNone(diskactivity.read_stat("sdy", block))
        self.stat("sdx", "1 2 3")
        self.assertIsNone(diskactivity.read_stat("sdx", block))


class DevicesTest(Tree):
    def setUp(self):
        super().setUp()
        self.tree.add_disk("sdb", 8 * GIB)
        self.tree.add_partition("sdb", "sdb1", 1, 2048, 4 * GIB)
        self.tree.add_disk("sdc", 8 * GIB)
        self.tree.add_disk("loop3", GIB)

    def devices(self, request):
        return diskactivity.devices_of(request, self.tree.block)

    def test_a_partition_is_watched_through_its_drive(self):
        self.assertEqual(diskactivity.whole_disk("sdb1", self.tree.block),
                         "sdb")
        self.assertEqual(diskactivity.whole_disk("sdb", self.tree.block),
                         "sdb")
        self.assertEqual(self.devices({"op": "format",
                                       "device": "/dev/sdb1"}), ["sdb"])

    def test_every_device_of_a_request_counts_once(self):
        request = {"op": "clone", "source": "/dev/sdb1",
                   "target": "/dev/sdc", "members": ["sdb", "loop3"],
                   "nested": {"name": "sdc"}}
        self.assertEqual(self.devices(request), ["sdb", "sdc", "loop3"])

    def test_what_is_not_a_device_is_passed_over(self):
        request = {"op": "image_create", "device": "/dev/sdb",
                   "path": "/home/me/disk.img", "label": "sdc backup",
                   "name": "Photos", "size": 5, "dev": "/dev/../etc/passwd"}
        self.assertEqual(self.devices(request), ["sdb"])


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.now = {}

    def monitor(self, **counters):
        self.now.update(counters)
        return diskactivity.Monitor(list(counters), read=self.now.get,
                                    stall_after=60)

    def test_amounts_and_rate_since_the_start(self):
        monitor = self.monitor(sda=Counters(0.0, 100, 1000, 5, 0))
        for second in range(1, 6):
            self.now["sda"] = Counters(float(second), 100,
                                       1000 + second * 4 * MIB, 5 + second, 1)
            self.assertEqual(monitor.sample(), [])
        drive = monitor.drives["sda"]
        self.assertEqual(drive.written, 20 * MIB)
        self.assertEqual(drive.read, 0)
        self.assertAlmostEqual(drive.rate("written"), 4 * MIB)
        line = diskactivity.describe(drive, disks.format_size)
        self.assertTrue(line.startswith("sda: 21.0 MB written"), line)
        self.assertIn("/s now", line)
        self.assertIn("working", line)

    def test_requests_waiting_with_none_finished_is_a_stall(self):
        monitor = self.monitor(sdb=Counters(0.0, 0, 0, 10, 3))
        self.now["sdb"] = Counters(59.0, 0, 0, 10, 3)
        self.assertEqual(monitor.sample(), [])
        self.assertIn("3 requests waiting, no answer for 59 s",
                      diskactivity.describe(monitor.drives["sdb"],
                                            disks.format_size))
        self.now["sdb"] = Counters(61.0, 0, 0, 10, 3)
        self.assertEqual(monitor.sample(), [diskactivity.Change(
            diskactivity.STALLED, "sdb", 61.0, 3)])
        self.assertEqual([d.name for d in monitor.stalled], ["sdb"])
        # Said once, not every second.
        self.now["sdb"] = Counters(62.0, 0, 0, 10, 3)
        self.assertEqual(monitor.sample(), [])
        # One request finishing ends it.
        self.now["sdb"] = Counters(130.0, 0, 0, 11, 2)
        self.assertEqual(monitor.sample(), [diskactivity.Change(
            diskactivity.RESUMED, "sdb", 62.0)])
        self.assertEqual(monitor.stalled, [])

    def test_an_idle_drive_is_never_stalled(self):
        """The tool is computing, or between two writes."""
        monitor = self.monitor(sdb=Counters(0.0, 0, 0, 10, 0))
        self.now["sdb"] = Counters(600.0, 0, 0, 10, 0)
        self.assertEqual(monitor.sample(), [])
        self.assertIn("idle", diskactivity.describe(monitor.drives["sdb"],
                                                    disks.format_size))

    def test_a_drive_that_cannot_be_read_is_unknown_not_stalled(self):
        monitor = self.monitor(sdq=None)
        self.assertIsNone(monitor.drives["sdq"])
        self.assertEqual(monitor.sample(), [])
        self.assertIsNone(diskactivity.describe(None, disks.format_size))
        # And is followed once it can be.
        self.now["sdq"] = Counters(3.0, 0, 0, 1, 0)
        monitor.sample()
        self.assertIsNotNone(monitor.drives["sdq"])

    def test_durations_read_naturally(self):
        self.assertEqual(diskactivity.duration(45), "45 s")
        self.assertEqual(diskactivity.duration(200), "3 min 20 s")
        self.assertEqual(diskactivity.duration(3900), "1 h 05 min")


class StallTextTest(unittest.TestCase):
    def test_what_to_do_depends_on_what_the_operation_does(self):
        erase = diskactivity.stall_text("sde", 90, 1, "format", False)
        self.assertIn("1 request waiting for 1 min 30 s", erase)
        self.assertIn("loses nothing", erase)
        self.assertNotIn("Stop", erase)
        read = diskactivity.stall_text("sde", 90, 2, "recover_scan", True)
        self.assertIn("only reads", read)
        self.assertIn("Stop asks", read)
        change = diskactivity.stall_text("sde", 90, 2, "part_move", False)
        self.assertIn("Do not unplug", change)


class KernelTest(Tree):
    def setUp(self):
        super().setUp()
        # A USB stick: the names it is known by in the kernel's messages
        # are in its path.
        path = os.path.join(self.root, "sys", "devices", "pci0000:00",
                            "usb2", "2-1", "2-1:1.0", "host6", "target6:0:0",
                            "6:0:0:0", "block", "sde")
        os.makedirs(path)
        os.symlink(path, os.path.join(self.tree.block, "sde"))

    def test_the_drives_lines_are_picked_out(self):
        journal = "\n".join([
            "Nov 17 kernel: usb 2-1: reset high-speed USB device number 4",
            "Nov 17 kernel: sd 6:0:0:0: [sde] tag#0 timing out command",
            "Nov 17 kernel: I/O error, dev sde1, sector 2048",
            "Nov 17 kernel: usb 2-10: new device",
            "Nov 17 kernel: I/O error, dev sdea, sector 1",
            "Nov 17 kernel: EXT4-fs (nvme0n1p2): mounted"])
        seen = []

        def run(argv, **options):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, journal, "")
        with mock.patch.object(diskactivity, "JOURNAL_DIR", self.tree.block):
            lines = diskactivity.kernel_lines(
                "sde", 1700000000.9, run=run,
                sys_class_block=self.tree.block)
        self.assertEqual(len(lines), 3)
        self.assertIn("--since=@1700000000", seen[0])
        self.assertIn("--dmesg", seen[0])
        # Without a journal (the recovery stick): the kernel's own buffer.
        with mock.patch.object(diskactivity, "JOURNAL_DIR", "/nonexistent"):
            lines = diskactivity.kernel_lines(
                "sde", 1700000000.9, run=run,
                sys_class_block=self.tree.block)
        self.assertEqual(len(lines), 3)
        self.assertEqual(seen[1][0], "dmesg")

    def test_a_journal_that_cannot_be_read_says_nothing(self):
        def refuse(argv, **options):
            return subprocess.CompletedProcess(argv, 1, "", "")

        def missing(argv, **options):
            raise FileNotFoundError("journalctl")
        for run in (refuse, missing):
            self.assertEqual(diskactivity.kernel_lines(
                "sde", 0, run=run, sys_class_block=self.tree.block), [])

    def test_the_helper_picks_out_the_same_lines(self):
        """The helper reads the messages for the Drive Messages view; the
        application while an operation runs. Both must mean one drive."""
        helper = load_helper()
        for name in ("sde", "nvme0n1", "sdz"):
            mine = diskactivity.drive_pattern(name, self.tree.block)
            theirs = helper.drive_pattern(name, self.tree.block)
            self.assertEqual(mine[0].pattern, theirs[0].pattern, name)
            self.assertEqual(mine[1], theirs[1], name)
        self.assertEqual(diskactivity.drive_pattern("sde", self.tree.block)[1],
                         ["2-1", "6:0:0:0", "host6", "sde"])


if __name__ == "__main__":
    unittest.main()
