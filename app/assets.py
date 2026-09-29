"""Small helper for loading the app's SVG icon at any resolution."""

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter, QIcon
from PySide6.QtSvg import QSvgRenderer

if getattr(sys, "frozen", False):
    # Running as a PyInstaller-built .exe: bundled data files (our assets/
    # folder included) do NOT sit next to this module's original source
    # location, so __file__-based resolution below would silently look in
    # the wrong place. sys._MEIPASS is PyInstaller's own, version-safe
    # answer to "where did my bundled files actually end up" -- it's kept
    # correct across PyInstaller's internal layout changes (e.g. 6.0+
    # moved onedir's bundled contents into a sub-folder named _internal by
    # default; code that instead assumed bundled files sit directly next
    # to sys.executable broke on that change, which is exactly the bug
    # this replaced). Falling back to sys.executable's own folder only
    # covers the unusual case _MEIPASS isn't set at all.
    _BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ASSETS_DIR = os.path.join(_BASE_DIR, "assets")
ICON_SVG_PATH = os.path.join(_ASSETS_DIR, "icon.svg")
ICON_ICO_PATH = os.path.join(_ASSETS_DIR, "icon.ico")

_pixmap_cache: dict[int, QPixmap] = {}
_icon_cache: QIcon = None


def load_icon_pixmap(size: int = 64, renderer: QSvgRenderer = None) -> QPixmap:
    """Render the app's SVG icon to a crisp QPixmap at the given pixel size.
    Results are cached per-size so repeated calls (header logo, window icon,
    etc.) never re-read or re-render the SVG more than once."""
    cached = _pixmap_cache.get(size)
    if cached is not None:
        return cached

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    if renderer is None:
        renderer = QSvgRenderer(ICON_SVG_PATH)
    if renderer.isValid():
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()

    _pixmap_cache[size] = pixmap
    return pixmap


def load_app_icon() -> QIcon:
    """Build the multi-resolution QIcon used for the window/taskbar icon.
    Built once and cached.

    Prefers loading the pre-built assets/icon.ico directly over assembling
    a QIcon at runtime from separately SVG-rendered QPixmaps. The two look
    identical inside the app itself, but on Windows they are NOT
    equivalent once Qt hands the icon off to the OS for the taskbar: a
    QIcon built purely from in-memory pixmaps has to be converted to a
    native HICON on the fly, and that conversion has been observed to lose
    alpha-channel data in exactly this scenario (SVG-rendered pixmaps with
    transparent corners around a rounded-square shape) — the symptom is a
    solid black square in the Windows 11 taskbar, even though the same
    icon renders correctly everywhere inside the app. Loading a real,
    pre-baked .ico file instead goes through Qt's own ICO plugin, which
    handles the native Windows conversion correctly (verified: every
    embedded size reports hasAlphaChannel=True after loading this way)."""
    global _icon_cache
    if _icon_cache is not None:
        return _icon_cache

    if os.path.isfile(ICON_ICO_PATH):
        icon = QIcon(ICON_ICO_PATH)
        if not icon.isNull():
            _icon_cache = icon
            return icon

    # Fallback for the rare case there's no .ico on disk (e.g. running
    # from a source checkout before one was generated) -- same approach
    # as before, just no longer the primary path.
    renderer = QSvgRenderer(ICON_SVG_PATH)
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(load_icon_pixmap(size, renderer=renderer))
    _icon_cache = icon
    return icon
