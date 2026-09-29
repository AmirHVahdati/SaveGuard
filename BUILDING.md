# Building a Windows .exe and installer

This has to be done **on an actual Windows machine** — PyInstaller (the tool
that turns the Python app into a `.exe`) doesn't cross-compile. Running it
on Linux or macOS produces a Linux/macOS executable, not something Windows
can run. Everything below assumes you're on Windows, in VS Code.

## 1. One-time setup

1. Install [Python 3.11+ for Windows](https://www.python.org/downloads/) if you don't have it. During install, tick **"Add python.exe to PATH"**.
2. Open the `saveguard` folder in VS Code (`File → Open Folder…`).
3. Open a terminal in VS Code: `` Terminal → New Terminal `` (or `` Ctrl+` ``). Make sure it's a normal **Command Prompt** or **PowerShell** terminal, not WSL/Linux — check the dropdown in the top-right of the terminal panel.
4. Create and activate a virtual environment:
   ```powershell
   python -m venv venv
   venv\Scripts\activate
   ```
   Your terminal prompt should now start with `(venv)`. VS Code may also pop up a notification asking to use this environment as the workspace's Python interpreter — click **Yes**.
5. Install the app's dependencies, plus PyInstaller:
   ```powershell
   pip install -r requirements.txt
   pip install pyinstaller
   ```

## 2. Build the .exe

Still in that same VS Code terminal, from the project's root folder (where `main.py` lives):

```powershell
pyinstaller saveguard.spec
```

This takes a minute or two. When it finishes, you'll have:

```
dist\SaveGuard\SaveGuard.exe      <- the app, plus its bundled Python/Qt files alongside it
build\                             <- PyInstaller's working files (safe to ignore/delete)
```

**Test it now, before building an installer**: double-click `dist\SaveGuard\SaveGuard.exe`. It should launch exactly like it does when you run `python main.py`. If it doesn't, see Troubleshooting below before moving on.

> **Note on `dist\SaveGuard\` being a folder, not a single file:** PyInstaller can also produce one giant single `.exe` (`--onefile`), but for a PySide6 app that's usually slower to start (it has to unpack itself into a temp folder every launch) and more likely to trip antivirus false-positives. The folder build in `saveguard.spec` is the more reliable choice — the installer below packages the whole folder up so the end user still only sees one thing to run.

## 3. Build the installer

1. Install [Inno Setup](https://jrsoftware.org/isdl.php) (free) — just run its installer with defaults.
2. Open `installer.iss` — either double-click it (opens in the Inno Setup Compiler) or right-click → **Open with → Inno Setup Compiler**. (VS Code itself can't run this part; Inno Setup's own app does the compiling.)
3. Click **Build → Compile** (or press `Ctrl+F9`).
4. The finished installer appears at `installer_output\SaveGuard-Setup-1.0.0.exe`.

Run that file to test — it should install the app, create a Start Menu entry (and Desktop shortcut if you check that box), and appear normally in **Settings → Apps → Installed apps** with a working uninstaller.

## Releasing a new version later

Two places need the version number bumped together:
- `app/__init__.py` → `__version__ = "..."` (shown in the app's own Settings → About tab)
- `installer.iss` → `#define MyAppVersion "..."` (shown in the installer and in Windows' Programs list)

Then repeat steps 2 and 3 above.

## Troubleshooting

**Taskbar icon shows as a solid black square, and/or the icon inside the app itself doesn't show either.** Root cause: PyInstaller 6.0+ changed onedir builds to place all bundled files (including `assets/`) inside a `_internal` subfolder next to the exe, instead of directly beside it. Code that assumes bundled data always sits right next to `sys.executable` — which is exactly what an earlier version of this app did — silently looks in the wrong place once built with a current PyInstaller version, so the icon files simply aren't found. (This is called out explicitly in PyInstaller's own 6.0 changelog: *"Assumptions that `os.path.dirname(sys.executable) == sys._MEIPASS` will break."*)

Fixed as of this version: `app/assets.py` now resolves bundled files via `sys._MEIPASS`, which PyInstaller itself keeps pointing at the correct location regardless of internal layout version. If you're rebuilding after pulling this fix and it's still wrong:
- Do a *clean* rebuild — delete the `build\` and `dist\` folders first, then re-run `pyinstaller saveguard.spec`. PyInstaller can otherwise reuse stale cached build state.
- Windows itself also caches icons separately from anything the app does. If the exe is genuinely fixed but Explorer/taskbar still shows the old one, sign out and back in (or reboot) to clear that cache.

**Windows Defender (or another antivirus) flags or deletes the .exe.** This is extremely common with PyInstaller apps and is a false positive — PyInstaller-built executables share a generic pattern (a bundled Python interpreter unpacking itself) that looks similar to how some real malware behaves, so heuristic scanners often flag brand-new, unsigned executables from small developers. It's not a real problem with the build. Options, roughly in order of effort:
- Add a Windows Defender exclusion for the `dist\` folder while testing.
- Longer-term, if you're distributing this to other people: code-signing the exe with a purchased code-signing certificate is the real fix — it's what makes Windows/Defender trust an executable from a new publisher. Without one, some users will see a "Windows protected your PC" SmartScreen warning the first time they run the installer (they can still click "More info → Run anyway"), which is normal for unsigned indie software and not a sign of a bad build.

**The exe launches and immediately closes / a console flashes and vanishes.** Almost always a startup exception. Temporarily set `console=True` in `saveguard.spec`, rebuild, and run the exe from a terminal (not by double-clicking) so you can actually read the error, then set `console=False` again once it's fixed.

**`ModuleNotFoundError` for something at runtime that worked fine with `python main.py`.** PyInstaller sometimes misses a dependency's submodules. Add the missing package to the `hiddenimports` list in `saveguard.spec` and rebuild.

**Build is slow / output is large.** Normal for the first build — PyInstaller bundles a full Python runtime plus the Qt libraries PySide6 needs, so 150-250MB installed is typical for a Qt app. The `excludes` list already in `saveguard.spec` trims out several large Qt modules SaveGuard doesn't use; if you find PyInstaller pulling in something else unnecessary, add it there too.
