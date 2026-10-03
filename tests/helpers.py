"""Helpers shared between test modules.

Kept out of conftest.py so individual test files can import them directly.
"""

from pathlib import Path


def write_files(root, files):
    """Create a tree of files under root and return root as a Path.

    files maps a relative path to its text content; intermediate directories
    are created as needed. A content of None creates an empty directory
    instead of a file.
    """
    root = Path(root)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.write_text(content, encoding="utf-8")
    return root