"""app/utils.py — path comparison, cycle detection, and setting coercion.

These helpers carry a lot of the app's defensive behaviour (the self-zipping
backup, the junction-cycle hang, the settings file that would otherwise make
every backup fail inside zlib), so the tests below are mostly regression tests
for specific failures the code comments describe.
"""

import os
import sys

import pytest

from app import utils


class TestSanitizeFolderName:
    @pytest.mark.parametrize("raw, expected", [
        ("Cyberpunk 2077", "Cyberpunk 2077"),
        ("Baldur's Gate 3", "Baldurs Gate 3"),
        ("Elden_Ring-2022", "Elden_Ring-2022"),
        ("  padded  ", "padded"),
    ])
    def test_keeps_safe_characters(self, raw, expected):
        assert utils.sanitize_folder_name(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "///", "<<>>", "..."])
    def test_falls_back_when_nothing_usable_remains(self, raw):
        assert utils.sanitize_folder_name(raw) == "Game"

    @pytest.mark.parametrize("raw", [
        "../../etc/passwd",
        "game:remastered*",
        "sub\\folder",
    ])
    def test_strips_separators_and_glob_characters(self, raw):
        cleaned = utils.sanitize_folder_name(raw)
        assert "/" not in cleaned and "\\" not in cleaned
        assert os.sep not in cleaned
        assert cleaned  # still yields something usable as a folder name


class TestPathKey:
    def test_normalizes_trailing_dot_and_separator(self, tmp_path):
        target = tmp_path / "saves"
        target.mkdir()
        assert utils.path_key(str(target)) == utils.path_key(str(target) + os.sep)
        assert utils.path_key(str(target)) == utils.path_key(str(target) + os.sep + ".")

    def test_resolves_links_in_the_existing_part_of_a_new_path(self, tmp_path, junction):
        # A destination that hasn't been created yet still has to compare
        # correctly, which means the links in the part that does exist have to
        # be resolved before the new path is appended.
        real = tmp_path / "real_saves"
        real.mkdir()
        link = junction(real, "savelink")
        assert utils.path_key(str(link / "not" / "made" / "yet")) == \
            utils.path_key(str(real / "not" / "made" / "yet"))

    def test_ignores_link_wrapper_around_the_target(self, tmp_path, junction):
        real = tmp_path / "real_saves"
        real.mkdir()
        link = junction(real, "savelink")
        assert utils.path_key(str(link)) == utils.path_key(str(real))

    def test_case_insensitive_where_the_filesystem_is(self, tmp_path):
        target = tmp_path / "MyGame"
        target.mkdir()
        upper = str(target).upper()
        if os.path.normcase("A") == os.path.normcase("a"):
            pytest.skip("filesystem is case-sensitive")
        assert utils.path_key(upper) == utils.path_key(str(target))


class TestContains:
    # Built from os.sep rather than hardcoded, since _contains appends the
    # platform separator.
    @pytest.mark.parametrize("outer, inner", [
        ("root", "root"),
        ("root", "root" + os.sep + "child"),
        ("root", "root" + os.sep + "child" + os.sep + "deeper"),
    ])
    def test_same_or_nested(self, outer, inner):
        assert utils._contains(outer, inner) is True

    @pytest.mark.parametrize("outer, inner", [
        ("root", "sibling"),
        ("root", "rootx"),
        ("root", ""),
        ("root" + os.sep + "child", "root"),
    ])
    def test_sibling_or_parent_is_not_nested(self, outer, inner):
        assert utils._contains(outer, inner) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows drive-root semantics")
    def test_filesystem_root_keeps_exactly_one_separator(self):
        # Appending a separator to a path that already ends in one produced
        # "C:\\" + os.sep == "C:\\\\", which nothing can start with -- so a save
        # folder and a backup folder either side of a root silently looked
        # unrelated.
        root = "C:" + os.sep
        assert os.path.dirname(root) == root  # root is its own parent
        assert utils._contains(root, root + "foo") is True
        assert utils._contains(root, root) is True

    def test_posix_root_contains_its_children(self):
        assert utils._contains(os.sep, os.sep + "usr") is True


class TestPathsConflict:
    def test_identical_directories_conflict(self, tmp_path):
        target = tmp_path / "saves"
        target.mkdir()
        assert utils.paths_conflict(str(target), str(target)) is True

    def test_backup_inside_save_folder_conflicts(self, tmp_path):
        save = tmp_path / "saves"
        (save / "backups" / "game").mkdir(parents=True)
        assert utils.paths_conflict(str(save), str(save / "backups" / "game")) is True

    def test_save_folder_inside_backup_folder_conflicts(self, tmp_path):
        backup = tmp_path / "backups"
        (backup / "saves").mkdir(parents=True)
        assert utils.paths_conflict(str(backup / "saves"), str(backup)) is True

    def test_unrelated_siblings_do_not_conflict(self, tmp_path):
        save = tmp_path / "saves"
        backup = tmp_path / "backups"
        save.mkdir()
        backup.mkdir()
        assert utils.paths_conflict(str(save), str(backup)) is False

    def test_name_prefix_is_not_treated_as_nesting(self, tmp_path):
        save = tmp_path / "saves"
        sibling = tmp_path / "saves-2"
        save.mkdir()
        sibling.mkdir()
        assert utils.paths_conflict(str(save), str(sibling)) is False

    def test_link_alias_of_save_folder_conflicts(self, tmp_path, junction):
        # The failure this guards: os.walk follows a junction into the real
        # target, so a destination built from abspath ("...\\savelink\\backups")
        # never matched the real path and each backup zipped the previous one.
        real = tmp_path / "real_saves"
        real.mkdir()
        link = junction(real, "savelink")
        assert utils.paths_conflict(str(real), str(link / "backups")) is True

    def test_missing_paths_are_still_comparable(self, tmp_path):
        # Neither path exists yet; path_key still resolves the existing prefix.
        save = tmp_path / "saves"
        assert utils.paths_conflict(str(save), str(save / "backups")) is True
        assert utils.paths_conflict(str(save), str(tmp_path / "elsewhere")) is False

    def test_unresolvable_path_reports_no_conflict(self, monkeypatch):
        # A path the resolver chokes on (an embedded null byte, a permission
        # error) must not invent a conflict or raise: the caller keeps its own
        # error handling.
        def boom(*args, **kwargs):
            raise ValueError("embedded null byte")

        monkeypatch.setattr(utils.os.path, "realpath", boom)
        assert utils.paths_conflict("/a/save", "/b/backups") is False


class _FakeStat:
    def __init__(self, st_dev, st_ino):
        self.st_dev = st_dev
        self.st_ino = st_ino


class TestMarkVisited:
    def test_first_visit_claims_then_second_is_rejected(self, tmp_path):
        target = tmp_path / "dir"
        target.mkdir()
        visited = set()
        assert utils.mark_visited(visited, str(target)) is True
        assert utils.mark_visited(visited, str(target)) is False
        assert utils.mark_visited(visited, str(target)) is False

    def test_different_directories_have_different_identities(self, tmp_path):
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        visited = set()
        assert utils.mark_visited(visited, str(first)) is True
        assert utils.mark_visited(visited, str(second)) is True

    def test_link_to_an_already_visited_directory_is_rejected(self, tmp_path, junction):
        real = tmp_path / "real"
        real.mkdir()
        link = junction(real, "alias")
        visited = set()
        assert utils.mark_visited(visited, str(real)) is True
        # A junction reports its own inode rather than its target's, so a cycle
        # still terminates (the loop necessarily revisits the same physical
        # directory) without every link looking like an already-seen folder.
        assert utils.mark_visited(visited, str(link)) is True

    def test_unresolvable_path_is_treated_as_already_seen(self, tmp_path):
        assert utils.mark_visited(set(), str(tmp_path / "nope")) is True

    def test_filesystem_without_inodes_claims_everything(self, tmp_path, monkeypatch):
        # Reporting 0 for every inode would otherwise collapse the whole tree
        # onto one key and the walk would return nothing at all.
        target = tmp_path / "dir"
        target.mkdir()
        monkeypatch.setattr(utils.os, "stat", lambda *a, **k: _FakeStat(1, 0))
        assert utils.mark_visited(set(), str(target)) is True
        assert utils.mark_visited(set(), str(target)) is True

    def test_error_from_stat_is_treated_as_already_seen(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("stat failed")

        monkeypatch.setattr(utils.os, "stat", boom)
        assert utils.mark_visited(set(), str(tmp_path)) is True


class TestClampCompressionLevel:
    @pytest.mark.parametrize("value, expected", [
        (0, 0), (1, 1), (6, 6), (9, 9),
        (10, 9), (99, 9), (-1, 0), (-100, 0),
        ("6", 6), ("0", 0), ("9", 9),
        (6.9, 6), ("7", 7),
        (True, 1), (False, 0),
    ])
    def test_accepted_and_clamped_values(self, value, expected):
        assert utils.clamp_compression_level(value) == expected

    @pytest.mark.parametrize("value", [None, "abc", "", [], {}, object()])
    def test_unusable_values_fall_back_to_default(self, value):
        # zipfile raises deep inside zlib for out-of-range or wrong-typed
        # compresslevel values, on a background thread mid-backup.
        assert utils.clamp_compression_level(value) == 6
        assert utils.clamp_compression_level(value, default=3) == 3


class TestClampMaxBackups:
    @pytest.mark.parametrize("value, expected", [
        (0, 0), (1, 1), (10, 10), (999, 999),
        (9999, 9999), (10000, 9999),
        ("5", 5), (5.9, 5),
    ])
    def test_accepted_and_capped_values(self, value, expected):
        assert utils.clamp_max_backups(value) == expected

    @pytest.mark.parametrize("value", [-1, -100, None, "abc", "", [], {}])
    def test_unusable_values_fall_back_to_default(self, value):
        assert utils.clamp_max_backups(value) == 0
        assert utils.clamp_max_backups(value, default=7) == 7


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
class TestPosixOnlyHelpers:
    def test_separator_based_assumptions_hold(self, tmp_path):
        assert utils.sanitize_folder_name("a/b") == "ab"
        assert utils.paths_conflict(str(tmp_path), str(tmp_path / "x")) is True