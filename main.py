"""SaveGuard - a launcher-free game save backup utility.

Run with:  python main.py
Requires:  PySide6
"""

import sys
import traceback
from datetime import datetime

from PySide6.QtWidgets import QApplication, QMessageBox

from app.ui.main_window import MainWindow
from app.styles import STYLE_SHEET
from app.assets import load_app_icon
from app.config import APP_DIR, ensure_app_dir


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

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
