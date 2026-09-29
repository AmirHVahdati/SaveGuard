"""Small shared helpers used across the app."""

import os


def sanitize_folder_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip()
    return cleaned or "Game"


def paths_conflict(path_a: str, path_b: str) -> bool:
    """True if path_a and path_b are the same directory, or if one is
    nested inside the other. Used to catch a save folder and backup
    destination that would make a backup include itself — confirmed this
    actually happens: each new backup would walk right past its own prior
    archives and zip them up inside itself, growing without bound."""
    try:
        a = os.path.normcase(os.path.normpath(os.path.abspath(path_a)))
        b = os.path.normcase(os.path.normpath(os.path.abspath(path_b)))
    except (OSError, ValueError):
        return False
    if a == b:
        return True
    return a.startswith(b + os.sep) or b.startswith(a + os.sep)


def clamp_compression_level(value, default: int = 6) -> int:
    """Coerce a compression-level setting into a valid 0-9 int, no matter
    what's actually in settings.json. zipfile raises deep inside zlib for
    out-of-range or wrong-type compresslevel values (confirmed: passing 15,
    a negative number, or a string all raise ValueError when the archive is
    closed) — and that happens on a background thread mid-backup, so a
    corrupted or hand-edited settings file could otherwise make every
    backup fail with a confusing, unrelated-looking error message."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(9, level))


def clamp_max_backups(value, default: int = 0) -> int:
    """Coerce a max-backups-to-keep setting into a valid non-negative int.
    0 means unlimited (keep every backup forever). Negative or non-numeric
    values (e.g. from a hand-edited or corrupted settings.json) fall back
    to the default rather than being passed through to the file-pruning
    logic, which would otherwise misbehave on bad input."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return default
    if count < 0:
        return default
    return min(count, 9999)  # sanity cap; nobody needs more than this
