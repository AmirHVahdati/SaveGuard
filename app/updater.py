"""Update checker — queries GitHub's public releases API for the latest
release of this project and reports whether a newer version is available.

Uses only the standard library (urllib) so no extra dependency is needed.
The GitHub API allows ~60 unauthenticated requests per hour per IP for
public repositories, which is more than sufficient for a desktop app that
checks once per session.

No server is required — GitHub Hosts the release tags and binaries.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from app import __version__
from app.config import APP_DIR

GITHUB_OWNER = "amirvahdati"
GITHUB_REPO = "saveguard"
API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
BROWSER_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# Cache the result so we only hit the API at most once per day.
_CACHE_FILE = APP_DIR / "update_cache.json"
_CACHE_TTL = 24 * 60 * 60  # 24 hours


@dataclass
class ReleaseInfo:
    version: str
    url: str
    notes: str
    is_prerelease: bool


def _is_newer_version(current: str, latest: str) -> bool:
    """Compare two semver-ish version strings (major.minor.patch)."""
    try:
        cur_parts = [int(p) for p in current.split(".")]
        new_parts = [int(p) for p in latest.split("v")[-1].split(".")]
        length = max(len(cur_parts), len(new_parts))
        cur_parts += [0] * (length - len(cur_parts))
        new_parts += [0] * (length - len(new_parts))
        return new_parts > cur_parts
    except ValueError:
        return False


def _load_cache() -> Optional[dict]:
    """Return cached check result if fresh enough, else None."""
    try:
        if not _CACHE_FILE.exists():
            return None
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - data.get("checked_at", 0) < _CACHE_TTL:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(latest_version: str) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"checked_at": time.time(), "latest_version": latest_version}),
            encoding="utf-8",
        )
    except OSError:
        pass  # never block the app over a cache write


def check_for_updates(force: bool = False) -> Optional[ReleaseInfo]:
    """Query GitHub's API for the latest release.

    Returns ReleaseInfo if a newer version is available, None if the app is
    up-to-date or the check failed (offline, API hiccup, etc.).
    """
    cache = _load_cache() if not force else None
    if cache:
        latest = cache["latest_version"]
        if not _is_newer_version(__version__, latest):
            return None
    else:
        try:
            req = urllib.request.Request(
                API_URL, headers={"User-Agent": "SaveGuard"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
            return None  # offline or API hiccup — don't block the user

        latest = data.get("tag_name", "").lstrip("v")
        if not latest or not _is_newer_version(__version__, latest):
            _save_cache(latest)
            return None

        _save_cache(latest)

    return ReleaseInfo(
        version=latest,
        url=BROWSER_URL,
        notes="",  # filled below only if we fetched fresh data
        is_prerelease=False,
    )


def fetch_release_notes(version: str) -> str:
    """Fetch release notes for a specific version tag from GitHub's API."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "SaveGuard"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("tag_name", "").lstrip("v") == version:
            return data.get("body", "")
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        pass
    return ""
