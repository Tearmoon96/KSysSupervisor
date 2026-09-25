"""The update check's user interface: the startup check, Help > Check for
Updates, the offer dialog and the restart into the new version.

The work itself - asking GitHub, downloading, verifying, installing - is in
updater.py, which knows nothing of Qt. This module decides when to run it and
keeps the window responsive while it does: the startup check runs on a plain
thread and reports back through a queued signal, and the install runs through
run_blocking() so the event loop keeps turning under a password prompt.
"""

import threading

from PyQt6.QtCore import QObject, QProcess, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox, QProgressDialog

from . import APP_NAME, APP_VERSION, GITHUB_REPO, installation, log, updater
from .widgets import run_blocking

KEY_CHECK_ON_START = "update_check_on_start"
KEY_AUTO_INSTALL = "update_auto_install"
KEY_SKIP_VERSION = "update_skip_version"
#: Left by an update for the version it installed, which checks on its first
#: start whether the fan helper has to follow.
KEY_CHECK_HELPER = "update_check_helper"

RELEASES_URL = "https://github.com/%s/releases" % GITHUB_REPO


class UpdateManager(QObject):
    """Owned by the main window; one per session."""

    # (release or None, error text or None), emitted from the check's thread.
    _checked = pyqtSignal(object, object)
    # (text, done, total), emitted from the install's thread.
    _progressed = pyqtSignal(str, int, int)

    def __init__(self, window, settings, install_kind=None):
        super().__init__(window)
        self.window = window
        self.settings = settings
        self.kind = install_kind or installation.kind()
        self._busy = False
        self._progress = None
        self._checked.connect(self._background_result,
                              Qt.ConnectionType.QueuedConnection)
        self._progressed.connect(self._show_progress,
                                 Qt.ConnectionType.QueuedConnection)

    # ---- preferences ----------------------------------------------------

    @property
    def check_on_start(self):
        return self.settings.value(KEY_CHECK_ON_START, True, type=bool)

    def set_check_on_start(self, enabled):
        self.settings.setValue(KEY_CHECK_ON_START, bool(enabled))

    @property
    def can_install(self):
        """Whether this copy can replace itself. A checkout cannot."""
        return self.kind != installation.SOURCE

    @property
    def auto_install(self):
        return (self.can_install
                and self.settings.value(KEY_AUTO_INSTALL, True, type=bool))

    def set_auto_install(self, enabled):
        self.settings.setValue(KEY_AUTO_INSTALL, bool(enabled))

    # ---- startup --------------------------------------------------------

    def startup(self):
        """Called once the window is up: finish a previous update, then check."""
        if self.settings.value(KEY_CHECK_HELPER, False, type=bool):
            self.settings.remove(KEY_CHECK_HELPER)
            self._update_helper_after_update()
        if self.check_on_start:
            self.check_in_background()

    def check_in_background(self):
        if self._busy:
            return

        def work():
            try:
                self._checked.emit(updater.check(), None)
            except updater.UpdateError as exc:
                self._checked.emit(None, str(exc))
            except Exception as exc:                # never kill the thread silently
                log.exception("Update check failed")
                self._checked.emit(None, str(exc))

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _background_result(self, release, error):
        if error:
            # Offline is a normal state for a desktop; not worth a dialog.
            log.info("Update check: %s", error)
            return
        if release is None:
            log.info("Update check: %s %s is the latest release",
                     APP_NAME, APP_VERSION)
            return
        log.info("Update check: %s is available", release.version)
        if self.settings.value(KEY_SKIP_VERSION, "", type=str) == release.version:
            return
        if self.auto_install:
            self.install(release, automatic=True)
        else:
            self.offer(release)

    # ---- manual check ---------------------------------------------------

    def check_now(self):
        """Help > Check for Updates."""
        if self._busy:
            return
        self._busy = True
        self.window.statusBar().showMessage("Checking for updates...")
        try:
            release, error = run_blocking(updater.check)
        finally:
            self._busy = False
            self.window.statusBar().clearMessage()
        if error is not None:
            QMessageBox.warning(self.window, "Check for Updates",
                                "Could not check for updates.\n\n%s" % error)
            return
        if release is None:
            QMessageBox.information(
                self.window, "Check for Updates",
                "%s %s is the latest version." % (APP_NAME, APP_VERSION))
            return
        # Asked for explicitly, so a version skipped earlier is offered again.
        self.offer(release)

    # ---- offering and installing ----------------------------------------

    def offer(self, release):
        box = QMessageBox(self.window)
        box.setWindowTitle("Update available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("%s %s is available. You have %s."
                    % (APP_NAME, release.version, APP_VERSION))
        if release.notes.strip():
            box.setDetailedText(release.notes.strip())

        if self.can_install:
            box.setInformativeText(
                "Updating replaces this version and restarts %s. Your "
                "settings are kept.%s" % (APP_NAME, self._password_note()))
            update = box.addButton("Update Now", QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setInformativeText(
                "This copy runs from a source folder: update it with "
                "'git pull', or download the release.")
            update = box.addButton("Open Download Page",
                                   QMessageBox.ButtonRole.AcceptRole)
        skip = box.addButton("Skip This Version", QMessageBox.ButtonRole.RejectRole)
        later = box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(update)
        box.setEscapeButton(later)
        box.exec()

        clicked = box.clickedButton()
        if clicked is skip:
            self.settings.setValue(KEY_SKIP_VERSION, release.version)
        elif clicked is update:
            if self.can_install:
                self.install(release)
            else:
                QDesktopServices.openUrl(QUrl(release.page_url or RELEASES_URL))

    def _password_note(self):
        if self.kind == installation.SYSTEM:
            return " It is installed for all users, so you will be asked " \
                   "for your password."
        return ""

    def install(self, release, automatic=False):
        """Download, verify and install `release`, then restart into it."""
        if self._busy or not self.can_install:
            return
        # The tools first: a stress test must stop and fans under manual
        # control go back to the board before the files under them change.
        if not self.window.close_tools():
            return

        self._busy = True
        self._progress = QProgressDialog(
            "Updating %s to %s..." % (APP_NAME, release.version),
            None, 0, 0, self.window)
        self._progress.setWindowTitle("Updating %s" % APP_NAME)
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setAutoClose(False)
        self._progress.show()
        try:
            launcher, error = run_blocking(
                lambda: updater.perform_update(
                    release, self.kind, progress=self._progressed.emit))
        finally:
            self._progress.close()
            self._progress = None
            self._busy = False

        if error is not None:
            log.error("Update to %s failed: %s", release.version, error)
            QMessageBox.warning(
                self.window, "Update failed",
                "%s was not updated to %s.\n\n%s"
                % (APP_NAME, release.version, error))
            return

        log.info("Updated to %s; restarting via %s", release.version, launcher)
        self.settings.setValue(KEY_CHECK_HELPER, True)
        self.settings.remove(KEY_SKIP_VERSION)
        self.settings.sync()
        self._restart(launcher, release.version)

    def _show_progress(self, text, done, total):
        if self._progress is None:
            return
        if total > 0:
            self._progress.setMaximum(100)
            self._progress.setValue(int(done * 100 / total))
        else:
            self._progress.setMaximum(0)
        self._progress.setLabelText(text)

    def _restart(self, launcher, version):
        if not launcher or not QProcess.startDetached(launcher, []):
            QMessageBox.information(
                self.window, "Update installed",
                "%s %s is installed. Start it again to use it." % (APP_NAME, version))
        self.window.close()

    # ---- the fan helper -------------------------------------------------

    def _update_helper_after_update(self):
        """First start after an update: bring the installed helper up to date.

        An AppImage carries its helper inside itself, where the version doing
        the update could not see it, so the new version checks. A system-wide
        install.sh update has already done this, and then nothing differs.
        """
        try:
            outdated = updater.helper_outdated()
        except Exception:
            log.exception("Could not compare the fan helper")
            return
        if not outdated:
            return
        from . import fanctl
        self.window.statusBar().showMessage(
            "Updating the fan control helper - enter your password to continue.")
        try:
            result, error = run_blocking(fanctl.install_bundled_helper)
        finally:
            self.window.statusBar().clearMessage()
        ok, message = result or (False, str(error))
        if ok:
            log.info("Fan control helper updated after the update")
        else:
            log.warning("Fan control helper not updated: %s", message)
            QMessageBox.warning(
                self.window, "Fan control helper",
                "%s was updated, but its fan control helper was not:\n\n%s\n\n"
                "Tools > Fan Control offers to update it." % (APP_NAME, message))
