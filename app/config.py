"""Persistence layer for SaveGuard. Stores the list of tracked games as JSON
inside the user's home directory so the app remembers everything between runs.
"""

import json
import os
import tempfile
from pathlib import Path

APP_DIR = Path.home() / ".saveguard"
CONFIG_FILE = APP_DIR / "games.json"
CONFIG_CORRUPT_FILE = APP_DIR / "games.json.corrupt"
CONFIG_BAK_FILE = APP_DIR / "games.json.bak"
STATE_FILE = APP_DIR / "state.json"
SETTINGS_FILE = APP_DIR / "settings.json"

# How many backup copies of games.json to keep beside the live one. One is
# enough: it is the file from before the last successful write, so it is
# always the most recent readable version and never more than one write
# behind. More would only add disk traffic for a copy nobody would use.
_BAK_COUNT = 1

# Set the first time a games.json fails to parse, and never cleared again
# for the life of the process. While it is set, save_games() refuses to
# write: the in-memory list is known to be incomplete (whatever was in the
# corrupt file is gone), so writing it out would destroy the only surviving
# copy on disk -- the .corrupt one -- without the user ever being told.
_games_corrupt: bool = False


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


def _atomic_write_json(path: Path, data, rotate_bak: Path = None) -> bool:
    """Write JSON atomically: the target file ends up either fully replaced
    with the new content or left completely untouched — never a
    half-written/corrupted file, even if the process is killed or the
    power drops mid-write. Without this, a bad-timed crash while writing
    games.json could silently wipe out someone's entire tracked-game list
    (load_games() treats a corrupt file as "no games saved").

    When `rotate_bak` is given, the file currently at `path` is first
    copied there, so a write that goes wrong still leaves the previous
    contents on disk one rename away. That copy is what makes recovery
    from a bad write possible at all: os.replace() is atomic, but it
    replaces rather than preserves, so without a backup the old contents
    exist only until the moment the new ones land.

    Returns True on success, False on any I/O failure (permission denied,
    disk full, etc.) — callers decide how to surface that rather than the
    app crashing on an uncaught exception from a UI action."""
    try:
        ensure_app_dir()
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            if rotate_bak is not None and os.path.exists(str(path)):
                _copy_to_backup(path, rotate_bak)
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


def _copy_to_backup(path: Path, backup: Path) -> None:
    """Copy the current contents of `path` to `backup`, keeping at most
    _BAK_COUNT generations. Best-effort: a failed backup copy must not
    fail the write it is protecting, so errors are swallowed here and the
    write proceeds on the strength of its own atomic replace alone.

    The copy is taken before the replace, not after, because the replace
    is the step that destroys the previous contents. After it, the old
    data is gone and a .bak would only hold the new data -- a backup of
    the very thing that was just written, which is worth nothing.
    """
    try:
        backup.write_bytes(path.read_bytes())
    except OSError:
        return
    # Prune older generations so the folder doesn't accumulate one file
    # per save forever. Numbered suffixes keep the ordering unambiguous
    # (games.json.bak.2 is older than games.json.bak.1) and sort the way
    # the numbers read, so the highest number is always the oldest.
    for index in range(_BAK_COUNT, 0, -1):
        older = backup.with_name(f"{backup.name}.{index}")
        if older.exists():
            try:
                os.remove(str(older))
            except OSError:
                pass
    for index in range(_BAK_COUNT, 1, -1):
        newer = backup.with_name(f"{backup.name}.{index - 1}")
        older = backup.with_name(f"{backup.name}.{index}")
        if newer.exists():
            try:
                os.replace(str(newer), str(older))
            except OSError:
                pass


def _quarantine_corrupt_games(path: Path) -> None:
    """Move an unparseable games.json aside to games.json.corrupt.

    Renaming rather than deleting is the whole point: the file's
    contents are still the user's data, just unreadable as JSON,
    and a hand-edited or partially-recovered file may still be
    salvageable by opening it in a text editor. Leaving it under
    its own name would make the next load try to parse it again
    and fail the same way, so it is moved to a fixed, distinct
    name the app can point the user at.

    A rename collision (a games.json.corrupt from an earlier
    incident) is resolved by keeping the newer one: the file being
    quarantined now is the one most likely to be recoverable, and
    the older quarantine has already had its chance.
    """
    try:
        if CONFIG_CORRUPT_FILE.exists():
            CONFIG_CORRUPT_FILE.unlink()
        os.replace(str(path), str(CONFIG_CORRUPT_FILE))
    except OSError:
        # Best-effort: if the rename fails the corrupt file stays
        # where it is, and _games_corrupt still stops save_games
        # from overwriting it, so the data is preserved either way.
        pass


def load_games() -> list:
    """Return a list of raw game dicts from disk (empty list if none saved yet).

    A file that exists but does not parse is treated as corrupt,
    not as "no games saved". The two used to be indistinguishable,
    and that indistinguishability was the data-loss bug: the empty
    list this returned was then written straight back by _persist()
    on the next colour change or backup, replacing the corrupt
    original with an empty file and losing whatever the user had.
    Now the corrupt file is quarantined, a process-wide flag is
    set, and save_games() refuses to write until the app is
    restarted -- so the user sees the problem instead of losing
    their list to a silent overwrite.

    Returns the games, or [] when there is no file yet or the
    file is corrupt (in which case `games_were_corrupt()` is True)."""
    global _games_corrupt
    ensure_app_dir()
    if not CONFIG_FILE.exists():
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            # A file that parses but isn't a list is just as
            # unusable as one that doesn't parse: callers expect a
            # list, and anything else would break the dict
            # comprehension downstream. Quarantine it the same way.
            _quarantine_corrupt_games(CONFIG_FILE)
            _games_corrupt = True
            return []
    except (json.JSONDecodeError, OSError):
        _quarantine_corrupt_games(CONFIG_FILE)
        _games_corrupt = True
        return []


def games_were_corrupt() -> bool:
    """True if games.json failed to parse during this process.

    Read by MainWindow at startup to warn the user and by
    save_games() to refuse to persist. Never cleared within a
    process: the in-memory list was built without the corrupt
    file's games, so writing it back would drop them, and the
    flag has to survive for as long as that incomplete list is
    the app's only copy. A restart clears it -- after a restart
    games.json.corrupt is no longer at CONFIG_FILE, so a fresh
    load is genuinely a clean one.
    """
    return _games_corrupt


def save_games(games: list) -> bool:
    """Persist a list of game dicts to disk. Returns True on success.

    Refuses to write while games_were_corrupt() is True. The
    caller's list is known to be missing whatever was in the
    corrupt file, so writing it would overwrite the only surviving
    copy -- the quarantined one -- with an incomplete list and
    make the loss permanent. Returning False rather than raising
    keeps the call site (MainWindow._persist) in control of how
    the failure is reported, consistent with every other write
    failure this module already reports that way.
    """
    if _games_corrupt:
        return False
    return _atomic_write_json(CONFIG_FILE, games, rotate_bak=CONFIG_BAK_FILE)


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
