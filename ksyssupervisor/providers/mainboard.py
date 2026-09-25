"""Mainboard and everything else: whatever chip no other provider claimed.

Takes the leftovers, so it covers Super-I/O chips, storage controllers, network
adapters and anything else the kernel happens to expose.
"""

import re

from ..hardware import mobo_name
from .base import Advice, Device, Reading, SensorProvider, Step
from .kernel import modprobe_step

DEVICE = "mobo"

# Chips whose bare name means nothing to a user.
PRETTY_PREFIX = {"nvme": "NVMe", "mt7921": "Wi-Fi", "r8169": "LAN"}

# inN_input is a voltage rail; intrusionN_alarm must not be mistaken for one.
VOLT_ATTR = re.compile(r"^in\d+_input$")
POWER_ATTR = re.compile(r"^power\d+_(input|average)$")
BARE_RAIL = re.compile(r"^in(\d+)$")

# The rails lm-sensors' own configuration names for the Nuvoton/Winbond
# family the kernel's nct6775 driver runs (sensors3.conf, "nct6775-*" to
# "nct6796-*"). The later chips of the family - 6797, 6798, 6799 - keep the
# pinout and are missing from that file only because it predates them: on
# an NCT6799D the three 3.3 V rails and the battery read exactly where the
# table puts them. in0 is the CPU core, which is how the CPU tile finds its
# voltage on boards whose processor does not report one.
FAMILY_RAILS = {0: "Vcore", 2: "AVCC", 3: "+3.3V", 7: "3VSB", 8: "Vbat"}
FAMILY_CHIPS = re.compile(r"^(w83627ehf|w83627dhg|w83667hg|"
                          r"nct677[569]|nct679[1-9])\b")


def display_label(label):
    """Tidy a driver label without destroying it.

    Driver labels are often deliberate acronyms - VBAT, AVCC, SYSTIN, CPUTIN -
    and .title() turns those into "Vbat" and "Systin", which no longer match
    what `sensors` prints. Only fully lowercase names get title-cased.
    """
    if label.islower():
        return label.replace('_', ' ').title()
    return label.replace('_', ' ')




class MainboardProvider(SensorProvider):
    name = "mainboard"
    takes_leftovers = True

    def __init__(self):
        self._saw_board_sensors = False

    def devices(self):
        return [Device(DEVICE, mobo_name(), order=60)]

    @staticmethod
    def _prefix(chip, slot):
        for start, pretty in PRETTY_PREFIX.items():
            if chip.startswith(start):
                return "%s %d" % (pretty, slot) if slot else pretty
        return chip.split('-')[0].upper()

    def read(self, ctx):
        out = []
        for chip, data in ctx.chips.items():
            slot = ctx.slots.get(chip, 0)
            for label, attrs in ctx.sensors(data):
                names = attrs.keys()
                is_temp = 'temp' in label or any('temp' in k and 'input' in k for k in names)
                is_fan = 'fan' in label or any('fan' in k and 'input' in k for k in names)
                nice = display_label(label)

                is_volt = any(VOLT_ATTR.match(k) for k in names)
                is_power = any(POWER_ATTR.match(k) for k in names)
                uid = "%s_%s" % (chip, label)

                if is_temp:
                    value = ctx.first(attrs, 'input')
                    if value is None:
                        continue
                    out.append(Reading(DEVICE, "Temperatures", uid,
                                       "%s %s" % (self._prefix(chip, slot), nice),
                                       value, "temp"))
                elif is_fan:
                    value = ctx.first(attrs, 'input')
                    if value is None:
                        continue
                    out.append(Reading(DEVICE, "Fans", uid, nice, value, "fan"))
                elif is_volt:
                    value = ctx.first(attrs, 'input')
                    if value is None:
                        continue
                    out.append(Reading(DEVICE, "Voltages", uid,
                                       self._rail_name(label, chip), value,
                                       "voltage"))
                elif is_power:
                    value = ctx.first(attrs, 'input', 'average')
                    if value is None:
                        continue
                    out.append(Reading(DEVICE, "Powers", uid, nice, value, "power"))

        # Whether to offer the Super-I/O tip is decided by what the board is
        # actually reporting, not by recognising a chip name: vendor EC drivers
        # (asus_ec_sensors, nzxt-smart2, dell-smm and others) deliver the same
        # fans and rails under names no hardcoded list would ever cover.
        self._saw_board_sensors = any(
            r.group in ("Fans", "Voltages") for r in out)
        return out

    @staticmethod
    def _rail_name(label, chip=""):
        """Super-I/O drivers label rails usefully ("Vcore", "+12V", "AVCC").

        Without a label file the raw "in3" comes through instead. On a chip
        whose pinout lm-sensors documents it gets that name; anything else
        gets a number of its own rather than a guess.
        """
        bare = BARE_RAIL.match(label)
        if bare:
            if FAMILY_CHIPS.match(chip):
                named = FAMILY_RAILS.get(int(bare.group(1)))
                if named:
                    return named
            return "Rail %s" % bare.group(1)
        return display_label(label)

    def advice(self):
        """Board fans and voltages need the Super-I/O driver, rarely autoloaded."""
        if self._saw_board_sensors:
            return []
        return [Advice(
            key="superio",
            title="Enable motherboard fan speeds and voltage rails",
            problem="No Super-I/O sensor chip is being reported on %s. This is "
                    "the chip that measures case fan RPM and the board's "
                    "voltage rails. Which driver it needs depends on the exact "
                    "chip the board maker fitted, and none of them load "
                    "automatically, so this takes a little trial and error."
                    % mobo_name(),
            effect="Adds Fans (CPU and case fan RPM) and the board's voltage "
                   "rails under the motherboard category. Most drivers ship no "
                   "names for the rails, so they arrive as 'Rail 0', 'Rail 1' "
                   "and so on unless lm_sensors has a configuration for this "
                   "exact board.",
            steps_title="Try these in order, stopping at the first that works",
            steps=(
                Step("sudo sensors-detect",
                     "The only step that reads the chip's actual ID and names "
                     "the driver it needs. Answering the default to every "
                     "question is safe. If it names a driver, load that one "
                     "and skip the guesses below."),
                modprobe_step("nct6775",
                              "Covers most Nuvoton chips (NCT6116, NCT679x) - "
                              "the common case on ASUS, ASRock, MSI and "
                              "Gigabyte boards."),
                modprobe_step("nct6683",
                              "For the NCT6683/6686/6687 variants, used on "
                              "some ASRock and embedded boards."),
                modprobe_step("nct6683", args="force=1",
                              note="Only if the plain nct6683 above said \"No "
                                   "such device\": some boards fit that chip "
                                   "but report a vendor ID the driver does not "
                                   "recognise, and this skips that check."),
                modprobe_step("it87",
                              "For ITE chips (IT86xx, IT87xx), mostly older "
                              "and some Gigabyte boards."),
            ),
            persist=(
                Step("echo nct6775 | sudo tee /etc/modules-load.d/superio.conf",
                     "Put whichever module actually worked here, not "
                     "necessarily nct6775. Without this the fans and voltages "
                     "disappear at the next reboot; the file is read by "
                     "systemd-modules-load at every boot."),
            ),
            result="\"No such device\" means that module loaded fine but your "
                   "board does not have that chip - move on to the next one. "
                   "When one takes, 'sensors' shows a new chip with fan and "
                   "voltage rows; restart KSysSupervisor to pick it up. To give "
                   "the rails real names, add a board-specific file under "
                   "/etc/sensors.d - unconnected fan headers reading 0 RPM and "
                   "unused inputs reading odd temperatures are normal, and can "
                   "be hidden the same way. If every "
                   "one fails, the board is hiding the chip behind ACPI: add "
                   "acpi_enforce_resources=lax to your kernel command line - "
                   "that lives in your bootloader's configuration (for GRUB, "
                   "GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub followed "
                   "by sudo grub-mkconfig -o /boot/grub/grub.cfg), so it "
                   "persists once set - and try again after a reboot.")]
