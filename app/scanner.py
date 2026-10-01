"""Lightweight change-detection for save folders.

Rather than hashing full file contents (which can be slow for large save
files), we fingerprint each file by its size + modification time. This is
fast and reliable enough to detect "did the save data change since the last
backup" for virtually every game.

Uses os.scandir (via an explicit stack) instead of os.walk + os.stat: each
DirEntry carries cached stat info from directory enumeration, avoiding a
second, separate stat syscall per file. This matters most on Windows, where
DirEntry.stat() is effectively free (already returned by FindFirstFile).
"""

import os
from typing import Dict, List, Optional, Set

from app.utils import mark_visited

Snapshot = Dict[str, List[float]]


def build_snapshot(root_path: str) -> Snapshot:
    """Walk root_path and return {relative_path: [size, mtime]} for every file."""
    snapshot: Snapshot = {}
    if not root_path or not os.path.isdir(root_path):
        return snapshot

    # Guards against a directory cycle (most realistically a Windows
    # junction pointing at its own ancestor, which looks like a plain
    # directory to is_dir(follow_symlinks=False) and would otherwise walk
    # forever) -- see mark_visited for why identity, not the link check.
    visited: Set[tuple] = set()
    stack = [root_path]
    while stack:
        current_dir = stack.pop()
        if not mark_visited(visited, current_dir):
            continue
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
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        rel = os.path.relpath(entry.path, root_path).replace("\\", "/")
                        snapshot[rel] = [stat.st_size, round(stat.st_mtime, 2)]
                except OSError:
                    continue
    return snapshot


def snapshots_differ(old: Optional[Snapshot], new: Snapshot) -> bool:
    """True if the new snapshot has any file added, removed, or modified vs old."""
    if old is None:
        return False  # "never backed up" is tracked separately, not as a diff
    if set(old.keys()) != set(new.keys()):
        return True
    for key, value in new.items():
        if old.get(key) != value:
            return True
    return False
