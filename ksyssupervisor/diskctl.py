"""The disk utility's link to its privileged helper.

Everything that changes a disk goes through helpers/ksyssupervisor-diskhelper,
which this module launches once under pkexec and then talks to over a pipe -
one password for a session, however many operations follow. Reading needs no
helper at all; that is disks.py, udisks.py and diskhealth.py.

The helper answers each operation with a stream of events (which step, how
far along) and one final reply. `DiskHelperClient.run()` blocks until that
reply, calling back for every event, so the window runs it on a thread.

Qt-free.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading

from . import helperbase, installation, log

# Has to equal the helper's own. Raised on both sides whenever a request's
# meaning or a reply's contents change, so that an older installed helper is
# refused at the handshake rather than half-understood.
PROTOCOL_VERSION = 15

HELPER_PATHS = helperbase.DISK.paths
HELPER_INSTALL_PATH = helperbase.DISK.install_path

#: How long the handshake may take: it waits on the password prompt.
FIRST_REPLY_TIMEOUT = 180.0
REPLY_TIMEOUT = 10.0


def bundled_helper():
    """(helper, policy) shipped with this copy, or (None, None)."""
    return helperbase.DISK.bundled()


def helper_path():
    """The installed helper, or None."""
    return helperbase.DISK.installed_path()


def direct():
    """Whether the app runs as root already - on the recovery stick, where
    there is no one else to be - and so starts its own bundled helper
    itself: nothing to install, no pkexec, no password."""
    return os.geteuid() == 0 and bundled_helper()[0] is not None


def installed_helper_version(path=None):
    return helperbase.protocol_version(path or helper_path())


def install_bundled_helper(runner=None):
    """Install the bundled helpers system-wide. Returns (ok, message)."""
    helper, _policy = bundled_helper()
    if helper is None:
        return False, ("This copy of KSysSupervisor does not ship the disk "
                       "helper, so there is nothing to install from here.")
    if runner is None and not shutil.which("pkexec"):
        return False, _no_pkexec_reason()
    code, output = helperbase.install(runner=runner)
    if code == 0:
        return True, "The disk helper is installed at %s." % HELPER_INSTALL_PATH
    if code == 126:
        return False, "Cancelled: the request was not authorised."
    if code == 127:
        return False, "Cancelled: no password was given."
    return False, ("The helper could not be installed (exit %s).\n\n%s"
                   % (code, output.strip() or "No output."))


#: availability() outcomes. A missing or outdated helper is what the app can
#: put right by itself, with one password prompt.
READY = "ready"
NO_HELPER = "no-helper"
OUTDATED_HELPER = "outdated-helper"
NO_PKEXEC = "no-pkexec"


def availability():
    """(state, reason): whether changes to disks can be made, and why not.

    Checked when an operation is about to start rather than once at startup,
    so installing the helper from the app takes effect straight away.
    """
    if direct():
        return READY, None
    if helper_path() is None:
        return NO_HELPER, (
            "Changing a disk needs a small helper that runs as root, and it "
            "is not installed yet.")
    if not shutil.which("pkexec"):
        return NO_PKEXEC, _no_pkexec_reason()
    version = installed_helper_version()
    if version is not None and version != PROTOCOL_VERSION:
        return OUTDATED_HELPER, (
            "The installed disk helper is from a different version of "
            "KSysSupervisor and has to be updated before it is used.")
    return READY, None


def can_install_helper(state):
    """Whether the app can fix `state` itself by installing its own helper."""
    return (state in (NO_HELPER, OUTDATED_HELPER)
            and bundled_helper()[0] is not None
            and shutil.which("pkexec") is not None)


def manual_install_hint():
    """The one command that installs the helpers from this copy."""
    return "%s --install-helpers" % installation.self_command()


def _no_pkexec_reason():
    return ("Changing a disk needs 'pkexec' to ask for authorisation. "
            "Install polkit and make sure your desktop runs a polkit "
            "authentication agent.")


class HelperError(Exception):
    """The helper refused, failed, died, or never started. The text is what
    the user is shown."""


class Cancelled(HelperError):
    """The user stopped the operation and the helper stopped it safely."""


class DiskHelperClient:
    """Owns the privileged helper process and speaks its line protocol.

    One instance per window. start() raises the polkit prompt; every
    operation after that goes down the same pipe.
    """

    def __init__(self, path=None, launcher=None):
        # Already root: the bundled helper, run by this same Python.
        if path is None and launcher is None and direct():
            path = bundled_helper()[0]
            launcher = lambda helper: [sys.executable, helper]  # noqa: E731
        self._path = path
        # How the helper is started; replaced by the tests and by the
        # hardware check script, which is already root.
        self._launcher = launcher or (lambda path: ["pkexec", path])
        self._proc = None
        self._readers = []
        self._replies = queue.Queue()
        self._write_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._next_id = 0
        self._busy = False

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    @property
    def busy(self):
        """Whether an operation is in progress right now."""
        return self._busy

    # ---- lifecycle ------------------------------------------------------

    def start(self):
        """Launch the helper and complete the handshake.

        Blocks until the user answers the authentication prompt, so callers
        must not run this on the thread that paints the window.
        """
        if self.running:
            return
        path = self._path or helper_path()
        if path is None:
            raise HelperError("The disk helper is not installed.")
        try:
            self._proc = subprocess.Popen(
                self._launcher(path), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                bufsize=1)
        except OSError as exc:
            self._proc = None
            raise HelperError("Could not start the disk helper: %s" % exc)

        self._replies = queue.Queue()
        proc = self._proc
        self._readers = [
            threading.Thread(target=self._read_replies,
                             args=(proc, self._replies),
                             name="disk-helper-out", daemon=True),
            threading.Thread(target=self._read_log, args=(proc,),
                             name="disk-helper-err", daemon=True)]
        for reader in self._readers:
            reader.start()

        try:
            reply = self._request({"cmd": "hello",
                                   "version": PROTOCOL_VERSION},
                                  timeout=FIRST_REPLY_TIMEOUT)
        except HelperError as exc:
            code = self._reap()
            # pkexec's own exit codes: the helper never ran.
            if code == 126:
                raise HelperError("Not authorised: the password prompt was "
                                  "dismissed or the password was wrong.")
            if code == 127:
                raise HelperError("Not authorised: no password was given.")
            raise exc
        if not reply.get("ok"):
            self._reap()
            if "protocol version" in str(reply.get("error", "")):
                raise HelperError(
                    "The installed disk helper is from a different version "
                    "of KSysSupervisor. Update it with:\n    %s"
                    % manual_install_hint())
            raise HelperError("Unexpected greeting from the disk helper: %s"
                              % reply.get("error", reply))
        log.info("Disk helper started (%s)", path)

    def stop(self, patience=30):
        """Tell the helper to finish and wait up to `patience` seconds.

        An operation still running is never killed: the helper carries it
        through to its end and exits then, whether or not this waited.
        """
        if self.running:
            try:
                self._send({"cmd": "bye", "id": self._new_id()})
            except HelperError:
                pass
        self._reap(patience)

    def _reap(self, patience=30):
        proc, self._proc = self._proc, None
        if proc is None:
            return None
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=patience)
        except subprocess.TimeoutExpired:
            log.warning("The disk helper (pid %s) is still finishing an "
                        "operation; leaving it to complete", proc.pid)
            return None
        # Its output ended with it, so the readers are at end of file; they
        # are given a moment to notice before the streams are closed under
        # them.
        for reader in self._readers:
            reader.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._readers = []
        return proc.returncode

    # ---- plumbing -------------------------------------------------------

    @staticmethod
    def _read_replies(proc, replies):
        """Every line the helper prints, parsed, onto the queue. Ends - with
        a None marker - when the helper closes its output."""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    log.warning("Unreadable line from the disk helper: %r",
                                line[:200])
                    continue
                if isinstance(message, dict):
                    replies.put(message)
        except (OSError, ValueError):
            pass
        replies.put(None)

    @staticmethod
    def _read_log(proc):
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    log.info("disk helper: %s", line)
        except (OSError, ValueError):
            pass

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def _send(self, message):
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise HelperError("The disk helper is not running.")
            try:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise HelperError("Lost contact with the disk helper: %s"
                                  % exc)

    def _request(self, message, timeout=REPLY_TIMEOUT, on_event=None):
        """Send one request and wait for the reply carrying its id.

        Events for that id go to `on_event`; anything for another id - the
        acknowledgement of a cancel sent meanwhile - is dropped. With no
        timeout the wait lasts as long as the helper lives.
        """
        ident = self._new_id()
        message = dict(message, id=ident)
        replies = self._replies
        self._send(message)
        waited = 0.0
        while True:
            try:
                reply = replies.get(timeout=0.5)
            except queue.Empty:
                waited += 0.5
                if timeout is not None and waited >= timeout:
                    raise HelperError("The disk helper did not answer.")
                continue
            if reply is None:
                raise HelperError(
                    "The disk helper stopped unexpectedly. Look at the "
                    "drive list before trying again: an operation may have "
                    "been left unfinished.")
            if reply.get("id") != ident:
                continue
            if "event" in reply:
                if on_event is not None:
                    try:
                        on_event(reply)
                    except Exception:
                        log.exception("Disk operation event handler failed")
                continue
            return reply

    # ---- operations -----------------------------------------------------

    def run(self, request, on_event=None):
        """Carry out one operation; returns the helper's result dictionary.

        Blocks for as long as the operation takes. Raises Cancelled when the
        user stopped it, HelperError when it was refused or failed - with the
        helper's own explanation as the text.
        """
        with self._run_lock:
            self.start()
            self._busy = True
            try:
                reply = self._request(dict(request, cmd="run"), timeout=None,
                                      on_event=on_event)
            finally:
                self._busy = False
        if reply.get("ok"):
            return reply.get("result") or {}
        if reply.get("cancelled"):
            raise Cancelled(reply.get("error") or "Stopped.")
        raise HelperError(reply.get("error") or "The helper refused.")

    def cancel(self):
        """Ask for the running operation to stop at the next safe point.

        Only a request: a step that cannot be interrupted finishes first.
        """
        if not self.running:
            return
        try:
            self._send({"cmd": "cancel", "id": self._new_id()})
        except HelperError:
            pass
