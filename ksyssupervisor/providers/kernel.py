"""What the running kernel can actually load.

Every hardware setup tip that says "sudo modprobe X" is only true if the
running kernel has a module tree to load X from. After a kernel package
upgrade it usually does not: the new modules are installed under the new
version and the running kernel's directory is removed, so every modprobe fails
with "module not found in directory /lib/modules/<running>" until the machine
reboots. That looks identical to a missing driver, so the app has to tell the
two apart before it advises anyone to run anything.
"""

import os
import glob

from .base import Advice, Step

# Module level so tests can point these at a fixture tree.
MODULES_ROOT = "/lib/modules"
SYS_MODULE_ROOT = "/sys/module"

_INDEX_CACHE = {}


def release():
    """The kernel release currently running, e.g. '7.2.0-1-cachyos'."""
    return os.uname().release


def modules_dir():
    return os.path.join(MODULES_ROOT, release())


def modules_ready():
    """True if the running kernel's module tree is present.

    When this is False nothing can be modprobe'd at all, whatever the module.
    """
    return os.path.isdir(modules_dir())


def installed_releases():
    """Kernel releases that do have a module tree installed."""
    return sorted(os.path.basename(p) for p in glob.glob(
        os.path.join(MODULES_ROOT, "*")) if os.path.isdir(p))


def _names_from(path, cutoff):
    """Module base names listed in a modules.dep / modules.builtin file."""
    names = set()
    try:
        with open(path, 'r') as f:
            for line in f:
                entry = line.split(cutoff, 1)[0].strip()
                if entry.endswith('/') or not entry:
                    continue
                names.add(os.path.basename(entry).split('.', 1)[0])
    except OSError:
        pass
    return names


def _index():
    """Every module name this kernel ships, loadable or built in."""
    directory = modules_dir()
    try:
        stamp = os.path.getmtime(directory)
    except OSError:
        return frozenset()

    cached = _INDEX_CACHE.get(directory)
    if cached and cached[0] == stamp:
        return cached[1]

    names = _names_from(os.path.join(directory, "modules.dep"), ':')
    names |= _names_from(os.path.join(directory, "modules.builtin"), '\n')
    names = frozenset(names)
    _INDEX_CACHE[directory] = (stamp, names)
    return names


def module_status(name):
    """One of 'loaded', 'available', 'absent' or 'unknown'.

    'unknown' means the running kernel has no module tree to look in, which is
    a different problem from the module not existing.
    """
    if os.path.isdir(os.path.join(SYS_MODULE_ROOT, name)):
        return "loaded"
    if not modules_ready():
        return "unknown"
    return "available" if name in _index() else "absent"


def modprobe_step(module, note, args=""):
    """A 'sudo modprobe' Step whose note reflects whether it can succeed.

    Providers describe what the module gives the user; this fills in what the
    running kernel has to say about it, so nobody is told to run a command
    that cannot work on this boot.
    """
    status = module_status(module)
    if status == "absent":
        note = ("Your running kernel (%s) does not ship this module, so this "
                "will fail. A distribution kernel normally has it; a custom "
                "or minimal one may need rebuilding with it enabled." % release())
    elif status == "unknown":
        note = ("Blocked until you reboot - see the kernel update tip above. "
                "%s" % note)
    command = "sudo modprobe %s" % module
    if args:
        command = "%s %s" % (command, args)
    return Step(command, note)


def advice():
    """The pending-reboot blocker, when the module tree is missing."""
    if modules_ready():
        return []

    others = [r for r in installed_releases() if r != release()]
    if others:
        problem = (
            "You are running kernel %s, but its modules are gone from %s - "
            "only %s %s installed. A kernel update replaced them and the new "
            "one takes effect at the next boot. Until then 'sudo modprobe' "
            "fails with \"module not found\" for every driver, which reads "
            "like a missing driver but is not."
            % (release(), MODULES_ROOT, " and ".join(others),
               "are" if len(others) > 1 else "is"))
    else:
        problem = (
            "You are running kernel %s, but there is no module directory for "
            "it under %s, so no kernel module can be loaded on this boot. "
            "'sudo modprobe' fails with \"module not found\" for every driver."
            % (release(), MODULES_ROOT))

    return [Advice(
        key="kernel-modules-missing",
        title="Reboot to finish a kernel update",
        problem=problem,
        effect="Restores the ability to load any sensor driver at all. Do this "
               "first: the other setup tips below cannot work until you have.",
        steps=(
            Step("uname -r", "The kernel you are running right now."),
            Step("ls %s" % MODULES_ROOT,
                 "The kernels that have modules installed. Reboot when the "
                 "first is not in the second."),
        ),
        persist_note="Nothing to make permanent here - the running kernel "
                     "matches its modules again from the next boot onward.",
        result="After rebooting, reopen this window. This tip disappears and "
               "any remaining ones become runnable.")]
