"""A drive in trouble: recognising factory mode, reading the kernel's
complaints, looking for drives again."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, MIB                              # noqa: E402
from test_diskhelper import HelperCase                     # noqa: E402
from test_diskwindow import DiskWindowCase                 # noqa: E402

from ksyssupervisor import diskdiag, diskfs, disks         # noqa: E402
from ksyssupervisor.diskmaintainui import DriveLogDialog   # noqa: E402

# Lines as the kernel prints them, from drives that were in these states.
BAD_SECTORS = [
    "[Tue Nov 10 10:00:01 2026] ata3.00: failed command: READ FPDMA QUEUED",
    "[Tue Nov 10 10:00:01 2026] ata3.00: error: { UNC }",
    "[Tue Nov 10 10:00:01 2026] blk_update_request: I/O error, dev sda, "
    "sector 2048 op 0x0:(READ) flags 0x0 phys_seg 1 prio class 0",
    "[Tue Nov 10 10:00:01 2026] Buffer I/O error on dev sda1, logical "
    "block 0, async page read",
]
BAD_CABLE = [
    "[Tue Nov 10 10:00:01 2026] ata3: SError: { PHYRdyChg CommWake }",
    "[Tue Nov 10 10:00:02 2026] ata3: hard resetting link",
    "[Tue Nov 10 10:00:08 2026] ata3: link is slow to respond, please be "
    "patient (ready=0)",
]
BAD_USB = [
    "[Tue Nov 10 10:00:01 2026] usb 1-4: device descriptor read/64, "
    "error -71",
    "[Tue Nov 10 10:00:02 2026] usb usb1-port4: unable to enumerate USB "
    "device",
]
BAD_NVME = ["[Tue Nov 10 10:00:01 2026] nvme nvme0: I/O 12 QID 3 timeout, "
            "aborting", "[Tue Nov 10 10:00:31 2026] nvme nvme0: controller "
            "is down; will reset: CSTS=0x3"]
HEALTHY = ["[Tue Nov 10 09:00:00 2026] sd 2:0:0:0: [sda] 15628053168 "
           "512-byte logical blocks: (8.00 TB/7.28 TiB)",
           "[Tue Nov 10 09:00:00 2026] sd 2:0:0:0: [sda] Attached SCSI disk"]


class ExplainTest(unittest.TestCase):
    def titles(self, lines):
        return [a.title for a in diskdiag.explain(lines)]

    def test_each_kind_of_trouble_is_told_apart(self):
        self.assertIn("The drive cannot read some of what is stored on it",
                      self.titles(BAD_SECTORS))
        self.assertEqual(self.titles(BAD_CABLE),
                         ["The connection to the drive keeps failing"])
        self.assertEqual(self.titles(BAD_USB),
                         ["The USB connection does not come up properly"])
        self.assertEqual(self.titles(BAD_NVME),
                         ["The NVMe drive stops answering"])
        self.assertEqual(self.titles(HEALTHY), [])

    def test_a_failing_drive_is_told_to_be_copied_first(self):
        advice = diskdiag.explain(BAD_SECTORS)[0]
        self.assertEqual(advice.level, diskfs.WARNING)
        self.assertIn("Skip unreadable areas", advice.text)
        cable = diskdiag.explain(BAD_CABLE)[0]
        self.assertIn("not the drive itself", cable.text)

    def test_each_finding_is_said_once(self):
        self.assertEqual(len(diskdiag.explain(BAD_CABLE * 5)), 1)

    def test_no_messages_and_harmless_ones_are_said_differently(self):
        nothing = diskdiag.summary([])
        self.assertIn("nothing to report", nothing[0].title)
        harmless = diskdiag.summary(HEALTHY)
        self.assertIn("Nothing here points at a known fault",
                      harmless[0].title)
        self.assertEqual(harmless[0].level, diskfs.INFO)

    def test_read_only_and_no_capacity(self):
        self.assertEqual(self.titles(
            ["sd 6:0:0:0: [sdd] Write Protect is on"]),
            ["The drive is read-only"])
        self.assertEqual(self.titles(
            ["sdd: detected capacity change from 15523840 to 0"]),
            ["The drive does not report its size"])


class FactoryModeTest(unittest.TestCase):
    def test_a_controllers_own_name_gives_the_state_away(self):
        for model in ("SATAFIRM   S11", "SM2258XT", "PS3111-S11"):
            disk = disks.Disk("sda", "/dev/sda", 240 * GIB, model=model)
            advice = diskdiag.factory_mode(disk)
            self.assertIsNotNone(advice, model)
            self.assertIn("firmware has failed", advice.title)
            self.assertIn("erasing it", advice.text)
            self.assertIn("laboratory", advice.text)

    def test_a_built_in_drive_of_a_few_megabytes_does_too(self):
        disk = disks.Disk("sda", "/dev/sda", 8 * MIB, model="KINGSTON SA400")
        self.assertIn("capacity of only", diskdiag.factory_mode(disk).text)

    def test_ordinary_drives_and_small_cards_are_left_alone(self):
        for disk in (
                disks.Disk("sda", "/dev/sda", 1000 * GIB, model="CT1000MX500"),
                disks.Disk("sdd", "/dev/sdd", 32 * MIB, model="Card",
                           removable=True, transport="usb"),
                disks.Disk("sdd", "/dev/sdd", 0, model="Reader")):
            self.assertIsNone(diskdiag.factory_mode(disk))


class HelperDiagnosisTest(HelperCase):
    def test_only_lines_about_the_drive_and_its_link_are_returned(self):
        self.tools.stdout["dmesg"] = "\n".join(
            BAD_SECTORS + ["[x] sdb: unrelated", "[x] sdab: also not",
                           "[x] usb 3-1: other device"]) + "\n"
        result = self.helper.op_drive_log({"op": "drive_log", "name": "sda"})
        self.assertEqual(len(result["lines"]), 2)       # the two naming sda
        self.assertTrue(all("sda" in line for line in result["lines"]))

    def test_a_name_that_is_not_one_is_refused(self):
        for name in ("../etc", "sda;ls", "", None):
            with self.assertRaises(self.module.Refusal):
                self.helper.op_drive_log({"op": "drive_log", "name": name})

    def test_rescanning_asks_every_controller(self):
        hosts = os.path.join(self.root, "sys", "class", "scsi_host")
        for name in ("host0", "host1"):
            os.makedirs(os.path.join(hosts, name))
            open(os.path.join(hosts, name, "scan"), "w").close()
        # The helper's `time` is everyone's: put back when the test ends.
        with mock.patch.object(self.module.time, "sleep",
                               lambda seconds: None):
            result = self.helper.op_rescan({"op": "rescan"})
        self.assertEqual(result["asked"], 2)
        with open(os.path.join(hosts, "host1", "scan")) as handle:
            self.assertEqual(handle.read(), "- - -")


class DiagnosisWindowTest(DiskWindowCase):
    def test_a_drive_in_factory_mode_says_so_in_its_details(self):
        self.machine = self.machine + [disks.Disk(
            "sdq", "/dev/sdq", 240 * GIB, model="SATAFIRM   S11",
            transport="sata")]
        window = self.window()
        window.select_device("sdq")
        self.assertIn("This drive's firmware has failed",
                      [card.advice.title for card in window._advice_cards])
        window.select_device("sdb")
        self.assertNotIn("This drive's firmware has failed",
                         [card.advice.title for card in window._advice_cards])

    def test_the_systems_messages_are_fetched_and_explained(self):
        window = self.window()
        window.select_device("sda")
        self.client.result = {"lines": BAD_CABLE}
        window.log_action.trigger()
        self.assertEqual(self.client.requests[-1],
                         {"op": "drive_log", "name": "sda"})
        dialog = self.dialogs[-1]
        self.assertIsInstance(dialog, DriveLogDialog)
        self.assertEqual(dialog.findings[0].title,
                         "The connection to the drive keeps failing")
        self.assertIn("hard resetting link", dialog.output.toPlainText())

    def test_looking_for_drives_again_reads_the_list_again(self):
        window = self.window()
        self.client.result = {"asked": 6}
        window.rescan_action.trigger()
        self.assertEqual(self.client.requests[-1], {"op": "rescan"})
        self.assertIn("Looked for drives again", window.status.text())


if __name__ == "__main__":
    unittest.main()
