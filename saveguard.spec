# PyInstaller spec for SaveGuard.
#
# This file must be run with PyInstaller ON WINDOWS to produce a Windows
# .exe — PyInstaller does not cross-compile, so running this on Linux/macOS
# produces a Linux/macOS executable instead, not something Windows can run.
# See BUILDING.md for the full step-by-step process in VS Code.
#
# Usage (from the project's root folder, in an activated venv):
#   pyinstaller saveguard.spec
#
# Output ends up in dist\SaveGuard\SaveGuard.exe (a folder build, not a
# single file — see BUILDING.md for why that's the recommended choice here).

import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # Bundles the icon files into the app folder so app/assets.py can still
    # find them at runtime via the same relative path it uses in dev.
    datas=[
        ("assets/icon.svg", "assets"),
        ("assets/icon.ico", "assets"),
    ],
    hiddenimports=collect_submodules("PySide6"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PySide6 ships several Qt modules SaveGuard never uses (WebEngine,
        # Multimedia, 3D, etc.) — excluding them keeps the build much
        # smaller and noticeably faster to start.
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
        "PySide6.QtPositioning", "PySide6.QtNetwork",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SaveGuard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # GUI app -- no terminal window behind it
    icon="assets/icon.ico",
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SaveGuard",
)
