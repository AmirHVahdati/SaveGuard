"""Tests for the application entry point in main.py.

Everything here is about how the app starts up and how it behaves when
something goes wrong: the global exception hook, the startup update check, and
main() itself. main() is never allowed to reach a real event loop or a real
network call in these tests.
"""

import sys
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import main as main_module
from app.updater import ReleaseInfo

from conftest import SignalRecorder


def release(version="9.9.9", url="https://example.invalid/release"):
    return ReleaseInfo(version=version, url=url, notes="", is_prerelease=False)


@pytest.fixture
def restore_excepthook():
    """The exception hook replaces sys.excepthook; put the real one back."""
    original = sys.excepthook
    yield
    sys.excepthook = original


@pytest.fixture
def crash_log_dir(tmp_path, monkeypatch):
    """Points the crash hook at a temporary directory.

    main.py binds APP_DIR by value at import time, so redirecting app.config's
    copy after the fact would leave the hook writing to the developer's real
    ~/.saveguard. Both names have to be redirected here.
    """
    log_dir = tmp_path / "appdata"
    monkeypatch.setattr(main_module, "APP_DIR", log_dir)
    monkeypatch.setattr(main_module, "ensure_app_dir",
                        lambda: log_dir.mkdir(parents=True, exist_ok=True))
    return log_dir


@pytest.fixture
def crash_log_unwritable(tmp_path, monkeypatch):
    """A crash hook that cannot write its log, the way a read-only or full
    profile directory would behave."""
    monkeypatch.setattr(main_module, "APP_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(main_module, "ensure_app_dir",
                        lambda: (_ for _ in ()).throw(OSError("read-only file system")))
    return tmp_path


# ---------------------------------------------------------------------------
# the global exception hook
# ---------------------------------------------------------------------------

class TestExceptionHook:
    def test_it_is_installed_as_the_global_hook(self, restore_excepthook):
        main_module._install_exception_hook()
        assert sys.excepthook is not sys.__excepthook__

    def test_a_crash_is_written_to_the_log(self, crash_log_dir, restore_excepthook,
                                           message_spy):
        main_module._install_exception_hook()
        try:
            raise ValueError("the disk melted")
        except ValueError:
            sys.excepthook(*sys.exc_info())

        log = (crash_log_dir / "crash.log").read_text(encoding="utf-8")
        assert "ValueError: the disk melted" in log
        assert "raise ValueError" in log  # the stack, not just the message
        assert "---" in log  # timestamped entry separator

    def test_the_log_says_where_the_details_went(self, crash_log_dir, restore_excepthook,
                                                 message_spy, qtbot):
        main_module._install_exception_hook()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert str(crash_log_dir / "crash.log") in message_spy.calls[0][3]

    def test_the_traceback_still_reaches_the_terminal(self, capsys, restore_excepthook,
                                                      message_spy, crash_log_dir):
        # Someone running from a terminal should see the real traceback, not
        # just a dialog they may never find.
        main_module._install_exception_hook()
        try:
            raise ValueError("visible")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        assert "ValueError: visible" in capsys.readouterr().err

    def test_it_reports_the_problem_to_the_user(self, restore_excepthook, message_spy,
                                                crash_log_dir, qtbot):
        main_module._install_exception_hook()
        try:
            raise ValueError("user visible")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        assert message_spy.calls[0][2] == "SaveGuard ran into a problem"

    def test_nothing_is_shown_when_there_is_no_app_to_show_it_on(self, restore_excepthook,
                                                                 message_spy,
                                                                 crash_log_dir,
                                                                 monkeypatch):
        # A crash during interpreter shutdown, before the QApplication exists,
        # must not try to raise a dialog off nothing.
        monkeypatch.setattr(main_module.QApplication, "instance",
                            staticmethod(lambda: None))
        main_module._install_exception_hook()
        try:
            raise ValueError("nobody home")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        assert message_spy.calls == []
        assert "ValueError: nobody home" in (crash_log_dir / "crash.log").read_text(
            encoding="utf-8")

    def test_a_keyboard_interrupt_is_left_alone(self, restore_excepthook, message_spy,
                                                 crash_log_dir, qtbot):
        # Ctrl+C is not a crash: it must keep its usual traceback and must not
        # raise a "something went wrong" box at someone who just quit.
        main_module._install_exception_hook()
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        assert message_spy.calls == []
        assert not (crash_log_dir / "crash.log").exists()

    def test_an_unwritable_log_is_not_fatal(self, restore_excepthook, message_spy,
                                             crash_log_unwritable, qtbot):
        main_module._install_exception_hook()
        try:
            raise ValueError("no log possible")
        except ValueError:
            sys.excepthook(*sys.exc_info())  # must not raise
        # The user still gets told, just without a path to send along.
        assert message_spy.calls
        assert "Details were saved to" not in message_spy.calls[0][3]

    def test_crash_entries_are_appended_not_overwritten(self, crash_log_dir,
                                                        restore_excepthook,
                                                        message_spy):
        main_module._install_exception_hook()
        for _ in range(2):
            try:
                raise ValueError("twice")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        log = (crash_log_dir / "crash.log").read_text(encoding="utf-8")
        assert log.count("ValueError: twice") == 2


# ---------------------------------------------------------------------------
# the Windows taskbar identity
# ---------------------------------------------------------------------------

class TestWindowsAppId:
    def test_it_is_a_no_op_off_windows(self, monkeypatch):
        monkeypatch.setattr(main_module.sys, "platform", "linux")
        main_module._set_windows_app_id()  # must not raise

    def test_a_failing_ctypes_call_is_swallowed(self, monkeypatch):
        # Cosmetic only: never worth failing startup over.
        monkeypatch.setattr(main_module.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "ctypes", None)
        main_module._set_windows_app_id()  # must not raise


# ---------------------------------------------------------------------------
# the startup update check
# ---------------------------------------------------------------------------

class TestStartupUpdateCheck:
    def test_the_check_runs_off_the_ui_thread(self, monkeypatch, qtbot):
        started = []
        monkeypatch.setattr(main_module._StartupUpdateWorker, "start",
                            lambda self: started.append(self))
        main_module._check_for_update_startup(None)
        assert len(started) == 1

    def test_the_worker_is_kept_alive_until_it_is_done(self, monkeypatch):
        # The only reference to the worker is this module global; dropping it
        # would let the thread be garbage collected mid-check.
        monkeypatch.setattr(main_module._StartupUpdateWorker, "start", lambda self: None)
        main_module._check_for_update_startup(None)
        assert main_module._startup_update_worker is not None

    def test_a_newer_release_is_reported(self, monkeypatch, message_spy):
        monkeypatch.setattr(main_module, "check_for_updates",
                            lambda force=False: release())
        monkeypatch.setattr(main_module, "fetch_release_notes", lambda version: "")
        seen = SignalRecorder()
        worker = main_module._StartupUpdateWorker()
        worker.result.connect(seen)
        worker.run()
        assert seen.last[0].version == "9.9.9"

    def test_no_update_means_no_release_info(self, monkeypatch):
        monkeypatch.setattr(main_module, "check_for_updates", lambda force=False: None)
        seen = SignalRecorder()
        worker = main_module._StartupUpdateWorker()
        worker.result.connect(seen)
        worker.run()
        assert seen.last == (None,)

    def test_a_failing_check_is_not_an_error(self, monkeypatch):
        # Offline, rate limited, or the API changed shape: startup carries on.
        def explode(force=False):
            raise RuntimeError("no network")

        monkeypatch.setattr(main_module, "check_for_updates", explode)
        seen = SignalRecorder()
        worker = main_module._StartupUpdateWorker()
        worker.result.connect(seen)
        worker.run()
        assert seen.last == (None,)

    def test_the_check_is_not_forced(self, monkeypatch):
        # Forcing would hit the network on every single launch.
        seen = []
        monkeypatch.setattr(main_module, "check_for_updates",
                            lambda force: seen.append(force))
        main_module._StartupUpdateWorker().run()
        assert seen == [False]


class TestUpdateDialog:
    def test_nothing_is_shown_when_there_is_no_update(self, monkeypatch, message_spy):
        main_module._show_update_dialog(None, None)
        assert message_spy.calls == []

    def test_it_offers_the_new_version(self, monkeypatch, message_spy):
        monkeypatch.setattr(main_module, "fetch_release_notes", lambda version: "")
        main_module._show_update_dialog(None, release())
        title, text = message_spy.calls[0][2], message_spy.calls[0][3]
        assert title == "Update Available"
        assert "9.9.9" in text
        assert main_module.__version__ in text

    def test_the_release_notes_are_included(self, monkeypatch, message_spy):
        monkeypatch.setattr(main_module, "fetch_release_notes",
                            lambda version: "Fixed a thing")
        main_module._show_update_dialog(None, release())
        assert "Fixed a thing" in message_spy.calls[0][3]

    def test_long_release_notes_are_truncated(self, monkeypatch, message_spy):
        monkeypatch.setattr(main_module, "fetch_release_notes",
                            lambda version: "x" * 5000)
        main_module._show_update_dialog(None, release())
        text = message_spy.calls[0][3]
        assert "x" * 500 in text
        assert "x" * 501 not in text

    def test_saying_yes_opens_the_release_page(self, monkeypatch):
        opened = []
        monkeypatch.setattr(main_module.QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toString()))
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        main_module._show_update_dialog(None, release(url="https://example.invalid/r"))
        assert opened == ["https://example.invalid/r"]

    def test_saying_no_opens_nothing(self, monkeypatch):
        opened = []
        monkeypatch.setattr(main_module.QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toString()))
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        main_module._show_update_dialog(None, release())
        assert opened == []


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def noop(*args, **kwargs):
    """Swallows anything a startup step would normally schedule or open."""


class FakeApp:
    def __init__(self, argv):
        self.argv = argv
        self.name = None
        self.style = None
        self.stylesheet = None
        self.icon = None
        self.executed = False

    def setApplicationName(self, name):
        self.name = name

    def setStyle(self, style):
        self.style = style

    def setStyleSheet(self, sheet):
        self.stylesheet = sheet

    def setWindowIcon(self, icon):
        self.icon = icon

    def exec(self):
        self.executed = True
        return 0


@pytest.fixture
def launched(monkeypatch, qapp):
    """Runs main() with the Qt event loop, the window and the icon stubbed out.

    qapp is requested deliberately: main() creates its own QApplication in
    production and only then loads the window icon, and Qt refuses to build a
    pixmap without an application object around. Asking pytest-qt for one keeps
    that precondition in place while the event loop itself stays stubbed. The
    icon loader is stubbed because app.assets tests cover it for real, and a
    pixmap outliving the fake app object it was handed to upsets Qt later on.
    """
    created = []
    apps = []

    class FakeWindow:
        def __init__(self):
            self.shown = False
            created.append(self)

        def show(self):
            self.shown = True

    class RecordingApp(FakeApp):
        def __init__(self, argv):
            super().__init__(argv)
            apps.append(self)

    monkeypatch.setattr(main_module, "QApplication", RecordingApp)
    monkeypatch.setattr(main_module, "MainWindow", FakeWindow)
    monkeypatch.setattr(main_module, "load_app_icon", lambda: "THE-ICON")
    monkeypatch.setattr(main_module.sys, "exit", lambda code: None)
    # main() only ever queues the update check on a timer, and a queued timer
    # outlives the test that armed it: it would fire during some later test,
    # reach the real GitHub API, and leave a live worker thread behind at
    # interpreter shutdown. Dropping the timers keeps startup self-contained;
    # the tests that care about the scheduling record them again on top.
    monkeypatch.setattr(main_module, "QTimer", SimpleNamespace(singleShot=noop))
    monkeypatch.setattr(main_module, "_check_for_update_startup", lambda window: None)
    return created, apps


class TestMain:
    def test_it_starts_the_app_and_the_window(self, launched):
        created, apps = launched
        main_module.main()
        assert len(apps) == 1
        assert len(created) == 1
        assert created[0].shown is True
        assert apps[0].executed is True

    def test_it_names_and_styles_the_app(self, launched):
        _created, apps = launched
        main_module.main()
        assert apps[0].name == "SaveGuard"
        # Fusion is what makes the hand-written stylesheet look the same
        # everywhere instead of inheriting each platform's widget defaults.
        assert apps[0].style == "Fusion"

    def test_it_applies_the_stylesheet(self, launched):
        from app.styles import STYLE_SHEET

        _created, apps = launched
        main_module.main()
        assert apps[0].stylesheet == STYLE_SHEET

    def test_it_applies_the_app_icon(self, launched, monkeypatch):
        icons = []
        monkeypatch.setattr(main_module, "load_app_icon",
                            lambda: icons.append(1) or "THE-ICON")
        _created, apps = launched
        main_module.main()
        assert apps[0].icon == "THE-ICON"
        assert icons == [1]  # loaded once, not rebuilt per startup

    def test_it_installs_the_crash_hook(self, launched, restore_excepthook):
        main_module.main()
        assert sys.excepthook is not sys.__excepthook__

    def test_the_update_check_waits_for_the_window_to_paint(self, launched, monkeypatch):
        # Checking before the window is up would put a dialog on screen while
        # the app still looks like it hasn't started.
        checks = []
        scheduled = []
        monkeypatch.setattr(main_module.QTimer, "singleShot",
                            lambda ms, fn: scheduled.append(ms))
        monkeypatch.setattr(main_module, "_check_for_update_startup",
                            lambda window: checks.append(window))
        main_module.main()
        assert scheduled == [1500]
        assert checks == []  # only the timer can trigger it, not main() itself

    def test_the_scheduled_callback_does_check(self, launched, monkeypatch):
        scheduled = {}
        monkeypatch.setattr(main_module.QTimer, "singleShot",
                            lambda ms, fn: scheduled.update(ms=ms, fn=fn))
        checks = []
        monkeypatch.setattr(main_module, "_check_for_update_startup",
                            lambda window: checks.append(window))
        main_module.main()
        scheduled["fn"]()
        assert len(checks) == 1