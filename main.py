"""SaveGuard - a game save backup utility.

Run with:  python main.py
Requires:  PySide6
"""

import sys
import traceback
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from app.ui.main_window import MainWindow
from app.styles import STYLE_SHEET
from app.assets import load_app_icon
from app.config import APP_DIR, ensure_app_dir
from app.updater import check_for_updates, fetch_release_notes
from app import __version__


def _install_exception_hook():
    """Catches anything that isn't already handled somewhere more specific
    and shows a plain-language message instead of the app just vanishing —
    most people running a packaged desktop app won't have a terminal open
    to see a traceback. Also logs the full traceback to a file so there's
    something concrete to include in a bug report."""

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        sys.stderr.write(tb_text)  # still visible to anyone running from a terminal/IDE

        log_path = None
        try:
            ensure_app_dir()
            log_path = APP_DIR / "crash.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n{tb_text}\n")
        except OSError:
            log_path = None

        app = QApplication.instance()
        if app is not None:
            detail = f"\n\nDetails were saved to:\n{log_path}" if log_path else ""
            QMessageBox.critical(
                None,
                "SaveGuard ran into a problem",
                "Something unexpected went wrong. SaveGuard will try to keep "
                "running, but if things seem off, it's a good idea to restart "
                "it." + detail,
            )

    sys.excepthook = handle_exception


def _set_windows_app_id():
    """Windows uses this ID (not the process/exe name) to decide whether a
    running app is 'the same app' for taskbar grouping, pinning, and jump
    lists. Without setting it explicitly, a Python-based app can end up
    grouped under Python's own generic identity instead of SaveGuard's —
    unrelated to the black-taskbar-icon issue this app has hit before, but
    a common companion fix for the same class of "Windows treats this
    packaged Python app oddly" problems, so it's set proactively here."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SaveGuard.SaveGuard.1")
        except Exception:
            pass  # cosmetic only -- never worth failing startup over


def _check_for_update_startup(window):
    """Check for updates on startup. If a new version is available, show
    a non-blocking modal dialog asking if the user wants to update."""
    global _startup_update_worker
    _startup_update_worker = _StartupUpdateWorker()
    _startup_update_worker.result.connect(lambda info: _show_update_dialog(window, info))
    _startup_update_worker.finished.connect(_startup_update_worker.deleteLater)
    _startup_update_worker.start()


def _show_update_dialog(window, release_info):
    if release_info is None:
        return

    notes = fetch_release_notes(release_info.version)
    msg = (
        f"A new version of SaveGuard (v{release_info.version}) is available.\n\n"
        f"You're currently on v{__version__}. Would you like to open the "
        "release page in your browser to download it?"
    )
    if notes:
        msg += f"\n\nWhat's new:\n{notes[:500]}"
    reply = QMessageBox.question(
        window,
        "Update Available",
        msg,
        QMessageBox.Yes | QMessageBox.No,
    )
    if reply == QMessageBox.Yes:
        QDesktopServices.openUrl(QUrl(release_info.url))


class _StartupUpdateWorker(QThread):
    result = Signal(object)

    def run(self):
        try:
            release = check_for_updates(force=False)
        except Exception:
            release = None
        self.result.emit(release)


_startup_update_worker: Optional[_StartupUpdateWorker] = None


def main():
    _install_exception_hook()
    _set_windows_app_id()

    app = QApplication(sys.argv)
    app.setApplicationName("SaveGuard")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE_SHEET)
    app.setWindowIcon(load_app_icon())

    window = MainWindow()
    window.show()

    QTimer.singleShot(1500, lambda: _check_for_update_startup(window))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
