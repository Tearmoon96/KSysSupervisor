"""Drive health from UDisks' SMART data, and the client of the disk helper.

The SMART tables are the ones real drives returned - a Crucial MX500, a
Samsung NVMe drive and a Seagate hard disk - trimmed to the rows that matter.
"""

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from ksyssupervisor import diskctl, diskhealth, disks, udisks

# (id, name, flags, value, worst, threshold, pretty, unit, expansion)
MX500 = [
    [1, "raw-read-error-rate", 47, 100, 100, 0, 0, 1, {}],
    [5, "reallocated-sector-count", 50, 100, 100, 10, 0, 3, {}],
    [9, "power-on-hours", 50, 100, 100, 0, 61689600000, 2, {}],
    [12, "power-cycle-count", 50, 100, 100, 0, 3357, 1, {}],
    [173, "attribute-173", 50, 94, 94, 0, 0, 0, {}],
    [194, "temperature-celsius-2", 34, 80, 34, 0, 293150, 4, {}],
    [197, "current-pending-sector", 50, 100, 100, 0, 0, 3, {}],
    [198, "offline-uncorrectable", 48, 100, 100, 0, 0, 3, {}],
    [199, "udma-crc-error-count", 50, 100, 100, 0, 0, 1, {}],
    [202, "ta-increase-count", 48, 94, 94, 1, 6, 1, {}],
]

ATA_OK = {"SmartSupported": True, "SmartEnabled": True,
          "SmartUpdated": 1794315192, "SmartFailing": False,
          "SmartPowerOnSeconds": 61689600, "SmartTemperature": 293.0,
          "SmartNumAttributesFailing": 0,
          "SmartNumAttributesFailedInThePast": 0,
          "SmartSelftestStatus": "success"}

NVME_OK = {"SmartUpdated": 1794315192, "SmartCriticalWarning": [],
           "SmartPowerOnHours": 698, "SmartTemperature": 305,
           "SmartSelftestStatus": "success"}
NVME_LOG = {"avail_spare": 100, "spare_thresh": 5, "percent_used": 2,
            "total_data_read": 4399448576000,
            "total_data_written": 7593367552000, "power_cycles": 212,
            "unsafe_shutdowns": 60, "media_errors": 0}


def rows(base, **changes):
    """`base` with some attributes' pretty values replaced, by id."""
    found = []
    for row in base:
        row = list(row)
        if "a%d" % row[0] in changes:
            row[6] = changes["a%d" % row[0]]
        found.append(row)
    return found


class SataSsdTest(unittest.TestCase):
    def health(self, ata=ATA_OK, table=MX500, model="CT1000MX500SSD1"):
        return diskhealth.from_ata(ata, table, model=model, rotation_rate=0)

    def test_a_healthy_drive(self):
        health = self.health()
        self.assertEqual(health.status, diskhealth.GOOD)
        self.assertEqual(health.reasons, [])
        self.assertEqual(health.temperature, 20)
        self.assertEqual(health.power_on_hours, 17136)
        self.assertEqual(health.power_cycles, 3357)
        self.assertEqual(health.reallocated, 0)

    def test_wear_comes_from_the_attribute_this_maker_uses(self):
        """UDisks calls 202 a "TA increase count", which is what the number
        meant on hard disks. On a Crucial it is the life left."""
        health = self.health()
        self.assertEqual(health.life_left, 94)
        self.assertIn("202", health.life_note)
        names = {a.id: a.name for a in health.attributes}
        self.assertEqual(names[202], "Percent lifetime remaining")
        self.assertEqual(names[173], "Average block erase count")
        self.assertEqual(names[5], "Reallocated sector count")
        self.assertEqual(names[194], "Temperature celsius")

    def test_an_unknown_maker_gets_no_invented_wear_figure(self):
        health = self.health(model="NONAME SSD 1TB")
        self.assertIsNone(health.life_left)
        self.assertIn("does not publish", health.life_note)
        self.assertEqual(health.status, diskhealth.GOOD)

    def test_a_widely_shared_attribute_is_shown_as_an_estimate(self):
        table = MX500 + [[231, "attribute-231", 50, 87, 87, 0, 0, 0, {}]]
        health = self.health(table=table, model="NONAME SSD 1TB")
        self.assertEqual(health.life_left, 87)
        self.assertIn("estimated", health.life_note)

    def test_a_hard_disk_has_no_wear_figure_at_all(self):
        health = diskhealth.from_ata(ATA_OK, MX500, model="ST8000DM004",
                                     rotation_rate=5400)
        self.assertIsNone(health.life_left)
        self.assertEqual(health.life_note, "")

    def test_a_raw_value_udisks_cannot_interpret_is_not_zero(self):
        health = self.health()
        erase = [a for a in health.attributes if a.id == 173][0]
        self.assertIsNone(erase.raw)
        self.assertEqual(erase.raw_text, "-")
        hours = [a for a in health.attributes if a.id == 9][0]
        self.assertEqual(hours.raw_text, "17136 h")

    def test_reallocated_sectors_are_a_caution(self):
        health = self.health(table=rows(MX500, a5=8))
        self.assertEqual(health.status, diskhealth.CAUTION)
        self.assertIn("8 sectors went bad", health.reasons[0])

    def test_one_pending_sector_is_worded_as_one(self):
        health = self.health(table=rows(MX500, a197=1))
        self.assertEqual(health.status, diskhealth.CAUTION)
        self.assertIn("1 sector could not be read", health.reasons[0])

    def test_the_drives_own_verdict_is_final(self):
        health = self.health(ata=dict(ATA_OK, SmartFailing=True))
        self.assertEqual(health.status, diskhealth.BAD)
        self.assertIn("Back up", health.reasons[0])

    def test_an_attribute_under_its_threshold_is_bad(self):
        table = [list(r) for r in MX500]
        table[1][3] = 9          # reallocated, threshold 10
        health = self.health(table=table)
        self.assertEqual(health.status, diskhealth.BAD)
        self.assertIn("below the failure threshold", health.reasons[0])

    def test_little_life_left_is_a_caution_none_is_bad(self):
        low = [list(r) for r in MX500]
        low[-1][3] = 7
        self.assertEqual(self.health(table=low).status, diskhealth.CAUTION)
        spent = [list(r) for r in MX500]
        spent[-1][3] = 0
        self.assertEqual(self.health(table=spent).status, diskhealth.BAD)

    def test_cable_errors_are_a_note_not_a_fault(self):
        health = self.health(table=rows(MX500, a199=12))
        self.assertEqual(health.status, diskhealth.GOOD)
        self.assertIn("cable", health.notes[0])

    def test_smart_switched_off_is_unknown_with_the_reason(self):
        for change, word in (({"SmartSupported": False}, "support"),
                             ({"SmartEnabled": False}, "switched off"),
                             ({"SmartUpdated": 0}, "not been read")):
            health = self.health(ata=dict(ATA_OK, **change))
            self.assertEqual(health.status, diskhealth.UNKNOWN)
            self.assertIn(word, health.reasons[0])


class NvmeTest(unittest.TestCase):
    def test_a_healthy_drive(self):
        health = diskhealth.from_nvme(NVME_OK, NVME_LOG)
        self.assertEqual(health.status, diskhealth.GOOD)
        self.assertEqual(health.life_left, 98)
        self.assertEqual(health.temperature, 32)
        self.assertEqual(health.written, 7593367552000)
        self.assertEqual(health.spare, 100)
        self.assertEqual(health.unsafe_shutdowns, 60)

    def test_use_beyond_the_rating_does_not_go_negative(self):
        health = diskhealth.from_nvme(NVME_OK,
                                      dict(NVME_LOG, percent_used=131))
        self.assertEqual(health.life_left, 0)
        self.assertEqual(health.status, diskhealth.BAD)

    def test_spare_under_its_threshold_is_bad(self):
        health = diskhealth.from_nvme(NVME_OK, dict(NVME_LOG, avail_spare=3))
        self.assertEqual(health.status, diskhealth.BAD)
        self.assertIn("3%", health.reasons[0])

    def test_a_critical_warning_is_bad_and_explained(self):
        health = diskhealth.from_nvme(
            dict(NVME_OK, SmartCriticalWarning=["readonly"]), NVME_LOG)
        self.assertEqual(health.status, diskhealth.BAD)
        self.assertIn("read-only", health.reasons[0])

    def test_media_errors_are_a_caution(self):
        health = diskhealth.from_nvme(NVME_OK, dict(NVME_LOG, media_errors=2))
        self.assertEqual(health.status, diskhealth.CAUTION)

    def test_a_missing_log_still_gives_what_the_properties_hold(self):
        health = diskhealth.from_nvme(NVME_OK, None)
        self.assertEqual(health.status, diskhealth.GOOD)
        self.assertIsNone(health.life_left)
        self.assertEqual(health.power_on_hours, 698)


def variant(kind, value):
    return {"type": kind, "data": value}


class UDisksTest(unittest.TestCase):
    """The busctl plumbing, with busctl played by a function."""

    DRIVE = "/org/freedesktop/UDisks2/drives/CT1000MX500SSD1_X"

    def managed(self):
        ata = {key: variant("b" if isinstance(value, bool) else "t", value)
               for key, value in ATA_OK.items()}
        return {"type": "a{oa{sa{sv}}}", "data": [{
            self.DRIVE: {
                udisks.DRIVE: {"Model": variant("s", "CT1000MX500SSD1"),
                               "RotationRate": variant("i", 0)},
                udisks.ATA: ata},
            "/org/freedesktop/UDisks2/block_devices/sdb": {
                udisks.BLOCK: {
                    "Device": variant("ay", [47, 100, 101, 118, 47, 115,
                                             100, 98, 0]),
                    "Drive": variant("o", self.DRIVE)}},
            "/org/freedesktop/UDisks2/block_devices/loop0": {
                udisks.BLOCK: {
                    "Device": variant("ay", [47, 100, 101, 118, 47, 108, 111,
                                             111, 112, 48, 0]),
                    "Drive": variant("o", "/")}},
        }]}

    def runner(self, calls=None):
        def run(argv, timeout=0):
            if calls is not None:
                calls.append(argv)
            if "GetManagedObjects" in argv:
                return 0, json.dumps(self.managed()), ""
            if "SmartGetAttributes" in argv:
                return 0, json.dumps({"type": "a(ysqiiixia{sv})",
                                      "data": [MX500]}), ""
            if "SmartUpdate" in argv:
                return 0, "", ""
            return 1, "", "Call failed: Unknown method"
        return run

    def test_the_wrappers_come_off_all_the_way_down(self):
        managed = udisks.objects(run=self.runner())
        self.assertEqual(managed[self.DRIVE][udisks.DRIVE]["Model"],
                         "CT1000MX500SSD1")
        self.assertIs(managed[self.DRIVE][udisks.ATA]["SmartEnabled"], True)

    def test_block_devices_lead_to_their_drive(self):
        paths = udisks.drive_paths(udisks.objects(run=self.runner()))
        self.assertEqual(paths, {"sdb": self.DRIVE})

    def test_health_of_a_drive_end_to_end(self):
        disk = disks.Disk("sdb", "/dev/sdb", 10 ** 12, model="CT1000MX500SSD1")
        other = disks.Disk("sdz", "/dev/sdz", 10 ** 10)
        found = diskhealth.read_all([disk, other], run=self.runner())
        self.assertEqual(found["sdb"].status, diskhealth.GOOD)
        self.assertEqual(found["sdb"].life_left, 94)
        self.assertEqual(found["sdz"].status, diskhealth.UNKNOWN)
        self.assertIn("memory sticks", found["sdz"].reasons[0])

    def test_a_refresh_never_wakes_a_sleeping_disk(self):
        calls = []
        disk = disks.Disk("sdb", "/dev/sdb", 10 ** 12)
        diskhealth.read_all([disk], refresh=True, run=self.runner(calls))
        update = [c for c in calls if "SmartUpdate" in c][0]
        self.assertEqual(update[update.index("SmartUpdate") + 1:],
                         ["a{sv}", "1", "nowakeup", "b", "true"])

    def test_no_service_is_unknown_not_a_crash(self):
        def run(argv, timeout=0):
            return 1, "", "Failed to connect to bus: No such file"
        disk = disks.Disk("sdb", "/dev/sdb", 10 ** 12)
        with mock.patch.object(diskhealth.log, "info"):
            found = diskhealth.read_all([disk], run=run)
        self.assertEqual(found["sdb"].status, diskhealth.UNKNOWN)
        self.assertIn("UDisks2", found["sdb"].reasons[0])

    def test_errors_lose_their_dbus_prefix(self):
        def run(argv, timeout=0):
            return 1, "", ("Error mounting /dev/sdd1: GDBus.Error:org."
                           "freedesktop.UDisks2.Error.AlreadyMounted: Device "
                           "/dev/sdd1 is already mounted at `/mnt/x'.\n")
        with self.assertRaises(udisks.UDisksError) as caught:
            udisks.mount("/dev/sdd1", run=run)
        self.assertEqual(str(caught.exception),
                         "Device /dev/sdd1 is already mounted at `/mnt/x'.")

    def test_a_mount_reports_where_it_landed(self):
        def run(argv, timeout=0):
            self.assertEqual(argv, ["udisksctl", "mount", "--block-device",
                                    "/dev/sdd1"])
            return 0, "Mounted /dev/sdd1 at /run/media/user/My Stick\n", ""
        self.assertEqual(udisks.mount("/dev/sdd1", run=run),
                         "/run/media/user/My Stick")


class AgeTest(unittest.TestCase):
    def test_a_reading_says_how_old_it_is(self):
        now = 1_000_000
        self.assertEqual(diskhealth.age_text(now - 30, now), "just now")
        self.assertEqual(diskhealth.age_text(now - 600, now), "10 minutes ago")
        self.assertEqual(diskhealth.age_text(now - 7200, now), "2 hours ago")
        self.assertEqual(diskhealth.age_text(None, now), "")


# ---- the helper's client -------------------------------------------------------

FAKE_HELPER = textwrap.dedent("""
    import json, sys, time
    cancelled = False
    for line in sys.stdin:
        request = json.loads(line)
        ident, command = request.get("id"), request.get("cmd")
        def say(**fields):
            print(json.dumps(dict(fields, id=ident)), flush=True)
        if command == "hello":
            if request.get("version") == %(version)d:
                say(ok=True, version=%(version)d)
            else:
                say(ok=False, error="unsupported protocol version")
        elif command == "run" and request.get("op") == "format":
            say(event="step", index=1, total=2, key="erase", text="Erasing")
            say(event="progress", done=5, total=10)
            say(event="step", index=2, total=2, key="mkfs", text="Creating")
            say(ok=True, result={"device": request["device"]})
        elif command == "run" and request.get("op") == "refuse":
            say(ok=False, error="it holds the running system")
        elif command == "run" and request.get("op") == "stop":
            say(ok=False, cancelled=True, error="Stopped.")
        elif command == "run" and request.get("op") == "die":
            sys.exit(3)
        elif command == "bye":
            say(ok=True)
            break
        else:
            say(ok=True)
""")


class ClientTest(unittest.TestCase):
    def client(self, version=diskctl.PROTOCOL_VERSION):
        handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        handle.write(FAKE_HELPER % {"version": version})
        handle.close()
        self.addCleanup(os.remove, handle.name)
        client = diskctl.DiskHelperClient(
            path=handle.name, launcher=lambda path: [sys.executable, path])
        self.addCleanup(client.stop)
        return client

    def test_an_operation_reports_its_steps_and_its_result(self):
        client = self.client()
        events = []
        result = client.run({"op": "format", "device": "/dev/sdd"},
                            on_event=events.append)
        self.assertEqual(result, {"device": "/dev/sdd"})
        self.assertEqual([e["event"] for e in events],
                         ["step", "progress", "step"])
        self.assertFalse(client.busy)
        # The same helper serves the next operation: one password a session.
        self.assertEqual(client.run({"op": "format", "device": "/dev/sde"}),
                         {"device": "/dev/sde"})

    def test_a_refusal_is_raised_with_the_helpers_words(self):
        with self.assertRaises(diskctl.HelperError) as caught:
            self.client().run({"op": "refuse"})
        self.assertEqual(str(caught.exception), "it holds the running system")
        self.assertNotIsInstance(caught.exception, diskctl.Cancelled)

    def test_a_stop_is_told_apart_from_a_failure(self):
        with self.assertRaises(diskctl.Cancelled):
            self.client().run({"op": "stop"})

    def test_a_helper_that_dies_is_reported_not_waited_for(self):
        started = time.monotonic()
        with self.assertRaises(diskctl.HelperError) as caught:
            self.client().run({"op": "die"})
        self.assertIn("stopped unexpectedly", str(caught.exception))
        self.assertLess(time.monotonic() - started, 10)

    def test_another_version_is_refused_at_the_handshake(self):
        client = self.client(version=diskctl.PROTOCOL_VERSION + 1)
        with self.assertRaises(diskctl.HelperError) as caught:
            client.start()
        self.assertIn("different version", str(caught.exception))
        self.assertFalse(client.running)

    def test_a_failing_event_handler_does_not_lose_the_result(self):
        def explode(event):
            raise RuntimeError("drawing failed")
        with mock.patch.object(diskctl.log, "exception"):
            result = self.client().run({"op": "format", "device": "/dev/x"},
                                       on_event=explode)
        self.assertEqual(result, {"device": "/dev/x"})

    def test_stopping_ends_the_process(self):
        client = self.client()
        client.start()
        proc = client._proc
        client.stop()
        self.assertFalse(client.running)
        self.assertEqual(proc.poll(), 0)


class AvailabilityTest(unittest.TestCase):
    def test_a_missing_helper_can_be_installed_from_here(self):
        with mock.patch.object(diskctl, "helper_path", return_value=None):
            state, reason = diskctl.availability()
        self.assertEqual(state, diskctl.NO_HELPER)
        self.assertIn("not installed", reason)
        with mock.patch("shutil.which", return_value="/usr/bin/pkexec"):
            self.assertTrue(diskctl.can_install_helper(state))

    def test_an_older_helper_is_seen_before_the_password_prompt(self):
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        handle.write("PROTOCOL_VERSION = %d\n" % (diskctl.PROTOCOL_VERSION + 5))
        handle.close()
        self.addCleanup(os.remove, handle.name)
        with mock.patch.object(diskctl, "helper_path",
                               return_value=handle.name), \
                mock.patch("shutil.which", return_value="/usr/bin/pkexec"):
            self.assertEqual(diskctl.availability()[0],
                             diskctl.OUTDATED_HELPER)

    def test_this_copy_ships_the_helper_and_its_policy(self):
        helper, policy = diskctl.bundled_helper()
        self.assertTrue(os.path.isfile(helper))
        with open(policy) as handle:
            text = handle.read()
        self.assertIn("org.ksyssupervisor.diskmanager.manage", text)
        self.assertIn("@HELPER_PATH@", text)
        self.assertIn("<allow_active>auth_admin</allow_active>", text)

    def test_one_prompt_installs_both_helpers(self):
        seen = {}

        def fake_pkexec(command):
            with open(command[-1]) as handle:
                seen["script"] = handle.read()
            seen["files"] = sorted(os.listdir(os.path.dirname(command[-1])))
            return 0, ""

        ok, _message = diskctl.install_bundled_helper(runner=fake_pkexec)
        self.assertTrue(ok)
        from ksyssupervisor import fanctl
        self.assertIn(diskctl.HELPER_INSTALL_PATH, seen["script"])
        self.assertIn(fanctl.HELPER_INSTALL_PATH, seen["script"])
        self.assertIn("org.ksyssupervisor.diskmanager.policy", seen["script"])
        self.assertEqual(seen["script"].count("install -Dm755 -o root -g root"),
                         2)
        self.assertEqual(seen["files"], ["helper", "helper-disk", "install.sh",
                                         "policy", "policy-disk"])


if __name__ == "__main__":
    unittest.main()
