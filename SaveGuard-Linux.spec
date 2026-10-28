# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('app')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQml', 'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtSensors', 'PySide6.QtPositioning', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtSql', 'PySide6.QtSvgWidgets', 'PySide6.QtXml', 'tkinter', 'unittest', 'pydoc'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SaveGuard-Linux',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['*'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
)
