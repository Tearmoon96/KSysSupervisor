"""Building the recovery stick's system out of the one running here.

The stick starts the computer into a small system that runs entirely from
memory and shows the Disk Utility full screen. That system is not
downloaded: it is put together from this computer's own files - its kernel,
its Python and Qt, its disk tools - so it works offline and matches what
the Disk Utility was tested with here.

Everything is gathered into one compressed cpio archive, the form the
kernel unpacks into memory at boot (an "initramfs"). Nothing else is needed
at boot: no root filesystem to find, no dracut, no squashfs. The build runs
as the user and only reads world-readable files; writing the stick, which
also takes the signed boot loader from the EFI partition, is the
privileged helper's part ("rescue_write").

Qt-free.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import APP_NAME, APP_VERSION

#: Where the stick's own files go on it; the boot loader's are in EFI/BOOT.
STICK_FOLDER = "kss"
KERNEL_NAME = "vmlinuz"
INITRD_NAME = "initrd.img"
MANIFEST_NAME = "manifest.json"
#: The stick's FAT label; at most 11 characters.
STICK_LABEL = "KSSRESCUE"

#: Present in the stick's system, and only there: what tells the app, and
#: the helper, that they are running from the stick. Holds the build's
#: settings as JSON.
MARKER = "/etc/kss-rescue"
#: Where the app and its helpers are in the stick's system.
APP_DIR = "/opt/ksyssupervisor"
#: Where an AppImage's own folder goes, when the app runs from one.
APPDIR_TARGET = "/opt/ksyssupervisor-runtime"

#: What the stick and the computer need, for the dialog to say beforehand.
NEED_STICK = 1024 ** 3
NEED_MEMORY = 2 * 1024 ** 3

MIB = 1024 * 1024

#: Folders of the Python standard library that are never needed on the
#: stick: tests, the IDE, Tk, and the installers of new packages.
STDLIB_SKIP = frozenset((
    "test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip",
    "lib2to3", "pydoc_data", "site-packages", "dist-packages",
    "config-3", "venv", "__phello__",
))
#: Extension modules left out for the same reason.
DYNLOAD_SKIP = re.compile(r"^(_tkinter|_test|xx|_xx|_ctypes_test)")

#: The PyQt modules the Disk Utility uses. (QtSvg: the icons are SVG.)
QT_MODULES = ("QtCore", "QtGui", "QtWidgets", "QtSvg", "QtDBus")
#: Qt plugins, by folder: the screen without a desktop, and SVG icons.
QT_PLUGINS = {
    "platforms": ("libqlinuxfb.so",),
    "imageformats": ("libqsvg.so",),
    "iconengines": ("libqsvgicon.so",),
    "generic": ("libqevdevmouseplugin.so",),
}
#: Qt's evdev handler for mice and touchpads. Qt's libinput handler keeps
#: the pointer in whole pixels and rounds every movement, dropping each one
#: under half a pixel - and a mouse moved slowly, polled hundreds of times
#: a second, makes nothing else: the pointer stuck, then jumped. The evdev
#: handler adds up every count. libinput keeps the keyboards, whose layouts
#: only it knows.
POINTER_PLUGIN = ("generic", "libqevdevmouseplugin.so")
POINTER_RULES = (
    "# Mice and touchpads go to Qt's evdev handler, keyboards to libinput.\n"
    'ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event*", '
    'ENV{ID_INPUT_KEYBOARD}!="1", ENV{ID_INPUT_MOUSE}=="1", '
    'ENV{LIBINPUT_IGNORE_DEVICE}="1"\n'
    'ACTION!="remove", SUBSYSTEM=="input", KERNEL=="event*", '
    'ENV{ID_INPUT_KEYBOARD}!="1", ENV{ID_INPUT_TOUCHPAD}=="1", '
    'ENV{LIBINPUT_IGNORE_DEVICE}="1"\n')
POINTER_RULES_FILE = "/etc/udev/rules.d/70-kss-pointer.rules"
#: The one pointer the stick shows: the desktop's arrow.
CURSOR_NAMES = ("left_ptr", "default", "arrow")
CURSOR_THEMES = ("breeze_cursors", "Adwaita", "default")
#: Other Python packages the app imports.
PACKAGES = ("psutil",)

#: Every program the stick's system may run: the helper's tools (the test
#: suite holds this against the helper), and what starting the system,
#: reading the drive list and a shell for the expert need. A tool missing
#: here is simply left out, as it would be missing on any computer, and the
#: Disk Utility greys out what needs it.
RESCUE_TOOLS = (
    # Starting and running the system.
    "bash", "sh", "mount", "umount", "kmod", "modprobe", "udevadm",
    "lsblk", "blkid", "findmnt", "dmesg", "sync", "ls", "cat", "less",
    "loadkeys", "setsid", "agetty", "grep", "ps", "kill", "dd", "cp",
    "mv", "rm", "mkdir", "df", "free", "lsmod", "head", "tail", "od",
    # Partitions, devices, signatures.
    "sfdisk", "wipefs", "blkdiscard", "partprobe", "partx", "blockdev",
    "losetup", "dmsetup", "mdadm", "fstrim",
    # Filesystems: making, naming, checking, resizing, copying.
    "mkfs.fat", "mkfs.vfat", "fatlabel", "fsck.fat", "fsck.vfat",
    "mkfs.exfat", "exfatlabel", "fsck.exfat",
    "mkfs.ntfs", "mkntfs", "ntfslabel", "ntfsfix", "ntfsresize",
    "ntfsclone", "ntfs-3g", "ntfs-3g.probe", "mount.ntfs-3g", "ntfsinfo",
    "mkfs.ext4", "mke2fs", "e2label", "e2fsck", "resize2fs", "e2image",
    "dumpe2fs", "tune2fs", "e4defrag", "debugfs",
    "mkfs.btrfs", "btrfs", "btrfstune",
    "mkfs.xfs", "xfs_admin", "xfs_repair", "xfs_growfs", "xfs_copy",
    "xfs_db", "xfs_fsr", "xfs_io",
    "mkfs.f2fs", "fsck.f2fs", "resize.f2fs",
    "mkswap", "swaplabel",
    "cryptsetup",
    # Recovery.
    "fsstat", "fls", "icat", "photorec", "testdisk", "ddrescue",
)
#: Directories the tools are looked for in, as the helper looks for them.
TOOL_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")

#: Kernel modules taken along, by folder under kernel/: storage of every
#: kind, USB, keyboards and mice, filesystems, and what those need. Graphics
#: drivers are left out on purpose: the screen is the one the firmware set
#: up, which needs no driver and no firmware file, while a graphics driver
#: loaded without its firmware leaves the screen black.
MODULE_FOLDERS = (
    "drivers/ata", "drivers/nvme", "drivers/scsi", "drivers/usb",
    "drivers/hid", "drivers/input", "drivers/md", "drivers/mmc",
    "drivers/block", "drivers/virtio", "drivers/ufs", "drivers/memstick",
    "drivers/cdrom", "drivers/firmware", "drivers/pci/controller",
    "drivers/char/hw_random", "drivers/platform/x86",
    "fs", "crypto", "lib", "arch/x86/crypto",
)
#: Graphics modules that are wanted after all: the firmware's own screen
#: (simpledrm, efifb) where a kernel builds them as modules.
MODULE_EXTRA = re.compile(r"/(simpledrm|efidrm|ofdrm|drm|drm_kms_helper|"
                          r"drm_shmem_helper|drm_client_lib|drm_ttm_helper|"
                          r"efifb|simplefb)\.ko")

#: Libraries loaded at run time by another one, which ldd cannot see: the
#: companion goes along whenever the first is taken. glibc loads libgcc_s to
#: end a thread; Fedora's SDL2 is a layer over SDL3 and aborts at load time
#: without it (pulled in by the touchscreen library Qt's linuxfb uses).
DLOPENED = {
    "libc.so.6": ("libgcc_s.so.1",),
    "libSDL2-2.0.so.0": ("libSDL3.so.0",),
}

#: Small files the stick's system needs from /etc.
ETC_FILES = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/nsswitch.conf",
             "/etc/udev/udev.conf", "/etc/mke2fs.conf", "/etc/os-release",
             # The time zone, so the times the stick shows are this PC's.
             "/etc/localtime")

#: Icon themes looked in, in order, for the icons the app names.
ICON_THEMES = ("breeze", "breeze-dark", "hicolor", "Adwaita")


class BuildError(Exception):
    """Why the recovery system could not be put together, in words."""


# ---- the archive --------------------------------------------------------------

@dataclass
class Entry:
    kind: str                     # "file", "dir", "link", "data", "node"
    mode: int = 0o644
    source: str = ""              # file: the host path to read
    data: bytes = b""             # data: the contents
    target: str = ""              # link: where it points
    rdev: tuple = (0, 0)          # node: (major, minor)
    mtime: int = 0


class Image:
    """What goes into the archive, by the path it will have there."""

    def __init__(self, moves=()):
        self.entries: dict[str, Entry] = {}
        #: [(host prefix, image prefix)] - files that move on the way, such
        #: as an AppImage's own folder, which is only mounted while it runs.
        self.moves = list(moves)
        self._elf_done: set[str] = set()
        self.missing: list[str] = []

    # ---- paths --------------------------------------------------------------

    def place(self, host_path):
        """Where a host path goes in the image."""
        for host, image in self.moves:
            if host_path == host or host_path.startswith(host + "/"):
                return image + host_path[len(host):]
        return host_path

    def _parents(self, path):
        parent = os.path.dirname(path)
        while parent and parent != "/" and parent not in self.entries:
            self.entries[parent] = Entry("dir", 0o755)
            parent = os.path.dirname(parent)

    def _put(self, path, entry):
        path = os.path.normpath(path)
        if path in ("/", "."):
            return
        existing = self.entries.get(path)
        if existing is not None and existing.kind == "dir" \
                and entry.kind == "dir":
            return
        self._parents(path)
        self.entries[path] = entry

    # ---- adding -------------------------------------------------------------

    def add_dir(self, path, mode=0o755):
        self._put(path, Entry("dir", mode))

    def add_data(self, path, data, mode=0o644):
        if isinstance(data, str):
            data = data.encode()
        self._put(path, Entry("data", mode, data=data,
                              mtime=int(time.time())))

    def add_link(self, path, target):
        self._put(path, Entry("link", 0o777, target=target))

    def add_node(self, path, major, minor, mode=0o600):
        self._put(path, Entry("node", stat.S_IFCHR | mode,
                              rdev=(major, minor)))

    def add_path(self, host_path):
        """A host file with every symlink met on the way to it, so that
        each name it is known by works in the image. Returns the real path,
        or None when there is nothing there."""
        links, real = resolve_chain(host_path)
        for link, target in links:
            if os.path.isabs(target):
                target = self.place(target)
            self.add_link(self.place(link), target)
        try:
            info = os.stat(real)
        except OSError:
            return None
        if stat.S_ISDIR(info.st_mode):
            self.add_dir(self.place(real), stat.S_IMODE(info.st_mode))
        elif stat.S_ISREG(info.st_mode):
            self._put(self.place(real), Entry(
                "file", stat.S_IMODE(info.st_mode), source=real,
                mtime=int(info.st_mtime)))
        else:
            return None
        return real

    def add_tree(self, host_dir, skip=None):
        """A folder and everything in it. `skip(path, is_dir)` leaves out
        what it returns True for. Symlinks are kept as symlinks."""
        real = self.add_path(host_dir)
        if real is None:
            return
        for root, dirs, files in os.walk(real):
            dirs[:] = sorted(d for d in dirs if not (
                skip and skip(os.path.join(root, d), True)))
            for name in dirs:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    self.add_link(self.place(path), os.readlink(path))
                else:
                    self.add_dir(self.place(path))
            for name in sorted(files):
                path = os.path.join(root, name)
                if skip and skip(path, False):
                    continue
                if os.path.islink(path):
                    self.add_link(self.place(path), os.readlink(path))
                    continue
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    self._put(self.place(path), Entry(
                        "file", stat.S_IMODE(info.st_mode), source=path,
                        mtime=int(info.st_mtime)))

    def add_program(self, host_path, ldd=None):
        """A program or library and the libraries it needs, recursively.
        A script brings its interpreter."""
        real = self.add_path(host_path)
        if real is None or real in self._elf_done:
            return real
        self._elf_done.add(real)
        try:
            with open(real, "rb") as handle:
                head = handle.read(256)
        except OSError:
            return real
        if head.startswith(b"#!"):
            line = head[2:].split(b"\n")[0].decode(errors="replace").split()
            if line:
                interpreter = line[0]
                if os.path.basename(interpreter) == "env" and len(line) > 1:
                    interpreter = find_program(line[1]) or ""
                if interpreter:
                    self.add_program(interpreter, ldd)
            return real
        if head[:4] != b"\x7fELF":
            return real
        for library in (ldd or library_deps)(real):
            if library is None:
                continue
            self.add_program(library, ldd)
            for companion in DLOPENED.get(os.path.basename(library), ()):
                path = os.path.join(os.path.dirname(library), companion)
                if os.path.exists(path):
                    self.add_program(path, ldd)
        return real

    def add_all_programs(self, paths, ldd=None):
        """Libraries of several files at once: every .so a folder copy
        brought in, for example."""
        for path in paths:
            self.add_program(path, ldd)

    # ---- writing ------------------------------------------------------------

    def files(self):
        return sorted(self.entries.items())

    def size(self):
        total = 0
        for _path, entry in self.entries.items():
            if entry.kind == "file":
                try:
                    total += os.path.getsize(entry.source)
                except OSError:
                    pass
            elif entry.kind == "data":
                total += len(entry.data)
        return total


def resolve_chain(path):
    """([(link, its target as written)], real path) - every symlink met on
    the way from `path` to what it finally names, directories included."""
    links = []
    parts = [p for p in os.path.normpath(path).split("/") if p]
    current = "/"
    index = 0
    hops = 0
    while index < len(parts):
        candidate = os.path.join(current, parts[index])
        index += 1
        if os.path.islink(candidate):
            hops += 1
            if hops > 40:
                raise BuildError("Too many symlinks on the way to %s."
                                 % path)
            target = os.readlink(candidate)
            links.append((candidate, target))
            base = "/" if os.path.isabs(target) else current
            rest = os.path.normpath(os.path.join(base, target,
                                                 *parts[index:]))
            parts = [p for p in rest.split("/") if p]
            current, index = "/", 0
            continue
        current = candidate
    return links, current


def find_program(name, dirs=TOOL_DIRS):
    for folder in dirs:
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


_LDD_LINE = re.compile(r"^\s*(?:(\S+)\s+=>\s+)?(\S+)?\s*(?:\(0x[0-9a-f]+\))?$")


def library_deps(path):
    """The libraries a program loads, as the system's loader resolves them
    (ldd). A library that cannot be found stops the build: a program
    copied without it would not start on the stick."""
    try:
        done = subprocess.run(["ldd", path], capture_output=True, text=True,
                              timeout=60, env={"LC_ALL": "C",
                                               "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if not line or "not a dynamic executable" in line \
                or line.startswith("statically linked"):
            continue
        if "=> not found" in line:
            raise BuildError("%s needs %s, which is not installed."
                             % (path, line.split()[0]))
        parts = line.split()
        if "=>" in parts:
            target = parts[parts.index("=>") + 1]
            if target.startswith("/"):
                found.append(target)
        elif parts[0].startswith("/"):
            found.append(parts[0])
    return found


# ---- the archive format -------------------------------------------------------

def _cpio_header(ino, mode, size, name, mtime=0, nlink=1, rdev=(0, 0)):
    encoded = name.encode() + b"\0"
    fields = (ino, mode, 0, 0, nlink, mtime, size, 0, 0, rdev[0], rdev[1],
              len(encoded), 0)
    header = b"070701" + b"".join(b"%08X" % value for value in fields)
    header += encoded
    return header + b"\0" * (-len(header) % 4)


def write_cpio(image, out, progress=None):
    """The image as a "newc" cpio archive, the format the kernel unpacks at
    boot. Parents always come before what is in them (the paths are
    sorted), and every file is owned by root."""
    done = 0
    total = image.size() or 1
    ino = 1
    position = 0

    def put(data):
        nonlocal position
        out.write(data)
        position += len(data)

    for path, entry in image.files():
        name = path.lstrip("/")
        ino += 1
        if entry.kind == "dir":
            put(_cpio_header(ino, stat.S_IFDIR | entry.mode, 0, name,
                             entry.mtime, nlink=2))
        elif entry.kind == "link":
            target = entry.target.encode()
            put(_cpio_header(ino, stat.S_IFLNK | 0o777, len(target), name))
            put(target + b"\0" * (-len(target) % 4))
        elif entry.kind == "node":
            put(_cpio_header(ino, entry.mode, 0, name, rdev=entry.rdev))
        else:
            if entry.kind == "data":
                chunks = [entry.data]
                size = len(entry.data)
            else:
                try:
                    size = os.path.getsize(entry.source)
                except OSError as exc:
                    raise BuildError("%s could not be read: %s"
                                     % (entry.source, exc.strerror))
                chunks = None
            put(_cpio_header(ino, stat.S_IFREG | entry.mode, size, name,
                             entry.mtime))
            written = 0
            if chunks is not None:
                for chunk in chunks:
                    put(chunk)
                    written += len(chunk)
            else:
                try:
                    with open(entry.source, "rb") as handle:
                        while True:
                            chunk = handle.read(4 * MIB)
                            if not chunk:
                                break
                            put(chunk)
                            written += len(chunk)
                            if progress:
                                progress(done + written, total)
                except OSError as exc:
                    raise BuildError("%s could not be read: %s"
                                     % (entry.source, exc.strerror))
            if written != size:
                raise BuildError("%s changed while it was being copied."
                                 % entry.source)
            put(b"\0" * (-size % 4))
            done += size
            if progress:
                progress(done, total)
    put(_cpio_header(0, 0, 0, "TRAILER!!!", nlink=1))
    put(b"\0" * (-position % 512))


def compressor():
    """(name, compressor object) - zstd where this Python has it (3.14 on),
    else xz with the CRC32 check the kernel's xz reader insists on. The
    kernel unpacks either."""
    try:
        from compression import zstd
        options = {zstd.CompressionParameter.compression_level: 12}
        try:
            options[zstd.CompressionParameter.nb_workers] = max(
                1, min(8, os.cpu_count() or 1))
        except Exception:
            pass
        return "zstd", zstd.ZstdCompressor(options=options)
    except ImportError:
        return "xz", lzma.LZMACompressor(
            format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32,
            filters=[{"id": lzma.FILTER_LZMA2, "preset": 6}])


class _Compressed:
    """A file object that compresses what is written to it, and hashes the
    result as it goes."""

    def __init__(self, path):
        self.kind, self.engine = compressor()
        self.handle = open(path, "wb")
        self.digest = hashlib.sha256()
        self.size = 0

    def _out(self, data):
        if data:
            self.handle.write(data)
            self.digest.update(data)
            self.size += len(data)

    def write(self, data):
        self._out(self.engine.compress(data))

    def close(self):
        self._out(self.engine.flush())
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


# ---- what this computer has ----------------------------------------------------

@dataclass
class Host:
    """What the build takes from the running system. Every path is a
    parameter so the tests can point it at a fixture tree."""
    kernel_release: str = field(default_factory=lambda: os.uname().release)
    modules_root: str = "/lib/modules"
    boot_dir: str = "/boot"
    python: str = sys.executable
    stdlib: str = field(default_factory=lambda: os.path.dirname(os.__file__))
    package: str = field(default_factory=lambda: os.path.dirname(
        os.path.abspath(__file__)))
    #: The AppImage's own folder when running from one, else "".
    appdir: str = field(default_factory=lambda: os.environ.get("APPDIR", "")
                        if os.environ.get("APPIMAGE") else "")
    tool_dirs: tuple = TOOL_DIRS
    udev_dirs: tuple = ("/usr/lib/udev", "/lib/udev")
    udevd: tuple = ("/usr/lib/systemd/systemd-udevd",
                    "/lib/systemd/systemd-udevd", "/usr/bin/udevadm",
                    "/sbin/udevd", "/usr/sbin/udevd")
    icon_roots: tuple = ("/usr/share/icons",)
    #: Where KDE keeps the cursor theme's name.
    cursor_config: str = field(default_factory=lambda: os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "kcminputrc"))
    xkb: str = "/usr/share/X11/xkb"
    #: libinput's descriptions of particular devices (touchpads above all).
    libinput: str = "/usr/share/libinput"
    locales: tuple = ("/usr/lib/locale/C.utf8", "/usr/lib/locale/C.UTF-8")
    terminfo: tuple = ("/usr/share/terminfo/l/linux",
                       "/lib/terminfo/l/linux", "/etc/terminfo/l/linux")

    def kernel(self):
        """The running kernel's image; None when it cannot be read."""
        for path in (os.path.join(self.modules_root, self.kernel_release,
                                  "vmlinuz"),
                     os.path.join(self.boot_dir,
                                  "vmlinuz-" + self.kernel_release)):
            if os.access(path, os.R_OK):
                return path
        return None

    def modules(self):
        return os.path.join(self.modules_root, self.kernel_release)


# ---- settings carried over ------------------------------------------------------

def keyboard_layout(run=subprocess.run, paths=("/etc/vconsole.conf",
                                               "/etc/default/keyboard",
                                               "/etc/X11/xorg.conf.d/"
                                               "00-keyboard.conf")):
    """(layout, variant) of this computer's keyboard, as far as it says;
    ("us", "") when it does not."""
    try:
        done = run(["localectl", "status"], capture_output=True, text=True,
                   timeout=10)
        layout = re.search(r"X11 Layout:\s*(\S+)", done.stdout or "")
        variant = re.search(r"X11 Variant:\s*(\S+)", done.stdout or "")
        if layout:
            return (layout.group(1).split(",")[0],
                    variant.group(1).split(",")[0] if variant else "")
    except (OSError, subprocess.SubprocessError):
        pass
    for path in paths:
        try:
            with open(path) as handle:
                text = handle.read()
        except OSError:
            continue
        layout = re.search(r'XKBLAYOUT="?([\w-]+)', text) or re.search(
            r'"XkbLayout"\s+"([\w-]+)', text)
        variant = re.search(r'XKBVARIANT="?([\w-]*)', text) or re.search(
            r'"XkbVariant"\s+"([\w-]*)', text)
        if layout:
            return layout.group(1), variant.group(1) if variant else ""
        keymap = re.search(r'KEYMAP="?([a-z]{2})', text)
        if keymap:
            return keymap.group(1), ""
    return "us", ""


def font_files(run=subprocess.run):
    """(family, files, monospace family): the desktop's sans-serif and
    monospace families and their files, so the stick's text has the same
    fonts and the same widths as here. Both families are named outright on
    the stick: left to choose, fontconfig there picks nothing for a generic
    name, and Qt draws boxes."""
    found = []
    families = []
    for pattern in ("sans-serif", "monospace"):
        try:
            family = run(["fc-match", "-f", "%{family[0]}", pattern],
                         capture_output=True, text=True,
                         timeout=20).stdout.strip()
            listed = run(["fc-list", ":family=%s" % family, "file"],
                         capture_output=True, text=True,
                         timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        families.append(family)
        for line in listed.splitlines():
            path = line.split(":")[0].strip()
            if path and path not in found and os.path.isfile(path):
                found.append(path)
    return (families[0] if families else ""), found, \
        (families[1] if len(families) > 1 else "")


FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
%s  <cachedir>/run/fontconfig</cachedir>
%s</fontconfig>
"""
FONT_ALIAS = """  <alias><family>%s</family><prefer><family>%s</family></prefer>
  </alias>
"""


def cursor_theme(config):
    """The cursor theme KDE's settings name, or ""."""
    try:
        with open(config, encoding="utf-8", errors="replace") as handle:
            section = ""
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    section = line
                elif section == "[Mouse]" and line.startswith("cursorTheme="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def cursor_file(roots, config):
    """The desktop's arrow pointer: an Xcursor file, or None."""
    themes = [cursor_theme(config)] + list(CURSOR_THEMES)
    for theme in themes:
        if not theme or "/" in theme:
            continue
        for root in roots:
            for name in CURSOR_NAMES:
                path = os.path.join(root, theme, "cursors", name)
                try:
                    with open(path, "rb") as handle:
                        if handle.read(4) == b"Xcur":
                            return os.path.realpath(path)
                except OSError:
                    continue
    return None


def icon_names(package):
    """Every string in the app's code that could be an icon name. Which of
    them are icons is told by the theme having a file of that name."""
    names = set()
    for root, _dirs, files in os.walk(package):
        for name in files:
            if not name.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            names.update(re.findall(r'"([a-z][a-z0-9-]{2,60})"', text))
    return names


def icon_files(names, roots=("/usr/share/icons",), themes=ICON_THEMES):
    """[(theme folder, file)] for the named icons, every size, plus each
    theme's index - a few hundred kilobytes instead of a whole theme."""
    found = []
    for root in roots:
        for theme in themes:
            folder = os.path.join(root, theme)
            if not os.path.isdir(folder):
                continue
            index = os.path.join(folder, "index.theme")
            if os.path.isfile(index):
                found.append(index)
            for base, _dirs, files in os.walk(folder):
                for name in files:
                    stem, _dot, kind = name.rpartition(".")
                    if kind in ("svg", "png") and stem in names:
                        found.append(os.path.join(base, name))
    return found


# ---- the build ------------------------------------------------------------------

@dataclass
class Settings:
    """What the stick's system starts with."""
    keyboard: str = "us"
    variant: str = ""
    dark: bool = False
    palette: dict = field(default_factory=dict)
    font: str = ""
    font_size: float = 10.0


@dataclass
class Result:
    folder: str
    kernel: str
    initrd: str
    manifest: str
    size: int                     # bytes the two take on the stick
    unpacked: int                 # bytes of memory the system takes
    compression: str
    missing: list


def _init_script(python, package_parent):
    """/init: the first program the kernel runs. A stub that hands over to
    rescueinit, which is part of the app's own package."""
    return ("#!%s -I\n"
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from ksyssupervisor import rescueinit\n"
            "rescueinit.main()\n" % (python, package_parent))


def build(folder, host=None, settings=None, progress=None,
          ldd=None, fonts=None, cancelled=lambda: False):
    """Put the stick's system together in `folder`: the kernel, the
    compressed archive and a manifest of both. `progress(text, done,
    total)` is told how far it has got."""
    host = host or Host()
    settings = settings or Settings()
    say = progress or (lambda text, done=0, total=0: None)

    kernel = host.kernel()
    if kernel is None:
        raise BuildError(
            "The running kernel (%s) could not be read from %s or %s."
            % (host.kernel_release, host.modules(), host.boot_dir))
    modules = host.modules()
    if not os.path.isdir(modules):
        raise BuildError("The running kernel's modules are not in %s. Was "
                         "the kernel updated without restarting since?"
                         % modules)

    # The app goes to one fixed place whatever way it was installed: a
    # checkout, ~/.local/share, /opt, or inside the AppImage. A venv inside
    # its folder moves with it. Listed first: the first move that fits wins.
    app_root = os.path.dirname(host.package)
    moves = [(app_root, APP_DIR)]
    if host.appdir:
        moves.append((host.appdir, APPDIR_TARGET))
    image = Image(moves)
    for path in ("/dev", "/proc", "/sys", "/run", "/tmp", "/root", "/mnt",
                 "/var/lib/ksyssupervisor", "/var/tmp"):
        image.add_dir(path, 0o1777 if path in ("/tmp", "/var/tmp") else
                      0o700 if path == "/root" else 0o755)
    image.add_node("/dev/console", 5, 1)
    image.add_node("/dev/null", 1, 3, 0o666)

    # ---- Python, the app, Qt ----------------------------------------------
    say("Collecting Python and the app")
    image.add_program(host.python, ldd)
    # A venv is told apart by the file beside its bin folder, which Python
    # looks for next to the path it was started by.
    venv = os.path.join(os.path.dirname(os.path.dirname(host.python)),
                        "pyvenv.cfg")
    if os.path.isfile(venv):
        image.add_path(venv)

    def stdlib_skip(path, is_dir):
        name = os.path.basename(path)
        if is_dir:
            return name in STDLIB_SKIP or name.startswith("config-")
        if name.endswith(".pyc") and (".opt-1." in name or
                                      ".opt-2." in name):
            return True
        return False

    image.add_tree(host.stdlib, stdlib_skip)
    dynload = os.path.join(host.stdlib, "lib-dynload")
    extensions = []
    if os.path.isdir(dynload):
        for name in sorted(os.listdir(dynload)):
            if name.endswith(".so") and not DYNLOAD_SKIP.match(name):
                extensions.append(os.path.join(dynload, name))
            elif name.endswith(".so"):
                image.entries.pop(image.place(os.path.join(dynload, name)),
                                  None)
    image.add_all_programs(extensions, ldd)

    import importlib.util
    for package in ("PyQt6",) + PACKAGES:
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            raise BuildError("The Python package '%s' is not installed for "
                             "this Python (%s)." % (package, host.python))
        where = list(spec.submodule_search_locations)[0]
        if package == "PyQt6":
            image.add_tree(where, lambda path, is_dir, where=where:
                           pyqt_skip(os.path.relpath(path, where), is_dir))
        else:
            image.add_tree(where)
        image.add_all_programs(
            [image.entries[p].source for p in list(image.entries)
             if image.entries[p].kind == "file"
             and image.entries[p].source.startswith(where + "/")
             and image.entries[p].source.endswith(".so")], ldd)

    plugins = qt_plugin_dir()
    for kind, names in QT_PLUGINS.items():
        for name in names:
            path = os.path.join(plugins, kind, name)
            if os.path.isfile(path):
                image.add_program(path, ldd)
            elif kind == "platforms":
                raise BuildError(
                    "Qt's '%s' screen plugin is not installed (looked in "
                    "%s). Fedora: sudo dnf install qt6-qtbase-gui"
                    % (name, os.path.join(plugins, kind)))

    image.add_tree(host.package)
    for extra in ("KSysSupervisor.py", "helpers", "Icons", "packaging"):
        path = os.path.join(app_root, extra)
        if os.path.isdir(path):
            image.add_tree(path)
        elif os.path.isfile(path):
            image.add_path(path)

    # ---- tools ----------------------------------------------------------------
    say("Collecting the disk tools")
    missing = []
    for name in RESCUE_TOOLS:
        path = find_program(name, host.tool_dirs)
        if path is None:
            missing.append(name)
            continue
        image.add_program(path, ldd)
    udevd = next((p for p in host.udevd if os.path.isfile(p)), None)
    if udevd is None:
        raise BuildError("udev (systemd-udevd) was not found. The stick's "
                         "system needs it to find the drives.")
    image.add_program(udevd, ldd)
    for udev in host.udev_dirs:
        if os.path.isdir(udev):
            image.add_tree(udev)
            image.add_all_programs(
                [os.path.join(udev, n) for n in os.listdir(udev)
                 if os.path.isfile(os.path.join(udev, n))
                 and os.access(os.path.join(udev, n), os.X_OK)], ldd)
            break
    for path in ETC_FILES:
        if os.path.isfile(path):
            image.add_path(path)
    for path in ("/etc/udev/hwdb.bin",):
        if os.path.isfile(path):
            image.add_path(path)
    for path in host.terminfo:
        if os.path.isfile(path):
            image.add_path(path)
            break
    for data in (host.xkb, host.libinput):
        if os.path.exists(data):
            image.add_tree(data)
    # The one locale the stick's system uses: Qt wants UTF-8.
    for path in host.locales:
        if os.path.isdir(path):
            image.add_tree(path)
            break

    # ---- the kernel's modules -----------------------------------------------
    say("Collecting the kernel's drivers")
    add_modules(image, modules)
    if cancelled():
        raise BuildError("Stopped.")

    # ---- fonts, icons ----------------------------------------------------------
    family, files, *rest = font_files() if fonts is None else fonts
    mono = rest[0] if rest else ""
    if not files:
        raise BuildError("No font was found (fc-match and fc-list from "
                         "fontconfig give none).")
    family = settings.font or family
    for path in files:
        image.add_path(path)
    folders = sorted({os.path.dirname(p) for p in files})
    image.add_data("/etc/fonts/fonts.conf", FONTS_CONF % (
        "".join("  <dir>%s</dir>\n" % image.place(d) for d in folders),
        "".join(FONT_ALIAS % pair for pair in (("sans-serif", family),
                                                ("monospace", mono))
                if pair[1])))
    for path in icon_files(icon_names(host.package), host.icon_roots):
        image.add_path(path)
    cursor = cursor_file(host.icon_roots, host.cursor_config)
    if cursor:
        image.add_path(cursor)
    pointer = os.path.join(plugins, *POINTER_PLUGIN)
    pointer = "evdev" if os.path.isfile(pointer) else "libinput"
    if pointer == "evdev":
        image.add_data(POINTER_RULES_FILE, POINTER_RULES)

    # ---- the stick's own files ------------------------------------------------
    # Started by the name it was started by here: a venv's python is a
    # symlink, and only that name finds the venv's packages.
    python_in_image = image.place(host.python)
    marker = {
        "app": APP_NAME, "version": APP_VERSION,
        "kernel": host.kernel_release,
        "built": time.strftime("%Y-%m-%d %H:%M"),
        "python": python_in_image,
        "app_dir": APP_DIR,
        "keyboard": settings.keyboard, "variant": settings.variant,
        "dark": settings.dark, "palette": settings.palette,
        "font": family, "font_size": settings.font_size, "mono": mono,
        "cursor": image.place(cursor) if cursor else "",
        "pointer": pointer,
        "udevd": image.place(udevd),
        "missing": missing,
    }
    image.add_data(MARKER, json.dumps(marker, indent=1, sort_keys=True))
    image.add_data("/etc/hostname", "kss-recovery\n")
    image.add_data("/etc/passwd", "root:x:0:0:root:/root:/bin/bash\n")
    image.add_data("/etc/group", "root:x:0:\ndisk:x:6:\n")
    image.add_data("/etc/fstab", "")
    image.add_link("/etc/mtab", "/proc/self/mounts")
    image.add_data("/init", _init_script(python_in_image, APP_DIR), 0o755)

    # ---- packing ------------------------------------------------------------
    os.makedirs(folder, exist_ok=True)
    initrd = os.path.join(folder, INITRD_NAME)
    partial = initrd + ".partial"
    unpacked = image.size()
    say("Packing %s" % format_size(unpacked), 0, unpacked)
    out = _Compressed(partial)
    try:
        def packed(done, total):
            if cancelled():
                raise BuildError("Stopped.")
            say("Packing", done, total)
        write_cpio(image, out, packed)
        out.close()
    except BaseException:
        out.handle.close()
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise
    os.replace(partial, initrd)

    kernel_copy = os.path.join(folder, KERNEL_NAME)
    shutil.copyfile(kernel, kernel_copy)
    manifest = {
        "app": APP_NAME, "version": APP_VERSION,
        "kernel_release": host.kernel_release,
        "compression": out.kind,
        "unpacked": unpacked,
        "files": {
            KERNEL_NAME: {"size": os.path.getsize(kernel_copy),
                          "sha256": sha256_of(kernel_copy)},
            INITRD_NAME: {"size": out.size,
                          "sha256": out.digest.hexdigest()},
        },
        "keyboard": settings.keyboard,
        "missing": missing,
    }
    manifest_path = os.path.join(folder, MANIFEST_NAME)
    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    return Result(folder, kernel_copy, initrd, manifest_path,
                  out.size + os.path.getsize(kernel_copy), unpacked,
                  out.kind, missing)


def pyqt_skip(relative, is_dir):
    """What of the PyQt6 package stays behind: the modules of Qt the Disk
    Utility does not use, the code generators, and - in a pip wheel - Qt's
    own folder, whose libraries and plugins are taken one by one as the
    modules need them."""
    name = os.path.basename(relative)
    if is_dir:
        return relative in ("uic", "lupdate", "bindings", "Qt6", "Qt")
    if name.endswith(".pyi"):
        return True
    if name.endswith(".so"):
        return name.split(".")[0] not in QT_MODULES + ("sip",)
    return False


def add_modules(image, modules):
    """The kernel modules the stick needs, with everything they depend on,
    and the kernel's indexes of them."""
    wanted = set()
    for folder in MODULE_FOLDERS:
        base = os.path.join(modules, "kernel", folder)
        for root, _dirs, files in os.walk(base):
            for name in files:
                if ".ko" in name:
                    wanted.add(os.path.relpath(os.path.join(root, name),
                                               modules))
    depends = {}
    try:
        with open(os.path.join(modules, "modules.dep")) as handle:
            for line in handle:
                head, _colon, tail = line.partition(":")
                depends[head.strip()] = tail.split()
    except OSError:
        raise BuildError("The kernel's list of modules (%s) could not be "
                         "read." % os.path.join(modules, "modules.dep"))
    for name in depends:
        if MODULE_EXTRA.search("/" + name):
            wanted.add(name)
    queue = list(wanted)
    while queue:
        name = queue.pop()
        for dependency in depends.get(name, ()):
            if dependency not in wanted:
                wanted.add(dependency)
                queue.append(dependency)
    for name in sorted(wanted):
        path = os.path.join(modules, name)
        if os.path.isfile(path):
            image.add_path(path)
    for name in os.listdir(modules):
        path = os.path.join(modules, name)
        if name.startswith("modules.") and os.path.isfile(path):
            image.add_path(path)


def qt_plugin_dir():
    from PyQt6.QtCore import QLibraryInfo
    return QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)


def sha256_of(path):
    found = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(4 * MIB)
            if not chunk:
                break
            found.update(chunk)
    return found.hexdigest()


def format_size(size):
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return ("%d %s" % (size, unit) if unit == "bytes"
                    else "%.0f %s" % (size, unit))
        size /= 1024.0
    return "%d" % size


def cache_folder():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP_NAME, "rescue")
