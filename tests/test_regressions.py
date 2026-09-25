"""Regression tests for defects that were found and fixed.

Each one pins behaviour that was previously wrong, using a synthetic sysfs tree
so it runs on any machine.
"""

import os
import shutil
import tempfile
import unittest
import unittest.mock

from ksyssupervisor.hwmon import hwmon_sensors, nv_value, read_hwmon_dir
from ksyssupervisor.providers import cpu as cpu_mod
from ksyssupervisor import hardware
from ksyssupervisor.providers import battery as battery_mod
from ksyssupervisor.providers import memory as memory_mod
from ksyssupervisor.providers import storage as storage_mod
from ksyssupervisor.providers.base import EnergyMeter, ReadContext
from ksyssupervisor.providers.gpu_amd import AmdGpuProvider
from ksyssupervisor.providers.gpu_intel import IntelGpuProvider
from ksyssupervisor.providers.gpu_nvidia import (NouveauProvider,
                                                 NvidiaProvider,
                                                 normalize_bus_id)
from ksyssupervisor.hardware import GpuCard


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TempTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ksysmon-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class AmdClockSourceTest(TempTree):
    """pp_dpm_* must win over the hwmon freq, which reads ~0 at idle.

    amdgpu's freq1_input is a gated average: it reported 4 MHz on a card
    actually running at 500 MHz, and a single such tick poisoned the recorded
    minimum permanently.
    """

    def _card(self, with_dpm):
        card = os.path.join(self.tmp, "device")
        write(os.path.join(card, "hwmon", "hwmon0", "name"), "amdgpu\n")
        write(os.path.join(card, "hwmon", "hwmon0", "freq1_input"), "4000000\n")
        write(os.path.join(card, "hwmon", "hwmon0", "freq1_label"), "sclk\n")
        if with_dpm:
            write(os.path.join(card, "pp_dpm_sclk"), "0: 500Mhz *\n1: 2475Mhz\n")
        return GpuCard("0000:03:00.0", "amdgpu", card, "Test GPU")

    def _graphics(self, card):
        provider = AmdGpuProvider()
        provider._cards = [card]
        readings = provider.read(ReadContext({}, {}, "sysfs"))
        return [r for r in readings if r.label == "Graphics"]

    def test_dpm_value_is_used_when_available(self):
        found = self._graphics(self._card(with_dpm=True))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].value, 500.0)

    def test_hwmon_freq_is_suppressed_when_dpm_exists(self):
        # The bogus 4 MHz must not appear at all, not even as a second row.
        for reading in self._graphics(self._card(with_dpm=True)):
            self.assertNotEqual(reading.value, 4.0)

    def test_hwmon_freq_is_used_when_dpm_is_absent(self):
        # radeon and older cards have no pp_dpm_*, so hwmon is all there is.
        found = self._graphics(self._card(with_dpm=False))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].value, 4.0)


class NvidiaParsingTest(unittest.TestCase):
    def test_bus_id_is_normalised_to_sysfs_form(self):
        self.assertEqual(normalize_bus_id("00000000:03:00.0"), "0000:03:00.0")
        self.assertEqual(normalize_bus_id("00000000:0B:00.0"), "0000:0b:00.0")

    def test_unsupported_and_na_fields_do_not_raise(self):
        for text in ("[Not Supported]", "[N/A]", "N/A", "", "Unknown"):
            self.assertIsNone(nv_value(text))
        self.assertEqual(nv_value("45"), 45.0)

    def _read(self, row):
        provider = NvidiaProvider()
        provider._query = lambda: [row]
        return provider.read(ReadContext({}, {}, "lm_sensors"))

    def test_zero_vram_total_does_not_divide_by_zero(self):
        row = ["00000000:03:00.0", "Test", "50", "10", "512", "0",
               "60", "200", "1500", "7000", "30"]
        labels = [r.label for r in self._read(row)]
        self.assertNotIn("VRAM", labels)

    def test_zero_power_limit_does_not_divide_by_zero(self):
        row = ["00000000:03:00.0", "Test", "50", "10", "512", "1024",
               "60", "0", "1500", "7000", "30"]
        labels = [r.label for r in self._read(row)]
        self.assertNotIn("Power Limit", labels)
        self.assertIn("Power", labels)

    def test_every_gpu_is_reported_not_just_the_first(self):
        provider = NvidiaProvider()
        provider._query = lambda: [
            ["00000000:03:00.0", "A", "50", "10", "512", "1024",
             "60", "200", "1500", "7000", "30"],
            ["00000000:04:00.0", "B", "55", "20", "256", "1024",
             "70", "200", "1600", "7000", "40"],
        ]
        devices = {r.device for r in provider.read(ReadContext({}, {}, "x"))}
        self.assertEqual(devices, {"gpu:0000:03:00.0", "gpu:0000:04:00.0"})


class HwmonScalingTest(TempTree):
    def test_raw_sysfs_units_are_converted(self):
        d = os.path.join(self.tmp, "hwmon0")
        write(os.path.join(d, "name"), "amdgpu\n")
        write(os.path.join(d, "temp1_input"), "51000\n")        # millidegrees
        write(os.path.join(d, "temp1_label"), "edge\n")
        write(os.path.join(d, "in0_input"), "818\n")            # millivolts
        write(os.path.join(d, "in0_label"), "vddgfx\n")
        write(os.path.join(d, "power1_average"), "39000000\n")  # microwatts
        write(os.path.join(d, "power1_label"), "PPT\n")
        write(os.path.join(d, "fan1_input"), "1200\n")          # already RPM

        chip = read_hwmon_dir(d)
        self.assertEqual(chip["edge"]["temp1_input"], 51.0)
        self.assertAlmostEqual(chip["vddgfx"]["in0_input"], 0.818)
        self.assertEqual(chip["PPT"]["power1_average"], 39.0)
        self.assertEqual(chip["fan1"]["fan1_input"], 1200.0)

    def test_unreadable_attribute_is_skipped_not_fatal(self):
        d = os.path.join(self.tmp, "hwmon0")
        write(os.path.join(d, "name"), "test\n")
        write(os.path.join(d, "temp1_input"), "N/A\n")
        write(os.path.join(d, "temp2_input"), "42000\n")
        chip = read_hwmon_dir(d)
        self.assertNotIn("temp1", chip)
        self.assertEqual(chip["temp2"]["temp2_input"], 42.0)


class SharedLabelTest(TempTree):
    """A label names a place, not a kind of sensor.

    xe (xe_hwmon.c) labels a temperature, a voltage and an energy counter all
    "pkg". Grouping by label merged them into one entry, and the caller asking
    for its input got whichever file happened to be read first.
    """

    def _xe_like(self):
        d = os.path.join(self.tmp, "hwmon0")
        write(os.path.join(d, "name"), "xe\n")
        write(os.path.join(d, "temp2_input"), "45000\n")
        write(os.path.join(d, "temp2_label"), "pkg\n")
        write(os.path.join(d, "in1_input"), "900\n")
        write(os.path.join(d, "in1_label"), "pkg\n")
        write(os.path.join(d, "energy2_input"), "5000000\n")
        write(os.path.join(d, "energy2_label"), "pkg\n")
        return d

    def test_sensors_are_told_apart_by_attribute(self):
        sensors = {s.raw: s for s in hwmon_sensors(self._xe_like())}
        self.assertEqual(sorted(sensors), ["energy2", "in1", "temp2"])
        self.assertEqual(sensors["temp2"].kind, "temp")
        self.assertEqual(sensors["temp2"].value("input"), 45.0)
        self.assertAlmostEqual(sensors["in1"].value("input"), 0.9)
        # A microjoule counter, in joules so its rate comes out in watts.
        self.assertEqual(sensors["energy2"].value("input"), 5.0)

    def test_a_shared_label_is_not_merged(self):
        chip = read_hwmon_dir(self._xe_like())
        self.assertNotIn("pkg", chip)
        self.assertEqual(chip["pkg (temp2)"], {"temp2_input": 45.0})
        self.assertEqual(chip["pkg (energy2)"], {"energy2_input": 5.0})

    def test_unique_labels_keep_the_sensors_j_shape(self):
        d = os.path.join(self.tmp, "hwmon1")
        write(os.path.join(d, "name"), "amdgpu\n")
        write(os.path.join(d, "temp1_input"), "51000\n")
        write(os.path.join(d, "temp1_label"), "edge\n")
        write(os.path.join(d, "temp1_crit"), "100000\n")
        self.assertEqual(read_hwmon_dir(d), {"edge": {"temp1_input": 51.0}})


class EnergyMeterTest(unittest.TestCase):
    def test_power_is_the_rate_of_the_counter(self):
        meter = EnergyMeter()
        self.assertIsNone(meter.rate("a", 100.0, now=10.0))
        self.assertAlmostEqual(meter.rate("a", 130.0, now=12.0), 15.0)

    def test_counters_are_tracked_separately(self):
        meter = EnergyMeter()
        meter.rate("a", 0.0, now=0.0)
        meter.rate("b", 0.0, now=0.0)
        self.assertAlmostEqual(meter.rate("a", 10.0, now=1.0), 10.0)
        self.assertAlmostEqual(meter.rate("b", 50.0, now=1.0), 50.0)

    def test_a_counter_that_resets_is_skipped_not_negative(self):
        """No known range: the driver reloaded, or the machine resumed."""
        meter = EnergyMeter()
        meter.rate("a", 1000.0, now=0.0)
        self.assertIsNone(meter.rate("a", 5.0, now=1.0))
        self.assertAlmostEqual(meter.rate("a", 15.0, now=2.0), 10.0)

    def test_a_counter_with_a_known_range_wraps(self):
        meter = EnergyMeter()
        meter.rate("a", 90.0, now=0.0, wrap=100.0)
        self.assertAlmostEqual(meter.rate("a", 10.0, now=1.0, wrap=100.0), 20.0)


class IntelGpuTest(TempTree):
    """i915 and xe publish energy and limits, never power*_input."""

    def _card(self, driver, files):
        card = os.path.join(self.tmp, "device")
        hwmon = os.path.join(card, "hwmon", "hwmon0")
        write(os.path.join(hwmon, "name"), driver + "\n")
        for name, value in files.items():
            write(os.path.join(hwmon, name), value + "\n")
        return GpuCard("0000:03:00.0", driver, card, "Test Arc"), hwmon

    def _provider(self, card):
        provider = IntelGpuProvider()
        provider._cards = [card]
        return provider

    def _tick(self, provider, when):
        with unittest.mock.patch("ksyssupervisor.providers.base.time.monotonic",
                                 return_value=when):
            return provider.read(ReadContext({}, {}, "sysfs"))

    def test_energy_counter_becomes_watts(self):
        card, hwmon = self._card("i915", {"energy1_input": "1000000",
                                          "power1_max": "190000000",
                                          "temp1_input": "50000"})
        provider = self._provider(card)
        first = self._tick(provider, 100.0)
        self.assertEqual([r for r in first if r.group == "Powers"], [])

        write(os.path.join(hwmon, "energy1_input"), "61000000\n")  # +60 J
        second = self._tick(provider, 102.0)
        powers = [r for r in second if r.group == "Powers"]
        self.assertEqual(len(powers), 1)
        self.assertEqual(powers[0].label, "Power")
        self.assertAlmostEqual(powers[0].value, 30.0)

    def test_xe_reports_every_sensor_under_its_place(self):
        card, hwmon = self._card("xe", {
            "temp2_input": "45000", "temp2_label": "pkg",
            "temp3_input": "52000", "temp3_label": "vram",
            "in1_input": "900", "in1_label": "pkg",
            "energy1_input": "0", "energy1_label": "card",
            "energy2_input": "0", "energy2_label": "pkg",
            "power1_cap": "200000000", "power1_label": "card",
            "fan1_input": "1500"})
        provider = self._provider(card)
        self._tick(provider, 0.0)
        write(os.path.join(hwmon, "energy1_input"), "100000000\n")
        write(os.path.join(hwmon, "energy2_input"), "80000000\n")
        readings = self._tick(provider, 1.0)

        got = {(r.group, r.label): r.value for r in readings}
        self.assertEqual(got[("Temperatures", "Package")], 45.0)
        self.assertEqual(got[("Temperatures", "VRAM")], 52.0)
        self.assertAlmostEqual(got[("Voltages", "Package")], 0.9)
        self.assertAlmostEqual(got[("Powers", "Card")], 100.0)
        self.assertAlmostEqual(got[("Powers", "Package")], 80.0)
        self.assertEqual(got[("Fans", "Fan")], 1500.0)
        # A limit is not a measurement.
        self.assertNotIn(("Powers", "Power"), got)

    def test_unlabelled_sensors_of_one_kind_are_numbered(self):
        card, _ = self._card("i915", {"temp1_input": "40000",
                                      "temp2_input": "41000"})
        labels = sorted(r.label for r in self._tick(self._provider(card), 0.0))
        self.assertEqual(labels, ["GPU 1", "GPU 2"])


class NouveauTest(TempTree):
    def test_a_labelled_rail_is_still_a_voltage(self):
        """nouveau labels in0 "GPU core"; matching labels dropped it."""
        card = os.path.join(self.tmp, "device")
        hwmon = os.path.join(card, "hwmon", "hwmon0")
        write(os.path.join(hwmon, "name"), "nouveau\n")
        write(os.path.join(hwmon, "in0_input"), "875\n")
        write(os.path.join(hwmon, "in0_label"), "GPU core\n")
        write(os.path.join(hwmon, "temp1_input"), "38000\n")
        provider = NouveauProvider()
        provider._cards = [GpuCard("0000:01:00.0", "nouveau", card, "GT")]
        got = {r.group: r for r in provider.read(ReadContext({}, {}, "sysfs"))}
        self.assertAlmostEqual(got["Voltages"].value, 0.875)
        self.assertEqual(got["Temperatures"].value, 38.0)


class RaplTest(TempTree):
    """Package power is a delta of a wrapping microjoule counter."""

    def _zone(self, energy, wrap=1000000):
        zone = os.path.join(self.tmp, "intel-rapl:0")
        write(os.path.join(zone, "name"), "package-0\n")
        write(os.path.join(zone, "max_energy_range_uj"), "%d\n" % wrap)
        write(os.path.join(zone, "energy_uj"), "%d\n" % energy)
        return zone

    def test_first_read_only_establishes_a_baseline(self):
        self._zone(1000)
        reader = cpu_mod.RaplReader()
        with unittest.mock.patch.object(cpu_mod, "RAPL_ROOT", self.tmp):
            self.assertEqual(reader.read(), [])

    def test_counter_wrap_does_not_produce_negative_power(self):
        zone = self._zone(900000, wrap=1000000)
        reader = cpu_mod.RaplReader()
        with unittest.mock.patch.object(cpu_mod, "RAPL_ROOT", self.tmp):
            reader.read()                                   # baseline
            write(os.path.join(zone, "energy_uj"), "100000\n")   # wrapped
            result = reader.read()
        self.assertEqual(len(result), 1)
        name, watts = result[0]
        self.assertEqual(name, "Package")
        self.assertGreater(watts, 0.0)

    def _zones(self, zones):
        for name, label in zones.items():
            zone = os.path.join(self.tmp, name)
            write(os.path.join(zone, "name"), label + "\n")
            write(os.path.join(zone, "max_energy_range_uj"), "1000000\n")
            write(os.path.join(zone, "energy_uj"), "0\n")
        reader = cpu_mod.RaplReader()
        with unittest.mock.patch.object(cpu_mod, "RAPL_ROOT", self.tmp):
            return sorted(name for _path, name, _wrap in reader.zones())

    def test_two_packages_are_numbered(self):
        """Both were "Package", keyed alike, so one socket vanished."""
        self.assertEqual(self._zones({"intel-rapl:0": "package-0",
                                      "intel-rapl:0:0": "core",
                                      "intel-rapl:1": "package-1",
                                      "intel-rapl:1:0": "core"}),
                         ["CPU 0 Cores", "CPU 1 Cores",
                          "Package 0", "Package 1"])

    def test_one_package_beside_psys_stays_plain(self):
        """A laptop's psys zone is a top-level zone but not a package."""
        self.assertEqual(self._zones({"intel-rapl:0": "package-0",
                                      "intel-rapl:0:0": "core",
                                      "intel-rapl:1": "psys"}),
                         ["Cores", "Package", "Platform"])

    def test_permission_denied_is_reported_as_advice(self):
        self._zone(1000)
        reader = cpu_mod.RaplReader()
        provider = cpu_mod.CpuProvider()
        provider.rapl = reader
        reader.permission_denied = True
        self.assertTrue(any("powercap" in tip.as_text() for tip in provider.advice()))


class BatteryTest(TempTree):
    def _battery(self, **files):
        path = os.path.join(self.tmp, "BAT0")
        write(os.path.join(path, "type"), "Battery\n")
        for name, value in files.items():
            write(os.path.join(path, name), "%s\n" % value)
        return path

    def test_charge_voltage_power_and_wear(self):
        self._battery(capacity="87", voltage_now="11400000",
                      power_now="8500000", energy_full="45000000",
                      energy_full_design="50000000",
                      manufacturer="ACME", model_name="X1")
        provider = battery_mod.BatteryProvider()
        with unittest.mock.patch.object(battery_mod, "POWER_SUPPLY_ROOT", self.tmp):
            devices = provider.devices()
            readings = {r.label: r for r in provider.read(ReadContext({}, {}, "x"))}

        self.assertEqual(devices[0].name, "ACME X1")
        self.assertEqual(readings["Charge"].value, 87.0)
        self.assertAlmostEqual(readings["Voltage"].value, 11.4)
        self.assertAlmostEqual(readings["Power"].value, 8.5)
        self.assertAlmostEqual(readings["Health"].value, 90.0)

    def test_non_battery_supplies_are_ignored(self):
        write(os.path.join(self.tmp, "AC", "type"), "Mains\n")
        provider = battery_mod.BatteryProvider()
        with unittest.mock.patch.object(battery_mod, "POWER_SUPPLY_ROOT", self.tmp):
            self.assertEqual(provider.devices(), [])

    def test_a_peripherals_battery_is_not_the_machines(self):
        """HID mice and keyboards register as type Battery, scope Device."""
        mouse = os.path.join(self.tmp, "hidpp_battery_0")
        write(os.path.join(mouse, "type"), "Battery\n")
        write(os.path.join(mouse, "scope"), "Device\n")
        self._battery(capacity="50")
        provider = battery_mod.BatteryProvider()
        with unittest.mock.patch.object(battery_mod, "POWER_SUPPLY_ROOT", self.tmp):
            self.assertEqual([d.key for d in provider.devices()],
                             ["battery:BAT0"])

    def test_a_ups_is_reported(self):
        ups = os.path.join(self.tmp, "ups0")
        write(os.path.join(ups, "type"), "UPS\n")
        write(os.path.join(ups, "capacity"), "100\n")
        provider = battery_mod.BatteryProvider()
        with unittest.mock.patch.object(battery_mod, "POWER_SUPPLY_ROOT", self.tmp):
            self.assertEqual([d.key for d in provider.devices()],
                             ["battery:ups0"])


class InstalledMemoryTest(TempTree):
    GIB = 1024 ** 3

    def test_totals_round_up_to_what_is_fitted(self):
        """Firmware only ever keeps memory back, so the fitted size is the one
        at or above the total. Nearest showed a 20 GB laptop as 16."""
        for raw, fitted in ((31.6, 32), (30.9, 32), (19.4, 20), (15.3, 16),
                            (39.2, 40), (377, 384), (7.6, 8)):
            self.assertEqual(memory_mod.installed_gb(raw * self.GIB), fitted,
                             raw)

    def test_an_unusual_total_is_shown_as_it_is(self):
        """A 10 GB virtual machine is not a 12 GB one."""
        self.assertEqual(memory_mod.installed_gb(9.99 * self.GIB), 10)

    def test_the_firmware_map_is_summed_by_type(self):
        for i, (kind, start, end) in enumerate((
                ("System RAM", 0x0, 0x9ffff),
                ("Reserved", 0xa0000, 0xfffff),
                ("System RAM", 0x100000, 0x7fffffff))):
            entry = os.path.join(self.tmp, str(i))
            write(os.path.join(entry, "type"), kind + "\n")
            write(os.path.join(entry, "start"), "0x%x\n" % start)
            write(os.path.join(entry, "end"), "0x%x\n" % end)
        self.assertEqual(memory_mod.firmware_ram_bytes(self.tmp),
                         0xa0000 + 0x7ff00000)

    def test_no_firmware_map_is_none(self):
        self.assertIsNone(memory_mod.firmware_ram_bytes(self.tmp))


class CpuNameTest(unittest.TestCase):
    def name(self, model):
        cpuinfo = "processor\t: 0\nmodel name\t: %s\n" % model
        with unittest.mock.patch("builtins.open",
                                 unittest.mock.mock_open(read_data=cpuinfo)):
            return hardware.cpu_name()

    def test_any_core_count_suffix_is_dropped(self):
        """Only the 8- and 16-core suffixes were, leaving "5600X 6-Core"."""
        self.assertEqual(self.name("AMD Ryzen 5 5600X 6-Core Processor"),
                         "AMD Ryzen 5 5600X")
        self.assertEqual(self.name("AMD Ryzen Threadripper PRO 7995WX "
                                   "96-Cores"),
                         "AMD Ryzen Threadripper PRO 7995WX")
        self.assertEqual(self.name("AMD Ryzen 9 7950X 16-Core Processor"),
                         "AMD Ryzen 9 7950X")

    def test_intel_names_keep_their_shape(self):
        self.assertEqual(self.name("Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz"),
                         "Intel Core i7-8700K")


class NouveauDiscoveryTest(unittest.TestCase):
    def test_nouveau_cards_are_kept_when_nvidia_smi_exists(self):
        """A card cannot be on both drivers; nvidia-smi never covers these."""
        card = GpuCard("0000:01:00.0", "nouveau", "/nonexistent", "GT 710")
        with unittest.mock.patch("ksyssupervisor.providers.gpu_nvidia"
                                 ".gpu_cards", return_value=[card]), \
                unittest.mock.patch("shutil.which",
                                    return_value="/usr/bin/nvidia-smi"):
            self.assertEqual(NouveauProvider()._found(), [card])


class RaplAdviceTest(unittest.TestCase):
    def test_the_trigger_actually_fires_the_rule(self):
        """udevadm trigger sends 'change' by default; the rule matches 'add'."""
        provider = cpu_mod.CpuProvider()
        provider.rapl.permission_denied = True
        text = provider.advice()[0].as_text()
        self.assertIn('ACTION=="add"', text)
        self.assertIn("--action=add", text)
        self.assertIn("PLATYPUS", text)


class StorageTest(TempTree):
    def test_nvme_drive_is_named_from_its_model(self):
        hwmon = os.path.join(self.tmp, "hwmon", "hwmon0")
        device = os.path.join(self.tmp, "nvme", "nvme0")
        write(os.path.join(hwmon, "name"), "nvme\n")
        write(os.path.join(hwmon, "temp1_input"), "39850\n")
        write(os.path.join(hwmon, "temp1_label"), "Composite\n")
        write(os.path.join(device, "model"), "Samsung SSD 980 500GB\n")
        os.makedirs(os.path.join(device, "nvme0n1"))
        os.symlink(device, os.path.join(hwmon, "device"))

        provider = storage_mod.StorageProvider()
        with unittest.mock.patch.object(storage_mod, "HWMON_ROOT",
                                        os.path.join(self.tmp, "hwmon")):
            devices = provider.devices()
            readings = provider.read(ReadContext({}, {}, "sysfs"))

        self.assertEqual(devices[0].key, "disk:nvme0n1")
        self.assertEqual(devices[0].name, "Samsung SSD 980 500GB")
        self.assertEqual(readings[0].value, 39.85)
        self.assertEqual(readings[0].device, "disk:nvme0n1")

    def test_drivetemp_advice_only_when_sata_disks_exist(self):
        provider = storage_mod.StorageProvider()
        empty = os.path.join(self.tmp, "noblock")
        os.makedirs(empty)
        with unittest.mock.patch.object(storage_mod, "BLOCK_ROOT", empty), \
             unittest.mock.patch.object(storage_mod, "HWMON_ROOT", empty):
            self.assertEqual(provider.advice(), [])

        block = os.path.join(self.tmp, "block")
        os.makedirs(os.path.join(block, "sda"))
        with unittest.mock.patch.object(storage_mod, "BLOCK_ROOT", block), \
             unittest.mock.patch.object(storage_mod, "HWMON_ROOT", empty):
            self.assertTrue(any("drivetemp" in tip.as_text()
                                for tip in provider.advice()))


# The blanking of a sensor that stops reporting used to be tested here
# against a hand-made KSysSupervisor with fake tree items. That rule now
# lives in display.Staleness, where it needs no window at all, and is
# covered by tests/test_display.py.

class ApplicationNameTest(unittest.TestCase):
    """Named after the QApplication was built, the app left Breeze reading a
    copy of KDE's config that a colour-scheme switch never reloaded - the
    menu bar stayed in the old theme until a restart. Reproducing that needs
    Plasma; what can be pinned is the order that avoids it."""

    def test_the_name_is_set_before_the_application_exists(self):
        from ksyssupervisor import app as app_mod
        fake = unittest.mock.MagicMock()
        fake.side_effect = RuntimeError("stop here")
        with unittest.mock.patch("PyQt6.QtWidgets.QApplication", fake), \
                unittest.mock.patch.object(app_mod, "setup_logging"), \
                unittest.mock.patch.object(app_mod, "check_compatibility"), \
                unittest.mock.patch.object(app_mod, "emergency_notify"):
            app_mod.main([])
        names = [c[0] for c in fake.mock_calls]
        self.assertIn("setApplicationName", names)
        self.assertIn("setDesktopFileName", names)
        built = names.index("")                 # the constructor call
        self.assertLess(names.index("setApplicationName"), built)
        self.assertLess(names.index("setDesktopFileName"), built)


if __name__ == "__main__":
    unittest.main()
