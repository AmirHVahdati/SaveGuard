"""app/assets.py — loading the app icon at various sizes.

These tests also stand in as a check that the bundled assets are actually
present: a packaged build with a missing icon is a much worse surprise than a
failing test.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app import assets


class TestAssetFiles:
    def test_the_svg_icon_exists(self):
        assert os.path.isfile(assets.ICON_SVG_PATH)

    def test_the_ico_icon_exists(self):
        # load_app_icon prefers the pre-built .ico: converting SVG-rendered
        # pixmaps to a native HICON on the fly has been observed to drop the
        # alpha channel, which shows up as a black square in the taskbar.
        assert os.path.isfile(assets.ICON_ICO_PATH)

    def test_both_files_are_in_the_assets_folder(self):
        assert os.path.basename(os.path.dirname(assets.ICON_SVG_PATH)) == "assets"
        assert os.path.dirname(assets.ICON_SVG_PATH) == \
            os.path.dirname(assets.ICON_ICO_PATH)


class TestLoadIconPixmap:
    def test_renders_at_the_requested_size(self, qapp):
        pixmap = assets.load_icon_pixmap(48)
        assert isinstance(pixmap, QPixmap)
        assert (pixmap.width(), pixmap.height()) == (48, 48)

    @pytest.mark.parametrize("size", [16, 24, 56, 72, 128])
    def test_handles_the_sizes_the_app_asks_for(self, qapp, size):
        pixmap = assets.load_icon_pixmap(size)
        assert (pixmap.width(), pixmap.height()) == (size, size)

    def test_is_cached_per_size(self, qapp):
        first = assets.load_icon_pixmap(37)
        second = assets.load_icon_pixmap(37)
        # Re-rendering the SVG per call is exactly what the cache avoids.
        assert first is second
        assert 37 in assets._pixmap_cache

    def test_actually_draws_something(self, qapp):
        pixmap = assets.load_icon_pixmap(64)
        image = pixmap.toImage()
        centre = image.pixelColor(32, 32)
        assert centre.alpha() > 0, "the middle of the icon should be painted"

    def test_falls_back_to_a_blank_pixmap_for_an_unreadable_icon(self, qapp, tmp_path):
        broken = QSvgRenderer(str(tmp_path / "missing.svg"))
        pixmap = assets.load_icon_pixmap(31, renderer=broken)
        # Better a blank icon than an exception during startup.
        assert (pixmap.width(), pixmap.height()) == (31, 31)
        assert broken.isValid() is False

    def test_the_svg_itself_is_valid(self, qapp):
        assert QSvgRenderer(assets.ICON_SVG_PATH).isValid() is True


class TestLoadAppIcon:
    def test_returns_a_usable_icon(self, qapp):
        icon = assets.load_app_icon()
        assert isinstance(icon, QIcon)
        assert icon.isNull() is False

    def test_is_cached(self, qapp):
        assert assets.load_app_icon() is assets.load_app_icon()

    def test_has_several_sizes_available(self, qapp):
        icon = assets.load_app_icon()
        sizes = icon.availableSizes()
        assert len(sizes) >= 1
        assert all(not size.isEmpty() for size in sizes)

    def test_falls_back_to_the_svg_when_no_ico_is_present(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(assets, "_icon_cache", None)
        monkeypatch.setattr(assets, "ICON_ICO_PATH", str(tmp_path / "absent.ico"))
        icon = assets.load_app_icon()
        assert icon.isNull() is False
        assert icon.availableSizes()


class TestFrozenBuildPaths:
    """When running as a PyInstaller build, bundled assets don't sit next to
    the module's original location, so resolution goes through sys._MEIPASS.

    Checked in a subprocess: the module decides this once at import time, and
    reloading it in-process would leave the frozen paths in place for every
    test that ran afterwards.
    """

    REPO_ROOT = Path(__file__).resolve().parent.parent

    def _resolve_with(self, preamble):
        result = subprocess.run(
            [sys.executable, "-c", preamble + "\nimport app.assets as a\nprint(a.ICON_SVG_PATH)"],
            capture_output=True, text=True, cwd=str(self.REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_resolves_assets_from_meipass_when_frozen(self, tmp_path):
        bundle = tmp_path / "bundle"
        (bundle / "assets").mkdir(parents=True)
        (bundle / "assets" / "icon.svg").write_text("<svg/>", encoding="utf-8")
        resolved = self._resolve_with(
            f"import sys\nsys.frozen = True\nsys._MEIPASS = {str(bundle)!r}"
        )
        assert resolved == str(bundle / "assets" / "icon.svg")

    def test_falls_back_to_the_executable_folder(self):
        resolved = self._resolve_with(
            "import sys\nsys.frozen = True\n"
            "sys.__dict__.pop('_MEIPASS', None)"
        )
        # Without _MEIPASS the best guess is the exe's own folder.
        assert resolved.endswith(os.path.join("assets", "icon.svg"))
        assert Path(resolved).parent.parent == Path(sys.executable).parent

    def test_a_source_checkout_resolves_next_to_the_source(self):
        resolved = self._resolve_with("import sys")
        assert resolved == str(self.REPO_ROOT / "assets" / "icon.svg")