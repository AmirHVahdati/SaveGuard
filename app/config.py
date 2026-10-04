"""Persistence layer for SaveGuard. Stores the list of tracked games as JSON
inside the user's home directory so the app remembers everything between runs.
"""

import json
import os
import tempfile
from pathlib import Path

APP_DIR = Path.home() / ".saveguard"
CONFIG_FILE = APP_DIR / "games.json"
STATE_FILE = APP_DIR / "state.json"
SETTINGS_FILE = APP_DIR / "settings.json"

# Defaults for every known setting. Stored as a plain dict (rather than a
# rigid schema/dataclass) so adding a new setting in the future is just a
# matter of adding a default here and a control in the settings dialog —
# no migration code needed for people who already have a settings.json
# without that key.
DEFAULT_SETTINGS = {
    "compression_level": 6,  # 0 = no compression (fastest, largest files), 9 = max compression
    # Where newly-discovered games from a location scan get their backup
    # destination by default (each game still gets its own subfolder here).
    # Editable any time from Settings > Backups; existing games are unaffected.
    "default_scan_backup_root": str(Path.home() / "SaveGuard Backups"),
    # How many past backups to keep per game. 0 = unlimited (never delete
    # old backups) -- the default. Any positive N keeps only the N most
    # recent backups for each game, deleting older ones right after a new
    # backup completes successfully.
    "max_backups": 0,
    # Whether SaveGuard may offer to look for games the first time it is
    # opened. It asks before reading anything, so this is the switch for never
    # being asked at all: the scan walks Documents, AppData and every Steam
    # library on the machine, which is enough disk to be felt while the app is
    # still opening on a slow drive. Opt-out rather than opt-in, since the
    # offer is now a question the user has to be present for. The "Scan Games"
    # button is unaffected either way.
    "auto_scan_on_first_run": True,
}


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data) -> bool:
    """Write JSON atomically: the target file ends up either fully replaced
    with the new content or left completely untouched — never a
    half-written/corrupted file, even if the process is killed or the power
    drops mid-write. Without this, a bad-timed crash while writing
    games.json could silently wipe out someone's entire tracked-game list
    (load_games() treats a corrupt file as "no games saved").

    Returns True on success, False on any I/O failure (permission denied,
    disk full, etc.) — callers decide how to surface that rather than the
    app crashing on an uncaught exception from a UI action."""
    try:
        ensure_app_dir()
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)  # atomic on both POSIX and Windows
            return True
        except OSError:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False
    except OSError:
        return False


def load_games() -> list:
    """Return a list of raw game dicts from disk (empty list if none saved yet)."""
    ensure_app_dir()
    if not CONFIG_FILE.exists():
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except (json.JSONDecodeError, OSError):
        return []


def save_games(games: list) -> bool:
    """Persist a list of game dicts to disk. Returns True on success."""
    return _atomic_write_json(CONFIG_FILE, games)


def load_state() -> dict:
    """Small persisted app state (currently just tracks whether the one-time
    automatic first-run location scan has already happened)."""
    ensure_app_dir()
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> bool:
    return _atomic_write_json(STATE_FILE, state)


def load_settings() -> dict:
    """User-configurable preferences, merged over DEFAULT_SETTINGS so a
    settings.json written by an older version (missing newer keys) still
    works — any key it doesn't have just falls back to its default."""
    ensure_app_dir()
    settings = dict(DEFAULT_SETTINGS)
    if not SETTINGS_FILE.exists():
        return settings
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            settings.update(data)
    except (json.JSONDecodeError, OSError):
        pass
    return settings


def save_settings(settings: dict) -> bool:
    return _atomic_write_json(SETTINGS_FILE, settings)
