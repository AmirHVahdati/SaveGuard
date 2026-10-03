"""Shared pytest fixtures for the SaveGuard test suite.

Every test runs with Qt on the offscreen platform and with the app's config
directory pointed at a throwaway tmp folder, so running the suite can never
touch a developer's real ~/.saveguard, and never opens a visible window.

The config redirection works by patching the module-level path constants in
app.config rather than HOME or USERPROFILE. Those constants are resolved once
at import time from Path.home(), but every function in app.config reads them
back out of the module globals at call time -- which is exactly the code path
these tests exercise -- so patching them redirects all reads and writes.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# Must be set before any QApplication exists, so it happens at import time of
# this conftest rather than inside a fixture. setdefault, not assignment, so a
# developer who wants to run the suite against a real display can opt in via
# the environment.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app import config as config_module
from app import game_scanner as game_scanner_module
from app import updater as updater_module
from PySide6.QtWidgets import QMessageBox

from helpers import write_files


# ---------------------------------------------------------------------------
# Signal capture
# ---------------------------------------------------------------------------

class SignalRecorder:
    """Records every emission of a Qt signal.

    Used with QThread subclasses whose run() is called synchronously instead
    of via start(): a signal emitted from the thread that created the
    connection is delivered directly, so emissions are recorded by the time
    run() returns with no event loop and no waiting.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)

    @property
    def last(self):
        return self.calls[-1] if self.calls else None

    @property
    def called(self):
        return bool(self.calls)

    def __len__(self):
        return len(self.calls)

    def __bool__(self):
        return bool(self.calls)


@pytest.fixture
def recorder():
    return SignalRecorder


# ---------------------------------------------------------------------------
# Filesystem isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    """Redirect every file app.config and app.updater read or write into a
    per-test temporary directory. Autouse: no test should be able to reach the
    real config directory by accident, and the directory is deliberately NOT
    created here so tests can cover ensure_app_dir() itself.
    """
    redirected = tmp_path / ".saveguard"
    monkeypatch.setattr(config_module, "APP_DIR", redirected)
    monkeypatch.setattr(config_module, "CONFIG_FILE", redirected / "games.json")
    monkeypatch.setattr(config_module, "STATE_FILE", redirected / "state.json")
    monkeypatch.setattr(config_module, "SETTINGS_FILE", redirected / "settings.json")
    # app.updater imports APP_DIR by value and derives its cache path from it
    # at import time, so both bindings need redirecting.
    monkeypatch.setattr(updater_module, "APP_DIR", redirected)
    monkeypatch.setattr(updater_module, "_CACHE_FILE", redirected / "update_cache.json")
    return redirected


@pytest.fixture(autouse=True)
def no_real_location_scan(monkeypatch):
    """Stub out the one function that walks the real user's save locations.

    app.game_scanner.discover_candidates has no injectable roots for the
    platform-specific branches (piracy folders, drive letters), so it is the
    one entry point that can escape a temp directory. Tests that exercise
    scanning pass explicit roots to the individual scan_* functions instead.
    """
    monkeypatch.setattr(game_scanner_module, "discover_candidates", lambda *a, **k: [])


@pytest.fixture
def pretend_windows(monkeypatch):
    """Make app.game_scanner's Windows-only code paths runnable on any OS.

    The module reads sys.platform directly at call time. Swapping the module's
    own reference for a stub leaves the real sys module -- and pytest's
    platform detection -- completely untouched.
    """
    def _apply(platform="win32"):
        monkeypatch.setattr(
            game_scanner_module, "sys", SimpleNamespace(platform=platform)
        )
    return _apply


# ---------------------------------------------------------------------------
# Filesystem content helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def write_save_tree():
    """Fixture form of helpers.write_files, for tests that prefer fixtures."""
    return write_files


@pytest.fixture
def save_root(tmp_path):
    """A realistic save folder: a file at the top level, a nested file, and a
    folder with no direct files of its own.
    """
    return write_files(
        tmp_path / "saves" / "MyGame",
        {
            "slot1.sav": "slot one",
            "settings.ini": "[video]\nwidth=1920\n",
            "SaveData/nested.sav": "nested payload",
        },
    )


@pytest.fixture
def backup_root(tmp_path):
    """A backup destination that does not exist yet -- BackupWorker is
    expected to create it.
    """
    return tmp_path / "backups"


# ---------------------------------------------------------------------------
# Links: the cycle / alias cases the app is explicitly hardened against
# ---------------------------------------------------------------------------

@pytest.fixture
def junction(tmp_path):
    """Factory creating a directory link, cleaned up after the test.

    Uses `mklink /J` on Windows (a real reparse point: islink() is False and
    os.walk follows it) and a symlink elsewhere. Both matter because that is
    exactly the case a directory-identity cycle guard has to survive.
    """
    created = []

    def _make(target, name="link", parent=None):
        base = Path(parent) if parent is not None else tmp_path
        link = base / name
        if sys.platform == "win32":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                pytest.skip(f"could not create junction: {result.stderr.strip()}")
        else:
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"symlinks unavailable: {exc}")
        created.append(link)
        return link

    yield _make

    for link in reversed(created):
        try:
            os.rmdir(link)  # removes a junction itself, never its target's contents
        except OSError:
            try:
                link.unlink()
            except OSError:
                pass


@pytest.fixture
def make_symlink(tmp_path):
    """Factory creating a file or directory symlink, skipped where unsupported
    (Windows needs Developer Mode or elevation)."""
    created = []

    def _make(target, name="link", parent=None, is_dir=False):
        base = Path(parent) if parent is not None else tmp_path
        link = base / name
        try:
            link.symlink_to(target, target_is_directory=is_dir)
        except (OSError, NotImplementedError, AttributeError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        created.append(link)
        return link

    yield _make

    for link in reversed(created):
        try:
            link.unlink()
        except OSError:
            pass


@pytest.fixture
def self_referencing_dir(tmp_path, junction):
    """A directory that contains a link back to itself: root/LINK -> root.

    Walking it naively recurses forever, which is the case every traversal in
    this app is guarded against.
    """
    root = tmp_path / "saves"
    write_files(root, {"sub/slot.sav": "payload"})
    junction(root, "LINK", parent=root)
    return root


# ---------------------------------------------------------------------------
# Qt helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def qapp_guard(qapp):
    """Ensures a QApplication exists for tests that don't otherwise need
    pytest-qt's qtbot (QThread objects are Qt objects too)."""
    return qapp


@pytest.fixture
def run_backup(qapp):
    """Factory that runs a BackupWorker synchronously and reports its signals.

    Calls run() directly instead of start() so assertions can be made
    immediately afterwards: emissions are delivered on the calling thread, so
    no event loop or wait is involved and the tests stay deterministic.
    """
    from app.backup_worker import BackupWorker

    def _run(save_path, backup_root, *, game_id="game-1", game_name="Test Game",
             compression_level=6, max_backups=0):
        worker = BackupWorker(
            game_id, game_name, str(save_path), str(backup_root),
            compression_level=compression_level, max_backups=max_backups,
        )
        progress = SignalRecorder()
        finished = SignalRecorder()
        errors = SignalRecorder()
        worker.progress.connect(progress)
        worker.backup_finished.connect(finished)
        worker.error.connect(errors)
        worker.run()
        return SimpleNamespace(
            worker=worker, progress=progress, finished=finished, errors=errors
        )

    return _run


@pytest.fixture
def message_spy(monkeypatch):
    """Captures QMessageBox.warning/critical/information calls instead of
    showing them (a modal box would hang the test run).

    Each call is recorded as ("warning", parent, title, text), so a test can
    assert both that the expected dialog appeared and which one it was. An
    empty recorder means no dialog appeared at all. Stubbed question() returns
    None, i.e. "No"; a test that needs a specific answer patches it itself.
    """
    captured = SignalRecorder()
    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(
            QMessageBox, name,
            lambda *args, _n=name, **kwargs: captured(*((_n,) + args)),
        )
    return captured


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch):
    """Keeps every modal question answered "No" for the length of each test.

    A modal dialog with no one to click it hangs the whole run, and Qt tests
    reach them from places a test never controls: closing a window that still
    tracks a running worker, for instance. Tests that care about the answer
    patch question() themselves, which takes precedence over this default.
    """
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)


@pytest.fixture
def main_window(qtbot, app_dir, monkeypatch):
    """A constructed MainWindow with no tracked games.

    The one-time automatic first scan is pre-marked as done in the state file,
    which is what keeps a test window from kicking off a location scan 500ms
    after it opens. Tests that specifically want that behaviour set the state
    themselves.
    """
    from app.config import save_state
    from app.ui.main_window import MainWindow

    save_state({"first_scan_done": True})
    window = MainWindow()
    qtbot.addWidget(window)
    yield window
    window.close()