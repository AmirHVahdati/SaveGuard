# Building SaveGuard

## Requirements

| Task | Requirements | Notes |
|---|---|---|
| Run the app | Python 3.9+ | Windows or Linux |
| Run tests | Python 3.9+ | Windows or Linux |
| Build `.exe` | Python 3.11+, [PyInstaller](https://pyinstaller.org) | Windows only |
| Build installer | Built `.exe`, [Inno Setup](https://jrsoftware.org/isdl.php) | Windows only |

> PyInstaller does not cross-compile. You must build on Windows.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Running tests

```powershell
python -m pytest
```

Useful flags:

```powershell
python -m pytest -v        # verbose
python -m pytest -x        # stop on first failure
python -m pytest -k name   # run tests matching name
```

Tests run headless with `QT_QPA_PLATFORM=offscreen` and use temp directories, so they're safe to run on your machine.

## Building the .exe

From the activated virtual environment:

```powershell
pyinstaller saveguard.spec
```

Output:

```
dist\SaveGuard\SaveGuard.exe
```

Test the exe before building the installer to catch any missing files.

## Building the installer

1. Build the `.exe` first
2. Open `installer.iss` in the Inno Setup Compiler
3. Click `Build → Compile` (`Ctrl+F9`)
4. The installer will be created in `installer_output\SaveGuard-Setup-<version>.exe`

It creates a Start Menu entry and a working uninstaller. User data stays in `~\.saveguard\`.

## Releasing a new version

Bump the version in both places:

- `app/__init__.py` — `__version__`
- `installer.iss` — `MyAppVersion`

Then rebuild the `.exe` and installer.

## Tips

- If the built exe crashes on launch, set `console=True` in `saveguard.spec`, rebuild, and run from a terminal to see the traceback. Set it back to `False` when done.
- If a new module causes `ModuleNotFoundError` in the built exe, add it to `hiddenimports` in `saveguard.spec`.
- Delete `build/` and `dist/` for a clean rebuild if things look cached.
