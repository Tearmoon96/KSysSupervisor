"""Provider tests driven by recorded sensor fixtures.

Providers are pure with respect to their input, so hardware the developer does
not own can still be covered by recording its `sensors -j` output once.
"""

import json
import os
import shutil
import tempfile
import unittest

from ksyssupervisor.providers import (chip_base, device_slots, kernel, memory,
                                     storage)
from ksyssupervisor.providers.base import GROUP_ORDER, ReadContext
from ksyssupervisor.providers.cpu import CpuProvider
from ksyssupervisor.providers.mainboard import MainboardProvider
from ksyssupervisor.providers.memory import MemoryProvider

# The machine's own DIMM slots must not leak into a fixture's: a test that
# wants firmware slot data writes its own file.
memory.UDEV_DMI = os.devnull

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def context(chips):
    """A ReadContext over the given chips, with slots resolved as in production."""
    return ReadContext(chips, device_slots(chips), "lm_sensors")


def by_label(readings):
    return {r.label: r for r in readings}


class ChipNamingTest(unittest.TestCase):
    def test_chip_base_handles_both_backends(self):
        self.assertEqual(chip_base("k10temp-pci-00c3"), "k10temp")
        self.assertEqual(chip_base("k10temp-hwmon3"), "k10temp")
        self.assertEqual(chip_base("nct6798-isa-0290"), "nct6798")

    def test_identical_chips_are_numbered(self):
        slots = device_slots(load("ddr4_jc42.json"))
        self.assertEqual(sorted(slots.values()), [1, 2])

    def test_unique_chip_gets_no_number(self):
        slots = device_slots(load("superio_nct6798.json"))
        self.assertEqual(list(slots.values()), [0])


class IntelCpuTest(unittest.TestCase):
    """coretemp labels its sensors quite differently from k10temp."""

    def setUp(self):
        chips = load("intel_coretemp.json")
        self.readings = [r for r in CpuProvider()._from_hwmon(context(chips))]

    def test_package_and_every_core_are_reported(self):
        labels = sorted(by_label(self.readings))
        self.assertEqual(labels, ["Core 0", "Core 1", "Core 2", "Core 3", "Package 0"])

    def test_package_id_is_renamed(self):
        # "Package id 0" is the driver's name; the UI should not show "id".
        self.assertIn("Package 0", by_label(self.readings))

    def test_all_are_temperatures(self):
        for reading in self.readings:
            self.assertEqual(reading.group, "Temperatures")
            self.assertEqual(reading.kind, "temp")
            self.assertEqual(reading.device, "cpu")


class CpuVarietyTest(unittest.TestCase):
    """CPUs that do not look like the Zen 5 desktop this was written on."""

    def read(self, chips):
        return CpuProvider()._from_hwmon(context(chips))

    def test_pre_zen_k10temp_still_reports_its_temperature(self):
        """k10temp publishes temp1_label only on Zen; a Phenom, FX or A-series
        APU has a bare temp1, which the old label test threw away."""
        readings = self.read({"k10temp-pci-00c3": {
            "Adapter": "PCI adapter",
            "temp1": {"temp1_input": 38.5, "temp1_max": 70.0}}})
        self.assertEqual([(r.label, r.value) for r in readings],
                         [("Package", 38.5)])

    def test_every_ccd_is_named_alike(self):
        readings = self.read({"k10temp-pci-00c3": {
            "Tctl": {"temp1_input": 60.0},
            "Tccd1": {"temp3_input": 55.0},
            "Tccd2": {"temp4_input": 57.0},
            "Tccd12": {"temp14_input": 50.0}}})
        self.assertEqual(sorted(r.label for r in readings),
                         ["Core (CCD1)", "Core (CCD12)", "Core (CCD2)",
                          "Package"])

    def test_two_sockets_do_not_overwrite_each_other(self):
        chips = {
            "coretemp-isa-0000": {"Package id 0": {"temp1_input": 50.0},
                                  "Core 0": {"temp2_input": 48.0}},
            "coretemp-isa-0001": {"Package id 1": {"temp1_input": 61.0},
                                  "Core 0": {"temp2_input": 59.0}}}
        readings = self.read(chips)
        self.assertEqual(len({r.uid for r in readings}), 4)
        got = {r.label: r.value for r in readings}
        self.assertEqual(got, {"CPU 1 Package": 50.0, "CPU 1 Core 0": 48.0,
                               "CPU 2 Package": 61.0, "CPU 2 Core 0": 59.0})

    def test_two_k10temp_chips_are_numbered(self):
        readings = self.read({"k10temp-pci-00c3": {"Tctl": {"temp1_input": 40.0}},
                              "k10temp-pci-00cb": {"Tctl": {"temp1_input": 45.0}}})
        self.assertEqual(sorted(r.label for r in readings),
                         ["CPU 1 Package", "CPU 2 Package"])

    def test_zenpower_rails_are_reported(self):
        readings = self.read({"zenpower-pci-00c3": {
            "Tdie": {"temp1_input": 50.0},
            "SVI2_Core": {"in1_input": 1.1},
            "SVI2_SoC": {"in2_input": 0.95},
            "SVI2_P_Core": {"power1_input": 40.0},
            "SVI2_P_SoC": {"power2_input": 12.0},
            "SVI2_C_Core": {"curr1_input": 36.0}}})
        got = {(r.group, r.label): r.value for r in readings}
        self.assertEqual(got[("Powers", "Core")], 40.0)
        self.assertEqual(got[("Powers", "SoC")], 12.0)
        self.assertEqual(got[("Voltages", "Core")], 1.1)
        self.assertEqual(got[("Voltages", "SoC")], 0.95)
        self.assertEqual(got[("Temperatures", "Core")], 50.0)

    def test_older_amd_package_power_is_claimed(self):
        provider = CpuProvider()
        self.assertTrue(provider.claims("fam15h_power"))
        self.assertTrue(provider.claims("k8temp"))
        readings = provider._from_hwmon(context(
            {"fam15h_power-pci-00c4": {"power1": {"power1_input": 35.0}}}))
        self.assertEqual([(r.group, r.label) for r in readings],
                         [("Powers", "Package")])


class Ddr4MemoryTest(unittest.TestCase):
    """DDR4 reports through jc42; DDR5 through spd5118."""

    def test_jc42_dimms_are_reported_as_ram(self):
        provider = MemoryProvider()
        chips = load("ddr4_jc42.json")
        readings = [r for r in provider.read(context(chips))
                    if r.group == "Temperatures"]
        self.assertEqual(sorted(by_label(readings)), ["DIMM 1", "DIMM 2"])
        for reading in readings:
            self.assertEqual(reading.device, "ram")

    def test_jc42_is_claimed(self):
        self.assertTrue(MemoryProvider().claims("jc42"))
        self.assertTrue(MemoryProvider().claims("spd5118"))

    def test_advice_offered_when_no_dimm_sensor_exists(self):
        provider = MemoryProvider()
        provider.read(context({}))
        self.assertTrue(any("jc42" in tip.as_text() for tip in provider.advice()))

    def test_no_advice_when_dimms_are_readable(self):
        provider = MemoryProvider()
        provider.read(context(load("ddr4_jc42.json")))
        self.assertEqual(provider.advice(), [])


class SuperIoTest(unittest.TestCase):
    """Board voltages and fans were previously dropped entirely."""

    def setUp(self):
        self.provider = MainboardProvider()
        self.readings = self.provider.read(context(load("superio_nct6798.json")))
        self.groups = {}
        for reading in self.readings:
            self.groups.setdefault(reading.group, []).append(reading)

    def test_voltage_rails_are_reported(self):
        labels = sorted(by_label(self.groups["Voltages"]))
        self.assertEqual(labels, ["+12V", "+5V", "3VCC", "Rail 5", "VBAT", "Vcore"])

    def test_voltage_values_are_passed_through(self):
        self.assertAlmostEqual(by_label(self.groups["Voltages"])["+12V"].value, 12.096)

    def test_bare_rail_gets_a_readable_name(self):
        # "in5" has no label in the driver, so it must not surface as "in5".
        self.assertIn("Rail 5", by_label(self.groups["Voltages"]))

    def test_intrusion_alarm_is_not_a_voltage(self):
        # intrusion0_alarm starts with "in" but is not a rail.
        self.assertNotIn("Intrusion0", by_label(self.groups["Voltages"]))

    def test_fans_are_reported(self):
        self.assertEqual(sorted(by_label(self.groups["Fans"])), ["Fan1", "Fan2"])

    def test_temperatures_are_reported(self):
        self.assertEqual(len(self.groups["Temperatures"]), 2)

    def test_acronym_labels_survive_intact(self):
        # SYSTIN/CPUTIN are what `sensors` prints; "Systin" would not match.
        labels = " ".join(by_label(self.groups["Temperatures"]))
        self.assertIn("SYSTIN", labels)
        self.assertIn("CPUTIN", labels)

    def test_no_superio_advice_when_chip_is_present(self):
        self.assertEqual(self.provider.advice(), [])

    def test_superio_advice_when_chip_is_absent(self):
        provider = MainboardProvider()
        provider.read(context({"nvme-pci-0700": {"Composite": {"temp1_input": 30.0}}}))
        self.assertTrue(any("sensors-detect" in tip.as_text()
                            for tip in provider.advice()))


class RealCaptureTest(unittest.TestCase):
    """A real `sensors -j` capture, to pin behaviour against actual hardware.

    Recorded from a Ryzen 7 9700X / RX 6800 / DDR5 machine. The GPU is read
    through its own sysfs node rather than this snapshot, so only the CPU,
    memory and leftover chips appear here.
    """

    def setUp(self):
        self.chips = load("amd_ryzen_9700x_rx6800.json")

    def test_k10temp_labels_are_humanised(self):
        readings = CpuProvider()._from_hwmon(context(
            {k: v for k, v in self.chips.items() if k.startswith("k10temp")}))
        labels = sorted(by_label(readings))
        self.assertEqual(labels, ["Core (CCD1)", "Package"])

    def test_ddr5_dimms_are_numbered(self):
        chips = {k: v for k, v in self.chips.items() if k.startswith("spd5118")}
        readings = [r for r in MemoryProvider().read(context(chips))
                    if r.group == "Temperatures"]
        self.assertEqual(sorted(by_label(readings)), ["DIMM 1", "DIMM 2"])

    def test_leftover_chips_get_readable_prefixes(self):
        chips = {k: v for k, v in self.chips.items()
                 if k.startswith(("nvme", "r8169", "mt7921"))}
        labels = " ".join(by_label(MainboardProvider().read(context(chips))))
        self.assertIn("NVMe", labels)
        self.assertIn("Wi-Fi", labels)
        self.assertIn("LAN", labels)

    def test_two_nvme_drives_are_distinguishable(self):
        chips = {k: v for k, v in self.chips.items() if k.startswith("nvme")}
        labels = by_label(MainboardProvider().read(context(chips)))
        self.assertTrue(any("NVMe 1" in l for l in labels), labels)
        self.assertTrue(any("NVMe 2" in l for l in labels), labels)


class GroupContractTest(unittest.TestCase):
    """Every group a provider emits must be one the UI knows how to order."""

    def test_groups_are_known(self):
        cases = [
            (CpuProvider()._from_hwmon, load("intel_coretemp.json")),
            (MemoryProvider().read, load("ddr4_jc42.json")),
            (MainboardProvider().read, load("superio_nct6798.json")),
        ]
        for read, chips in cases:
            for reading in read(context(chips)):
                self.assertIn(reading.group, GROUP_ORDER,
                              "%s emitted unknown group %r" % (read, reading.group))


if __name__ == "__main__":
    unittest.main()


class AdviceShapeTest(unittest.TestCase):
    """Advice is structured data now, so the UI can lay it out properly."""

    def _all_advice(self):
        return every_advice(self)

    def test_every_tip_is_fully_populated(self):
        tips = self._all_advice()
        self.assertTrue(tips)
        for tip in tips:
            self.assertTrue(tip.key, "missing key")
            self.assertTrue(tip.title, "missing title on %s" % tip.key)
            self.assertTrue(tip.problem, "missing problem on %s" % tip.key)
            self.assertTrue(tip.effect, "missing effect on %s" % tip.key)
            self.assertTrue(tip.steps, "no commands on %s" % tip.key)
            self.assertTrue(tip.result, "missing result on %s" % tip.key)

    def test_every_command_has_a_note_explaining_it(self):
        for tip in self._all_advice():
            for step in tip.steps:
                self.assertTrue(step.command.strip())
                self.assertTrue(step.note, "%s: %r has no note"
                                % (tip.key, step.command))

    def test_keys_are_unique(self):
        keys = [t.key for t in self._all_advice()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_as_text_still_renders_for_the_log(self):
        for tip in self._all_advice():
            text = tip.as_text()
            self.assertIn(tip.title, text)
            for step in tip.steps:
                self.assertIn(step.command, text)


class KernelModuleTest(unittest.TestCase):
    """The modprobe tips must not promise what the running kernel cannot do.

    Reproduces the case that motivated this: after a kernel upgrade the running
    kernel's module tree is deleted, so 'sudo modprobe drivetemp' fails with
    "module not found" even though the driver is installed for the new kernel.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._root = kernel.MODULES_ROOT
        kernel.MODULES_ROOT = self.tmp
        self.addCleanup(setattr, kernel, "MODULES_ROOT", self._root)
        # The real /sys/module says what this machine happens to have loaded,
        # which is not what these tests are about: a dev box with drivetemp
        # loaded would otherwise report "loaded" for every case below.
        self.sys_module = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.sys_module, True)
        self._sys_root = kernel.SYS_MODULE_ROOT
        kernel.SYS_MODULE_ROOT = self.sys_module
        self.addCleanup(setattr, kernel, "SYS_MODULE_ROOT", self._sys_root)
        kernel._INDEX_CACHE.clear()

    def _tree(self, release, modules=(), builtin=()):
        path = os.path.join(self.tmp, release)
        os.makedirs(path)
        with open(os.path.join(path, "modules.dep"), "w") as f:
            for m in modules:
                f.write("kernel/drivers/hwmon/%s.ko.zst: kernel/lib/foo.ko.zst\n" % m)
        with open(os.path.join(path, "modules.builtin"), "w") as f:
            for m in builtin:
                f.write("kernel/drivers/hwmon/%s.ko\n" % m)
        return path

    def test_missing_tree_blocks_every_module(self):
        self._tree("9.9.9-other", modules=["drivetemp"])
        self.assertFalse(kernel.modules_ready())
        self.assertEqual(kernel.module_status("drivetemp"), "unknown")

        tips = kernel.advice()
        self.assertEqual(len(tips), 1)
        self.assertEqual(tips[0].key, "kernel-modules-missing")
        self.assertIn("9.9.9-other", tips[0].problem)
        self.assertIn(kernel.release(), tips[0].problem)

    def test_blocked_modprobe_step_says_so(self):
        self._tree("9.9.9-other")
        step = kernel.modprobe_step("drivetemp", "Loads the driver.")
        self.assertEqual(step.command, "sudo modprobe drivetemp")
        self.assertIn("reboot", step.note.lower())
        self.assertIn("Loads the driver.", step.note)

    def test_present_tree_keeps_the_provider_note(self):
        self._tree(kernel.release(), modules=["drivetemp"], builtin=["k10temp"])
        self.assertTrue(kernel.modules_ready())
        self.assertEqual(kernel.advice(), [])
        self.assertEqual(kernel.module_status("drivetemp"), "available")
        self.assertEqual(kernel.module_status("k10temp"), "available")
        os.makedirs(os.path.join(self.sys_module, "k10temp"))
        self.assertEqual(kernel.module_status("k10temp"), "loaded")

        step = kernel.modprobe_step("drivetemp", "Loads the driver.")
        self.assertEqual(step.note, "Loads the driver.")

    def test_module_absent_from_this_kernel(self):
        self._tree(kernel.release(), modules=["k10temp"])
        self.assertEqual(kernel.module_status("drivetemp"), "absent")
        step = kernel.modprobe_step("drivetemp", "Loads the driver.")
        self.assertIn("does not ship this module", step.note)
        self.assertIn(kernel.release(), step.note)

    def test_index_is_cached_but_follows_the_tree(self):
        path = self._tree(kernel.release(), modules=["k10temp"])
        self.assertEqual(kernel.module_status("drivetemp"), "absent")

        with open(os.path.join(path, "modules.dep"), "a") as f:
            f.write("kernel/drivers/hwmon/drivetemp.ko.zst:\n")
        os.utime(path, (0, 0))          # a changed tree must invalidate the cache
        self.assertEqual(kernel.module_status("drivetemp"), "available")


class DuplicateDriveNameTest(unittest.TestCase):
    """Two drives of the same model must not become two identical categories."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hwmon = os.path.join(self.tmp, "hwmon")
        os.makedirs(self.hwmon)
        for root, value in (("HWMON_ROOT", self.hwmon),):
            self.addCleanup(setattr, storage, root, getattr(storage, root))
        storage.HWMON_ROOT = self.hwmon

    def _drive(self, index, block, model):
        node = os.path.join(self.hwmon, "hwmon%d" % index)
        device = os.path.join(self.tmp, "dev%d" % index)
        os.makedirs(os.path.join(device, "block", block))
        with open(os.path.join(device, "model"), "w") as f:
            f.write(model + "\n")
        os.makedirs(node)
        with open(os.path.join(node, "name"), "w") as f:
            f.write("drivetemp\n")
        os.symlink(device, os.path.join(node, "device"))

    def test_same_model_is_disambiguated_by_block_device(self):
        self._drive(0, "sda", "CT1000MX500SSD1")
        self._drive(1, "sdb", "CT1000MX500SSD1")
        self._drive(2, "sdc", "Samsung SSD 980")

        found = {key: name for key, name, _ in storage.drives()}
        self.assertEqual(found["disk:sda"], "CT1000MX500SSD1 (sda)")
        self.assertEqual(found["disk:sdb"], "CT1000MX500SSD1 (sdb)")
        # A unique model keeps its clean name.
        self.assertEqual(found["disk:sdc"], "Samsung SSD 980")

    def test_keys_stay_stable_and_unique(self):
        self._drive(0, "sda", "CT1000MX500SSD1")
        self._drive(1, "sdb", "CT1000MX500SSD1")
        keys = [key for key, _, _ in storage.drives()]
        self.assertEqual(sorted(keys), ["disk:sda", "disk:sdb"])


class DriveUsageTest(unittest.TestCase):
    """How full each volume is, and which drive it lives on."""

    def setUp(self):
        from collections import namedtuple
        from unittest import mock
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # /sys/class/block links into a devices tree, disk above partition.
        self.devices = os.path.join(self.tmp, "devices")
        self.classes = os.path.join(self.tmp, "class")
        os.makedirs(self.classes)
        self.addCleanup(setattr, storage, "CLASS_BLOCK_ROOT",
                        storage.CLASS_BLOCK_ROOT)
        storage.CLASS_BLOCK_ROOT = self.classes
        self.mounts = []
        self.sizes = {}
        part = namedtuple("part", "device mountpoint")
        usage = namedtuple("usage", "used total")
        patches = (
            mock.patch.object(storage.psutil, "disk_partitions",
                              lambda all=False: [part(*m)
                                                 for m in self.mounts]),
            mock.patch.object(storage.psutil, "disk_usage",
                              lambda path: usage(*self.sizes[path])),
            # Mounted devices are named by path; resolving them is the OS's.
            mock.patch.object(storage.os.path, "realpath", self._realpath))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    _real = staticmethod(os.path.realpath)

    def _realpath(self, path):
        if path.startswith("/dev/mapper/"):
            return "/dev/dm-0"
        return self._real(path)

    def _block(self, disk, part=None, slaves=()):
        path = os.path.join(self.devices, disk)
        if part:
            path = os.path.join(path, part)
            os.makedirs(path)
            open(os.path.join(path, "partition"), "w").close()
        else:
            os.makedirs(path, exist_ok=True)
        for slave in slaves:
            os.makedirs(os.path.join(path, "slaves", slave))
        os.symlink(path, os.path.join(self.classes, part or disk))

    def test_each_volume_is_listed_on_its_disk(self):
        self._block("sda")
        self._block("sda", "sda1")
        self._block("sda", "sda2")
        self.mounts = [("/dev/sda1", "/"), ("/dev/sda2", "/data")]
        self.sizes = {"/": (10, 100), "/data": (5, 50)}
        self.assertEqual(storage.volumes_by_disk(),
                         {"sda": [("/", 10, 100), ("/data", 5, 50)]})

    def test_a_filesystem_mounted_twice_counts_once(self):
        """btrfs puts one filesystem at / and at /home."""
        self._block("nvme0n1")
        self._block("nvme0n1", "nvme0n1p3")
        self.mounts = [("/dev/nvme0n1p3", "/"), ("/dev/nvme0n1p3", "/home")]
        self.sizes = {"/": (40, 500), "/home": (40, 500)}
        self.assertEqual(storage.volumes_by_disk(),
                         {"nvme0n1": [("/", 40, 500)]})

    def test_an_encrypted_volume_belongs_to_the_disk_under_it(self):
        self._block("nvme0n1")
        self._block("nvme0n1", "nvme0n1p2")
        self._block("dm-0", slaves=("nvme0n1p2",))
        self.mounts = [("/dev/mapper/luks-1234", "/")]
        self.sizes = {"/": (7, 70)}
        self.assertEqual(storage.volumes_by_disk(),
                         {"nvme0n1": [("/", 7, 70)]})

    def test_a_filesystem_over_two_disks_is_counted_on_neither(self):
        for disk in ("sda", "sdb"):
            self._block(disk)
            self._block(disk, disk + "1")
        self._block("md0", slaves=("sda1", "sdb1"))
        self.mounts = [("/dev/md0", "/raid")]
        self.sizes = {"/raid": (1, 2)}
        self.assertEqual(storage.volumes_by_disk(), {})

    def test_what_is_not_a_block_device_is_ignored(self):
        self.mounts = [("tmpfs", "/tmp")]
        self.assertEqual(storage.volumes_by_disk(), {})

    def test_a_volume_is_named_by_its_mount_point(self):
        self.assertEqual(storage.volume_label("/"), "System")
        self.assertEqual(storage.volume_label("/mnt/giochi"), "giochi")
        self.assertEqual(storage.volume_label("/home/"), "home")


class RailNameTest(unittest.TestCase):
    """Bare rails named from lm-sensors' own table, and only for its chips."""

    def rails(self, chip):
        chips = {chip: {"in%d" % i: {"in%d_input" % i: 1.0}
                        for i in (0, 1, 2, 3, 7, 8)}}
        readings = MainboardProvider().read(context(chips))
        return [r.label for r in readings if r.kind == "voltage"]

    def test_the_nuvoton_family_gets_its_documented_names(self):
        self.assertEqual(self.rails("nct6799-isa-0290"),
                         ["Vcore", "Rail 1", "AVCC", "+3.3V", "3VSB", "Vbat"])

    def test_another_chip_keeps_numbered_rails(self):
        self.assertEqual(self.rails("it8689-isa-0a40"),
                         ["Rail 0", "Rail 1", "Rail 2", "Rail 3", "Rail 7",
                          "Rail 8"])


class DimmSlotTest(unittest.TestCase):
    """Sticks named by the slot they are in, from the firmware's tables."""

    DMI = ("E:MEMORY_DEVICE_0_PRESENT=0\n"
           "E:MEMORY_DEVICE_1_SIZE=17179869184\n"
           "E:MEMORY_DEVICE_2_PRESENT=0\n"
           "E:MEMORY_DEVICE_3_SIZE=17179869184\n"
           "E:MEMORY_ARRAY_NUM_DEVICES=4\n")

    def setUp(self):
        handle, self.path = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(os.remove, self.path)
        with open(self.path, "w") as f:
            f.write(self.DMI)
        self.addCleanup(setattr, memory, "UDEV_DMI", memory.UDEV_DMI)
        self.addCleanup(setattr, memory, "_slot_count", memory._slot_count)
        memory.UDEV_DMI = self.path

    def test_filled_slots_come_from_the_firmware(self):
        self.assertEqual(memory.memory_slots(), (4, [2, 4]))

    def test_sensors_take_the_filled_slots_in_address_order(self):
        chips = {"spd5118-i2c-11-53": {"temp1": {"temp1_input": 35.0}},
                 "spd5118-i2c-11-51": {"temp1": {"temp1_input": 36.0}}}
        readings = MemoryProvider().read(context(chips))
        labels = {r.key: r.label for r in readings if r.kind == "temp"}
        self.assertEqual(labels, {"spd5118-i2c-11-51_temp1": "DIMM 2",
                                  "spd5118-i2c-11-53_temp1": "DIMM 4"})

    def test_a_count_that_does_not_agree_numbers_them_as_before(self):
        chips = {"spd5118-i2c-11-51": {"temp1": {"temp1_input": 36.0}}}
        readings = MemoryProvider().read(context(chips))
        self.assertEqual([r.label for r in readings if r.kind == "temp"],
                         ["DIMM"])

    def test_four_slots_fill_the_second_of_each_channel_first(self):
        order = sorted((1, 2, 3, 4), key=lambda n: memory.fill_rank(n, 4))
        self.assertEqual(order, [2, 4, 1, 3])
        order = sorted((1, 2), key=lambda n: memory.fill_rank(n, 2))
        self.assertEqual(order, [1, 2])

    def test_no_firmware_record_is_survivable(self):
        memory.UDEV_DMI = os.path.join(self.path, "missing")
        self.assertEqual(memory.memory_slots(), (0, []))


class VendorNeutralAdviceTest(unittest.TestCase):
    """Tips must key off what is missing, not off a list of known chip names."""

    def test_vendor_ec_fans_suppress_the_superio_tip(self):
        # asus_ec_sensors, nzxt-smart2, dell-smm and friends report board fans
        # without any Super-I/O chip, and no name whitelist would cover them.
        chips = {"asus_ec_sensors-isa-0000": {
            "Chipset": {"temp1_input": 45.0},
            "CPU Fan": {"fan1_input": 900.0},
        }}
        provider = MainboardProvider()
        readings = provider.read(context(chips))
        self.assertTrue(any(r.group == "Fans" for r in readings))
        self.assertEqual(provider.advice(), [])

    def test_temperature_only_chip_still_offers_the_tip(self):
        # A board reporting temperatures but no fans or rails is exactly the
        # case the tip exists for.
        chips = {"acpitz-acpi-0": {"temp1": {"temp1_input": 40.0}}}
        provider = MainboardProvider()
        provider.read(context(chips))
        tips = provider.advice()
        self.assertEqual([t.key for t in tips], ["superio"])

    def test_memory_tip_covers_both_ddr_generations(self):
        provider = MemoryProvider()
        provider.read(context({}))
        tip, = provider.advice()
        commands = [s.command for s in tip.steps]
        self.assertIn("sudo modprobe spd5118", commands)   # DDR5
        self.assertIn("sudo modprobe jc42", commands)      # DDR3/DDR4

    def test_usb_disks_do_not_trigger_the_drivetemp_tip(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        block = os.path.join(tmp, "block")
        os.makedirs(os.path.join(block, "sda"))
        usb = os.path.join(tmp, "devices", "pci0000:00", "usb1", "1-1")
        os.makedirs(usb)
        os.symlink(usb, os.path.join(block, "sda", "device"))

        for name, value in (("BLOCK_ROOT", block),
                            ("HWMON_ROOT", os.path.join(tmp, "hwmon"))):
            self.addCleanup(setattr, storage, name, getattr(storage, name))
            setattr(storage, name, value)
        os.makedirs(os.path.join(tmp, "hwmon"))

        provider = storage.StorageProvider()
        self.assertEqual(provider.advice(), [])

        # ...but a real SATA disk on an ata link still does.
        ata = os.path.join(tmp, "devices", "pci0000:00", "ata1", "host0")
        os.makedirs(ata)
        os.makedirs(os.path.join(block, "sdb"))
        os.symlink(ata, os.path.join(block, "sdb", "device"))
        self.assertEqual([t.key for t in provider.advice()], ["drivetemp"])


def every_advice(case):
    """Every tip the app can produce, with each precondition forced.

    Collected in one place so a new tip cannot quietly skip the contract the
    tests below enforce.
    """
    tips = []

    for provider in (MainboardProvider(), MemoryProvider()):
        provider.read(context({}))
        tips.extend(provider.advice())

    cpu = CpuProvider()
    cpu.rapl.permission_denied = True
    tips.extend(cpu.advice())

    tmp = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, tmp, True)

    block = os.path.join(tmp, "block")
    ata = os.path.join(tmp, "devices", "ata1", "host0")
    os.makedirs(os.path.join(block, "sda"))
    os.makedirs(ata)
    os.symlink(ata, os.path.join(block, "sda", "device"))
    os.makedirs(os.path.join(tmp, "hwmon"))
    for name, value in (("BLOCK_ROOT", block),
                        ("HWMON_ROOT", os.path.join(tmp, "hwmon"))):
        case.addCleanup(setattr, storage, name, getattr(storage, name))
        setattr(storage, name, value)
    tips.extend(storage.StorageProvider().advice())

    empty = os.path.join(tmp, "no-modules")
    os.makedirs(empty)
    root, kernel.MODULES_ROOT = kernel.MODULES_ROOT, empty
    try:
        tips.extend(kernel.advice())
    finally:
        kernel.MODULES_ROOT = root

    return tips


class PersistenceContractTest(unittest.TestCase):
    """Every tip has to say what happens to it after a reboot.

    A change that only lasts until the next boot, presented without saying so,
    sends the user back to a machine that quietly stopped reporting sensors.
    """

    def test_all_five_tips_are_covered(self):
        keys = sorted(t.key for t in every_advice(self))
        self.assertEqual(keys, ["drivetemp", "kernel-modules-missing",
                                "rapl-permission", "spd-sensor", "superio"])

    def test_no_tip_is_silent_about_persistence(self):
        for tip in every_advice(self):
            self.assertTrue(
                tip.persist or tip.persist_note,
                "%s says nothing about surviving a reboot" % tip.key)

    def test_modprobe_tips_ship_a_modules_load_file(self):
        for tip in every_advice(self):
            if not any("modprobe" in s.command for s in tip.steps):
                continue
            self.assertTrue(tip.persist, "%s loads a module but never makes it "
                                         "permanent" % tip.key)
            self.assertTrue(
                any("/etc/modules-load.d/" in s.command for s in tip.persist),
                "%s does not write a modules-load.d file" % tip.key)

    def test_persist_steps_explain_themselves(self):
        for tip in every_advice(self):
            for step in tip.persist:
                self.assertTrue(step.note, "%s: %r has no note"
                                % (tip.key, step.command))
                self.assertIn("boot", step.note.lower())

    def test_persistence_reaches_the_plain_text_report(self):
        for tip in every_advice(self):
            text = tip.as_text()
            for step in tip.persist:
                self.assertIn(step.command, text)
            if tip.persist_note:
                self.assertIn("After a reboot", text)
