"""Lightweight change-detection for save folders.

Each file is fingerprinted by its size + modification time rather than a hash
of its contents. The mtime is kept at full nanosecond precision
(st_mtime_ns), not rounded: rounding to two decimals quantises it into 10ms
buckets, which is coarser than the ~467us granularity NTFS actually records,
and it demonstrably loses changes. Two same-size writes 6ms apart land in the
same bucket and read as unchanged; the nanosecond values differ and are caught.

Hashing contents would close that gap completely and was measured rather than
assumed: it costs about 44x more than the stat-only walk (319ms vs 7ms for a
52MB, 603-file save folder, warm cache, and far worse on a spinning disk).
build_snapshot runs once per game from MainWindow._compute_status on the GUI
thread's startup path, so that cost is paid N times over during a refresh --
seconds of frozen UI for a handful of games. A stat-only fingerprint that
catches every change the filesystem actually records is the right trade here;
a same-size, same-instant rewrite is not something mtime can catch by
definition.

Uses os.scandir (via an explicit stack) instead of os.walk + os.stat: each
DirEntry carries cached stat info from directory enumeration, avoiding a
second, separate stat syscall per file. This matters most on Windows, where
DirEntry.stat() is effectively free (already returned by FindFirstFile).
"""

import os
from typing import Dict, List, Optional, Set

from app.utils import mark_visited

# {relpath: [size, mtime_ns]}. st_mtime_ns is an int, which is fine under the
# numeric tower for a float-annotated list, and JSON round-trips it exactly.
Snapshot = Dict[str, List[float]]

# Snapshots written before mtimes gained nanosecond precision stored
# round(st_mtime, 2) instead -- whole seconds to two decimals, quantised into
# 10ms buckets. Seconds-scale values for any plausible file are ~1e9 and
# nanosecond ones ~1e18, so this threshold separates the two with a huge
# margin on both sides.
_LEGACY_SECONDS_CEILING = 1e12

# Half of one legacy bucket. A legacy entry is a rounded value and so can
# never exactly equal the precise value that replaced it; without this
# tolerance every tracked game would report as modified the first time it was
# checked after an upgrade.
_LEGACY_BUCKET_SECONDS = 0.005


def build_snapshot(root_path: str) -> Snapshot:
    """Walk root_path and return {relative_path: [size, mtime_ns]} for every file."""
    snapshot: Snapshot = {}
    if not root_path or not os.path.isdir(root_path):
        return snapshot

# Guards against a directory cycle (most realistically a Windows
    # junction pointing at its own ancestor, which looks like a plain
    # directory to is_dir(follow_symlinks=False) and would otherwise walk
    # forever) -- see mark_visited for why identity, not the link check.
    #
    # The directory is claimed when it's discovered, not when it's popped.
    # Popping means the winner of a race is whichever path reaches a
    # directory first, and a junction's path can reach one before the real
    # path does: a self-referencing junction made AutoSave-0/a.json vanish
    # from the snapshot entirely, replaced by JUNC/AutoSave-0/a.json.
    # Claiming on push makes the real path win, because a directory is
    # always discovered from its real parent before any alias can reach it.
    # Matches how BackupWorker marks the same way.
    visited: Set[tuple] = set()
    stack = [root_path]
    while stack:
        current_dir = stack.pop()
        try:
            entries = os.scandir(current_dir)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if mark_visited(visited, entry.path):
                            stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        rel = os.path.relpath(entry.path, root_path).replace("\\", "/")
                        # st_mtime_ns, not round(st_mtime, 2): the rounding
                        # collapses changes within the same 10ms bucket into
                        # "unchanged" (see the module docstring).
                        snapshot[rel] = [stat.st_size, stat.st_mtime_ns]
                except OSError:
                    continue
    return snapshot


def _same_mtime(previous, current) -> bool:
    """True if two snapshot mtimes represent the same moment.

    Both are exact nanosecond values for snapshots taken now. A snapshot
    written before that change carries round(st_mtime, 2) instead, a value
    rounded to a 10ms bucket and therefore never exactly equal to the precise
    value that replaced it -- so comparing a legacy entry against a current
    one exactly would report every tracked game as modified the first time it
    was seen after upgrading, spurring a burst of redundant backups. Legacy
    values are matched to within half a bucket instead.
    """
    previous_legacy = previous < _LEGACY_SECONDS_CEILING
    current_legacy = current < _LEGACY_SECONDS_CEILING
    if not previous_legacy and not current_legacy:
        return previous == current
    previous_s = previous if previous_legacy else previous / 1e9
    current_s = current if current_legacy else current / 1e9
    return abs(previous_s - current_s) <= _LEGACY_BUCKET_SECONDS


def snapshots_differ(old: Optional[Snapshot], new: Snapshot) -> bool:
    """True if the new snapshot has any file added, removed, or modified vs old.

    A missing baseline (old is None) counts as different. There is nothing to
    compare against, so nothing supports claiming these saves are unchanged --
    and "unchanged" is the answer that quietly stops backups happening, while
    "changed" only ever costs one redundant backup.

    This is reachable, not theoretical: last_snapshot and last_backup_time are
    persisted as two independent keys (Game.from_dict), so nothing stops a
    truncated or hand-edited games.json carrying a backup timestamp with no
    snapshot behind it. That combination used to report "synced" permanently
    -- the game looked fine, its saves changed, new files appeared, and nothing
    ever prompted another backup.

    It does not alter the "never backed up" status. MainWindow._compute_status
    returns "never" when last_backup_time is None without reaching this call,
    so for a consistently untouched game (both fields None) nothing changes.
    Returning True is what makes that ordering safe to rely on rather than
    load-bearing: if a later refactor drops or reorders the guard, the worst
    case becomes a redundant backup rather than a game that silently stops
    being backed up.
    """
    if old is None:
        return True
    if set(old.keys()) != set(new.keys()):
        return True
    for key, value in new.items():
        previous = old[key]  # key sets are equal, so this is always present
        if previous[0] != value[0] or not _same_mtime(previous[1], value[1]):
            return True
    return False
