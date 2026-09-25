"""Fan control: discovering pwm channels and driving the privileged helper.

The unprivileged half of the feature. Reading a fan's current state needs no
privileges at all, so all of that happens here; only the writes go through
helpers/ksyssupervisor-fanhelper, which this module launches once under pkexec and
then talks to over a pipe.

Qt-free on purpose, following the same rule as the sensor providers: the logic
is testable against a fake sysfs tree without a display.
"""

from __future__ import annotations

import os
import re
import glob
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional

from . import installation, log
from .hardware import gpu_cards, mobo_name

# 2 since AUTO stopped meaning "restore what CLAIM found" and started meaning
# "hand the channel to the driver's own regulation". The two disagree in
# exactly one case - a channel that was already manual when it was claimed -
# and that case is the Automatic button's whole job, so an old helper must be
# refused at HELLO rather than half-working. 3 since dell-smm stopped being
# treated as inverted: a version 2 helper's AUTO writes the driver's manual
# value there.
PROTOCOL_VERSION = 3

HWMON_ROOT = "/sys/class/hwmon"

# Where install.sh --system and --install-fan-helper put the helper. Fan
# control needs the helper to be root-owned and not writable by the user,
# otherwise the polkit action would authorise whatever the user cared to put
# there - which is why a --user install deliberately has nowhere valid to place
# it.
HELPER_PATHS = (
    "/usr/local/lib/ksyssupervisor/ksyssupervisor-fanhelper",
    "/usr/lib/ksyssupervisor/ksyssupervisor-fanhelper",
)

#: Where install_bundled_helper() puts what it installs. The first of
#: HELPER_PATHS: /usr/local is for software not managed by the package manager,
#: which is exactly what a copy carried inside an AppImage is.
HELPER_INSTALL_PATH = HELPER_PATHS[0]
POLICY_INSTALL_PATH = \
    "/usr/share/polkit-1/actions/org.ksyssupervisor.fancontrol.policy"

PWM_RE = re.compile(r"^pwm([0-9]{1,2})$")

#: Temperature labels that mean "this is the processor".
#:
#: Super-I/O chips name their inputs, and a fan's automatic curve records which
#: of them it follows in pwmN_temp_sel. Following that link is the one way to
#: tell a CPU fan from a case fan that does not amount to guessing from RPM:
#: it is the driver's own account of what the header cools, not an inference.
#:
#: CPUTIN is the socket's own thermal pad; SMBUSMASTER is the CPU diode read
#: over SMBus (Tctl on AMD); TSI0 is the AMD SB-TSI channel. PECI is Intel's
#: equivalent.
CPU_TEMP_LABELS = ("CPUTIN", "SMBUSMASTER", "TSI0", "TSI1", "PECI", "TCTL",
                   "CPU TEMP", "CPU_TEMP")

#: Chips whose pwm channels are not a case/CPU fan header we should offer.
#:
#: Everything with a pwmN is drivable in principle, but a couple of drivers
#: expose one for something that is not a fan at all, and putting a slider in
#: front of it would be worse than not listing it.
NON_FAN_CHIPS = frozenset((
    "acpitz",          # thermal zone shim, no real header behind it
))

#: Chips known to report a pwm range other than the hwmon-standard 0..255.
#: Read from pwmN_max where the driver publishes it; this is the fallback.
DEFAULT_PWM_MAX = 255

#: How much of a channel's real range the slider is allowed to ask for.
#:
#: The top of the range is left unused on purpose. nct6775 reports a channel
#: held at exactly its maximum as "no fan speed control" rather than "manual",
#: because on that chip the two are one register state; FanState.manual knows
#: this, but never writing the value beats recognising it afterwards, and a
#: driver nobody has read yet may special-case its own top value the same way.
#:
#: The cost is close to nothing: fan speed is roughly linear in duty, so 98%
#: duty is about 99% of the fan's top speed. The scale is rescaled rather than
#: clipped, so the slider's 100% is this ceiling and the round trip back
#: through to_percent() still lands on 100 - a clipped scale would report 98%
#: for a slider the user just put at 100% and snap it back on the next refresh.
MAX_DUTY_FRACTION = 0.98

# How long to wait for the helper's answer to one command. Generous, because the
# very first one is answered only after the user has dealt with the polkit
# prompt.
FIRST_REPLY_TIMEOUT = 120.0
REPLY_TIMEOUT = 5.0

#: pwmN_enable value that means "manual" on every driver implementing it.
#:
#: dell-smm included, although this module once believed otherwise:
#: dell_smm_write() maps 1 to "BIOS fan control off" and 2 to "on", the same
#: numbering as everyone else. What it does differently is publish the attribute
#: write-only on some machines - see FanChannel.enable_readable.
MANUAL = 1

#: pwmN_enable value that the hwmon ABI defines as "no fan speed control",
#: i.e. the fan running flat out.
OFF = 0

#: Chips whose driver reports manual control at full duty as OFF, not MANUAL.
#:
#: nct6775 does not store pwmN_enable; it derives it from the chip's fan-mode
#: register and the current duty, in reg_to_pwm_enable():
#:
#:     if (mode == 0 && pwm == 255)
#:             return off;
#:     return mode + 1;
#:
#: and pwm_enable_to_reg() maps both off and manual back to the same register
#: value. So on these chips "manual at 255" and "no control, full speed" are one
#: hardware state that the driver simply names differently at the top of the
#: range - there is no way to be in one and not the other.
#:
#: Taking the 0 at face value there means concluding the board has taken the fan
#: back at exactly the moment the user asks for 100%, when in fact nothing was
#: handed back and the fan is pinned at full speed under our control.
#:
#: The names are the driver's own table (nct6775_sio_names). Deliberately not a
#: prefix match: nct6683 is a different driver and was not read.
FULL_DUTY_READS_AS_OFF = frozenset((
    "nct6106", "nct6116", "nct6775", "nct6776", "nct6779", "nct6791",
    "nct6792", "nct6793", "nct6795", "nct6796", "nct6797", "nct6798",
    "nct6799",
))


@dataclass(frozen=True)
class FanChannel:
    """One controllable pwm channel, paired with the fan it drives."""

    hwmon: str                          # /sys/class/hwmon/hwmonN
    chip: str                           # "nct6799", "amdgpu"
    index: int                          # the N in pwmN
    has_enable: bool                    # pwmN_enable exists
    fan_input: Optional[str] = None     # fanN_input, when the index pairs up
    fan_label: Optional[str] = None     # fanN_label, rarely present
    source_temp: Optional[str] = None   # label of the temperature it tracks
    pwm_min: int = 0                    # from pwmN_min, when published
    pwm_max: int = DEFAULT_PWM_MAX      # from pwmN_max, when published
    pwm_mode: Optional[bool] = None     # pwmN_mode: True = PWM, False = DC
    writable: bool = True               # pwmN is writable by root
    enable_writable: bool = True        # pwmN_enable is writable by root
    #: pwmN_enable can be read back. dell-smm publishes it write-only (0200)
    #: where it is a global BIOS switch, so the mode is simply not known there.
    enable_readable: bool = True
    #: What the hwmon node hangs off: a PCI address for a graphics card, a
    #: platform device ("nct6775.656") for a Super-I/O chip. "" when the node
    #: has no device link at all.
    device: str = ""

    @property
    def key(self):
        """Stable across reboots, unlike the hwmonN number itself.

        The device is part of it because the chip name alone is not unique:
        two graphics cards are both "amdgpu", and keyed "amdgpu:1" alike they
        shared one claim, one pwm range and one saved name.
        """
        if self.device:
            return "%s@%s:%d" % (self.chip, self.device, self.index)
        return self.legacy_key

    @property
    def legacy_key(self):
        """The key before it named the device, kept to find old settings."""
        return "%s:%d" % (self.chip, self.index)

    @property
    def coarse(self):
        """True when the driver offers only a handful of steps, not 0..255.

        Decided by the pwmN_max the driver publishes. A percentage slider over
        three values is a lie, so the UI shows the raw steps instead. (dell-smm
        has few steps too, but keeps the ABI's 0..255 on the outside and
        publishes no pwmN_max, so it is not one of these.)
        """
        return self.pwm_max < 16

    @property
    def duty_ceiling(self):
        """The highest raw value the percentage scale will ask for.

        Short of pwm_max by MAX_DUTY_FRACTION - see there for why. Coarse
        channels are exempt: on a 0..2 range there is no headroom to give away,
        and trimming any would cost the top speed outright rather than a
        rounding error's worth of it.
        """
        if self.coarse:
            return self.pwm_max
        span = self.pwm_max - self.pwm_min
        return self.pwm_min + int(round(span * MAX_DUTY_FRACTION))

    def to_pwm(self, percent):
        """Percentage to a raw value this channel actually accepts.

        Clamping here rather than at the helper matters: the helper reads the
        value back and calls a mismatch a driver refusal, so sending something
        the driver is bound to clamp would report a failure that is really just
        us asking for the impossible.
        """
        span = self.duty_ceiling - self.pwm_min
        value = self.pwm_min + int(round(percent * span / 100.0))
        return max(self.pwm_min, min(self.duty_ceiling, value))

    def to_percent(self, pwm):
        """The inverse, so that what is displayed is what was asked for.

        A duty above duty_ceiling reads as 100%: the board's own curve is free
        to run the fan flat out even though the slider will not, and reporting
        102% would be worse than reporting the top of the scale.
        """
        span = self.duty_ceiling - self.pwm_min
        if pwm is None or span <= 0:
            return None
        return max(0, min(100, round((pwm - self.pwm_min) * 100.0 / span)))

    @property
    def pwm_path(self):
        return os.path.join(self.hwmon, "pwm%d" % self.index)

    @property
    def enable_path(self):
        return os.path.join(self.hwmon, "pwm%d_enable" % self.index)


@dataclass
class FanState:
    """What a channel is doing right now. All of it reads without privileges."""

    pwm: Optional[int] = None
    enable: Optional[int] = None
    rpm: Optional[int] = None
    channel: Optional["FanChannel"] = None

    @property
    def manual(self):
        """Whether this channel is currently under direct control.

        Not a plain comparison with MANUAL: on nct6775 the value is derived
        from the duty as well as the mode - see FULL_DUTY_READS_AS_OFF.
        """
        if self.enable is None:
            return False
        return self.enable == MANUAL or self._full_duty_manual()

    def _full_duty_manual(self):
        """Whether this is nct6775 renaming manual-at-full-duty to "off".

        Scoped to the chips whose driver was read: on anything else an enable
        of 0 is what the ABI says it is, and amdgpu in particular refuses a pwm
        write in that mode, so treating it as manual would be wrong.
        """
        channel = self.channel
        if channel is None or channel.chip not in FULL_DUTY_READS_AS_OFF:
            return False
        return (self.enable == OFF and self.pwm is not None
                and self.pwm >= channel.pwm_max)

    @property
    def percent(self):
        if self.pwm is None:
            return None
        if self.channel is not None:
            return self.channel.to_percent(self.pwm)
        return round(self.pwm * 100.0 / DEFAULT_PWM_MAX)


def _read_int(path):
    try:
        with open(path) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _read_text(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def discover_channels(root=HWMON_ROOT):
    """Every pwm channel the kernel exposes, in a stable order.

    A pwmN is matched to fanN by index, which is the convention every hwmon
    driver follows and the same assumption lm_sensors' own fancontrol makes.
    Channels whose fan header has nothing plugged into it still appear: the
    header is real and drivable, it just reads 0 RPM.

    Channels the kernel will not let even root write are dropped rather than
    listed and then failed on: several laptop drivers (thinkpad_acpi without
    fan_control=1, some applesmc revisions) publish a read-only pwmN, and a
    slider that cannot move is worse than no slider.

    `root` is a parameter so the tests can point this at a fake tree.
    """
    channels = []
    for hwmon in sorted(glob.glob(os.path.join(root, "hwmon*")),
                        key=_hwmon_sort_key):
        chip = _read_text(os.path.join(hwmon, "name"))
        if not chip or chip in NON_FAN_CHIPS:
            continue

        for path in sorted(glob.glob(os.path.join(hwmon, "pwm*")),
                           key=_attr_sort_key):
            match = PWM_RE.match(os.path.basename(path))
            if not match:
                continue                # pwm1_enable, pwm1_auto_point1_pwm, ...
            index = int(match.group(1))

            channel = _build_channel(hwmon, chip, index)
            if channel is not None:
                channels.append(channel)
    return channels


def _hwmon_sort_key(path):
    """hwmon10 after hwmon9, not after hwmon1."""
    name = os.path.basename(path)
    digits = name[5:]
    return (int(digits), name) if digits.isdigit() else (1 << 30, name)


def _attr_sort_key(path):
    """pwm10 after pwm9. Chips with more than nine headers do exist."""
    match = re.match(r"^([a-z_]+)([0-9]*)(.*)$", os.path.basename(path))
    if not match:
        return (os.path.basename(path), 0, "")
    prefix, digits, rest = match.groups()
    return (prefix, int(digits) if digits else 0, rest)


def _build_channel(hwmon, chip, index):
    """One FanChannel, or None if this pwm is not something we can offer."""
    pwm = os.path.join(hwmon, "pwm%d" % index)
    if not os.access(pwm, os.R_OK):
        return None

    # Root is what will do the writing, so the mode bits are what matter here,
    # not whether *this* process could write it.
    if not _root_writable(pwm):
        log.info("Ignoring %s: the driver publishes it read-only", pwm)
        return None

    enable = os.path.join(hwmon, "pwm%d_enable" % index)
    has_enable = os.path.isfile(enable)

    fan_input = _fan_input_for(hwmon, index)
    label_path = os.path.join(hwmon, "fan%d_label" % index)
    low, high = _pwm_bounds(hwmon, index)

    return FanChannel(
        hwmon=hwmon,
        chip=chip,
        index=index,
        has_enable=has_enable,
        fan_input=fan_input,
        fan_label=_read_text(label_path),
        source_temp=_source_temp(hwmon, index),
        pwm_min=low,
        pwm_max=high,
        pwm_mode=_pwm_mode(hwmon, index),
        writable=True,
        enable_writable=has_enable and _root_writable(enable),
        enable_readable=has_enable and _root_readable(enable),
        device=_device_name(hwmon),
    )


def _device_name(hwmon):
    """Basename of the device behind an hwmon node, or "" if it has none."""
    link = os.path.join(hwmon, "device")
    if not os.path.exists(link):
        return ""
    return os.path.basename(os.path.realpath(link))


def _pwm_bounds(hwmon, index):
    """The range this channel accepts, as the driver states it.

    Sanity-checked rather than trusted outright: the values come from a driver,
    and a nonsensical pair here would size the slider wrongly and make every
    write land somewhere the user did not ask for. Anything that does not
    describe a usable range falls back to the hwmon ABI default.
    """
    low = _read_int(os.path.join(hwmon, "pwm%d_min" % index)) or 0
    high = _read_int(os.path.join(hwmon, "pwm%d_max" % index))

    if high is None or not 0 < high <= DEFAULT_PWM_MAX:
        high = DEFAULT_PWM_MAX
    if not 0 <= low < high:
        low = 0
    return low, high


def _fan_input_for(hwmon, index):
    """The tachometer that belongs to pwmN.

    Same index is the convention and is right on every board seen so far, but
    it is only a convention: a chip that publishes fewer tachometers than pwm
    channels (or numbers them from a different base, as a few it87 variants do)
    would otherwise get a reading that belongs to another header. Falling back
    to 'no reading' is the honest answer; showing another fan's RPM next to
    this one's slider would be actively misleading.
    """
    path = os.path.join(hwmon, "fan%d_input" % index)
    return path if os.path.isfile(path) else None


def _root_writable(path):
    """Whether the owner may write this attribute.

    sysfs attributes are root-owned, so the owner-write bit is what decides
    whether the helper's write can succeed. Checked with os.access only when
    already running as root, since otherwise it answers the wrong question.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return bool(mode & 0o200)


def _root_readable(path):
    """Whether the owner may read this attribute. See _root_writable()."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return bool(mode & 0o400)


def _pwm_mode(hwmon, index):
    """pwmN_mode: 0 is DC (voltage), 1 is PWM. None when the chip is silent.

    Only reported, never written: changing it on a header wired the other way
    can leave a fan at full tilt or refusing to spin, and no reading in the UI
    is worth that.
    """
    value = _read_int(os.path.join(hwmon, "pwm%d_mode" % index))
    if value is None:
        return None
    return value == 1


def _source_temp(hwmon, index):
    """Label of the temperature this channel's automatic curve follows.

    pwmN_temp_sel holds a 1-based index into the chip's own tempN inputs, so
    the driver already knows which sensor drives which header - it just states
    it in two files instead of one. Only Super-I/O chips provide this.
    """
    selected = _read_int(os.path.join(hwmon, "pwm%d_temp_sel" % index))
    if not selected:
        return None
    return _read_text(os.path.join(hwmon, "temp%d_label" % selected))


def is_cpu_fan(channel):
    """True when the driver says this header tracks the processor.

    Deliberately narrow: a channel whose chip publishes no temp_sel link, or
    one pointing at an ambient sensor, is left alone rather than guessed at
    from its speed. Being unsure is reported as unsure.
    """
    label = (channel.source_temp or "").upper()
    if not label:
        return False
    return any(needle in label for needle in CPU_TEMP_LABELS)


def read_state(channel):
    """Current pwm, mode and speed of one channel."""
    return FanState(
        pwm=_read_int(channel.pwm_path),
        enable=_read_int(channel.enable_path) if channel.has_enable else None,
        rpm=_read_int(channel.fan_input) if channel.fan_input else None,
        channel=channel,
    )


#: Drivers whose pwm channels are always a graphics card's own fan.
GPU_CHIPS = frozenset(("amdgpu", "radeon", "nouveau", "i915", "xe", "nvidia"))


def default_name(channel, cards=None):
    """A name that says which fan this is, rather than which file it lives in.

    Super-I/O chips almost never ship fanN_label - this machine's nct6799 ships
    none at all - so "fan3" is all the kernel offers. Naming the device it
    belongs to at least narrows it to the board or a specific card; telling the
    case fans apart is what the rename in the UI is for.
    """
    owner = _owner_name(channel, cards)

    if channel.fan_label:
        return "%s - %s" % (owner, channel.fan_label) if owner else channel.fan_label

    owner = owner or channel.chip
    return "%s - %s %d" % (owner, _role(channel), channel.index)


def _role(channel):
    """What this header cools, when the driver says so plainly enough.

    A graphics driver's pwm is its own fan by construction, so that one needs
    no sensor link. Everything else is only called a CPU fan when the chip's
    own temp_sel says it follows a processor sensor - otherwise it stays a
    plain fan, because guessing from RPM would put the label on the wrong
    header often enough to be worse than no label.
    """
    if channel.chip in GPU_CHIPS:
        return "GPU Fan"
    if is_cpu_fan(channel):
        return "CPU Fan"
    return "Fan"


#: The fan window's sections, top to bottom: the graphics card's own fans,
#: then the headers the board says cool the processor, then everything else
#: on the board.
GPU_SECTION, CPU_SECTION, BOARD_SECTION = "gpu", "cpu", "board"
SECTION_TITLES = {GPU_SECTION: "GPU Fans", CPU_SECTION: "CPU Fans",
                  BOARD_SECTION: "Motherboard Fans"}


def section_of(channel):
    """Which section a channel belongs in. See _role() for how it is decided."""
    if channel.chip in GPU_CHIPS:
        return GPU_SECTION
    if is_cpu_fan(channel):
        return CPU_SECTION
    return BOARD_SECTION


def sections(channels):
    """[(title, [channels])] in display order, empty sections left out.

    Discovery order is kept inside each section, so a board's headers still
    read fan1, fan2, ... A laptop's embedded controller is not a motherboard
    in anyone's vocabulary, so a board section made only of those is called
    System Fans instead.
    """
    grouped = {GPU_SECTION: [], CPU_SECTION: [], BOARD_SECTION: []}
    for channel in channels:
        grouped[section_of(channel)].append(channel)

    out = []
    for key in (GPU_SECTION, CPU_SECTION, BOARD_SECTION):
        members = grouped[key]
        if not members:
            continue
        title = SECTION_TITLES[key]
        if key == BOARD_SECTION and all(c.chip in EC_CHIPS for c in members):
            title = "System Fans"
        out.append((title, members))
    return out


#: Chips that are a laptop's embedded controller rather than a desktop
#: Super-I/O. Naming these "Motherboard" is technically true and practically
#: useless, so they get named after what they are.
EC_CHIPS = {
    "thinkpad": "ThinkPad EC",
    "dell_smm": "Dell SMM",
    "applesmc": "Apple SMC",
    "asus_wmi_sensors": "ASUS EC",
    "asus_wmi_ec_sensors": "ASUS EC",
    "asustf103c": "ASUS EC",
    "hp_wmi": "HP EC",
    "acer_wmi": "Acer EC",
    "system76_acpi": "System76 EC",
    "gigabyte_wmi": "Gigabyte EC",
}


def _owner_name(channel, cards=None):
    """Which piece of hardware this hwmon node hangs off.

    Matched against each card's own hwmon directory rather than against the
    device path, because the two are not the same depth on every driver:
    amdgpu's hwmon sits directly under the card device, nouveau's does not.
    Asking the card where its hwmon node is avoids having to know which.
    """
    if channel.chip in EC_CHIPS:
        return EC_CHIPS[channel.chip]

    hwmon = os.path.realpath(channel.hwmon)
    device = _device_of(channel.hwmon)

    for card in (gpu_cards() if cards is None else cards):
        try:
            card_hwmon = card.hwmon_dir()
        except Exception:
            card_hwmon = None
        if card_hwmon and os.path.realpath(card_hwmon) == hwmon:
            return card.name
        # Fallback for a driver that nests its hwmon somewhere else under the
        # card: the device the node hangs off is still the card itself.
        if device and device == os.path.realpath(card.path):
            return card.name

    if device.startswith("/sys/devices/platform/"):
        return mobo_name()
    return ""


def _device_of(hwmon):
    try:
        return os.path.realpath(os.path.join(hwmon, "device"))
    except OSError:
        return ""


def bundled_helper():
    """The helper and polkit policy shipped with this copy, if there are any.

    An AppImage carries both, because there is nowhere else for its user to get
    them from: there is no checkout and no install.sh. A normal source checkout
    has them too, so the same install path works from either.

    Returns (helper, policy), or (None, None) when this copy ships neither.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    helper = os.path.join(root, "helpers", "ksyssupervisor-fanhelper")
    policy = os.path.join(root, "packaging",
                          "org.ksyssupervisor.fancontrol.policy")
    if os.path.isfile(helper) and os.path.isfile(policy):
        return helper, policy
    return None, None


def install_bundled_helper(runner=None):
    """Install the bundled helper system-wide, asking for a password via pkexec.

    Returns (ok, message). The files are staged into a temporary directory
    first and installed from there, never straight out of the directory they
    ship in: inside an AppImage that directory is a FUSE mount owned by the
    user, which root cannot read, so copying as root from it would fail with a
    permission error that has nothing to do with the password.
    """
    helper, policy = bundled_helper()
    if helper is None:
        return False, ("This copy of %s does not ship the fan helper, so there "
                       "is nothing to install from here." % "KSysSupervisor")
    # Only when we are the ones about to run it: a caller that supplies its
    # own runner is not going through pkexec at all.
    if runner is None and not shutil.which("pkexec"):
        return False, ("pkexec was not found. Install polkit, or copy the "
                       "helper into place yourself as root.")

    with tempfile.TemporaryDirectory(prefix="ksyssupervisor-fanhelper-") as stage:
        # World-readable: root reads these back out of the staging directory.
        os.chmod(stage, 0o755)
        staged_helper = os.path.join(stage, "helper")
        staged_policy = os.path.join(stage, "policy")
        shutil.copyfile(helper, staged_helper)
        shutil.copyfile(policy, staged_policy)
        os.chmod(staged_helper, 0o755)
        os.chmod(staged_policy, 0o644)

        script = os.path.join(stage, "install.sh")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(_INSTALL_SCRIPT % {
                "helper_src": staged_helper,
                "policy_src": staged_policy,
                "helper_dst": HELPER_INSTALL_PATH,
                "policy_dst": POLICY_INSTALL_PATH,
            })
        os.chmod(script, 0o755)

        run = runner or _run_pkexec
        code, output = run(["pkexec", "/bin/sh", script])

    if code == 0:
        return True, ("The fan helper is installed at %s. Fan control is "
                      "ready: open Tools > Fan Control." % HELPER_INSTALL_PATH)
    if code == 126:
        return False, "Cancelled: the request was not authorised."
    if code == 127:
        return False, "Cancelled: no password was given."
    return False, ("The helper could not be installed (exit %s).\n\n%s"
                   % (code, output.strip() or "No output."))


#: Run as root. Installed with the mode the helper needs and no more: root-owned
#: and not writable by anyone else, because the polkit action authorises this
#: path and a user-writable file there would authorise whatever it held.
_INSTALL_SCRIPT = """\
set -e
install -Dm755 -o root -g root '%(helper_src)s' '%(helper_dst)s'
sed 's|@HELPER_PATH@|%(helper_dst)s|' '%(policy_src)s' > '%(policy_dst)s'
chown root:root '%(policy_dst)s'
chmod 644 '%(policy_dst)s'
"""


def _run_pkexec(command):
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
    except Exception as exc:                 # pragma: no cover - environmental
        log.exception("Could not run pkexec")
        return 1, str(exc)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def helper_path():
    """The installed helper, or None if this is not a system install."""
    for path in HELPER_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


_HELPER_VERSION_RE = re.compile(r"^PROTOCOL_VERSION\s*=\s*([0-9]+)\s*$",
                                re.MULTILINE)


def installed_helper_version(path=None):
    """The protocol version the installed helper speaks, or None if unknown.

    The helper is a world-readable script whose version is a plain constant,
    so the same mismatch HELLO would refuse can be seen for free, before a
    password prompt has been spent on starting it. None means the file could
    not be read or does not state a version; the handshake stays the
    authority in that case rather than this guess.
    """
    path = path or helper_path()
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(65536)
    except OSError:
        return None
    match = _HELPER_VERSION_RE.search(text)
    return int(match.group(1)) if match else None


#: For a copy that carries no helper of its own - a hand-made copy of the
#: sources - the README's manual commands are the only way left.
MANUAL_INSTALL_HINT = ("    see \"Fan control\" in the README for the two "
                       "commands that\n    install it by hand\n")


def _reinstall_hint():
    """The one command that updates the installed helper from this copy."""
    if bundled_helper()[0] is not None:
        return "    %s --install-fan-helper\n" % installation.self_command()
    return MANUAL_INSTALL_HINT


def _outdated_helper_reason():
    return ("The installed fan control helper is from an older version of "
            "KSysSupervisor and no longer matches this one. Using it would "
            "bring back bugs this version fixes: the Automatic button "
            "putting a fan straight back into manual, and on Dell laptops "
            "switching the BIOS fan control off instead of on.\n\n"
            "Update it with:\n" + _reinstall_hint())


#: availability() outcomes. The two helper states are the ones the app can put
#: right by itself, with one password prompt; the others need the user.
READY = "ready"
NO_CHANNELS = "no-channels"
NO_HELPER = "no-helper"
OUTDATED_HELPER = "outdated-helper"
NO_PKEXEC = "no-pkexec"


def availability():
    """(state, reason): whether fan control can run, and why not.

    reason is None when state is READY. Checked when fan control is opened
    rather than once at startup, so installing the helper from the app takes
    effect straight away - there is nothing to restart.
    """
    if not discover_channels():
        return NO_CHANNELS, _no_channels_reason()
    if helper_path() is None:
        return NO_HELPER, _missing_helper_reason()
    if not shutil.which("pkexec"):
        return NO_PKEXEC, _no_pkexec_reason()
    version = installed_helper_version()
    if version is not None and version != PROTOCOL_VERSION:
        return OUTDATED_HELPER, _outdated_helper_reason()
    return READY, None


def can_install_helper(state):
    """Whether the app can fix `state` itself by installing its own helper.

    Only a missing or outdated helper, and only when this copy carries one to
    install and pkexec is there to ask for the password. Installing it moves
    nothing else: the application stays where it was installed, and its
    settings are not touched.
    """
    return (state in (NO_HELPER, OUTDATED_HELPER)
            and bundled_helper()[0] is not None
            and shutil.which("pkexec") is not None)


def unavailable_reason():
    """Why fan control cannot run here, or None when it can."""
    return availability()[1]


def _no_pkexec_reason():
    return ("Fan control needs 'pkexec' to ask for authorisation. Install "
            "polkit and make sure your desktop runs a polkit authentication "
            "agent.")


def _no_channels_reason():
    return ("No controllable fan channels were found.\n\n"
            "The kernel exposes none of the pwm attributes fan control "
            "needs. On a desktop this almost always means the Super-I/O "
            "driver for the board is not loaded: 'sudo sensors-detect' "
            "identifies the chip and names the module to load (nct6775, "
            "it87, w83627ehf and so on). Some boards additionally need "
            "the module's acpi_enforce_resources=lax option, because the "
            "firmware claims the chip for itself.\n\n"
            "On a laptop the fans are usually driven by the embedded "
            "controller and are simply not exposed to the kernel at all, "
            "in which case there is nothing to control from here.\n\n"
            "See Help > Hardware Setup.")


def _missing_helper_reason():
    # The flag, run by whatever command starts this very copy: it works from
    # a checkout, an installation and an AppImage alike.
    how = _reinstall_hint() + "\n"
    return ("Fan control needs a small helper that runs as root, and it "
            "is not installed.\n\n"
            "Add it with:\n" + how +
            "That installs the helper and nothing else - it does not move "
            "or reinstall KSysSupervisor, so a per-user installation stays "
            "exactly where it is. Only the helper has to be system-wide: "
            "it runs as root, so it must live somewhere you cannot modify "
            "without root yourself, or authorising it would authorise "
            "whatever you put there.")


class HelperError(Exception):
    """The helper refused a command, died, or never started."""


class HelperClient:
    """Owns the privileged helper process and speaks its line protocol.

    One instance per session. start() raises the polkit prompt; from then on
    every write goes down the same pipe, so the user authenticates once no
    matter how much the sliders move.

    Closing the pipe is not merely cleanup, it is the safety mechanism: the
    helper restores every channel it was not told to keep, so the fans come back
    under the board's control even if this process is killed outright.
    """

    def __init__(self, path=None):
        self._path = path or helper_path()
        self._proc = None
        self._lock = threading.Lock()
        self._claimed = set()
        self._bounds = {}

    # ---- lifecycle ------------------------------------------------------

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        """Launch the helper under pkexec and complete the handshake.

        Blocks until the user answers the authentication prompt, so callers
        must not run this on a thread that is painting the UI.
        """
        if self.running:
            return
        # Looked up again rather than trusted from construction: the helper
        # may have been installed from the app since this client was made.
        if self._path is None:
            self._path = helper_path()
        if self._path is None:
            raise HelperError("The fan control helper is not installed.")

        try:
            self._proc = subprocess.Popen(
                ["pkexec", self._path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as exc:
            raise HelperError("Could not run pkexec: %s" % exc)

        try:
            reply = self._hello()
        except HelperError:
            # pkexec exits 126 when the user dismisses or fails the prompt, and
            # 127 when the action is not registered; either way the helper never
            # ran, so there is nothing to tear down.
            self._reap()
            raise

        if not reply.startswith("READY"):
            self._reap()
            raise HelperError("Unexpected greeting from the helper: %s" % reply)
        log.info("Fan control helper started (%s)", self._path)

    def _hello(self):
        try:
            return self._exchange("HELLO %d" % PROTOCOL_VERSION,
                                  timeout=FIRST_REPLY_TIMEOUT)
        except HelperError as exc:
            # An old helper refuses the version rather than mis-serving it.
            # unavailable_reason() catches this case without a prompt, but
            # only for the versions it can read out of the file, so the
            # refusal still needs a better answer than the raw ERR text.
            if "protocol version" in str(exc):
                raise HelperError(
                    "The installed fan control helper is from a different "
                    "version of KSysSupervisor.\n\nUpdate it with:\n"
                    + _reinstall_hint())
            raise

    def stop(self):
        """Ask the helper to restore and exit, then make sure it has."""
        if not self.running:
            self._reap()
            return
        try:
            self._exchange("BYE")
        except HelperError:
            pass
        self._reap()

    def _reap(self):
        proc, self._proc = self._proc, None
        self._claimed.clear()
        self._bounds.clear()
        if proc is None:
            return
        # stdin first and alone: EOF is what tells the helper to restore and
        # exit. stdout waits until it has - a reader left behind by a timed
        # out reply can still be blocked in readline(), holding the buffer's
        # lock, and closing the stream under it would block here with it.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # It should have exited on EOF; if it has not, signalling it is
            # still safe because its own handler restores the fans first. It
            # usually cannot be signalled at all, though: pkexec made it root.
            for stop in (proc.terminate, proc.kill):
                try:
                    stop()
                    proc.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except OSError as exc:
                    log.warning("Could not stop the fan control helper "
                                "(pid %s): %s", proc.pid, exc)
                    break
        if proc.poll() is not None:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass

    # ---- protocol -------------------------------------------------------

    def _exchange(self, command, timeout=REPLY_TIMEOUT):
        """Send one command, return its reply. Serialised across threads."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise HelperError("The fan control helper is not running.")

            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise HelperError("Lost contact with the helper: %s" % exc)

            reply = _read_line(proc, timeout)

        if reply is None:
            # Not survivable: the reader waiting for this reply is still
            # there, and would take the next command's reply as this one's,
            # leaving every answer after it off by one. Ending the session is
            # also the safe way out - the helper restores the fans on EOF.
            log.warning("The fan control helper did not answer %r in time; "
                        "ending the session", command.split()[0])
            self._reap()
            raise HelperError("The fan control helper stopped responding, so "
                              "the session was ended. Fans it held go back to "
                              "automatic control as it exits.")
        if reply.startswith("ERR"):
            raise HelperError(reply[4:].strip() or "the helper refused")
        return reply

    # ---- commands -------------------------------------------------------

    def holds(self, channel):
        """Whether this session has claimed the channel and may write to it.

        A row can read as manual without this being true - a board left a fan
        under direct control in its own firmware, say - and offering a slider
        there would offer a write the helper is right to refuse.
        """
        return self.running and channel.key in self._claimed

    def claim(self, channel):
        """Record a channel's state with the helper before touching it.

        Returns (enable, pwm) as found, which is also what will be put back.
        """
        reply = self._exchange("CLAIM %s %d" % (channel.hwmon, channel.index))
        self._claimed.add(channel.key)

        parts = reply.split()
        def maybe(text):
            return None if text == "-" else int(text)
        try:
            enable, pwm = maybe(parts[1]), maybe(parts[2])
        except (IndexError, ValueError):
            return None, None

        # Older helpers answer without the bound; treat its absence as "the
        # discovery-time value stands" rather than as a protocol error.
        if len(parts) > 3:
            try:
                self._bounds[channel.key] = int(parts[3])
            except ValueError:
                pass
        return enable, pwm

    def pwm_max(self, channel, default=None):
        """What the helper said this channel accepts, if it has said."""
        return self._bounds.get(channel.key,
                                channel.pwm_max if default is None else default)

    def set_pwm(self, channel, value):
        """Put the channel under manual control at `value` (0-255).

        The returned number is what the driver actually kept, not what we asked
        for; a driver that ignored the write raises instead.
        """
        # Clamped to what this channel accepts, not to 0..255: a driver with a
        # narrower range clamps silently, and the helper's readback would then
        # report that clamp as a refusal.
        value = max(channel.pwm_min, min(channel.pwm_max, int(value)))
        if channel.key not in self._claimed:
            self.claim(channel)
        reply = self._exchange("SET %s %d %d"
                               % (channel.hwmon, channel.index, value))
        try:
            return int(reply.split()[1])
        except (IndexError, ValueError):
            return value

    def set_auto(self, channel):
        """Hand the channel back to the driver's own regulation.

        Not "back to whatever it was originally": that is what the helper does
        on the way out, and it is the wrong thing here, because a channel left
        in manual by an earlier session would be restored straight back into
        manual. Refuses rather than returning quietly when the channel is not
        held - silently doing nothing left the user pressing a button that
        never worked.
        """
        if channel.key not in self._claimed:
            raise HelperError(
                "This channel is not under this session's control, so its "
                "mode cannot be changed.")
        self._exchange("AUTO %s %d" % (channel.hwmon, channel.index))

    def keep(self, channel):
        """Exempt a channel from the restore the helper does when it exits.

        The user has to have confirmed this explicitly: it leaves a fan pinned
        at a fixed speed with nothing regulating it.
        """
        if channel.key not in self._claimed:
            return
        self._exchange("KEEP %s %d" % (channel.hwmon, channel.index))

    def ping(self):
        """Reset the helper's deadman timer."""
        self._exchange("PING")


def _read_line(proc, timeout):
    """One line from the helper, or None if it goes quiet or dies.

    readline() on the pipe would block forever if the helper hung, and this runs
    with fans under manual control, so every wait is bounded.
    """
    result = []

    def reader():
        try:
            result.append(proc.stdout.readline())
        except (OSError, ValueError):
            result.append("")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or not result or not result[0]:
        return None
    return result[0].strip()
