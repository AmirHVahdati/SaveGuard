"""Runs a single backup operation on a background thread so the UI never
freezes while save files (potentially large) are being copied.

Backups are written as a single compressed .zip archive per snapshot rather
than a plain folder copy — smaller on disk, still one file per backup to
browse/restore from. Change detection is unaffected by this: it compares
file size + modified-time fingerprints stored separately in games.json
against the live save folder, and never needs to read the archive back.

Note: the result signal is named `backup_finished`, not `finished` — QThread
already defines a built-in `finished` signal that fires only once the
underlying OS thread has actually terminated, and callers rely on that for
safe cleanup (see MainWindow._on_backup_requested). Naming our own signal
`finished` would shadow it and risk destroying the thread object while it's
still technically running."""

import os
import re
import zipfile
from datetime import datetime

from PySide6.QtCore import QThread, Signal

from app.scanner import build_snapshot
from app.utils import clamp_compression_level, clamp_max_backups, mark_visited

# Default if no compression_level setting is passed in: zlib's own default
# level — negligible speed difference from level 1 on typical save-folder
# sizes, but consistently better compression ratio. User-configurable via
# Settings > Backups.
DEFAULT_COMPRESS_LEVEL = 6

# Matches the exact filename format backups are written with (see run()
# below). Used to recognize SaveGuard's own previous backups if the backup
# destination happens to sit inside the save folder being backed up —
# without this, each new backup would walk right past its own prior
# archives sitting in that folder and zip them up inside itself, growing
# without bound on every run.
_BACKUP_FILENAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.zip$")


def _compression_for_level(level: int):
    """Maps a 0-9 setting to an actual (method, compresslevel) pair.

    Levels 0-8 use DEFLATE (the standard, universally-compatible zip
    algorithm) at increasing effort. Level 9 switches to LZMA instead of
    just cranking DEFLATE higher — measured: DEFLATE's compresslevel 9
    produces byte-identical output to level 6 on real save-file-shaped data
    (its 32KB window is already saturated by then), so a plain "level 9"
    would be a no-op tier. LZMA has a much larger dictionary and models data
    differently, and measured 18-33% smaller than DEFLATE-9 on compressible
    save data, for a real but background-thread-friendly time cost (roughly
    5-10x DEFLATE's, i.e. still well under a second for a typical save
    folder). It's a standard, documented zip method (14) that Python's own
    zipfile reads back without issue and that 7-Zip handles natively — the
    one caveat is some older/minimal zip tools (and older Windows Explorer
    versions specifically) only understand DEFLATE, so a "Maximum" backup
    could need 7-Zip (or similar) to open manually on those. compresslevel
    has no effect for LZMA in Python's zipfile, so we return None for it.
    """
    if level <= 0:
        return zipfile.ZIP_STORED, None
    if level >= 9:
        return zipfile.ZIP_LZMA, None
    return zipfile.ZIP_DEFLATED, level


# Top-level POSIX system directories that must never be accepted as a game
# save folder. Compared after normalization/case-folding, so "/etc",
# "/etc/" and "/etc/." all match.
_POSIX_SYSTEM_DIRS = {
    "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/media", "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv",
    "/sys", "/usr", "/var",
}


def _is_forbidden_save_path(path: str) -> bool:
    """True if `path` is a filesystem root or a well-known system directory
    that should never be treated as a game's save folder.

    A save folder is one game's save data, so pointing this at an OS root
    or a top-level system directory means "back it up" would walk tens of
    thousands of unrelated files and try to squeeze them all into a single
    archive — at best a multi-hour hang, at worst filling the disk. Caught
    here, up front, instead of being discovered halfway through a doomed
    archive.

    Deliberately a short, fixed list rather than anything clever about
    recognising "real" save folders: the goal is only to stop the
    catastrophic case, not to second-guess legitimate save folders, which
    can live anywhere the user likes. An unresolvable path counts as
    forbidden so a broken value fails loudly instead of silently walking
    something unexpected.
    """
    try:
        normalized = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except (OSError, ValueError):
        return True  # can't even resolve it -- refuse rather than guess

    # A filesystem root ("C:\", "/", "\\\\server\\share\") is its own parent.
    # Its dirname is itself, so this catches archiving an entire volume.
    if os.path.dirname(normalized) == normalized:
        return True

    if normalized in _POSIX_SYSTEM_DIRS:
        return True

    # Windows system roots. Read from the environment when possible so this
    # follows a non-default install location; the fallbacks only apply on
    # Windows, and on POSIX they normalize to a path that can't collide with
    # a real one, so they're harmless there.
    for env_var, fallback in (
        ("SystemRoot", r"C:\Windows"),
        ("ProgramData", r"C:\ProgramData"),
    ):
        base = os.environ.get(env_var) or fallback
        try:
            if normalized == os.path.normcase(os.path.normpath(os.path.abspath(base))):
                return True
        except (OSError, ValueError):
            continue

    return False


class BackupWorker(QThread):
    progress = Signal(str, int, int)             # game_id, current, total
    backup_finished = Signal(str, dict, str, str, int)  # game_id, new_snapshot, backup_time_str, zip_path, backups_remaining
    error = Signal(str, str)                      # game_id, message

    def __init__(self, game_id: str, game_name: str, save_path: str, backup_root: str,
                 compression_level: int = DEFAULT_COMPRESS_LEVEL, max_backups: int = 0):
        super().__init__()
        self.game_id = game_id
        self.game_name = game_name
        self.save_path = save_path
        self.backup_root = backup_root
        # 0 means "no compression" (ZIP_STORED); 1-9 map to ZIP_DEFLATED's
        # compresslevel, where 1 is fastest and 9 is smallest-but-slowest.
        # Clamped defensively here too (not just at the settings-dialog
        # level) since this class shouldn't have to trust its caller —
        # zipfile raises deep inside zlib for out-of-range values, and
        # that's much harder to diagnose from inside a background thread.
        self.compression_level = clamp_compression_level(compression_level)
        # 0 = keep every backup forever; N = keep only the N most recent.
        # Same defensive clamping rationale as compression_level above.
        self.max_backups = clamp_max_backups(max_backups)

    def run(self):
        zip_path = None
        try:
            if not os.path.isdir(self.save_path):
                self.error.emit(self.game_id, "The save folder path no longer exists.")
                return
            # Checked before anything touches the disk below -- os.makedirs on
            # the backup root and the os.walk of the save folder both become
            # far more expensive than they look if this path is a system root.
            if _is_forbidden_save_path(self.save_path):
                self.error.emit(
                    self.game_id,
                    f"“{self.game_name}” has its save folder set to a system "
                    f"location, which can't be backed up:\n{self.save_path}\n\n"
                    "Please point it at the specific folder the game writes "
                    "its saves to.",
                )
                return
            if not self.backup_root:
                self.error.emit(self.game_id, "No backup destination configured.")
                return

            os.makedirs(self.backup_root, exist_ok=True)

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            zip_path = os.path.join(self.backup_root, f"{timestamp}.zip")

            # Only relevant if the backup destination happens to sit inside
            # (or exactly at) the save folder — see the constant's comment
            # above for why this matters.
            backup_root_abs = os.path.normcase(os.path.normpath(os.path.abspath(self.backup_root)))

            files = []
            # Cycle guard, applied by pruning dirnames in place so os.walk
            # never descends into a directory it has already seen. This is
            # needed because os.walk's own followlinks=False (the default
            # here) does NOT catch a Windows junction: islink() is False
            # for one, so a junction pointing back at its own ancestor
            # makes the walk recurse forever -- see mark_visited.
            visited_dirs: set = set()
            mark_visited(visited_dirs, self.save_path)
            for dirpath, dirnames, filenames in os.walk(self.save_path):
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and mark_visited(visited_dirs, os.path.join(dirpath, d))
                ]
                in_backup_root = os.path.normcase(os.path.normpath(os.path.abspath(dirpath))) == backup_root_abs
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    # Skip symlinked files so the archive can't pick up the
                    # contents of a file outside the save folder. Keeps the
                    # archive in step with build_snapshot, which already
                    # ignores these (is_file(follow_symlinks=False) is False
                    # for a link), so a symlink's target changing no longer
                    # alters the backup without ever being detected as a change.
                    if os.path.islink(os.path.join(dirpath, fname)):
                        continue
                    if in_backup_root and _BACKUP_FILENAME_PATTERN.match(fname):
                        continue  # one of our own previous backups, not save data
                    files.append(os.path.join(dirpath, fname))

            compression, compresslevel = _compression_for_level(self.compression_level)

            total = max(len(files), 1)
            zip_kwargs = {"compresslevel": compresslevel} if compresslevel is not None else {}
            with zipfile.ZipFile(
                zip_path, "w",
                compression=compression,
                **zip_kwargs,
            ) as zf:
                for i, src in enumerate(files, start=1):
                    rel = os.path.relpath(src, self.save_path)
                    zf.write(src, arcname=rel)
                    self.progress.emit(self.game_id, i, total)

            backups_remaining = self._prune_old_backups()

            # Fingerprint the live save folder (not the archive) — this is
            # what change detection compares against on the next launch.
            new_snapshot = build_snapshot(self.save_path)
            self.backup_finished.emit(self.game_id, new_snapshot, timestamp, zip_path, backups_remaining)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            # Clean up a partially-written archive so it doesn't look like a
            # valid backup.
            if zip_path and os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
            self.error.emit(self.game_id, str(exc))

    def _prune_old_backups(self) -> int:
        """Deletes the oldest backups beyond self.max_backups, if a limit is
        set. Runs on this same background thread, right after the new
        backup is written — the file list here is always small (equal to
        however many backups this one game has, typically tens at most),
        and backup filenames sort chronologically as plain strings (thanks
        to the YYYY-MM-DD_HH-MM-SS naming), so this is a cheap, one-pass
        scandir + sort + delete with no measurable cost next to the backup
        itself. A failure deleting any one old file (permission issue, file
        open elsewhere, etc.) is skipped rather than failing the backup —
        the new backup already succeeded and that's what matters most.

        Returns how many backups actually remain on disk afterward, so the
        UI's "N backups" label can reflect reality instead of just counting
        up forever regardless of pruning."""
        try:
            entries = [
                entry.name for entry in os.scandir(self.backup_root)
                if entry.is_file(follow_symlinks=False) and _BACKUP_FILENAME_PATTERN.match(entry.name)
            ]
        except OSError:
            return 1  # can't even verify -- assume just the one we made

        if self.max_backups <= 0:
            return len(entries)  # unlimited -- nothing to delete

        entries.sort()  # chronological, oldest first -- the timestamp format sorts correctly as plain text
        excess = len(entries) - self.max_backups
        if excess <= 0:
            return len(entries)

        for name in entries[:excess]:
            try:
                os.remove(os.path.join(self.backup_root, name))
            except OSError:
                pass  # best-effort; the new backup already succeeded regardless

        return len(entries) - excess
