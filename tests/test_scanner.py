"""app/scanner.py — save-folder fingerprinting and change detection.

Every test builds a real (small) folder tree in tmp_path, because the whole
point of this module is what it does against an actual filesystem.
"""

import os

import pytest

from app import scanner
from app.scanner import build_snapshot, snapshots_differ

# A fixed nanosecond timestamp, so mtime assertions never depend on the clock.
BASE_NS = 1_757_623_800_000_000_000


def set_mtime(path, ns):
    os.utime(path, ns=(ns, ns))


class TestBuildSnapshot:
    def test_missing_folder_yields_empty_snapshot(self, tmp_path):
        assert build_snapshot(str(tmp_path / "nope")) == {}

    def test_empty_string_path_yields_empty_snapshot(self):
        assert build_snapshot("") == {}

    def test_empty_folder_yields_empty_snapshot(self, tmp_path):
        (tmp_path / "empty").mkdir()
        assert build_snapshot(str(tmp_path / "empty")) == {}

    def test_top_level_file(self, tmp_path):
        target = tmp_path / "slot.sav"
        target.write_bytes(b"x" * 32)
        snapshot = build_snapshot(str(tmp_path))
        assert list(snapshot) == ["slot.sav"]
        assert snapshot["slot.sav"][0] == 32

    def test_nested_files_use_forward_slash_relative_keys(self, tmp_path):
        # Keys are normalised so a snapshot taken on Windows compares equal to
        # one taken on Linux, and so archive entry names match.
        nested = tmp_path / "SaveData" / "deep" / "slot.sav"
        nested.parent.mkdir(parents=True)
        nested.write_text("payload")
        snapshot = build_snapshot(str(tmp_path))
        assert "SaveData/deep/slot.sav" in snapshot
        assert not any("\\" in key for key in snapshot)

    def test_directories_are_not_entries(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        assert build_snapshot(str(tmp_path)) == {}

    def test_hidden_files_and_folders_are_skipped(self, tmp_path):
        (tmp_path / "visible.sav").write_text("a")
        (tmp_path / ".hidden.sav").write_text("b")
        hidden_dir = tmp_path / ".hidden_dir"
        hidden_dir.mkdir()
        (hidden_dir / "inner.sav").write_text("c")
        assert list(build_snapshot(str(tmp_path))) == ["visible.sav"]

    def test_symlinked_files_are_skipped(self, tmp_path, make_symlink):
        outside = tmp_path / "outside.sav"
        outside.write_text("secret")
        save = tmp_path / "save"
        save.mkdir()
        (save / "real.sav").write_text("kept")
        make_symlink(outside, "link.sav", parent=save)
        # Otherwise the archive could pick up a file outside the save folder
        # whose contents change without ever registering as a change.
        assert list(build_snapshot(str(save))) == ["real.sav"]

    def test_records_full_nanosecond_mtime(self, tmp_path):
        target = tmp_path / "slot.sav"
        target.write_bytes(b"x" * 8)
        set_mtime(target, BASE_NS)
        snapshot = build_snapshot(str(tmp_path))
        assert snapshot["slot.sav"][1] == BASE_NS

    def test_terminates_on_a_self_referencing_link(self, self_referencing_dir):
        # os.path.islink() is False for a Windows junction, so a link pointing
        # at its own ancestor would otherwise walk forever.
        snapshot = build_snapshot(str(self_referencing_dir))
        assert snapshot  # terminated and found something

    def test_link_never_replaces_the_real_path_in_the_snapshot(self, self_referencing_dir):
        # A directory is claimed when discovered, not when popped, so the real
        # path wins the race against an alias that reaches it first.
        snapshot = build_snapshot(str(self_referencing_dir))
        assert "sub/slot.sav" in snapshot
        assert not any(key.startswith("LINK/") for key in snapshot)

    def test_unreadable_entry_does_not_abort_the_walk(self, tmp_path, monkeypatch):
        (tmp_path / "good.sav").write_text("a")
        (tmp_path / "locked.sav").write_text("b")
        real_stat = os.DirEntry.stat

        def failing_stat(self, **kwargs):
            if self.name == "locked.sav":
                raise PermissionError("access denied")
            return real_stat(self, **kwargs)

        monkeypatch.setattr(os.DirEntry, "stat", failing_stat, raising=False)
        assert list(build_snapshot(str(tmp_path))) == ["good.sav"]

    def test_unreadable_subfolder_does_not_abort_the_walk(self, tmp_path, monkeypatch):
        (tmp_path / "good").mkdir()
        (tmp_path / "good" / "a.sav").write_text("a")
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "b.sav").write_text("b")

        real_scandir = os.scandir

        def failing_scandir(path=".", *args, **kwargs):
            if str(path).endswith("bad"):
                raise PermissionError("access denied")
            return real_scandir(path, *args, **kwargs)

        # Delegating for every other path keeps the patch contained to the one
        # folder the walk is meant to survive.
        monkeypatch.setattr(os, "scandir", failing_scandir)
        assert list(build_snapshot(str(tmp_path))) == ["good/a.sav"]


class TestSameMtime:
    def test_identical_nanosecond_values_match(self):
        assert scanner._same_mtime(BASE_NS, BASE_NS) is True

    def test_one_nanosecond_apart_is_a_change(self):
        assert scanner._same_mtime(BASE_NS, BASE_NS + 1) is False

    def test_legacy_rounded_value_matches_the_current_value(self):
        # Snapshots written before nanosecond precision stored
        # round(st_mtime, 2). Comparing that to the precise value it replaced
        # exactly would mark every tracked game as modified after an upgrade.
        current_ns = BASE_NS
        legacy_seconds = round(current_ns / 1e9, 2)
        assert scanner._same_mtime(legacy_seconds, current_ns) is True

    def test_legacy_value_beyond_half_a_bucket_is_a_change(self):
        current_ns = BASE_NS
        legacy_seconds = round(current_ns / 1e9, 2) + 0.006
        assert scanner._same_mtime(legacy_seconds, current_ns) is False

    def test_two_legacy_values_inside_one_bucket_match(self):
        assert scanner._same_mtime(1_757_623_800.120, 1_757_623_800.124) is True

    def test_two_legacy_values_in_adjacent_buckets_differ(self):
        assert scanner._same_mtime(1_757_623_800.12, 1_757_623_800.13) is False

    def test_two_legacy_values_far_apart_differ(self):
        assert scanner._same_mtime(1_757_623_800.0, 1_757_623_860.0) is False


class TestSnapshotsDiffer:
    def test_missing_baseline_counts_as_changed(self):
        assert snapshots_differ(None, {"a.sav": [1, BASE_NS]}) is True

    def test_missing_baseline_with_empty_current_snapshot_counts_as_changed(self):
        assert snapshots_differ(None, {}) is True

    def test_identical_snapshots_are_unchanged(self):
        snapshot = {"a.sav": [10, BASE_NS], "sub/b.sav": [20, BASE_NS + 5]}
        assert snapshots_differ(dict(snapshot), dict(snapshot)) is False

    def test_two_empty_snapshots_are_unchanged(self):
        assert snapshots_differ({}, {}) is False

    def test_added_file_is_a_change(self):
        old = {"a.sav": [10, BASE_NS]}
        new = {"a.sav": [10, BASE_NS], "b.sav": [20, BASE_NS]}
        assert snapshots_differ(old, new) is True

    def test_removed_file_is_a_change(self):
        old = {"a.sav": [10, BASE_NS], "b.sav": [20, BASE_NS]}
        new = {"a.sav": [10, BASE_NS]}
        assert snapshots_differ(old, new) is True

    def test_renamed_file_counts_as_removed_plus_added(self):
        old = {"a.sav": [10, BASE_NS]}
        new = {"a.sav.bak": [10, BASE_NS]}
        assert snapshots_differ(old, new) is True

    def test_changed_size_is_a_change(self):
        old = {"a.sav": [10, BASE_NS]}
        new = {"a.sav": [11, BASE_NS]}
        assert snapshots_differ(old, new) is True

    def test_changed_mtime_is_a_change(self):
        old = {"a.sav": [10, BASE_NS]}
        new = {"a.sav": [10, BASE_NS + 1]}
        assert snapshots_differ(old, new) is True

    def test_same_size_rewrite_within_one_legacy_bucket_is_still_a_change(self, tmp_path):
        # Rounding mtimes to two decimals quantises them into 10ms buckets,
        # which is coarser than the granularity the filesystem actually
        # records: two same-size writes 6ms apart used to read as unchanged.
        target = tmp_path / "slot.sav"
        target.write_bytes(b"A" * 100)
        set_mtime(target, BASE_NS)
        before = build_snapshot(str(tmp_path))

        target.write_bytes(b"B" * 100)  # identical size on purpose
        set_mtime(target, BASE_NS + 6_000_000)
        after = build_snapshot(str(tmp_path))

        assert snapshots_differ(before, after) is True

    def test_upgraded_app_does_not_report_every_game_as_modified(self, tmp_path):
        target = tmp_path / "slot.sav"
        target.write_bytes(b"A" * 100)
        set_mtime(target, BASE_NS)
        current = build_snapshot(str(tmp_path))
        legacy = {"slot.sav": [100, round(BASE_NS / 1e9, 2)]}
        assert snapshots_differ(legacy, current) is False

    def test_legacy_baseline_still_detects_a_real_change(self, tmp_path):
        target = tmp_path / "slot.sav"
        target.write_bytes(b"A" * 100)
        set_mtime(target, BASE_NS)
        legacy = {"slot.sav": [100, round(BASE_NS / 1e9, 2)]}

        target.write_bytes(b"A" * 200)
        set_mtime(target, BASE_NS + 5_000_000_000)
        assert snapshots_differ(legacy, build_snapshot(str(tmp_path))) is True


class TestRealFolderWorkflow:
    """The end-to-end intent: back up, no change reported; save a game, change
    reported; back up again, quiet again."""

    def test_backup_then_edit_then_backup(self, tmp_path):
        save = tmp_path / "saves"
        save.mkdir()
        target = save / "slot.sav"
        target.write_text("first")
        set_mtime(target, BASE_NS)

        baseline = build_snapshot(str(save))
        assert snapshots_differ(baseline, build_snapshot(str(save))) is False

        target.write_text("second but longer")
        set_mtime(target, BASE_NS + 10_000_000_000)
        changed = build_snapshot(str(save))
        assert snapshots_differ(baseline, changed) is True

        assert snapshots_differ(changed, build_snapshot(str(save))) is False