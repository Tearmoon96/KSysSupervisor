"""The recovery stick's first program: what the kernel runs at boot.

It sets up the little system the stick starts (the kernel's own
filesystems, udev so that drives and keyboards appear, the stick itself
mounted so that what must survive a restart is kept there) and then runs
the Disk Utility full screen, again if it stops unexpectedly, until it asks
for the computer to be restarted or switched off.

Being process 1 it must never end - the kernel stops with a panic when it
does - and it collects every orphaned process. Only the standard library
is used: nothing else is certain to work this early.

Qt-free.
"""

import ctypes
import ctypes.util
import json
import os
import signal
import subprocess
import sys
import time

MARKER = "/etc/kss-rescue"
STICK_MOUNT = "/run/kss/stick"
#: On the stick: what the helper keeps (partition tables as they were before
#: each edit, the record of a partition move in progress) and the logs.
STICK_FOLDER = "KSysSupervisor"
STATE_DIR = "/var/lib/ksyssupervisor"
RUN_DIR = "/run/kss"

#: What the app's exit code asks for. EXIT_RESTART: start the app again,
#: with the keyboard layout it left in KEYBOARD_FILE.
EXIT_POWER_OFF = 10
EXIT_REBOOT = 11
EXIT_RESTART = 12
KEYBOARD_FILE = "/run/kss/keyboard"

#: Drivers asked for by name, besides what udev loads for the hardware it
#: finds: the stick's FAT and its character sets, and what a USB stick and
#: an internal drive are reached through when the kernel has them as
#: modules.
MODULES = ("vfat", "nls_cp437", "nls_iso8859-1", "nls_utf8", "loop",
           "dm_mod", "usb_storage", "uas", "sd_mod", "nvme", "ahci",
           "xhci_pci", "ehci_pci", "usbhid", "hid_generic", "evdev",
           "simpledrm", "efivarfs")

PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

#: reboot(2) commands.
RB_AUTOBOOT = 0x01234567
RB_POWER_OFF = 0x4321FEDC

_libc = None


def libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                            use_errno=True)
    return _libc


def log(text):
    line = "[%7.2f] kss: %s\n" % (time.monotonic(), text)
    for path in ("/dev/console", os.path.join(RUN_DIR, "init.log")):
        try:
            with open(path, "a") as handle:
                handle.write(line)
        except OSError:
            pass


def mount(source, target, fstype, flags=0, data=""):
    os.makedirs(target, exist_ok=True)
    if libc().mount(source.encode(), target.encode(), fstype.encode(),
                    flags, data.encode() if data else None) != 0:
        error = ctypes.get_errno()
        log("mount %s on %s: %s" % (fstype, target, os.strerror(error)))
        return False
    return True


MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_BIND = 2, 4, 8, 4096


def basic_mounts():
    mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount("sysfs", "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount("devtmpfs", "/dev", "devtmpfs", MS_NOSUID, "mode=0755")
    mount("devpts", "/dev/pts", "devpts", MS_NOSUID | MS_NOEXEC,
          "gid=5,mode=620,ptmxmode=000")
    mount("tmpfs", "/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777")
    mount("tmpfs", "/run", "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755")
    mount("efivarfs", "/sys/firmware/efi/efivars", "efivarfs",
          MS_NOSUID | MS_NODEV | MS_NOEXEC)
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs("/run/user/0", mode=0o700, exist_ok=True)


def console():
    """Standard input and output on the console, now that /dev is the
    kernel's."""
    try:
        fd = os.open("/dev/console", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return
    for target in (0, 1, 2):
        os.dup2(fd, target)
    if fd > 2:
        os.close(fd)


def settings():
    try:
        with open(MARKER) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def cmdline():
    """kss.name=value arguments of the kernel command line, as a dict."""
    found = {}
    try:
        with open("/proc/cmdline") as handle:
            words = handle.read().split()
    except OSError:
        return found
    for word in words:
        key, _sep, value = word.partition("=")
        if key.startswith("kss."):
            found[key[4:]] = value
    return found


def run(argv, timeout=120):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True,
                              timeout=timeout,
                              env={"PATH": PATH, "LC_ALL": "C"}).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        log("%s: %s" % (argv[0], exc))
        return None


def start_udev(config):
    udevd = config.get("udevd") or "/usr/lib/systemd/systemd-udevd"
    if os.path.basename(udevd) == "udevadm":
        # One program under two names; the daemon is told by its name.
        argv = [udevd, "--daemon"]
        target = "/run/kss/systemd-udevd"
        try:
            os.symlink(udevd, target)
            argv[0] = target
        except OSError:
            pass
    else:
        argv = [udevd, "--daemon"]
    if run(argv) != 0:
        log("udev did not start; drives may be missing")
        return False
    run(["udevadm", "trigger", "--type=subsystems", "--action=add"])
    run(["udevadm", "trigger", "--type=devices", "--action=add"])
    run(["udevadm", "settle", "--timeout=30"], 60)
    return True


def load_modules():
    for name in MODULES:
        run(["modprobe", "-q", name], 30)


def find_stick(uuid, wait=30.0):
    """The stick's device, from the FAT ID the boot loader passed on. USB
    devices can take a while to show up."""
    if not uuid:
        return None
    link = "/dev/disk/by-uuid/%s" % uuid
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if os.path.exists(link):
            return os.path.realpath(link)
        run(["udevadm", "settle", "--timeout=5"], 10)
        time.sleep(0.5)
    return None


def mount_stick(device):
    os.makedirs(STICK_MOUNT, exist_ok=True)
    # "flush" writes each file out as it is closed: the stick may be pulled
    # the moment the screen says it is safe.
    if run(["mount", "-t", "vfat", "-o",
            "rw,noatime,flush,utf8,umask=0022", device, STICK_MOUNT]) != 0:
        log("the stick (%s) could not be mounted" % device)
        return False
    for folder in ("state", "logs"):
        os.makedirs(os.path.join(STICK_MOUNT, STICK_FOLDER, folder),
                    exist_ok=True)
    state = os.path.join(STICK_MOUNT, STICK_FOLDER, "state")
    os.makedirs(STATE_DIR, exist_ok=True)
    mount(state, STATE_DIR, "", MS_BIND)
    return True


def screen_width():
    """Width in pixels of the screen the firmware set up, or 0."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size") as handle:
            return int(handle.read().split(",")[0])
    except (OSError, ValueError):
        pass
    try:
        for name in sorted(os.listdir("/sys/class/drm")):
            path = os.path.join("/sys/class/drm", name, "modes")
            if os.path.isfile(path):
                with open(path) as handle:
                    first = handle.readline().strip()
                if "x" in first:
                    return int(first.split("x")[0])
    except (OSError, ValueError):
        pass
    return 0


def screen_scale():
    """Qt's scale factor for the screen the firmware set up: text at its
    usual size on a high-resolution panel."""
    width = screen_width()
    if width >= 3200:
        return "2"
    if width >= 2400:
        return "1.5"
    return "1"


def app_env(config, stick, debug=False):
    env = {
        "PATH": PATH, "HOME": "/root", "USER": "root", "LOGNAME": "root",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "linux",
        "XDG_RUNTIME_DIR": "/run/user/0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "QT_QPA_PLATFORM": "linuxfb",
        "QT_SCALE_FACTOR": screen_scale(),
        # Many kernels (Fedora's among them) no longer make /dev/fb0 for
        # programs; the firmware's screen is then reached as a DRM device,
        # which linuxfb draws on as well.
        "QT_QPA_FB_DRM": "0" if os.path.exists("/dev/fb0") else "1",
        "FONTCONFIG_FILE": "/etc/fonts/fonts.conf",
        "XKB_DEFAULT_LAYOUT": config.get("keyboard") or "us",
        "XKB_DEFAULT_VARIANT": config.get("variant") or "",
        "KSS_RESCUE": "1",
    }
    if config.get("pointer") == "evdev":
        # Mice and touchpads through Qt's evdev handler; the stick's udev
        # rule keeps them from libinput (see rescuebuild.POINTER_PLUGIN).
        env["QT_QPA_GENERIC_PLUGINS"] = "evdevmouse"
    if stick:
        env["XDG_STATE_HOME"] = os.path.join(STICK_MOUNT, STICK_FOLDER,
                                             "logs")
    if debug:
        # kss.debug on the boot line: what Qt does with the screen and the
        # input devices, and a Python traceback of any crash, in the log.
        env.update({"QT_DEBUG_PLUGINS": "1", "PYTHONFAULTHANDLER": "1",
                    "QT_LOGGING_RULES": "qt.qpa.*=true"})
    return env


class Children:
    """Process 1 collects every process that ends, its own or orphaned."""

    def __init__(self):
        self.shell = None
        self.shell_tty = None

    def spawn_shell(self, tty):
        """A root shell on a serial line or console, for the expert, when
        the boot line asks for one (kss.shell=ttyS0)."""
        self.shell_tty = tty
        path = "/dev/" + tty
        try:
            self.shell = subprocess.Popen(
                ["setsid", "--ctty", "bash", "-l"],
                stdin=open(path, "rb"), stdout=open(path, "wb"),
                stderr=subprocess.STDOUT, env={
                    "PATH": PATH, "HOME": "/root", "TERM": "linux",
                    "PS1": "kss# "}).pid
        except OSError as exc:
            log("no shell on %s: %s" % (tty, exc))
            self.shell = None

    def wait_for(self, pid):
        """Wait until `pid` ends, reaping the others; its exit code."""
        while True:
            try:
                ended, status = os.waitpid(-1, 0)
            except ChildProcessError:
                return None
            except InterruptedError:
                continue
            if ended == self.shell and self.shell_tty:
                time.sleep(1)
                self.spawn_shell(self.shell_tty)
            if ended == pid:
                return os.waitstatus_to_exitcode(status)


def stop(how):
    """Everything written out, the stick unmounted, then off or restart."""
    log("stopping (%s)" % ("restart" if how == RB_AUTOBOOT else "power off"))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(-1, sig)
        except OSError:
            pass
        time.sleep(1)
    os.sync()
    for point in (STATE_DIR, STICK_MOUNT):
        libc().umount2(point.encode(), 0)
    os.sync()
    libc().reboot(how)
    # Only reached when the kernel refused.
    while True:
        time.sleep(3600)


def emergency(reason, children):
    """Something went wrong that the app cannot show: say so on the console
    and keep a shell there. Never return."""
    log(reason)
    print("\n%s\n\nA shell is open on this screen. Press Ctrl+Alt+Del to "
          "restart the computer.\n" % reason, flush=True)
    while True:
        try:
            shell = subprocess.Popen(["bash", "-l"], env={
                "PATH": PATH, "HOME": "/root", "TERM": "linux",
                "PS1": "kss# "})
            children.wait_for(shell.pid)
        except Exception:
            time.sleep(5)


def main():
    os.umask(0o022)
    children = Children()
    try:
        basic_mounts()
        console()
        # Ctrl+Alt+Del asks process 1 instead of resetting at once: the
        # stick is unmounted first.
        try:
            with open("/proc/sys/kernel/ctrl-alt-del", "w") as handle:
                handle.write("0\n")
        except OSError:
            pass
        signal.signal(signal.SIGINT, lambda *_a: stop(RB_AUTOBOOT))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        config = settings()
        log("KSysSupervisor recovery %s, kernel %s"
            % (config.get("version", "?"), os.uname().release))
        args = cmdline()
        load_modules()
        start_udev(config)
        stick = find_stick(args.get("stick"))
        stick_ok = bool(stick) and mount_stick(stick)
        if not stick_ok:
            log("running without the stick mounted: nothing will be kept "
                "across a restart")
        if args.get("shell"):
            children.spawn_shell(args["shell"])
        env = app_env(config, stick_ok, "debug" in args)
        python = config.get("python") or sys.executable
        script = os.path.join(config.get("app_dir", "/opt/ksyssupervisor"),
                              "KSysSupervisor.py")
        failures = []
        while True:
            app = subprocess.Popen([python, script, "--rescue"], env=env,
                                   cwd="/root")
            code = children.wait_for(app.pid)
            if code == EXIT_POWER_OFF:
                stop(RB_POWER_OFF)
            if code == EXIT_REBOOT:
                stop(RB_AUTOBOOT)
            if code == EXIT_RESTART:
                try:
                    with open(KEYBOARD_FILE) as handle:
                        layout = handle.read().strip()
                except OSError:
                    layout = ""
                if layout:
                    env["XKB_DEFAULT_LAYOUT"] = layout
                    env["XKB_DEFAULT_VARIANT"] = ""
                    log("keyboard layout now %s" % layout)
                continue
            log("the Disk Utility stopped (exit code %s); starting it again"
                % code)
            now = time.monotonic()
            failures = [t for t in failures if now - t < 60] + [now]
            if len(failures) >= 3:
                emergency("The Disk Utility keeps stopping (exit code %s). "
                          "Its log is in %s on the stick."
                          % (code, os.path.join(STICK_FOLDER, "logs")),
                          children)
    except Exception as exc:
        emergency("The recovery system could not start: %r" % exc, children)


if __name__ == "__main__":
    main()
