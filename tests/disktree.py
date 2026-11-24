"""A fake block-device world for the disk utility's tests.

Builds, under a temporary directory, the parts of /sys, /dev and /proc that
the disk helper and the inventory read: /sys/class/block with real nested
device directories behind its symlinks (a partition's parent is found by
resolving them), /dev nodes as plain files, a mountinfo and a swaps file.
Nothing here needs root or real hardware.
"""

import importlib.machinery
import importlib.util
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(REPO, "helpers", "ksyssupervisor-diskhelper")

MIB = 1024 * 1024
GIB = 1024 * MIB


def load_helper(name="diskhelper"):
    """The helper script as a module; it has no .py extension to import by."""
    loader = importlib.machinery.SourceFileLoader(name, HELPER)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeBlockTree:
    def __init__(self, root):
        self.root = root
        self.block = os.path.join(root, "sys", "class", "block")
        self.devices = os.path.join(root, "sys", "devices")
        self.btrfs = os.path.join(root, "sys", "fs", "btrfs")
        self.dev = os.path.join(root, "dev")
        self.mountinfo = os.path.join(root, "proc", "mountinfo")
        self.swaps = os.path.join(root, "proc", "swaps")
        self.run = os.path.join(root, "run")
        for path in (self.block, self.devices, self.btrfs, self.dev,
                     os.path.dirname(self.mountinfo), self.run):
            os.makedirs(path)
        self._mounts = []
        self._swaps = []
        self._write_tables()

    # ---- building ---------------------------------------------------------

    def _put(self, directory, name, value):
        path = os.path.join(directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("%s\n" % value)

    def _node(self, name, size):
        path = os.path.join(self.dev, name)
        with open(path, "wb") as handle:
            handle.truncate(min(size, 64 * MIB))
        return path

    def add_disk(self, name, size, diskseq=1, read_only=0, logical=512,
                 major=8, minor=0):
        directory = os.path.join(self.devices, name)
        os.makedirs(os.path.join(directory, "holders"))
        self._put(directory, "size", size // 512)
        self._put(directory, "ro", read_only)
        self._put(directory, "dev", "%d:%d" % (major, minor))
        if diskseq is not None:
            self._put(directory, "diskseq", diskseq)
        self._put(directory, "queue/logical_block_size", logical)
        os.symlink(directory, os.path.join(self.block, name))
        self._node(name, size)
        return directory

    def add_partition(self, disk, name, number, start, size, read_only=0):
        """`start` in 512-byte sectors, as sysfs counts it."""
        directory = os.path.join(self.devices, disk, name)
        os.makedirs(os.path.join(directory, "holders"))
        self._put(directory, "partition", number)
        self._put(directory, "start", start)
        self._put(directory, "size", size // 512)
        self._put(directory, "ro", read_only)
        self._put(directory, "dev", "8:%d" % number)
        os.symlink(directory, os.path.join(self.block, name))
        self._node(name, size)
        return directory

    def remove(self, name):
        target = os.path.realpath(os.path.join(self.block, name))
        os.remove(os.path.join(self.block, name))
        for base, dirs, files in os.walk(target, topdown=False):
            for each in files:
                os.remove(os.path.join(base, each))
            for each in dirs:
                os.rmdir(os.path.join(base, each))
        os.rmdir(target)
        try:
            os.remove(os.path.join(self.dev, name))
        except OSError:
            pass

    def add_holder(self, name, holder, size=GIB):
        """Stack `holder` (a dm device, say) on top of `name`."""
        if not os.path.exists(os.path.join(self.block, holder)):
            self.add_disk(holder, size, diskseq=None, major=253)
        link = os.path.join(os.path.realpath(os.path.join(self.block, name)),
                            "holders", holder)
        os.symlink(os.path.realpath(os.path.join(self.block, holder)), link)

    def add_btrfs(self, uuid, *names):
        directory = os.path.join(self.btrfs, uuid, "devices")
        os.makedirs(directory)
        for name in names:
            os.symlink(os.path.join(self.block, name),
                       os.path.join(directory, name))

    def mount(self, name, mountpoint, fstype="ext4", options="rw,relatime"):
        self._mounts.append((name, mountpoint, fstype, options))
        self._write_tables()

    def unmount(self, mountpoint):
        self._mounts = [m for m in self._mounts if m[1] != mountpoint]
        self._write_tables()

    def swap(self, name):
        self._swaps.append(name)
        self._write_tables()

    def mounted(self):
        return [m[1] for m in self._mounts]

    def _write_tables(self):
        with open(self.mountinfo, "w") as handle:
            handle.write("21 1 0:20 / /sys rw - sysfs sysfs rw\n")
            for index, (name, mountpoint, fstype, options) in enumerate(
                    self._mounts):
                handle.write(
                    "%d 1 8:%d / %s %s shared:1 - %s %s %s\n"
                    % (30 + index, index, mountpoint.replace(" ", "\\040"),
                       options, fstype, os.path.join(self.dev, name),
                       options))
        with open(self.swaps, "w") as handle:
            handle.write("Filename\tType\tSize\tUsed\tPriority\n")
            for name in self._swaps:
                handle.write("%s partition 1024 0 -2\n"
                             % os.path.join(self.dev, name))

    # ---- pointing code at it ------------------------------------------------

    def install_helper(self, module):
        """Point a loaded helper module at this tree."""
        module.SYS_CLASS_BLOCK = self.block
        module.SYS_BTRFS = self.btrfs
        module.DEV_ROOT = self.dev
        module.MOUNTINFO = self.mountinfo
        module.SWAPS = self.swaps
        module.LOCK_PATH = os.path.join(self.run, "disk.lock")
        module.RUN_DIR = self.run
        module.STATE_DIR = os.path.join(self.root, "state")
        module.SYS_ROOT = os.path.join(self.root, "sys")
        # /dev here holds plain files, so "is it the block device the kernel
        # lists" becomes "is there a node of that name".
        module.Helper.is_block_device = staticmethod(
            lambda device: os.path.exists(device.path))

    def identity(self, name):
        """What disks.identity() would say of a device in this tree."""
        entry = os.path.join(self.block, name)

        def number(base, attribute):
            try:
                with open(os.path.join(base, attribute)) as handle:
                    return int(handle.read().strip())
            except (OSError, ValueError):
                return None

        is_partition = os.path.exists(os.path.join(entry, "partition"))
        parent = (os.path.dirname(os.path.realpath(entry)) if is_partition
                  else entry)
        return {
            "name": name,
            "disk": os.path.basename(parent) if is_partition else name,
            "size": number(entry, "size") * 512,
            "diskseq": number(parent, "diskseq"),
            "start": number(entry, "start") if is_partition else None,
        }
