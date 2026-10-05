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

import errno
import os
import re
import zipfile
from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from app.scanner import build_snapshot
from app.utils import (
    clamp_compression_level, clamp_max_backups, mark_visited, path_key,
    paths_conflict,
)

# Default if no compression_level setting is passed in: zlib's own default
# level — negligible speed difference from level 1 on typical save-folder
# sizes, but consistently better compression ratio. User-configurable via
# Settings > Backups.
DEFAULT_COMPRESS_LEVEL = 6

# Matches the exact filename format backups are written with (see run()
# below). Used to enumerate a game's own archives on disk for retention
# pruning (_prune_old_backups).
#
# It used to double as a guard against a backup destination nested inside
# the save folder -- each new backup walking past its own prior archives and
# zipping them up inside itself, growing without bound. That case is now
# refused outright by the paths_conflict check in run(), and the destination
# is pruned from the walk, so this pattern is no longer load-bearing for it.
_BACKUP_FILENAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.zip$")

# Error codes that mean "this path is too long", and the POSIX equivalent.
# 206 is ERROR_FILENAME_EXCED_RANGE ("The filename or extension is too long"),
# which is what a path past MAX_PATH's 260 characters produces on a Windows
# install without long path support; 123 is ERROR_INVALID_NAME, what an
# over-long single path component produces. ENAMETOOLONG is the same complaint
# from POSIX.
#
# Deliberately narrow. ERROR_PATH_NOT_FOUND (3) also covers a directory deleted
# while the walk was running, and ERROR_ACCESS_DENIED (5) a folder the user
# simply cannot read -- neither is a path-length problem, and retrying actually
# fixes both, so treating them as one would fail backups over conditions that
# resolve themselves.
_PATH_LENGTH_WINERRORS = frozenset({206, 123})
_PATH_LENGTH_ERRNOS = frozenset({errno.ENAMETOOLONG})


class BackupCancelled(Exception):
    """Raised inside run() when an in-flight backup is asked to stop.

    A cancellation, not a failure: the partial archive is discarded the same
    way one from a failed write is, but it isn't surfaced as an error to the
    user -- the shutdown path triggers it, and "cancelled" isn't an error
    worth a modal. Raised at a file boundary only (never mid-write), so no zip
    member is ever left half-written.
    """


def _is_path_length_error(exc: OSError) -> bool:
    """True if `exc` is the filesystem reporting a path that is too long."""
    # A live Win32 call populates both attributes -- measured winerror=123
    # alongside errno=EINVAL -- so winerror is checked first and errno covers
    # the POSIX case, where it is the only one set.
    if getattr(exc, "winerror", None) in _PATH_LENGTH_WINERRORS:
        return True
    return exc.errno in _PATH_LENGTH_ERRNOS


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
        normalized = path_key(path)
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
            if normalized == path_key(base):
                return True
        except (OSError, ValueError):
            continue

    return False


def _arcname_key(name: str) -> str:
    """Comparison key for an archive entry name, matching how zipfile stores it.

    ZipFile.write doesn't store the arcname it's given: it normalizes the
    path and, where the platform separator isn't "/", replaces os.sep with
    "/" so the archive stays portable. namelist() therefore returns forward
    slashes even on Windows, while the name recorded on the way in is
    whatever os.path.relpath produced -- backslashes on Windows. Comparing
    those two directly made every nested save folder report all of its files
    as missing AND unexpected at the same time (reported on Cyberpunk 2077
    as "603 file(s) missing ... 603 unexpected entry/entries").

    Replaces os.sep rather than a literal backslash so a POSIX save file
    with a backslash in its name survives intact: there os.sep is already
    "/", making this a no-op, which is exactly right because zipfile does
    no substitution on POSIX either.
    """
    return name.replace(os.sep, "/")


def _verify_backup_archive(zip_path: str, expected_entries: List[str], game_name: str) -> None:
    """Raise OSError if the just-written archive isn't a trustworthy backup.
    Returns normally when the archive looks good.

    Without this, a backup that silently failed to write everything still
    gets a success signal, a recorded timestamp, and an "Up to date" badge
    -- and change detection would then skip that game until its saves
    changed again, so the user wouldn't find out the archive was unusable
    until they went to restore it. The case worth catching is a write that
    *looks* successful: a drive filling up, removable media acknowledging
    writes it didn't keep, a failing sector corrupting data after the OS
    reported it stored. Those are invisible from the write side.

    Three checks, cheapest first, because they aren't equally informative:

      1. the file exists and is non-empty -- catches a create that produced
         nothing at all
      2. it opens and its central directory parses -- catches truncation,
         which is what an interrupted write leaves behind
      3. every member's CRC-32 is recomputed from the stored bytes

    Check 3 is the one that actually matters, and the only one with real
    cost: it decompresses the whole archive a second time. That's a modest
    fraction of the compression the backup already spent, on a background
    thread that's doing nothing else, which is a fair price for a tool
    whose entire job is that the copy came out good. Checks 1 and 2 alone
    are NOT a substitute -- measured on a deliberately bit-flipped
    archive, they all passed: the size was right, the archive opened, and
    the entry names and count were perfect, while the stored bytes were
    garbage.

    The entry list is compared as well, because all three checks above can
    pass on an archive that is internally valid but incomplete -- a save
    file that disappeared between the walk and the write, say. A quietly
    incomplete backup is exactly as useless as a corrupt one.

    Raises OSError, which run()'s existing handler turns into a deleted
    suspect archive plus a message to the user. That's the right outcome:
    the save folder itself is never touched, so the backup can simply be
    retried.
    """
    if not os.path.isfile(zip_path):
        raise OSError(f"{game_name}: the backup file was never created.")

    try:
        size = os.path.getsize(zip_path)
    except OSError as exc:
        raise OSError(f"{game_name}: couldn't read the backup back to verify it ({exc}).") from exc
    if size <= 0:
        raise OSError(
            f"{game_name}: the backup file came out empty (0 bytes) — "
            f"the backup drive may be full."
        )

    try:
        with zipfile.ZipFile(zip_path) as zf:
            corrupted = zf.testzip()
            if corrupted is not None:
                raise OSError(
                    f"{game_name}: the backup file is corrupt — the data in "
                    f"'{corrupted}' doesn't match its checksum."
                )
            written = {_arcname_key(name) for name in zf.namelist()}
    except zipfile.BadZipFile as exc:
        raise OSError(
            f"{game_name}: the backup file couldn't be read back ({exc}). "
            f"The write may have been cut short — check free space on the "
            f"backup drive."
        ) from exc

    expected = {_arcname_key(name) for name in expected_entries}
    if written != expected:
        missing = sorted(expected - written)
        unexpected = sorted(written - expected)
        parts = []
        if missing:
            parts.append(f"{len(missing)} file(s) missing (e.g. '{missing[0]}')")
        if unexpected:
            count = len(unexpected)
            parts.append(f"{count} unexpected entr{'y' if count == 1 else 'ies'}")
        raise OSError(f"{game_name}: the backup file is incomplete — {'; '.join(parts)}.")


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
        # A cancellation flag checked between files, not mid-write. Set by
        # MainWindow when the user quits while a backup is in flight: the
        # run() loop notices at the next file boundary and raises
        # BackupCancelled, which the existing cleanup path discards the
        # in-progress archive for. Deliberately cooperative rather than
        # QThread.terminate(): killing the thread mid-zip-write would leave
        # a corrupt archive on disk and skip the handlers run() would
        # otherwise reach.
        self._cancelled = False

    def cancel(self) -> None:
        """Ask this backup to abort at the next file boundary.

        Returns immediately and never blocks the caller -- it is typically
        invoked from the main thread inside closeEvent. The worker notices the
        flag at the next file and raises BackupCancelled, which discards the
        in-progress archive the same way a verification failure would. The
        check happens between files, never inside one, so no zip member is
        ever left half-written in the archive or a file write interrupted
        mid-call.
        """
        self._cancelled = True

    def _aborted(self) -> bool:
        return self._cancelled

    def run(self):
        zip_path = None
        # Flipped once the archive has been written AND verified, so the failure
        # handler can tell a half-written archive from a complete one. Both
        # halves matter: the file exists from the moment ZipFile opens it, so
        # its mere existence says nothing about whether it holds the saves.
        archive_verified = False
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
            # Re-checked here, not just in the Add/Edit dialog. That dialog
            # validates at selection time, but this walk happens later on a
            # background thread -- a folder can be moved, renamed, replaced
            # with a link, or a tracked game loaded from a hand-edited
            # games.json in between. Walking a save folder that contains its
            # own backup destination is the unbounded-growth case, so the
            # check has to hold at the moment of use rather than when the
            # path was typed.
            if paths_conflict(self.save_path, self.backup_root):
                self.error.emit(
                    self.game_id,
                    f"“{self.game_name}” can't be backed up: its backup "
                    f"destination is the same as, or inside, its save "
                    f"folder.\n\nSave folder: {self.save_path}\n"
                    f"Backup destination: {self.backup_root}\n\n"
                    "Please pick a separate destination from Edit.",
                )
                return

            os.makedirs(self.backup_root, exist_ok=True)

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            zip_path = os.path.join(self.backup_root, f"{timestamp}.zip")

            # Normally outside the save folder entirely, since a conflicting
            # destination was just refused above — kept as containment that
            # doesn't depend on that check holding. Keyed with path_key, like
            # the conflict test above, so it still matches when the save
            # folder is reached through a symlink or junction.
            backup_root_abs = path_key(self.backup_root)

            files = []
            # Cycle guard and destination pruning, both applied by editing
            # dirnames in place so os.walk never descends into anything
            # skipped here. A directory is dropped when it's hidden, when it
            # has already been visited (a Windows junction pointing at its
            # own ancestor -- see mark_visited), or when it IS the backup
            # destination. Pruning the destination wholesale means nothing
            # inside it can reach the archive, rather than only the
            # timestamped archives this app recognises.
            visited_dirs: set = set()
            mark_visited(visited_dirs, self.save_path)
            # os.walk swallows an OSError from scandir unless it is given an
            # onerror handler, which leaves a folder it could not read
            # indistinguishable from one holding no files. That matters more
            # than it looks: _verify_backup_archive checks the archive against
            # the list built by *this* walk, so a walk that silently skipped a
            # subtree produces an archive that verifies as complete while
            # missing saves. A path-length failure is the one case worth
            # refusing over, because retrying cannot fix it and the folder's
            # depth is a property of where the game keeps its saves.
            unreachable: List[OSError] = []
            for dirpath, dirnames, filenames in os.walk(
                self.save_path, onerror=unreachable.append
            ):
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and mark_visited(visited_dirs, os.path.join(dirpath, d))
                    and path_key(os.path.join(dirpath, d)) != backup_root_abs
                ]
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
                    # The file list is a snapshot of the tree at one instant,
                    # and for a large library it is also the slow part of the
                    # walk -- so check for cancellation inside it, not just
                    # after it, so a quit request isn't blocked by the walk it
                    # is trying to end. Between files only: no file is ever read
                    # partway through.
                    if self._aborted():
                        raise BackupCancelled()
                    files.append(os.path.join(dirpath, fname))

            # A cancellation that landed during the walk is handled here, before
            # any zip is opened -- so there is no partial archive to clean up,
            # only the backup_root directory (already expected to exist) left
            # behind, which needs no special handling.
            if self._aborted():
                raise BackupCancelled()

            # Raised before the archive is opened, so a refused backup leaves
            # nothing behind at all rather than a partial .zip that looks like
            # a backup. run()'s handler turns this into an error signal, and
            # the save folder is untouched, so the fix is to retry once the
            # path is shorter or long path support is on.
            too_long = [exc for exc in unreachable if _is_path_length_error(exc)]
            if too_long:
                example = getattr(too_long[0], "filename", None) or self.save_path
                raise OSError(
                    f"“{self.game_name}” keeps part of its saves in a folder "
                    f"Windows can't reach, because the path runs past the "
                    f"260-character limit:\n\n{example}\n\n"
                    f"{len(too_long)} folder(s) would have been left out, so "
                    f"the backup would have been quietly incomplete. Nothing "
                    f"has been written and your earlier backups are "
                    f"untouched.\n\n"
                    f"Either point SaveGuard at a shorter folder, or turn on "
                    f"Windows' long path support (LongPathsEnabled under "
                    f"HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem, or "
                    f"the equivalent Group Policy setting) and restart SaveGuard."
                )

            compression, compresslevel = _compression_for_level(self.compression_level)

            total = max(len(files), 1)
            zip_kwargs = {"compresslevel": compresslevel} if compresslevel is not None else {}
            arcnames: List[str] = []
            with zipfile.ZipFile(
                zip_path, "w",
                compression=compression,
                **zip_kwargs,
            ) as zf:
                for i, src in enumerate(files, start=1):
                    if self._aborted():
                        # Mid-archive this time. The `with` closes and finalises
                        # the zip on the way out so it is a valid (partial)
                        # archive rather than a truncated file, and
                        # _discard_incomplete_archive deletes it below.
                        raise BackupCancelled()
                    rel = os.path.relpath(src, self.save_path)
                    arcnames.append(rel)
                    zf.write(src, arcname=rel)
                    self.progress.emit(self.game_id, i, total)

            # Before pruning, deliberately. Retention is about deleting the
            # OLDEST good archives, so deleting any of them on the strength
            # of a new archive that hasn't been proven good would trade a
            # recoverable failure (retry the backup; the save folder is
            # untouched) for an unrecoverable one. Callers rely on nothing
            # being pruned and no success signal being emitted unless the
            # archive verifies, so a bad backup leaves the previous ones
            # exactly as they were.
            _verify_backup_archive(zip_path, arcnames, self.game_name)
            archive_verified = True

            backups_remaining = self._prune_old_backups()

            # Fingerprint the live save folder (not the archive) — this is
            # what change detection compares against on the next launch.
            new_snapshot = build_snapshot(self.save_path)
            self.backup_finished.emit(self.game_id, new_snapshot, timestamp, zip_path, backups_remaining)
        except BackupCancelled:
            # Asked to stop (today, only by MainWindow shutting down). The
            # partial archive -- if any made it this far -- is already being
            # handled: either no zip was opened, or the `with` block above
            # finalised it as a valid-but-incomplete archive before this was
            # reached. Discard whichever case we're in and say nothing: this
            # is a deliberate abort, not a failure the user needs to act on.
            self._discard_incomplete_archive(zip_path, archive_verified)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self._discard_incomplete_archive(zip_path, archive_verified)
            self.error.emit(self.game_id, str(exc))
        except BaseException:
            # Ctrl-C or an interpreter shutdown landing mid-write. Not ours to
            # report as a backup error -- it isn't the backup's fault and the
            # caller still needs to see it -- but the half-written archive does
            # have to go. zipfile closes the file on its way out of the with
            # block, so what survives an interrupted write is not a corrupt
            # file: it opens cleanly and reads back as a valid archive holding
            # only the files written before the interruption. Left in place,
            # that is worse than nothing -- it counts as one of this game's
            # backups, and the next retention pass can spend a good older
            # archive making room for it.
            self._discard_incomplete_archive(zip_path, archive_verified)
            raise

    @staticmethod
    def _discard_incomplete_archive(zip_path: Optional[str], verified: bool) -> None:
        """Delete the archive of a backup that did not complete, so a
        half-written one can't pass for a backup.

        Skipped once the archive has verified: from that point on it is a
        complete backup, and a later failure -- pruning old ones, or
        fingerprinting the save folder -- has no business destroying it. The
        saves are safely archived at that moment, so deleting the file trades
        a recoverable failure (the backup succeeded; only housekeeping failed)
        for an unrecoverable one.

        Best-effort by design: an archive held open by something else, or on a
        drive that has gone away, must not turn one reported failure into two.
        """
        if verified or not zip_path or not os.path.exists(zip_path):
            return
        try:
            os.remove(zip_path)
        except OSError:
            pass

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
