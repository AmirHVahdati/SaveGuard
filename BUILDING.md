# Building SaveGuard

Everything needed to run the test suite, build a Windows executable, and produce
a Windows installer.

| Task | Requires | Platform |
|---|---|---|
| Run the app | Python 3.9+ | Windows, Linux |
| Run the tests | Python 3.9+ | Windows, Linux |
| Build the `.exe` | Python 3.11+, PyInstaller | **Windows only** |
| Build the installer | The built `.exe`, Inno Setup | **Windows only** |

> **PyInstaller does not cross-compile.** Building on Linux or macOS produces an
> executable for that operating system, not one Windows can run. The `.exe` and
> the installer both have to be built on a Windows machine.

---

## Contents

- [Running the tests](#running-the-tests)
- [Building the .exe](#building-the-exe)
- [Building the installer](#building-the-installer)
- [Releasing a new version](#releasing-a-new-version)
- [How the build is wired](#how-the-build-is-wired)
- [Troubleshooting](#troubleshooting)

---

## Setup

1. Install [Python for Windows](https://www.python.org/downloads/) if you don't
   have it. Tick **"Add python.exe to PATH"** during install.
2. Open the project folder in VS Code (`File → Open Folder…`).
3. Open a terminal: `Terminal → New Terminal` (`` Ctrl+` ``). Make sure it is a
   normal **Command Prompt** or **PowerShell**, not WSL — check the dropdown at
   the top right of the terminal panel.
4. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   Your prompt should now start with `(.venv)`. VS Code may offer to use this
   environment as the workspace interpreter — accept it.

5. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

   That single file covers running the app, running the tests, and PyInstaller.

---

## Running the tests

```powershell
python -m pytest
```

851 tests in about 9 seconds, no arguments required. `pytest.ini` sets
`testpaths = tests` and `qt_api = pyside6`.

Useful variations:

| Command | Does |
|---|---|
| `python -m pytest tests/test_ui_main_window.py` | One file |
| `python -m pytest -k "backup_all"` | By name |
| `python -m pytest -x` | Stop at the first failure |
| `python -m pytest --lf` | Re-run what failed last time |
| `python -m pytest -v` | One line per test |
| `python -m pytest -rs` | Show why anything was skipped |

### Why it is safe to run

Running the tests must never disturb a real SaveGuard install.
`tests/conftest.py` arranges that:

- **No windows.** Qt runs on its `offscreen` platform, set at import time in
  `conftest.py`. Set `QT_QPA_PLATFORM` yourself before invoking pytest if you
  *want* real windows.
- **No real config.** Every read and write of `~/.saveguard/` — games, settings,
  state, the update cache, the crash log — is redirected into a throwaway
  `tmp_path` per test.
- **No real user folders.** The one function that walks the real user's save
  locations is stubbed out.
- **No network.** Update-check tests use fake GitHub responses.
- **No hung runs.** Modal dialogs are answered by fixtures rather than clicked.
  An autouse fixture holds `QMessageBox.question` at *No*, because a test that
  leaves a message box open hangs the entire run instead of failing.

### What the skipped tests are

The 8 skips are POSIX-only path and symlink assertions that cannot run on
Windows. Each sits beside the branch it guards and carries its own reason string,
and the Windows side of the same branch is covered separately through the
`pretend_windows` fixture. They are a deliberate platform split, not suppressed
failures.

### Writing new UI tests

Two Qt behaviours catch people out:

- **The window recomputes every game's status as soon as the event loop turns.**
  State written during fixture *setup* can therefore be overwritten before the
  test body runs. Set it up in the test body instead.
- **An unshown offscreen window reports `isVisible() == False` for every child.**
  Test visibility with `isHidden()`, not `isVisible()`.

For anything that needs a dialog or a background thread:

- `message_spy` captures `QMessageBox.warning/critical/information/question`
  calls instead of showing them, so a test can assert on the dialog that appeared.
- `FakeBackupWorker` and `FakeScanWorker` in `tests/test_ui_main_window.py`
  replace the real workers. They subclass `QObject` so their signals still
  connect, define `finished` because the window waits on that signal before
  dropping its reference, record how they were constructed, and never start.

---

## Building the .exe

From the project root, in the activated environment:

```powershell
pyinstaller saveguard.spec
```

A minute or two later:

```
dist\SaveGuard\SaveGuard.exe    <- the app, with its bundled Python/Qt alongside
build\                           <- PyInstaller's working files, safe to delete
```

**Test it before building an installer.** Run `dist\SaveGuard\SaveGuard.exe`. It
should behave exactly like `python main.py`. If it does not, see
[Troubleshooting](#troubleshooting).

---

## Building the installer

1. Install [Inno Setup](https://jrsoftware.org/isdl.php) (free), defaults are fine.
2. Open `installer.iss` — double-click it, or right-click → **Open with → Inno
   Setup Compiler**. VS Code cannot compile this file; Inno Setup's own
   application does.
3. **Build → Compile**, or `Ctrl+F9`.
4. The installer appears at `installer_output\SaveGuard-Setup-1.0.0.exe`.

It installs the app, creates a Start Menu entry (plus an optional Desktop
shortcut), and registers a working uninstaller under **Settings → Apps →
Installed apps**.

It installs per-machine, which asks for administrator rights once. SaveGuard's own
data lives in `~\.saveguard`, not in Program Files, so there is nothing to
relocate. To install without admin rights, set `PrivilegesRequired=lowest` in
`installer.iss`.

Uninstalling removes program files only. Tracked games, settings and backups in
`~\.saveguard` are left alone.

---

## Releasing a new version

Two places carry the version, and they must be bumped together:

| File | Key | Where it shows up |
|---|---|---|
| `app/__init__.py` | `__version__` | Settings → About, update checks |
| `installer.iss` | `MyAppVersion` | Installer and Windows' Programs list |

Then repeat the two build steps above.

---

## How the build is wired

`saveguard.spec` bundles `main.py` and the two icon files, and nothing else —
the test suite is not packaged. Worth knowing when editing it:

- **`datas`** — the `.svg` and `.ico` icons, so `app/assets.py` finds them at
  runtime by the same relative path it uses in development.
- **`excludes`** — several large PySide6 modules the app never touches
  (WebEngine, Multimedia, Quick/QML, Bluetooth, 3D). Removing them makes the
  build smaller and faster to start.
- **`console=False`** — a windowed app with no terminal behind it. Temporarily
  flip this to `True` when chasing a startup crash.
- **`upx=True`** — UPX compression. If UPX is the cause of an antivirus false
  positive, set this to `False` and rebuild.

---

## Troubleshooting

### The taskbar icon is a solid black square, or the in-app icon is missing

**Fixed in the current version.** PyInstaller 6.0+ moved bundled files (including
`assets/`) into a `_internal` subfolder next to the executable instead of placing
them directly beside it. Code that assumes bundled data always sits next to
`sys.executable` silently looks in the wrong place — this is called out in
PyInstaller's own 6.0 changelog: *"Assumptions that
`os.path.dirname(sys.executable) == sys._MEIPASS` will break."*

`app/assets.py` now resolves bundled files through `sys._MEIPASS`, which
PyInstaller keeps pointing at the correct location regardless of internal layout
version. If you are rebuilding after pulling that fix and icons are still wrong:

- **Do a clean rebuild.** Delete `build\` and `dist\`, then run
  `pyinstaller saveguard.spec` again. PyInstaller reuses cached build state
  otherwise.
- **Clear Explorer's icon cache.** Windows caches icons independently of
  anything the app does, so a genuinely fixed build can still show a stale icon
  until you sign out and back in.

### Antivirus flags or deletes the executable

Expected, and almost always a false positive. PyInstaller executables share a
generic pattern — a bundled interpreter unpacking itself — that resembles what
real malware does, so heuristic scanners frequently flag new, unsigned binaries
from small developers.

- For your own testing, add a Windows Defender exclusion for `dist\`.
- For actual distribution, code signing is the real fix: a purchased code-signing
  certificate lets Windows trust the binary from a new publisher. Until then,
  users may see a "Windows protected your PC" SmartScreen prompt, which is
  normal for unsigned software — **More info → Run anyway** gets past it.

### The executable closes immediately, or a console flashes and vanishes

Nearly always a startup exception. Temporarily set `console=True` in
`saveguard.spec`, rebuild, and run the executable from a terminal rather than by
double-clicking it so the traceback is visible. Set it back to `False` afterwards.

### `ModuleNotFoundError` for something that worked under `python main.py`

PyInstaller sometimes misses a dependency's submodules. Add the package to
`hiddenimports` in `saveguard.spec` and rebuild.

### The build is slow, or the output is large

Normal the first time. PyInstaller bundles a whole Python runtime plus Qt, so
150–250 MB installed is typical for a Qt application. The `excludes` list in
`saveguard.spec` already trims the modules SaveGuard does not use; if PyInstaller
pulls in something else unnecessary, add it there too.