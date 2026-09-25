"""Fan control tests.

Two things are worth pinning here: that channel discovery picks out real pwm
channels and nothing else, and that the privileged helper refuses every path it
should. The second matters most - the helper runs as root, so its validation is
the only thing between a client and an arbitrary write.

The helper is loaded from source rather than executed, so none of this touches a
real fan.
"""

import importlib.util
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from ksyssupervisor import fanctl


HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "helpers", "ksyssupervisor-fanhelper")


def load_helper():
    """Import the helper by path: it has no .py suffix and is not a package."""
    spec = importlib.util.spec_from_loader(
        "fanhelper", importlib.machinery.SourceFileLoader("fanhelper", HELPER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TempTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def hwmon(self, index, name):
        path = os.path.join(self.root, "hwmon%d" % index)
        write(os.path.join(path, "name"), name)
        return path


class DiscoveryTest(TempTree):
    def test_finds_pwm_channels_with_their_fans(self):
        chip = self.hwmon(0, "nct6799")
        for i in (1, 2):
            write(os.path.join(chip, "pwm%d" % i), "128")
            write(os.path.join(chip, "pwm%d_enable" % i), "5")
            write(os.path.join(chip, "fan%d_input" % i), "%d" % (900 * i))

        channels = fanctl.discover_channels(self.root)
        self.assertEqual([c.index for c in channels], [1, 2])
        self.assertTrue(all(c.chip == "nct6799" for c in channels))
        self.assertTrue(all(c.has_enable for c in channels))
        self.assertTrue(all(c.fan_input for c in channels))

    def test_ignores_pwm_sub_attributes(self):
        """pwm1_enable and pwm1_auto_point1_pwm are settings, not channels."""
        chip = self.hwmon(0, "nct6799")
        write(os.path.join(chip, "pwm1"), "128")
        write(os.path.join(chip, "pwm1_enable"), "5")
        write(os.path.join(chip, "pwm1_auto_point1_pwm"), "60")
        write(os.path.join(chip, "pwm1_mode"), "1")

        channels = fanctl.discover_channels(self.root)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].index, 1)

    def test_channel_without_a_tachometer_is_still_controllable(self):
        """An empty header can be driven; it just reports no speed."""
        chip = self.hwmon(0, "nct6799")
        write(os.path.join(chip, "pwm4"), "255")
        write(os.path.join(chip, "pwm4_enable"), "5")

        channels = fanctl.discover_channels(self.root)
        self.assertEqual(len(channels), 1)
        self.assertIsNone(channels[0].fan_input)

    def test_chip_without_a_name_is_skipped(self):
        path = os.path.join(self.root, "hwmon0")
        write(os.path.join(path, "pwm1"), "128")
        self.assertEqual(fanctl.discover_channels(self.root), [])

    def test_state_reads_speed_and_mode(self):
        chip = self.hwmon(0, "amdgpu")
        write(os.path.join(chip, "pwm1"), "51")
        write(os.path.join(chip, "pwm1_enable"), "1")
        write(os.path.join(chip, "fan1_input"), "1200")

        channel = fanctl.discover_channels(self.root)[0]
        state = fanctl.read_state(channel)
        self.assertEqual((state.pwm, state.enable, state.rpm), (51, 1, 1200))
        self.assertTrue(state.manual)
        self.assertEqual(state.percent, 20)

    def test_channel_key_survives_hwmon_renumbering(self):
        """hwmonN numbers shuffle between boots; saved names must not."""
        chip = self.hwmon(7, "nct6799")
        write(os.path.join(chip, "pwm3"), "128")
        write(os.path.join(chip, "pwm3_enable"), "5")
        self.assertEqual(fanctl.discover_channels(self.root)[0].key, "nct6799:3")

    def card_at(self, index, pci):
        """An amdgpu hwmon node whose device link points at a PCI address."""
        device = os.path.join(self.root, "devices", pci)
        os.makedirs(device)
        chip = self.hwmon(index, "amdgpu")
        os.symlink(device, os.path.join(chip, "device"))
        write(os.path.join(chip, "pwm1"), "80")
        write(os.path.join(chip, "pwm1_enable"), "2")
        return chip

    def test_two_cards_of_one_chip_are_two_channels(self):
        """Both were "amdgpu:1": one claim, one range and one name for two."""
        self.card_at(1, "0000:03:00.0")
        self.card_at(2, "0000:0a:00.0")
        channels = fanctl.discover_channels(self.root)
        self.assertEqual([c.key for c in channels],
                         ["amdgpu@0000:03:00.0:1", "amdgpu@0000:0a:00.0:1"])
        self.assertEqual({c.legacy_key for c in channels}, {"amdgpu:1"})

    def test_saved_names_move_to_the_new_key(self):
        from PyQt6.QtCore import QSettings

        from ksyssupervisor.fanwindow import FanControlWindow

        self.card_at(1, "0000:03:00.0")
        settings = QSettings(os.path.join(self.root, "s.ini"),
                             QSettings.Format.IniFormat)
        settings.setValue("fan_name_amdgpu:1", "GPU cooler")

        class Window:
            pass

        window = Window()
        window.settings = settings
        window.channels = fanctl.discover_channels(self.root)
        FanControlWindow._adopt_legacy_names(window)

        self.assertEqual(settings.value("fan_name_amdgpu@0000:03:00.0:1"),
                         "GPU cooler")
        self.assertFalse(settings.contains("fan_name_amdgpu:1"))


class HelperValidationTest(TempTree):
    """Everything the client sends is a claim to be checked, not a path to use."""

    def setUp(self):
        super().setUp()
        self.mod = load_helper()
        self.chip = self.hwmon(0, "nct6799")
        write(os.path.join(self.chip, "pwm1"), "128")
        write(os.path.join(self.chip, "pwm1_enable"), "5")

        # Point the helper's idea of /sys/class/hwmon at the fake tree.
        self.mod.HWMON_ROOT = self.root
        self.helper = self.mod.Helper(out=io.StringIO())

    def assert_refused(self, command, fragment=None):
        reply = self.helper.handle(command)
        self.assertTrue(reply.startswith("ERR"),
                        "expected refusal, got %r for %r" % (reply, command))
        if fragment:
            self.assertIn(fragment, reply)

    def test_rejects_paths_outside_hwmon(self):
        self.assert_refused("CLAIM /etc 1", "not an hwmon directory")
        self.assert_refused("CLAIM / 1")
        self.assert_refused("CLAIM %s 1" % self.root)

    def test_rejects_traversal_and_symlinks(self):
        self.assert_refused("CLAIM %s/hwmon0/../.. 1" % self.root)

        link = os.path.join(self.root, "sneaky")
        os.symlink("/etc", link)
        self.assert_refused("CLAIM %s 1" % link, "not an hwmon directory")

    def test_traversal_that_resolves_back_inside_is_accepted(self):
        """realpath, not string matching: a valid path stays valid."""
        reply = self.helper.handle("CLAIM %s/hwmon0/./ 1" % self.root)
        self.assertTrue(reply.startswith("OK"), reply)

    def test_rejects_bad_indexes(self):
        for bad in ("x", "-1", "999", "1;2", ""):
            self.assert_refused("CLAIM %s %s" % (self.chip, bad))

    def test_rejects_missing_channel(self):
        self.assert_refused("CLAIM %s 9" % self.chip, "no such pwm channel")

    def test_rejects_bad_values(self):
        self.helper.handle("CLAIM %s 1" % self.chip)
        for bad in ("256", "1000", "-1", "0x40", "40.5"):
            self.assert_refused("SET %s 1 %s" % (self.chip, bad))

    def test_rejects_unknown_commands(self):
        self.assert_refused("EXEC rm -rf /", "unknown command")
        self.assert_refused("SET", "usage")

    def test_set_requires_a_claim_first(self):
        """Without a claim there is no saved state, so no way back."""
        self.assert_refused("SET %s 1 200" % self.chip, "not claimed")


class HelperBehaviourTest(TempTree):
    def setUp(self):
        super().setUp()
        self.mod = load_helper()
        self.chip = self.hwmon(0, "nct6799")
        write(os.path.join(self.chip, "pwm1"), "97")
        write(os.path.join(self.chip, "pwm1_enable"), "5")
        self.mod.HWMON_ROOT = self.root
        self.helper = self.mod.Helper(out=io.StringIO())

    def read(self, attr):
        with open(os.path.join(self.chip, attr)) as f:
            return f.read().strip()

    def test_handshake(self):
        self.assertEqual(
            self.helper.handle("HELLO %d" % fanctl.PROTOCOL_VERSION),
            "READY %d" % fanctl.PROTOCOL_VERSION)
        self.assertTrue(self.helper.handle("HELLO 99").startswith("ERR"))

    def test_set_switches_to_manual_and_reads_back(self):
        self.assertEqual(self.helper.handle("CLAIM %s 1" % self.chip), "OK 5 97 255")
        self.assertEqual(self.helper.handle("SET %s 1 200" % self.chip), "OK 200")
        self.assertEqual(self.read("pwm1"), "200")
        self.assertEqual(self.read("pwm1_enable"), "1")

    def test_restore_puts_back_the_exact_original_mode(self):
        """Not a hardcoded 2: automatic is 5 here, and 2 on an amdgpu."""
        self.helper.handle("CLAIM %s 1" % self.chip)
        self.helper.handle("SET %s 1 200" % self.chip)
        self.helper.restore_all()
        self.assertEqual(self.read("pwm1_enable"), "5")
        self.assertEqual(self.read("pwm1"), "97")

    def test_auto_restores_one_channel(self):
        self.helper.handle("CLAIM %s 1" % self.chip)
        self.helper.handle("SET %s 1 200" % self.chip)
        self.assertEqual(self.helper.handle("AUTO %s 1" % self.chip), "OK")
        self.assertEqual(self.read("pwm1_enable"), "5")

    def test_keep_exempts_a_channel_from_the_restore(self):
        self.helper.handle("CLAIM %s 1" % self.chip)
        self.helper.handle("SET %s 1 200" % self.chip)
        self.assertEqual(self.helper.handle("KEEP %s 1" % self.chip), "OK")
        self.helper.restore_all()
        self.assertEqual(self.read("pwm1"), "200")
        self.assertEqual(self.read("pwm1_enable"), "1")

    def test_run_returns_on_eof(self):
        self.helper.handle("CLAIM %s 1" % self.chip)
        self.helper.run(stdin=io.StringIO(""))     # immediate EOF
        self.assertFalse(self.helper.finished)     # ended by EOF, not by BYE

    def test_bye_ends_the_session(self):
        self.assertEqual(self.helper.handle("BYE"), "OK")
        self.assertTrue(self.helper.finished)

    def test_driver_that_ignores_the_write_is_reported(self):
        """Several AMD cards accept a pwm write and then keep their own curve."""
        real_write = self.mod._write

        def ignore_pwm(path, value):
            if os.path.basename(path) == "pwm1":
                return                              # silently drop it
            real_write(path, value)

        self.mod._write = ignore_pwm
        self.addCleanup(setattr, self.mod, "_write", real_write)

        self.helper.handle("CLAIM %s 1" % self.chip)
        reply = self.helper.handle("SET %s 1 200" % self.chip)
        self.assertTrue(reply.startswith("ERR"), reply)
        self.assertIn("kept 97", reply)

    def test_claim_is_idempotent(self):
        """A second claim must not overwrite the saved original with our value."""
        self.helper.handle("CLAIM %s 1" % self.chip)
        self.helper.handle("SET %s 1 200" % self.chip)
        self.assertEqual(self.helper.handle("CLAIM %s 1" % self.chip), "OK 5 97 255")
        self.helper.restore_all()
        self.assertEqual(self.read("pwm1"), "97")

    def test_channel_without_enable_is_still_usable(self):
        """Some drivers expose pwmN with no mode attribute at all."""
        os.remove(os.path.join(self.chip, "pwm1_enable"))
        self.assertEqual(self.helper.handle("CLAIM %s 1" % self.chip), "OK - 97 255")
        self.assertEqual(self.helper.handle("SET %s 1 200" % self.chip), "OK 200")
        self.helper.restore_all()
        self.assertEqual(self.read("pwm1"), "97")



# The helper's whole safety guarantee is that the *process* restores the fans
# however it ends, so it is worth proving against a real process and a real
# pipe rather than a hand-called restore_all().
SHIM = """
import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader("fanhelper", %r)
spec = importlib.util.spec_from_loader("fanhelper", loader)
m = importlib.util.module_from_spec(spec)
sys.modules["fanhelper"] = m
spec.loader.exec_module(m)
m.HWMON_ROOT = %r
m.DEADMAN_SECONDS = %r
sys.exit(m.main())
"""


class HelperProcessTest(TempTree):
    """End-to-end over a pipe, with the fake sysfs tree standing in for hwmon."""

    def setUp(self):
        super().setUp()
        self.chip = self.hwmon(0, "nct6799")
        write(os.path.join(self.chip, "pwm1"), "97")
        write(os.path.join(self.chip, "pwm1_enable"), "5")

    def spawn(self, deadman=30.0):
        shim = os.path.join(self.root, "shim.py")
        write(shim, SHIM % (HELPER, self.root, deadman))
        proc = subprocess.Popen([sys.executable, shim],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, bufsize=1)
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def send(proc, command):
        proc.stdin.write(command + "\n")
        proc.stdin.flush()
        return proc.stdout.readline().strip()

    def read(self, attr):
        with open(os.path.join(self.chip, attr)) as f:
            return f.read().strip()

    def pin_a_fan(self, proc):
        self.assertEqual(self.send(proc, "HELLO %d" % fanctl.PROTOCOL_VERSION),
                         "READY %d" % fanctl.PROTOCOL_VERSION)
        self.assertEqual(self.send(proc, "CLAIM %s 1" % self.chip), "OK 5 97 255")
        self.assertEqual(self.send(proc, "SET %s 1 255" % self.chip), "OK 255")
        self.assertEqual((self.read("pwm1"), self.read("pwm1_enable")), ("255", "1"))

    def assert_restored(self):
        self.assertEqual((self.read("pwm1"), self.read("pwm1_enable")), ("97", "5"))

    def test_closing_the_pipe_restores(self):
        """The client crashed or was killed: the kernel closes the pipe for us."""
        proc = self.spawn()
        self.pin_a_fan(proc)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assert_restored()

    def test_deadman_restores_a_client_that_went_quiet(self):
        """Pipe still open, but nothing coming down it - hung or SIGSTOPped."""
        proc = self.spawn(deadman=1.0)
        self.pin_a_fan(proc)
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assert_restored()

    def test_sigterm_restores(self):
        proc = self.spawn()
        self.pin_a_fan(proc)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assert_restored()

    def test_bye_restores_and_exits(self):
        proc = self.spawn()
        self.pin_a_fan(proc)
        self.assertEqual(self.send(proc, "BYE"), "OK")
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assert_restored()

    def test_kept_channel_survives_the_exit(self):
        proc = self.spawn()
        self.pin_a_fan(proc)
        self.assertEqual(self.send(proc, "KEEP %s 1" % self.chip), "OK")
        proc.stdin.close()
        proc.wait(timeout=10)
        self.assertEqual((self.read("pwm1"), self.read("pwm1_enable")), ("255", "1"))


if __name__ == "__main__":
    unittest.main()


class RoleTest(TempTree):
    """Naming a fan after what it cools, when the driver actually says so."""

    def channel(self, **kwargs):
        chip = self.hwmon(0, kwargs.pop("chip", "nct6799"))
        write(os.path.join(chip, "pwm1"), "128")
        write(os.path.join(chip, "pwm1_enable"), "5")
        for name, value in kwargs.pop("files", {}).items():
            write(os.path.join(chip, name), value)
        return fanctl.discover_channels(self.root)[0]

    def test_temp_sel_pointing_at_the_cpu_names_a_cpu_fan(self):
        channel = self.channel(files={"pwm1_temp_sel": "7",
                                      "temp7_label": "SMBUSMASTER 0"})
        self.assertEqual(channel.source_temp, "SMBUSMASTER 0")
        self.assertTrue(fanctl.is_cpu_fan(channel))
        self.assertIn("CPU Fan 1", fanctl.default_name(channel, cards=[]))

    def test_temp_sel_pointing_at_ambient_stays_a_plain_fan(self):
        channel = self.channel(files={"pwm1_temp_sel": "1",
                                      "temp1_label": "SYSTIN"})
        self.assertFalse(fanctl.is_cpu_fan(channel))
        name = fanctl.default_name(channel, cards=[])
        self.assertIn("Fan 1", name)
        self.assertNotIn("CPU", name)

    def test_a_chip_with_no_temp_sel_is_reported_as_unsure(self):
        """Not guessing is the point: a chip that never says stays a plain fan."""
        channel = self.channel()
        self.assertIsNone(channel.source_temp)
        self.assertFalse(fanctl.is_cpu_fan(channel))
        self.assertNotIn("CPU", fanctl.default_name(channel, cards=[]))

    def test_temp_sel_pointing_at_a_label_that_does_not_exist(self):
        channel = self.channel(files={"pwm1_temp_sel": "9"})
        self.assertIsNone(channel.source_temp)
        self.assertFalse(fanctl.is_cpu_fan(channel))

    def test_a_graphics_driver_is_a_gpu_fan_without_any_sensor_link(self):
        channel = self.channel(chip="nouveau")
        self.assertIn("GPU Fan 1", fanctl.default_name(channel, cards=[]))

    def test_a_laptop_ec_is_not_called_a_motherboard(self):
        channel = self.channel(chip="dell_smm")
        self.assertTrue(fanctl.default_name(channel, cards=[])
                        .startswith("Dell SMM"))


class HardwareVarianceTest(TempTree):
    """Boards and drivers that do not look like the reference nct6799."""

    def build(self, chip="nct6799", files=None):
        path = self.hwmon(0, chip)
        for name, value in (files or {}).items():
            write(os.path.join(path, name), value)
        return path

    def test_pwm_range_comes_from_the_driver(self):
        """A driver publishing pwm1_max of 2 has three steps, and a percentage
        over three steps would round two of them together."""
        self.build("stepped", {"pwm1": "1", "pwm1_enable": "2",
                               "pwm1_max": "2"})
        channel = fanctl.discover_channels(self.root)[0]
        self.assertEqual(channel.pwm_max, 2)
        self.assertTrue(channel.coarse)

    def test_dell_smm_keeps_the_abi_range(self):
        """dell-smm has few speed states but scales them onto 0..255 and
        publishes no pwm1_max, so it is not a coarse channel."""
        self.build("dell_smm", {"pwm1": "128", "pwm1_enable": "2"})
        channel = fanctl.discover_channels(self.root)[0]
        self.assertEqual(channel.pwm_max, 255)
        self.assertFalse(channel.coarse)

    def test_a_standard_channel_is_not_coarse(self):
        self.build(files={"pwm1": "128", "pwm1_enable": "5"})
        channel = fanctl.discover_channels(self.root)[0]
        self.assertEqual(channel.pwm_max, 255)
        self.assertFalse(channel.coarse)

    def test_percentages_are_scaled_to_the_channels_own_range(self):
        self.build(files={"pwm1": "128", "pwm1_enable": "5",
                          "pwm1_min": "60", "pwm1_max": "200"})
        channel = fanctl.discover_channels(self.root)[0]
        # 0% is the driver's floor; 100% is duty_ceiling, which stops short of
        # pwm_max on purpose - see MAX_DUTY_FRACTION.
        self.assertEqual(channel.to_pwm(0), 60)
        self.assertEqual(channel.to_pwm(100), 197)
        self.assertEqual(channel.to_pwm(50), 128)
        # Never below what the driver accepts: it would clamp silently and the
        # helper's readback would then call that clamp a refusal.
        self.assertEqual(channel.to_pwm(-10), 60)
        self.assertEqual(channel.to_pwm(999), 197)
        self.assertEqual(channel.to_percent(128), 50)

    def test_manual_is_read_against_the_drivers_own_value(self):
        """enable=0 on nct6775 is 'full speed', which is not manual control."""
        self.build(files={"pwm1": "128", "pwm1_enable": "0"})
        channel = fanctl.discover_channels(self.root)[0]
        self.assertFalse(fanctl.read_state(channel).manual)

        write(os.path.join(channel.hwmon, "pwm1_enable"), "1")
        self.assertTrue(fanctl.read_state(channel).manual)

    def test_dell_smm_uses_the_standard_numbering(self):
        """dell_smm_write(): 1 turns BIOS control off, 2 turns it on."""
        self.build("dell_smm", {"pwm1": "255", "pwm1_enable": "1"})
        channel = fanctl.discover_channels(self.root)[0]
        self.assertTrue(fanctl.read_state(channel).manual)

        write(os.path.join(channel.hwmon, "pwm1_enable"), "2")
        self.assertFalse(fanctl.read_state(channel).manual)

    def test_a_write_only_mode_is_marked_unreadable(self):
        """dell-smm makes pwm1_enable 0200 where it is a global BIOS switch."""
        path = self.build("dell_smm", {"pwm1": "128", "pwm1_enable": "2"})
        os.chmod(os.path.join(path, "pwm1_enable"), 0o200)
        channel = fanctl.discover_channels(self.root)[0]
        self.assertTrue(channel.has_enable)
        self.assertTrue(channel.enable_writable)
        self.assertFalse(channel.enable_readable)

    def test_a_read_only_pwm_is_not_offered(self):
        """Several laptop drivers publish a pwm the kernel will not let even
        root write. A slider that cannot move is worse than no slider."""
        path = self.build(files={"pwm1": "128", "pwm1_enable": "5"})
        os.chmod(os.path.join(path, "pwm1"), 0o444)
        self.assertEqual(fanctl.discover_channels(self.root), [])

    def test_a_read_only_enable_is_listed_but_flagged(self):
        path = self.build(files={"pwm1": "128", "pwm1_enable": "5"})
        os.chmod(os.path.join(path, "pwm1_enable"), 0o444)
        channel = fanctl.discover_channels(self.root)[0]
        self.assertTrue(channel.has_enable)
        self.assertFalse(channel.enable_writable)

    def test_pwm_mode_is_reported_and_never_assumed(self):
        self.build(files={"pwm1": "128", "pwm1_enable": "5", "pwm1_mode": "0"})
        self.assertIs(fanctl.discover_channels(self.root)[0].pwm_mode, False)

        shutil.rmtree(self.root)
        self.build(files={"pwm1": "128", "pwm1_enable": "5"})
        self.assertIsNone(fanctl.discover_channels(self.root)[0].pwm_mode)

    def test_channels_beyond_nine_sort_numerically(self):
        """A chip with ten headers must not list pwm10 between pwm1 and pwm2."""
        files = {}
        for i in range(1, 13):
            files["pwm%d" % i] = "128"
            files["pwm%d_enable" % i] = "5"
        self.build(files=files)
        found = [c.index for c in fanctl.discover_channels(self.root)]
        self.assertEqual(found, list(range(1, 13)))

    def test_hwmon_nodes_sort_numerically(self):
        for index in (1, 2, 10):
            path = self.hwmon(index, "chip%d" % index)
            write(os.path.join(path, "pwm1"), "128")
            write(os.path.join(path, "pwm1_enable"), "5")
        found = [c.chip for c in fanctl.discover_channels(self.root)]
        self.assertEqual(found, ["chip1", "chip2", "chip10"])

    def test_a_header_with_no_tachometer_reports_no_speed(self):
        """Rather than borrowing the neighbouring fan's reading."""
        path = self.build(files={"pwm1": "128", "pwm1_enable": "5",
                                 "pwm2": "128", "pwm2_enable": "5",
                                 "fan2_input": "900"})
        channels = {c.index: c for c in fanctl.discover_channels(self.root)}
        self.assertIsNone(channels[1].fan_input)
        self.assertIsNone(fanctl.read_state(channels[1]).rpm)
        self.assertEqual(fanctl.read_state(channels[2]).rpm, 900)

    def test_nonsensical_driver_bounds_fall_back_to_the_abi_default(self):
        """The numbers come from a driver, so a pair that does not describe a
        usable range must not end up sizing the slider."""
        for files, expected in (
                ({"pwm1_max": "0"}, (0, 255)),
                ({"pwm1_max": "4096"}, (0, 255)),
                ({"pwm1_min": "200", "pwm1_max": "100"}, (0, 100)),
                ({"pwm1_min": "255", "pwm1_max": "255"}, (0, 255)),
        ):
            with self.subTest(files=files):
                shutil.rmtree(self.root, ignore_errors=True)
                base = {"pwm1": "128", "pwm1_enable": "5"}
                base.update(files)
                self.build(files=base)
                channel = fanctl.discover_channels(self.root)[0]
                self.assertEqual((channel.pwm_min, channel.pwm_max), expected)

    def test_non_fan_chips_are_skipped(self):
        self.build("acpitz", {"pwm1": "128", "pwm1_enable": "1"})
        self.assertEqual(fanctl.discover_channels(self.root), [])


class HelperHardwareTest(TempTree):
    """The helper against drivers that do not behave like the reference one."""

    def setUp(self):
        super().setUp()
        self.module = load_helper()
        self.module.HWMON_ROOT = self.root
        self.helper = self.module.Helper(out=io.StringIO())

    def chip_at(self, name, files):
        path = self.hwmon(0, name)
        for attr, value in files.items():
            write(os.path.join(path, attr), value)
        return path

    def test_dell_smm_is_taken_over_with_one(self):
        """0 is not a mode dell-smm has at all - it answers EINVAL."""
        chip = self.chip_at("dell_smm", {"pwm1": "128", "pwm1_enable": "2"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertEqual(self.helper.handle("SET %s 1 255" % chip), "OK 255")
        with open(os.path.join(chip, "pwm1_enable")) as f:
            self.assertEqual(f.read().strip(), "1")

    def quantise_like_dell_smm(self, fan_max=2):
        """Make pwm1 writes land the way dell_smm_write() stores them."""
        real_write = self.module._write
        mult = -(-255 // fan_max)

        def quantise(path, value):
            if path.endswith("pwm1"):
                state = min(fan_max, (int(value) + mult // 2) // mult)
                value = min(255, state * mult)
            real_write(path, value)

        self.module._write = quantise
        self.addCleanup(setattr, self.module, "_write", real_write)

    def test_dell_smm_steps_are_not_a_refusal(self):
        """Asked for 100 on a two-state fan, the driver keeps 128."""
        chip = self.chip_at("dell_smm", {"pwm1": "0", "pwm1_enable": "2"})
        self.quantise_like_dell_smm(fan_max=2)
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertEqual(self.helper.handle("SET %s 1 100" % chip), "OK 128")
        self.assertEqual(self.helper.handle("SET %s 1 30" % chip), "OK 0")

    def test_three_state_dell_steps_are_accepted_too(self):
        chip = self.chip_at("dell_smm", {"pwm1": "0", "pwm1_enable": "2"})
        self.quantise_like_dell_smm(fan_max=3)
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertEqual(self.helper.handle("SET %s 1 150" % chip), "OK 170")

    def test_an_ignored_dell_write_is_still_caught(self):
        """The step rule must not turn every readback into a pass."""
        chip = self.chip_at("dell_smm", {"pwm1": "0", "pwm1_enable": "2"})
        real_write = self.module._write

        def ignore_pwm(path, value):
            if not path.endswith("pwm1"):
                real_write(path, value)

        self.module._write = ignore_pwm
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertTrue(self.helper.handle("SET %s 1 255" % chip)
                        .startswith("ERR"))

    def test_the_step_rule_is_only_for_dell_smm(self):
        chip = self.chip_at("nct6799", {"pwm1": "0", "pwm1_enable": "5"})
        real_write = self.module._write

        def step(path, value):
            real_write(path, 128 if path.endswith("pwm1") else value)

        self.module._write = step
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertTrue(self.helper.handle("SET %s 1 100" % chip)
                        .startswith("ERR"))

    def test_a_value_above_the_channels_maximum_is_refused(self):
        """Refused rather than clamped: the driver would clamp it silently and
        the readback would then blame the driver for our own overreach."""
        chip = self.chip_at("stepped", {"pwm1": "1", "pwm1_enable": "2",
                                        "pwm1_max": "2"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertIn("maximum", self.helper.handle("SET %s 1 200" % chip))

    def test_claim_reports_the_channels_maximum(self):
        chip = self.chip_at("stepped", {"pwm1": "1", "pwm1_enable": "2",
                                        "pwm1_max": "2"})
        self.assertEqual(self.helper.handle("CLAIM %s 1" % chip), "OK 2 1 2")

    def test_a_nonsense_maximum_falls_back_to_the_abi_default(self):
        chip = self.chip_at("nct6799", {"pwm1": "128", "pwm1_enable": "5",
                                        "pwm1_max": "0"})
        self.assertEqual(self.helper._pwm_max(chip, 1), 255)

    def test_rounding_within_tolerance_is_accepted(self):
        """amdgpu quantises to its own steps; that is not a refused write."""
        chip = self.chip_at("amdgpu", {"pwm1": "128", "pwm1_enable": "2"})
        self.helper.handle("CLAIM %s 1" % chip)

        real_write = self.module._write

        def quantise(path, value):
            real_write(path, value - 4 if path.endswith("pwm1") else value)

        self.module._write = quantise
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.assertEqual(self.helper.handle("SET %s 1 200" % chip), "OK 196")

    def test_a_driver_that_applies_the_write_late_is_not_called_a_refusal(self):
        """Measured on a Radeon RX 6800: the card takes a few milliseconds to
        report a decrease, and reading once in that window reports a refusal
        for a write it actually accepted - a GPU fan that cannot be turned
        down."""
        chip = self.chip_at("amdgpu", {"pwm1": "228", "pwm1_enable": "1"})
        self.helper.handle("CLAIM %s 1" % chip)

        real_write = self.module._write
        real_read = self.module._read
        state = {"reads": 0}

        def lazy_write(path, value):
            if path.endswith("pwm1"):
                state["pending"] = value      # accepted, not yet visible
            else:
                real_write(path, value)

        def lagging_read(path):
            if path.endswith("pwm1"):
                state["reads"] += 1
                # Still the old value for the first couple of reads.
                if state["reads"] <= 2:
                    return "228"
                return str(state["pending"])
            return real_read(path)

        self.module._write = lazy_write
        self.module._read = lagging_read
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.addCleanup(setattr, self.module, "_read", real_read)

        self.assertEqual(self.helper.handle("SET %s 1 114" % chip), "OK 114")
        self.assertGreater(state["reads"], 1, "should have retried the readback")

    def test_a_driver_that_truly_ignores_the_write_is_still_caught(self):
        """The retry must not blunt the check it exists inside."""
        chip = self.chip_at("amdgpu", {"pwm1": "228", "pwm1_enable": "1"})
        self.helper.handle("CLAIM %s 1" % chip)

        real_write = self.module._write

        def ignore_duty(path, value):
            if not path.endswith("pwm1"):
                real_write(path, value)

        self.module._write = ignore_duty
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.assertIn("kept 228", self.helper.handle("SET %s 1 114" % chip))

    def test_a_driver_that_will_not_leave_automatic_mode_is_reported(self):
        """The firmware keeping the fan is a different failure from a bad path,
        and has to be distinguishable from a successful takeover."""
        chip = self.chip_at("thinkpad", {"pwm1": "128", "pwm1_enable": "2"})
        self.helper.handle("CLAIM %s 1" % chip)

        real_write = self.module._write

        def ignore_mode(path, value):
            if not path.endswith("_enable"):
                real_write(path, value)

        self.module._write = ignore_mode
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.assertIn("manual", self.helper.handle("SET %s 1 200" % chip))

    def test_restore_counts_as_done_when_the_mode_went_back(self):
        """Handing the channel to the driver is the safety guarantee; the duty
        write is a courtesy the driver overwrites anyway."""
        chip = self.chip_at("nct6799", {"pwm1": "128", "pwm1_enable": "5"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.handle("SET %s 1 200" % chip)

        real_write = self.module._write

        def refuse_duty(path, value):
            if path.endswith("pwm1"):
                raise OSError("driver owns the duty in automatic mode")
            real_write(path, value)

        self.module._write = refuse_duty
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.assertEqual(self.helper.handle("AUTO %s 1" % chip), "OK")
        with open(os.path.join(chip, "pwm1_enable")) as f:
            self.assertEqual(f.read().strip(), "5")


class BundledHelperTest(unittest.TestCase):
    """Installing the helper that ships alongside the app.

    This is the only route an AppImage user has: there is no checkout and no
    install.sh, so the files have to come out of the bundle itself.
    """

    def test_the_checkout_ships_both_files(self):
        helper, policy = fanctl.bundled_helper()
        self.assertIsNotNone(helper)
        self.assertTrue(os.path.isfile(helper))
        self.assertTrue(os.path.isfile(policy))

    def test_it_installs_from_a_staging_copy_not_the_bundle(self):
        """Root cannot read an AppImage's FUSE mount, so the files must be
        copied somewhere world-readable before pkexec touches them."""
        seen = {}

        def fake_pkexec(command):
            self.assertEqual(command[0], "pkexec")
            script = command[-1]
            with open(script) as handle:
                seen["script"] = handle.read()
            seen["stage"] = os.path.dirname(script)
            seen["mode"] = os.stat(seen["stage"]).st_mode & 0o777
            seen["files"] = sorted(os.listdir(seen["stage"]))
            return 0, ""

        ok, message = fanctl.install_bundled_helper(runner=fake_pkexec)
        self.assertTrue(ok, message)

        bundle = os.path.dirname(fanctl.bundled_helper()[0])
        self.assertNotEqual(seen["stage"], bundle)
        self.assertEqual(seen["mode"], 0o755)
        self.assertIn("helper", seen["files"])
        self.assertIn("policy", seen["files"])

        # The polkit action must point at the installed path, never at the
        # staging copy - it authorises whatever path it names.
        self.assertIn(fanctl.HELPER_INSTALL_PATH, seen["script"])
        self.assertIn("@HELPER_PATH@", seen["script"])
        self.assertIn("-o root -g root", seen["script"])
        self.assertNotIn("chmod 777", seen["script"])

    def test_the_staging_directory_does_not_survive(self):
        stage = {}

        def fake_pkexec(command):
            stage["path"] = os.path.dirname(command[-1])
            return 0, ""

        fanctl.install_bundled_helper(runner=fake_pkexec)
        self.assertFalse(os.path.exists(stage["path"]))

    def test_a_refused_prompt_is_reported_as_cancelled(self):
        for code, expected in ((126, "not authorised"), (127, "No password")):
            ok, message = fanctl.install_bundled_helper(
                runner=lambda command, c=code: (c, ""))
            self.assertFalse(ok)
            self.assertIn(expected.split()[-1].lower(), message.lower())

    def test_a_failure_carries_the_output_back(self):
        ok, message = fanctl.install_bundled_helper(
            runner=lambda command: (1, "install: cannot create regular file"))
        self.assertFalse(ok)
        self.assertIn("cannot create regular file", message)


class FullDutyModeTest(unittest.TestCase):
    """nct6775 renaming manual-at-full-duty to "off".

    The driver derives pwmN_enable from the fan-mode register and the current
    duty, so a channel we put under manual control reports 0 rather than 1 the
    moment the duty reaches 255. Read at face value that says the board has
    taken the fan back, which is the opposite of what has happened: the fan is
    pinned at full speed and still ours.
    """

    def channel(self, chip, **kwargs):
        return fanctl.FanChannel(hwmon="/sys/class/hwmon/hwmon3", chip=chip,
                                 index=1, has_enable=True, **kwargs)

    def state(self, chip, enable, pwm, **kwargs):
        channel = self.channel(chip, **kwargs)
        return fanctl.FanState(pwm=pwm, enable=enable, channel=channel)

    def test_full_duty_reported_as_off_is_still_manual(self):
        self.assertTrue(self.state("nct6799", enable=0, pwm=255).manual)

    def test_the_ordinary_manual_value_is_unaffected(self):
        self.assertTrue(self.state("nct6799", enable=1, pwm=200).manual)

    def test_an_automatic_curve_is_not_mistaken_for_manual(self):
        for enable in (2, 3, 4, 5):
            self.assertFalse(self.state("nct6799", enable=enable, pwm=255).manual,
                             "enable %d read as manual" % enable)

    def test_off_below_full_duty_is_not_manual(self):
        """The driver only aliases the two at the very top of the range."""
        self.assertFalse(self.state("nct6799", enable=0, pwm=254).manual)

    def test_it_follows_a_channel_whose_ceiling_is_not_255(self):
        state = self.state("nct6796", enable=0, pwm=180, pwm_max=180)
        self.assertTrue(state.manual)

    def test_other_drivers_keep_the_plain_abi_meaning(self):
        """amdgpu refuses a pwm write with enable 0, so calling that manual
        would offer a control the card will not honour."""
        self.assertFalse(self.state("amdgpu", enable=0, pwm=255).manual)
        self.assertFalse(self.state("it8792", enable=0, pwm=255).manual)
        self.assertFalse(self.state("nct6683", enable=0, pwm=255).manual)

    def test_dell_smm_is_not_inverted(self):
        """A 0 there is not manual: dell-smm refuses 0 outright."""
        self.assertTrue(self.state("dell_smm", enable=1, pwm=128).manual)
        self.assertFalse(self.state("dell_smm", enable=2, pwm=128).manual)
        self.assertFalse(self.state("dell_smm", enable=0, pwm=255).manual)

    def test_a_channel_with_no_mode_attribute_is_never_guessed_at(self):
        state = fanctl.FanState(pwm=255, enable=None,
                                channel=self.channel("nct6799"))
        self.assertFalse(state.manual)


class ClaimTrackingTest(unittest.TestCase):
    """holds() is what the window asks before offering a live slider."""

    class FakeProcess:
        def __init__(self, alive=True):
            self._alive = alive

        def poll(self):
            return None if self._alive else 0

    def client(self, alive=True, claimed=()):
        client = fanctl.HelperClient(path="/nonexistent")
        client._proc = self.FakeProcess(alive)
        client._claimed = set(claimed)
        return client

    def channel(self):
        return fanctl.FanChannel(hwmon="/sys/class/hwmon/hwmon3",
                                 chip="nct6799", index=1, has_enable=True)

    def test_a_claimed_channel_is_held(self):
        channel = self.channel()
        self.assertTrue(self.client(claimed=[channel.key]).holds(channel))

    def test_an_unclaimed_channel_is_not(self):
        self.assertFalse(self.client().holds(self.channel()))

    def test_nothing_is_held_once_the_helper_has_gone(self):
        """The helper restores every channel it held when it exits, so a claim
        recorded before that is no longer a claim on anything."""
        channel = self.channel()
        client = self.client(alive=False, claimed=[channel.key])
        self.assertFalse(client.holds(channel))


class HelperTimeoutTest(unittest.TestCase):
    """A reply that does not come in time must end the session."""

    SLOW = ("import sys, time\n"
            "sys.stdin.readline(); time.sleep(1); print('OK late', flush=True)\n"
            "for line in sys.stdin: print('OK', flush=True)\n")

    def test_a_late_reply_ends_the_session(self):
        """The reader left waiting for it would take the next command's reply
        as its own, and every answer after that would be off by one."""
        client = fanctl.HelperClient(path="/nonexistent")
        client._proc = subprocess.Popen(
            [sys.executable, "-c", self.SLOW], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, bufsize=1)
        proc = client._proc
        self.addCleanup(lambda: proc.poll() is None and proc.kill())

        with self.assertRaises(fanctl.HelperError):
            client._exchange("PING", timeout=0.2)
        self.assertFalse(client.running)
        # Ended by EOF, which is what makes the real helper restore the fans.
        self.assertIsNotNone(proc.poll())

    def test_a_helper_that_cannot_be_signalled_is_not_fatal(self):
        """pkexec runs it as root, so terminate() is refused with EPERM."""
        class RootProcess:
            pid = 4242
            stdin = stdout = None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("helper", timeout)

            def poll(self):
                return None

            def terminate(self):
                raise PermissionError(1, "Operation not permitted")

            kill = terminate

        client = fanctl.HelperClient(path="/nonexistent")
        client._proc = RootProcess()
        client._reap()                  # must not raise
        self.assertFalse(client.running)


class HelperStartTest(unittest.TestCase):
    """The polkit prompt must not freeze the fan window."""

    def setUp(self):
        # Same as the other widget tests: no display needed.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    def test_start_runs_off_the_gui_thread(self):
        import threading

        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        from ksyssupervisor.fanwindow import FanControlWindow

        app = QApplication.instance() or QApplication([])
        gui = threading.current_thread()
        seen = {}

        class Client:
            def start(self):
                seen["thread"] = threading.current_thread()
                time.sleep(0.3)         # the user reading the prompt

        class Window:
            client = Client()

        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(20)
        error = FanControlWindow._start_helper(Window())
        timer.stop()

        self.assertIsNone(error)
        self.assertIsNot(seen["thread"], gui)
        # The event loop kept running while start() blocked.
        self.assertGreater(len(ticks), 3)
        del app

    def test_a_refusal_comes_back_as_the_error(self):
        from PyQt6.QtWidgets import QApplication

        from ksyssupervisor.fanwindow import FanControlWindow

        app = QApplication.instance() or QApplication([])

        class Client:
            def start(self):
                raise fanctl.HelperError("not authorised")

        class Window:
            client = Client()

        error = FanControlWindow._start_helper(Window())
        self.assertIsInstance(error, fanctl.HelperError)
        del app


class SectionTest(unittest.TestCase):
    """GPU fans first, then CPU fans, then the rest of the board."""

    def channel(self, chip, index, source_temp=None):
        return fanctl.FanChannel(hwmon="/sys/class/hwmon/hwmon%d" % index,
                                 chip=chip, index=index, has_enable=True,
                                 source_temp=source_temp)

    def test_sections_come_in_order_whatever_the_discovery_order(self):
        case = self.channel("nct6799", 3)
        cpu = self.channel("nct6799", 1, source_temp="CPUTIN")
        gpu = self.channel("amdgpu", 1)
        got = fanctl.sections([case, cpu, gpu])
        self.assertEqual([(title, [c.key for c in members])
                          for title, members in got],
                         [("GPU Fans", [gpu.key]), ("CPU Fans", [cpu.key]),
                          ("Motherboard Fans", [case.key])])

    def test_discovery_order_is_kept_inside_a_section(self):
        fans = [self.channel("nct6799", i) for i in (2, 5, 7)]
        [(_title, members)] = fanctl.sections(fans)
        self.assertEqual([c.index for c in members], [2, 5, 7])

    def test_empty_sections_are_left_out(self):
        self.assertEqual([t for t, _m in fanctl.sections(
            [self.channel("amdgpu", 1)])], ["GPU Fans"])
        self.assertEqual(fanctl.sections([]), [])

    def test_a_laptop_ec_is_not_a_motherboard(self):
        self.assertEqual([t for t, _m in fanctl.sections(
            [self.channel("thinkpad", 1)])], ["System Fans"])


class AvailabilityTest(unittest.TestCase):
    """What Tools > Fan Control does when clicked, decided up front."""

    def state(self, channels=True, helper="/usr/local/lib/x", pkexec=True,
              version=None, bundled=True):
        from unittest import mock

        version = fanctl.PROTOCOL_VERSION if version is None else version
        which = (lambda name: "/usr/bin/" + name
                 if pkexec or name != "pkexec" else None)
        with mock.patch.object(fanctl, "discover_channels",
                               return_value=[object()] if channels else []), \
                mock.patch.object(fanctl, "helper_path", return_value=helper), \
                mock.patch.object(fanctl, "installed_helper_version",
                                  return_value=version), \
                mock.patch.object(fanctl, "bundled_helper",
                                  return_value=("h", "p") if bundled
                                  else (None, None)), \
                mock.patch.object(fanctl.shutil, "which", side_effect=which):
            state, reason = fanctl.availability()
            return state, reason, fanctl.can_install_helper(state)

    def test_ready(self):
        self.assertEqual(self.state(), (fanctl.READY, None, False))

    def test_a_missing_helper_can_be_installed_from_the_app(self):
        state, reason, fixable = self.state(helper=None)
        self.assertEqual(state, fanctl.NO_HELPER)
        self.assertTrue(fixable)
        self.assertNotIn("Restart", reason)

    def test_an_outdated_helper_can_be_updated_from_the_app(self):
        state, _reason, fixable = self.state(version=1)
        self.assertEqual(state, fanctl.OUTDATED_HELPER)
        self.assertTrue(fixable)

    def test_nothing_to_install_from_is_not_fixable(self):
        """A copy without the bundled helper - an old install.sh install."""
        self.assertFalse(self.state(helper=None, bundled=False)[2])

    def test_no_pkexec_is_not_fixable(self):
        self.assertFalse(self.state(helper=None, pkexec=False)[2])

    def test_no_fan_headers_is_not_fixable(self):
        state, _reason, fixable = self.state(channels=False, helper=None)
        self.assertEqual(state, fanctl.NO_CHANNELS)
        self.assertFalse(fixable)

    def test_the_client_finds_a_helper_installed_after_it_was_made(self):
        from unittest import mock

        with mock.patch.object(fanctl, "helper_path", return_value=None):
            client = fanctl.HelperClient()
        with mock.patch.object(fanctl, "helper_path",
                               return_value="/nonexistent/helper"), \
                mock.patch.object(fanctl.subprocess, "Popen",
                                  side_effect=OSError("no pkexec here")):
            with self.assertRaises(fanctl.HelperError) as caught:
                client.start()
        # Got as far as launching it, rather than "not installed".
        self.assertIn("pkexec", str(caught.exception))


class DutyCeilingTest(unittest.TestCase):
    """The slider stops short of the channel's real maximum.

    Belt and braces over FanState.manual: nct6775 gives the top of the range a
    second meaning, and the cheapest way not to be caught by that - or by the
    same trick in a driver nobody has read - is never to write the value.
    """

    def channel(self, chip="nct6799", **kwargs):
        return fanctl.FanChannel(hwmon="/sys/class/hwmon/hwmon3", chip=chip,
                                 index=1, has_enable=True, **kwargs)

    def test_full_slider_never_reaches_the_hardware_maximum(self):
        channel = self.channel()
        self.assertEqual(channel.to_pwm(100), 250)
        self.assertLess(channel.to_pwm(100), channel.pwm_max)

    def test_it_is_within_a_couple_of_percent_of_the_top(self):
        """Giving up real cooling headroom would be a poor trade for this."""
        channel = self.channel()
        self.assertGreaterEqual(channel.to_pwm(100), int(channel.pwm_max * 0.97))

    def test_the_round_trip_is_exact_at_every_step(self):
        """A scale that reported 98% for a slider just put at 100% would snap
        it back on the next refresh - the bug this guards against, reinvented."""
        channel = self.channel()
        for percent in range(0, 101):
            self.assertEqual(channel.to_percent(channel.to_pwm(percent)),
                             percent, "%d%% did not round-trip" % percent)

    def test_a_floor_is_respected_as_well_as_the_ceiling(self):
        channel = self.channel(pwm_min=60, pwm_max=200)
        self.assertEqual(channel.to_pwm(0), 60)
        self.assertEqual(channel.to_pwm(100), 197)
        self.assertEqual(channel.to_percent(channel.to_pwm(100)), 100)

    def test_a_board_running_flat_out_still_reads_as_100(self):
        """The board's own curve is free to use the top of the range."""
        self.assertEqual(self.channel().to_percent(255), 100)

    def test_coarse_channels_keep_their_top_step(self):
        """On a 0..2 range there is nothing to trim, and trimming would cost
        the high setting outright."""
        for ceiling in (2, 3):
            channel = self.channel(chip="stepped", pwm_max=ceiling)
            self.assertTrue(channel.coarse)
            self.assertEqual(channel.duty_ceiling, ceiling)

    def test_the_helper_would_accept_the_capped_value(self):
        """It clamps to the channel's real ceiling, so a lower value passes
        through untouched and the readback cannot call it a refusal."""
        channel = self.channel()
        self.assertLessEqual(channel.to_pwm(100), channel.pwm_max)


class HandBackTest(TempTree):
    """AUTO must mean automatic, not "whatever CLAIM happened to find".

    The two were the same code, which is wrong the moment a channel is already
    manual when it is claimed: reopening the window onto a fan a previous
    session deliberately left pinned, pressing Automatic, and watching the row
    snap straight back to Manual because "as found" was manual.
    """

    def setUp(self):
        super().setUp()
        self.module = load_helper()
        self.module.HWMON_ROOT = self.root
        self.helper = self.module.Helper(out=io.StringIO())

    def chip_at(self, name, files):
        path = self.hwmon(0, name)
        for attr, value in files.items():
            write(os.path.join(path, attr), value)
        return path

    def enable(self, chip):
        with open(os.path.join(chip, "pwm1_enable")) as handle:
            return handle.read().strip()

    def test_a_channel_claimed_in_manual_still_goes_automatic(self):
        """The regression, exactly: found manual, asked for automatic."""
        chip = self.chip_at("nct6799", {"pwm1": "200", "pwm1_enable": "1"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertTrue(self.helper.handle("AUTO %s 1" % chip).startswith("OK"))
        self.assertNotEqual(self.enable(chip), "1")
        self.assertEqual(self.enable(chip), "5")

    def test_the_boards_own_mode_is_preferred_over_a_guess(self):
        """A channel found on SmartFan III goes back to III, not to IV."""
        chip = self.chip_at("nct6799", {"pwm1": "128", "pwm1_enable": "4"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.handle("SET %s 1 200" % chip)
        self.assertEqual(self.enable(chip), "1")
        self.helper.handle("AUTO %s 1" % chip)
        self.assertEqual(self.enable(chip), "4")

    def test_full_speed_is_never_treated_as_automatic(self):
        """enable 0 is "no fan speed control" - the fan runs flat out. Handing
        a fan back must not mean pinning it at maximum."""
        chip = self.chip_at("nct6799", {"pwm1": "255", "pwm1_enable": "0"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.handle("AUTO %s 1" % chip)
        self.assertNotEqual(self.enable(chip), "0")

    def dell_refuses_what_it_lacks(self):
        """dell_smm_write() answers EINVAL to anything but 1 and 2."""
        real_write = self.module._write

        def dell(path, value):
            if path.endswith("_enable") and int(value) not in (1, 2):
                raise OSError(22, "Invalid argument")
            real_write(path, value)

        self.module._write = dell
        self.addCleanup(setattr, self.module, "_write", real_write)

    def test_dell_smm_goes_back_to_the_firmware_with_two(self):
        """The regression: AUTO used to write 1, which is BIOS control *off*."""
        chip = self.chip_at("dell_smm", {"pwm1": "255", "pwm1_enable": "1"})
        self.dell_refuses_what_it_lacks()
        self.helper.handle("CLAIM %s 1" % chip)
        self.assertEqual(self.helper.handle("AUTO %s 1" % chip), "OK")
        self.assertEqual(self.enable(chip), "2")

    def write_only_dell(self):
        chip = self.chip_at("dell_smm", {"pwm1": "128", "pwm1_enable": "2"})
        os.chmod(os.path.join(chip, "pwm1_enable"), 0o200)
        self.addCleanup(os.chmod, os.path.join(chip, "pwm1_enable"), 0o644)
        self.dell_refuses_what_it_lacks()
        return chip

    def enable_of_write_only(self, chip):
        path = os.path.join(chip, "pwm1_enable")
        os.chmod(path, 0o644)
        try:
            return self.enable(chip)
        finally:
            os.chmod(path, 0o200)

    def test_a_write_only_mode_can_still_be_taken_over(self):
        """Reading it first used to fail every SET with "will not release"."""
        chip = self.write_only_dell()
        self.assertEqual(self.helper.handle("CLAIM %s 1" % chip), "OK - 128 255")
        self.assertEqual(self.helper.handle("SET %s 1 255" % chip), "OK 255")
        self.assertEqual(self.enable_of_write_only(chip), "1")

    def test_a_write_only_mode_is_handed_back_on_exit(self):
        """Nothing was recorded to restore, and leaving the mode alone would
        leave the BIOS regulation off after the app has gone."""
        chip = self.write_only_dell()
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.handle("SET %s 1 255" % chip)
        self.helper.restore_all()
        self.assertEqual(self.enable_of_write_only(chip), "2")
        with open(os.path.join(chip, "pwm1")) as handle:
            self.assertEqual(handle.read().strip(), "128")

    def test_a_write_only_mode_is_left_alone_when_never_taken(self):
        chip = self.write_only_dell()
        write_enable = os.path.join(chip, "pwm1_enable")
        os.chmod(write_enable, 0o644)
        write(write_enable, "1")        # as a previous session might leave it
        os.chmod(write_enable, 0o200)
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.restore_all()
        self.assertEqual(self.enable_of_write_only(chip), "1")

    def test_auto_on_a_write_only_mode_trusts_the_write(self):
        chip = self.write_only_dell()
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.handle("SET %s 1 255" % chip)
        self.assertEqual(self.helper.handle("AUTO %s 1" % chip), "OK")
        self.assertEqual(self.enable_of_write_only(chip), "2")
        # And a second take-over writes the mode again rather than assuming it.
        write(os.path.join(chip, "pwm1_enable"), "2")
        self.helper.handle("SET %s 1 255" % chip)
        self.assertEqual(self.enable_of_write_only(chip), "1")

    def test_a_driver_that_refuses_every_mode_is_reported(self):
        chip = self.chip_at("nct6799", {"pwm1": "200", "pwm1_enable": "1"})
        self.helper.handle("CLAIM %s 1" % chip)

        real_write = self.module._write

        def refuse_enable(path, value):
            if path.endswith("pwm1_enable"):
                raise OSError("read-only")
            real_write(path, value)

        self.module._write = refuse_enable
        self.addCleanup(setattr, self.module, "_write", real_write)
        self.assertTrue(self.helper.handle("AUTO %s 1" % chip).startswith("ERR"))

    def test_the_exit_restore_does_not_undo_the_hand_back(self):
        """Restoring "as found" on the way out would put the channel back into
        the manual mode AUTO was just asked to leave."""
        chip = self.chip_at("nct6799", {"pwm1": "200", "pwm1_enable": "1"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.handle("AUTO %s 1" % chip)
        self.assertEqual(self.enable(chip), "5")
        self.helper.restore_all()
        self.assertEqual(self.enable(chip), "5")

    def test_exit_still_restores_a_channel_that_was_left_alone(self):
        """The crash-safety promise is unchanged for the ordinary case."""
        chip = self.chip_at("nct6799", {"pwm1": "97", "pwm1_enable": "5"})
        self.helper.handle("CLAIM %s 1" % chip)
        self.helper.handle("SET %s 1 200" % chip)
        self.assertEqual(self.enable(chip), "1")
        self.helper.restore_all()
        self.assertEqual(self.enable(chip), "5")
        with open(os.path.join(chip, "pwm1")) as handle:
            self.assertEqual(handle.read().strip(), "97")


class HelperVersionTest(unittest.TestCase):
    """Recognising an installed helper from another version of the app.

    The helper is updated by an install step the app cannot perform for
    itself, so nothing keeps it in step with the code that talks to it. When
    the two drifted apart in practice, AUTO quietly meant the wrong thing and
    the Automatic button put fans straight back into manual - the check has to
    happen before that, not after.
    """

    def _helper_file(self, text):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(text)
        handle.close()
        return handle.name

    def test_reads_the_version_out_of_the_shipped_helper(self):
        helper, _ = fanctl.bundled_helper()
        self.assertEqual(fanctl.installed_helper_version(helper),
                         fanctl.PROTOCOL_VERSION)

    def test_an_older_helper_reports_its_own_version(self):
        path = self._helper_file(
            "#!/usr/bin/env python3\nPROTOCOL_VERSION = 1\n")
        self.assertEqual(fanctl.installed_helper_version(path), 1)

    def test_a_file_without_a_version_reads_as_unknown(self):
        path = self._helper_file("#!/usr/bin/env python3\npass\n")
        self.assertIsNone(fanctl.installed_helper_version(path))

    def test_a_missing_file_reads_as_unknown(self):
        self.assertIsNone(
            fanctl.installed_helper_version("/nonexistent/helper"))

    def test_the_mismatch_message_names_the_fix(self):
        reason = fanctl._outdated_helper_reason()
        self.assertIn("--install-fan-helper", reason)
        self.assertIn("older version", reason)

    def test_an_old_helper_refusal_names_the_fix(self):
        """The handshake backstop, driven through a scripted exchange."""
        client = fanctl.HelperClient(path="/nonexistent/helper")

        def refuse(command, timeout=None):
            raise fanctl.HelperError("unsupported protocol version")
        client._exchange = refuse
        with self.assertRaises(fanctl.HelperError) as caught:
            client._hello()
        self.assertIn("--install-fan-helper", str(caught.exception))


class FanTileThemeTest(TempTree):
    def test_a_light_theme_does_not_trip_the_tile_while_it_is_built(self):
        """Breeze Light's placeholder is not the card's dim text colour, so
        Card.__init__ rewrites the palette - and the PaletteChange it sends
        reached FanTile._sync_accent before manual_button existed. The
        offscreen palette's placeholder is plain black, which hid it."""
        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtWidgets import QApplication

        from ksyssupervisor.fanwindow import FanTile

        app = QApplication.instance() or QApplication([])
        saved = QApplication.palette()
        palette = QPalette(saved)
        palette.setColor(QPalette.ColorRole.Window, QColor("#eff0f1"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#232629"))
        palette.setColor(QPalette.ColorRole.PlaceholderText,
                         QColor(112, 125, 138))
        QApplication.setPalette(palette)
        self.addCleanup(QApplication.setPalette, saved)

        errors = []
        hook = sys.excepthook
        sys.excepthook = lambda *info: errors.append(info)
        self.addCleanup(setattr, sys, "excepthook", hook)

        chip = self.hwmon(0, "nct6799")
        write(os.path.join(chip, "pwm1"), "128")
        write(os.path.join(chip, "pwm1_enable"), "5")
        write(os.path.join(chip, "fan1_input"), "900")
        channel, = fanctl.discover_channels(self.root)
        tile = FanTile(channel, panel=None, name="CPU fan")
        self.assertEqual(errors, [])
        self.assertFalse(tile.manual_button.isChecked())
        del app
