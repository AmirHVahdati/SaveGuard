"""Small shared helpers used across the app."""

import os


def sanitize_folder_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip()
    return cleaned or "Game"


def path_key(path: str) -> str:
    """Canonical comparison key for "are these the same directory, or is one
    inside the other" questions. Single source of truth for every such
    comparison in the app.

    Resolves links via os.path.realpath, not os.path.abspath, and that
    distinction is load-bearing rather than pedantic. os.walk *follows* a
    symlinked or junctioned save folder into its real target, so a key
    built from abspath compares "...\\savelink\\backups" against
    "...\\real_save\\backups", never matches, and silently fails to prune
    the backup destination -- letting each backup zip the previous one,
    growing without bound. Verified: a save folder reached through a
    junction bypassed the conflict check entirely, because abspath left
    the link in the string while the walk had already stepped through it.

    realpath also normalizes as it goes, so normpath/abspath calls around
    it are redundant. Paths that don't exist yet still resolve their
    existing prefix, which is all a pre-creation comparison needs.
    """
    return os.path.normcase(os.path.realpath(path))


def comparable_path_key(path: str) -> str:
    """path_key for equality comparisons, with a textual fallback.

    path_key resolves the filesystem, so it can raise on an exotic path (an
    unresolvable one on Windows, a symlink loop). A duplicate-detection pass
    runs over every candidate on every scan, where one unreadable path must
    not abandon the whole comparison -- and where comparing the raw text is
    still better than not comparing at all.

    Deliberately not folded into path_key: paths_conflict treats an
    unresolvable path as "no conflict", and quietly turning that into a
    textual comparison would let a save folder and a backup destination that
    merely look different be judged unrelated.
    """
    try:
        return path_key(path)
    except (OSError, ValueError):
        return os.path.normcase(os.path.normpath(path))


def _contains(outer: str, inner: str) -> bool:
    """True if `inner` is `outer` itself or lives somewhere beneath it.

    The trailing separator is only appended when it isn't already there.
    normpath keeps exactly one on a filesystem root, so appending os.sep
    unconditionally produced a doubled separator ('C:\\\\', '//') that no
    real path can start with -- which silently reported "no conflict" for
    a save folder and a backup destination sitting either side of a root
    (C:\\ vs C:\\foo, / vs /foo). Those are ordinary layouts, not
    hypotheticals: a Proton prefix root or a Steam library root is a
    plausible parent for a save folder.
    """
    if inner == outer:
        return True
    if not outer.endswith(os.sep):
        outer += os.sep
    return inner.startswith(outer)


def paths_conflict(path_a: str, path_b: str) -> bool:
    """True if path_a and path_b are the same directory, or if one is
    nested inside the other. Used to catch a save folder and backup
    destination that would make a backup include itself — confirmed this
    actually happens: each new backup would walk right past its own prior
    archives and zip them up inside itself, growing without bound.

    Call this at the moment the paths are about to be used, not only when
    they're first entered: BackupWorker re-checks before walking (see
    backup_worker.run), because a destination validated in the Add/Edit
    dialog can still be nested inside the save folder by the time the
    background backup thread actually starts."""
    try:
        a = path_key(path_a)
        b = path_key(path_b)
    except (OSError, ValueError):
        return False
    return _contains(a, b) or _contains(b, a)


def mark_visited(visited: set, path: str) -> bool:
    """True the first time `path` is seen, False if already visited.

    Directory identity is (st_dev, st_ino), which is what makes this
    useful against the cycle case that the `follow_symlinks=False` checks
    don't actually catch: on Windows a junction is reported by the OS as
    an ordinary directory, so `os.path.islink()` is False and os.walk's
    own followlinks=False gate lets os.walk descend into it. A junction
    pointing back at its own ancestor therefore walks forever
    (confirmed: mklink /J A/JUNC A makes both os.walk and build_snapshot
    recurse JUNC\\JUNC\\JUNC... indefinitely). Keying on the resolved
    identity catches that regardless of how the loop is spelled, and
    works the same way for POSIX symlink loops.

    A junction reports its own st_ino rather than its target's, but a
    cycle still terminates because the loop necessarily revisits the same
    physical directory, which necessarily yields the same ino.

    Filesystems that report st_ino as 0 give up the protection rather than
    risk skipping legitimate directories — every entry would otherwise
    collapse onto the same (dev, 0) key and the walk would return nothing
    at all. Same for an unresolvable path: this only reports whether the
    path was already seen, so callers keep their own error handling.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError):
        return True
    if not getattr(st, "st_ino", 0):
        return True
    key = (st.st_dev, st.st_ino)
    if key in visited:
        return False
    visited.add(key)
    return True


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
