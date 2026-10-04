"""app/backup_worker.py — writing, verifying, and pruning a backup archive.

Two things are worth knowing about how these tests drive the worker:

* run() is called directly instead of start(). BackupWorker is a QThread, but
  its run() is an ordinary method and its signals are emitted on the calling
  thread, so the conftest run_backup fixture can assert on them the moment
  run() returns -- no event loop, no waiting, no flakes.
* The archive is inspected the way a user would: opened as a zip, its members
  listed, and its CRCs re-checked. That's the same inspection
  _verify_backup_archive performs, so a passing backup here means a passing
  backup in the app.
"""

import errno
import os
import sys
import zipfile
from pathlib import Path

import pytest

from app.backup_worker import (
    BackupWorker, _arcname_key, _BACKUP_FILENAME_PATTERN,
    _compression_for_level, _is_forbidden_save_path, _is_path_length_error,
    _verify_backup_archive,
)
from app.scanner import build_snapshot


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def filesystem_root():
    """The root of the filesystem holding the temp directory (e.g. C:\\ or /)."""
    drive, _ = os.path.splitdrive(str(Path(os.sep).anchor or os.sep))
    if drive:
        return drive + os.sep
    return os.sep


def archive_like_the_worker(save_root, archive_path, compression=zipfile.ZIP_DEFLATED):
    """Build an archive the way BackupWorker.run() does, including arcnames
    taken straight from os.path.relpath (backslashes on Windows).

    Returns the list of arcnames written.
    """
    sources = []
    for dirpath, _dirnames, filenames in os.walk(str(save_root)):
        for name in filenames:
            sources.append(os.path.join(dirpath, name))
    arcnames = [os.path.relpath(src, str(save_root)) for src in sources]
    with zipfile.ZipFile(str(archive_path), "w", compression=compression) as zf:
        for src, arcname in zip(sources, arcnames):
            zf.write(src, arcname=arcname)
    return arcnames


def flip_first_member_byte(archive_path):
    """Corrupt the stored bytes of the first member, leaving the zip structure
    (central directory, entry names, sizes) perfectly intact.

    This is the failure the CRC check exists for: a write that reports success
    but stores bytes that aren't what was read -- a lying drive, a failing
    sector, removable media that acknowledges writes it didn't keep.
    """
    raw = bytearray(Path(archive_path).read_bytes())
    assert raw[:4] == b"PK\x03\x04", "expected a local file header first"
    name_len = int.from_bytes(raw[26:28], "little")
    extra_len = int.from_bytes(raw[28:30], "little")
    data_offset = 30 + name_len + extra_len
    raw[data_offset] ^= 0xFF
    Path(archive_path).write_bytes(bytes(raw))


def seed_backups(backup_root, count, first_year=2000):
    """Create `count` older timestamped archives (contents don't matter for
    retention; they're never read). Years are far enough in the past that the
    suite keeps behaving correctly once the calendar moves on."""
    backup_root.mkdir(parents=True, exist_ok=True)
    names = []
    for index in range(count):
        name = f"{first_year + index:04d}-01-01_00-00-00.zip"
        (backup_root / name).write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        names.append(name)
    return names


def backup_names(backup_root):
    return sorted(p.name for p in Path(backup_root).iterdir() if p.is_file())


def win_error(winerror, err=errno.EINVAL, msg="synthetic"):
    """An OSError shaped like a real one produced by Windows.

    CPython fills *both* attributes from a live Win32 call -- measured
    winerror=123 alongside errno=EINVAL -- so a test that sets only errno, as
    OSError(206, "...") does, doesn't resemble the real thing and would
    misreport what the predicate is being asked to classify.
    """
    exc = OSError(err, msg)
    exc.winerror = winerror
    return exc


def scandir_failing_for(subdir, exc):
    """A scandir replacement that raises `exc` for one subdirectory only.

    Long paths can't be reproduced by building a deep tree on a machine with
    long path support enabled (this one has it on), and MAX_PATH is not the
    only thing that makes a folder unreadable, so the failure is injected at
    the same point the OS would raise it: the scandir that os.walk makes while
    descending. `exc.filename` is filled in the way a real one is, since the
    error message reports it back to the user.
    """
    real_scandir = os.scandir

    def fake_scandir(path):
        path = str(path)
        if os.path.basename(path) == subdir:
            exc.filename = path
            raise exc
        return real_scandir(path)

    return fake_scandir


def archived_names(result):
    with zipfile.ZipFile(result.finished.last[3]) as zf:
        return sorted(_arcname_key(name) for name in zf.namelist())


# ---------------------------------------------------------------------------
# compression mapping
# ---------------------------------------------------------------------------

class TestCompressionForLevel:
    @pytest.mark.parametrize("level, expected", [
        (0, (zipfile.ZIP_STORED, None)),
        (1, (zipfile.ZIP_DEFLATED, 1)),
        (6, (zipfile.ZIP_DEFLATED, 6)),
        (8, (zipfile.ZIP_DEFLATED, 8)),
        # 9 switches algorithm instead of being a DEFLATE no-op tier.
        (9, (zipfile.ZIP_LZMA, None)),
        (12, (zipfile.ZIP_LZMA, None)),
    ])
    def test_level_maps_to_method_and_effort(self, level, expected):
        assert _compression_for_level(level) == expected

    def test_lzma_needs_no_compresslevel(self):
        # Passing compresslevel with LZMA is not just ignored, it's meaningless.
        assert _compression_for_level(9)[1] is None

    def test_stored_needs_no_compresslevel(self):
        assert _compression_for_level(0)[1] is None

    @pytest.mark.parametrize("level, method", [
        (0, zipfile.ZIP_STORED),
        (6, zipfile.ZIP_DEFLATED),
        (9, zipfile.ZIP_LZMA),
    ])
    def test_written_archive_uses_the_mapped_method(self, save_root, backup_root,
                                                    run_backup, level, method):
        result = run_backup(save_root, backup_root, compression_level=level)
        assert result.finished, result.errors.last
        with zipfile.ZipFile(result.finished.last[3]) as zf:
            assert zf.getinfo("slot1.sav").compress_type == method

    def test_lzma_is_smaller_than_deflate_on_save_shaped_data(self, tmp_path, run_backup):
        save = tmp_path / "saves"
        save.mkdir()
        # Repetitive content, like the XML/JSON blobs games actually write.
        (save / "chunk.sav").write_text("a" * 200_000)
        for level in (8, 9):
            result = run_backup(save, tmp_path / f"out{level}", compression_level=level)
            assert result.finished, result.errors.last
        deflate_size = (tmp_path / "out8").glob("*.zip").__next__().stat().st_size
        lzma_size = (tmp_path / "out9").glob("*.zip").__next__().stat().st_size
        assert lzma_size < deflate_size


# ---------------------------------------------------------------------------
# refusing catastrophic save paths
# ---------------------------------------------------------------------------

class TestIsForbiddenSavePath:
    def test_filesystem_root_is_forbidden(self):
        assert _is_forbidden_save_path(filesystem_root()) is True

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="POSIX /etc has no equivalent resolution on Windows")
    @pytest.mark.parametrize("path", ["/etc", "/usr", "/", "/var", "/home"])
    def test_posix_system_directories_are_forbidden(self, path):
        assert _is_forbidden_save_path(path) is True

    def test_windows_directory_is_forbidden(self, tmp_path, monkeypatch):
        windows = tmp_path / "Windows"
        windows.mkdir()
        monkeypatch.setenv("SystemRoot", str(windows))
        assert _is_forbidden_save_path(str(windows)) is True

    def test_program_data_is_forbidden(self, tmp_path, monkeypatch):
        program_data = tmp_path / "ProgramData"
        program_data.mkdir()
        monkeypatch.setenv("ProgramData", str(program_data))
        assert _is_forbidden_save_path(str(program_data)) is True

    def test_system_root_follows_a_non_default_install_location(self, tmp_path, monkeypatch):
        # Read from the environment rather than hardcoded, so an install
        # somewhere other than C:\Windows is still recognised.
        elsewhere = tmp_path / "WinNT"
        elsewhere.mkdir()
        monkeypatch.setenv("SystemRoot", str(elsewhere))
        assert _is_forbidden_save_path(str(elsewhere)) is True

    def test_ordinary_folder_is_allowed(self, save_root):
        assert _is_forbidden_save_path(str(save_root)) is False

    def test_folder_merely_containing_a_system_name_is_allowed(self, tmp_path):
        decoy = tmp_path / "Windows"
        decoy.mkdir()
        save = decoy / "SomeGame"
        save.mkdir()
        assert _is_forbidden_save_path(str(save)) is False

    def test_missing_folder_is_allowed(self, tmp_path):
        # A save folder on an unplugged drive must not be treated as a system
        # location; the "path no longer exists" error covers it instead.
        assert _is_forbidden_save_path(str(tmp_path / "not_mounted")) is False

    def test_unresolvable_path_is_refused(self, monkeypatch):
        def boom(*args, **kwargs):
            raise ValueError("unresolvable")

        import app.backup_worker as worker_module
        monkeypatch.setattr(worker_module, "path_key", boom)
        assert _is_forbidden_save_path(str(Path.cwd())) is True


# ---------------------------------------------------------------------------
# archive verification
# ---------------------------------------------------------------------------

class TestVerifyBackupArchive:
    def test_accepts_a_complete_archive(self, save_root, tmp_path):
        archive = tmp_path / "backup.zip"
        arcnames = archive_like_the_worker(save_root, archive)
        assert _verify_backup_archive(str(archive), arcnames, "MyGame") is None

    def test_accepts_nested_arcnames_exactly_as_written(self, save_root, tmp_path):
        # namelist() returns forward slashes while os.path.relpath produced
        # backslashes; comparing the two directly reported every nested file as
        # both missing and unexpected at the same time.
        archive = tmp_path / "nested.zip"
        arcnames = archive_like_the_worker(save_root, archive)
        assert any(os.sep in arc or "/" in arc for arc in arcnames)
        _verify_backup_archive(str(archive), arcnames, "MyGame")  # must not raise

    def test_rejects_a_file_that_was_never_created(self, tmp_path):
        with pytest.raises(OSError, match="never created"):
            _verify_backup_archive(str(tmp_path / "nope.zip"), [], "MyGame")

    def test_rejects_an_empty_file(self, tmp_path):
        archive = tmp_path / "empty.zip"
        archive.write_bytes(b"")
        with pytest.raises(OSError, match="0 bytes"):
            _verify_backup_archive(str(archive), [], "MyGame")

    def test_rejects_a_truncated_archive(self, save_root, tmp_path):
        archive = tmp_path / "truncated.zip"
        arcnames = archive_like_the_worker(save_root, archive)
        raw = archive.read_bytes()
        archive.write_bytes(raw[: len(raw) - 30])  # cut off the directory
        with pytest.raises(OSError, match="couldn't be read back"):
            _verify_backup_archive(str(archive), arcnames, "MyGame")

    def test_rejects_an_archive_whose_stored_bytes_are_wrong(self, save_root, tmp_path):
        archive = tmp_path / "lying.zip"
        arcnames = archive_like_the_worker(save_root, archive, compression=zipfile.ZIP_STORED)
        flip_first_member_byte(archive)

        # Everything shallow still looks fine: same size, opens, same names.
        with zipfile.ZipFile(str(archive)) as zf:
            assert set(_arcname_key(n) for n in zf.namelist()) == \
                set(_arcname_key(n) for n in arcnames)

        with pytest.raises(OSError, match="corrupt"):
            _verify_backup_archive(str(archive), arcnames, "MyGame")

    def test_rejects_a_file_missing_from_the_archive(self, save_root, tmp_path):
        archive = tmp_path / "partial.zip"
        arcnames = archive_like_the_worker(save_root, archive)
        with pytest.raises(OSError, match="1 file\\(s\\) missing"):
            _verify_backup_archive(str(archive), arcnames + ["gone.sav"], "MyGame")

    def test_rejects_an_unexpected_extra_entry(self, tmp_path):
        save = tmp_path / "saves"
        save.mkdir()
        (save / "written.sav").write_text("payload")
        archive = tmp_path / "extra.zip"
        archive_like_the_worker(save, archive)
        # Nothing expected at all, but one member is present.
        with pytest.raises(OSError, match="1 unexpected entry"):
            _verify_backup_archive(str(archive), [], "MyGame")

    def test_reports_the_plural_form_for_several_extra_entries(self, save_root, tmp_path):
        archive = tmp_path / "extra.zip"
        archive_like_the_worker(save_root, archive)
        with pytest.raises(OSError, match="3 unexpected entries"):
            _verify_backup_archive(str(archive), [], "MyGame")

    def test_reports_both_missing_and_unexpected(self, tmp_path):
        save = tmp_path / "saves"
        save.mkdir()
        (save / "written.sav").write_text("payload")
        archive = tmp_path / "both.zip"
        archive_like_the_worker(save, archive)
        with pytest.raises(OSError) as excinfo:
            _verify_backup_archive(str(archive), ["expected.sav"], "MyGame")
        message = str(excinfo.value)
        assert "expected.sav" in message   # missing: named as an example
        assert "1 unexpected entry" in message  # unexpected: counted only
        assert "incomplete" in message

    def test_error_message_names_the_game(self, save_root, tmp_path):
        archive = tmp_path / "backup.zip"
        arcnames = archive_like_the_worker(save_root, archive)
        with pytest.raises(OSError, match="MyGame"):
            _verify_backup_archive(str(archive), arcnames + ["gone.sav"], "MyGame")


class TestArcnameKey:
    def test_normalises_the_platform_separator(self):
        assert _arcname_key("sub" + os.sep + "a.sav") == "sub/a.sav"

    def test_is_idempotent(self):
        assert _arcname_key("sub/a.sav") == "sub/a.sav"

    def test_leaves_other_characters_alone(self):
        assert _arcname_key("weird name (1).sav") == "weird name (1).sav"


# ---------------------------------------------------------------------------
# construction-time clamping
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_clamps_compression_level(self):
        assert BackupWorker("id", "n", "s", "b", compression_level=99).compression_level == 9
        assert BackupWorker("id", "n", "s", "b", compression_level=-4).compression_level == 0
        assert BackupWorker("id", "n", "s", "b", compression_level="junk").compression_level == 6

    def test_clamps_max_backups(self):
        assert BackupWorker("id", "n", "s", "b", max_backups=-1).max_backups == 0
        assert BackupWorker("id", "n", "s", "b", max_backups="junk").max_backups == 0
        assert BackupWorker("id", "n", "s", "b", max_backups=10_000).max_backups == 9999

    def test_defaults(self):
        worker = BackupWorker("id", "n", "s", "b")
        assert worker.compression_level == 6
        assert worker.max_backups == 0

    def test_keeps_its_own_fields(self):
        worker = BackupWorker("g1", "Hades", "/saves", "/backups")
        assert (worker.game_id, worker.game_name) == ("g1", "Hades")
        assert (worker.save_path, worker.backup_root) == ("/saves", "/backups")


# ---------------------------------------------------------------------------
# a successful backup
# ---------------------------------------------------------------------------

class TestSuccessfulBackup:
    def test_reports_success_once(self, save_root, backup_root, run_backup):
        result = run_backup(save_root, backup_root)
        assert len(result.finished) == 1
        assert not result.errors

    def test_creates_the_backup_destination(self, save_root, backup_root, run_backup):
        assert not backup_root.exists()
        run_backup(save_root, backup_root)
        assert backup_root.is_dir()

    def test_archive_contains_every_save_file(self, save_root, backup_root, run_backup):
        result = run_backup(save_root, backup_root)
        zip_path = result.finished.last[3]
        with zipfile.ZipFile(zip_path) as zf:
            assert sorted(_arcname_key(n) for n in zf.namelist()) == [
                "SaveData/nested.sav", "settings.ini", "slot1.sav",
            ]

    def test_archived_contents_match_the_save_folder(self, save_root, backup_root, run_backup):
        result = run_backup(save_root, backup_root)
        with zipfile.ZipFile(result.finished.last[3]) as zf:
            assert zf.read("slot1.sav") == (save_root / "slot1.sav").read_bytes()
            assert zf.read("SaveData/nested.sav") == \
                (save_root / "SaveData" / "nested.sav").read_bytes()

    def test_archive_passes_its_own_verification(self, save_root, backup_root, run_backup):
        result = run_backup(save_root, backup_root)
        zip_path = result.finished.last[3]
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.testzip() is None

    def test_timestamp_is_used_for_the_filename(self, save_root, backup_root, run_backup):
        result = run_backup(save_root, backup_root)
        _game_id, snapshot, timestamp, zip_path, remaining = result.finished.last
        assert _BACKUP_FILENAME_PATTERN.match(f"{timestamp}.zip")
        assert os.path.basename(zip_path) == f"{timestamp}.zip"
        assert remaining == 1

    def test_reports_the_post_backup_snapshot_of_the_live_folder(
            self, save_root, backup_root, run_backup):
        # Change detection compares against the save folder, never the archive,
        # so the emitted snapshot has to match the folder as it is on disk.
        result = run_backup(save_root, backup_root)
        assert result.finished.last[1] == build_snapshot(str(save_root))

    def test_emits_progress_once_per_file(self, save_root, backup_root, run_backup):
        result = run_backup(save_root, backup_root)
        assert result.progress.calls == [
            ("game-1", 1, 3), ("game-1", 2, 3), ("game-1", 3, 3),
        ]

    def test_progress_totals_match_the_file_count(self, tmp_path, run_backup):
        save = tmp_path / "saves"
        save.mkdir()
        for index in range(7):
            (save / f"file{index}.sav").write_text("x")
        result = run_backup(save, tmp_path / "backups")
        assert [call[2] for call in result.progress.calls] == [7] * 7

    def test_empty_save_folder_still_produces_a_valid_archive(self, tmp_path, run_backup):
        save = tmp_path / "saves"
        save.mkdir()
        result = run_backup(save, tmp_path / "backups")
        assert result.finished, result.errors.last
        with zipfile.ZipFile(result.finished.last[3]) as zf:
            assert zf.namelist() == []

    def test_backup_destination_may_not_exist_yet(self, save_root, tmp_path, run_backup):
        result = run_backup(save_root, tmp_path / "a" / "b" / "c")
        assert result.finished, result.errors.last
        assert (tmp_path / "a" / "b" / "c").is_dir()


class TestWhatEndsUpInTheArchive:
    def test_hidden_files_are_excluded(self, tmp_path, run_backup):
        save = tmp_path / "saves"
        save.mkdir()
        (save / "visible.sav").write_text("a")
        (save / ".hidden.sav").write_text("b")
        result = run_backup(save, tmp_path / "backups")
        with zipfile.ZipFile(result.finished.last[3]) as zf:
            assert zf.namelist() == ["visible.sav"]

    def test_hidden_folders_are_excluded(self, tmp_path, run_backup):
        save = tmp_path / "saves"
        (save / ".git").mkdir(parents=True)
        (save / ".git" / "config").write_text("a")
        (save / "slot.sav").write_text("b")
        result = run_backup(save, tmp_path / "backups")
        with zipfile.ZipFile(result.finished.last[3]) as zf:
            assert zf.namelist() == ["slot.sav"]

    def test_symlinked_files_are_excluded(self, tmp_path, run_backup, make_symlink):
        outside = tmp_path / "outside.sav"
        outside.write_text("secret")
        save = tmp_path / "saves"
        save.mkdir()
        (save / "slot.sav").write_text("kept")
        make_symlink(outside, "link.sav", parent=save)
        result = run_backup(save, tmp_path / "backups")
        with zipfile.ZipFile(result.finished.last[3]) as zf:
            assert zf.namelist() == ["slot.sav"]

    def test_a_link_to_the_folder_itself_terminates_and_is_not_duplicated(
            self, self_referencing_dir, tmp_path, run_backup):
        result = run_backup(self_referencing_dir, tmp_path / "backups")
        assert result.finished, result.errors.last
        with zipfile.ZipFile(result.finished.last[3]) as zf:
            assert sorted(_arcname_key(n) for n in zf.namelist()) == ["sub/slot.sav"]


# ---------------------------------------------------------------------------
# refusing to run at all
# ---------------------------------------------------------------------------

class TestRefusals:
    def test_missing_save_folder(self, tmp_path, run_backup):
        result = run_backup(tmp_path / "gone", tmp_path / "backups")
        assert not result.finished
        assert "no longer exists" in result.errors.last[1]

    def test_no_backup_destination_configured(self, save_root, run_backup):
        result = run_backup(save_root, "")
        assert not result.finished
        assert "No backup destination" in result.errors.last[1]
        assert not (save_root / ".zip").exists()

    def test_destination_nested_in_the_save_folder(self, save_root, run_backup):
        # Each backup would walk right past its own previous archives and zip
        # them up inside itself, growing without bound.
        nested = save_root / "backups"
        result = run_backup(save_root, nested)
        assert not result.finished
        message = result.errors.last[1]
        assert "inside" in message and "save folder" in message
        assert not nested.exists()

    def test_destination_equal_to_the_save_folder(self, save_root, run_backup):
        result = run_backup(save_root, save_root)
        assert not result.finished
        assert "save folder" in result.errors.last[1]
        assert not list(save_root.glob("*.zip"))

    def test_destination_reached_through_a_link_to_the_save_folder(
            self, save_root, tmp_path, junction, run_backup):
        # Validated in the dialog when the paths were typed, but the folder can
        # be replaced with a link before the background thread starts.
        link = junction(save_root, "savelink")
        result = run_backup(save_root, link / "backups")
        assert not result.finished
        assert "inside" in result.errors.last[1]

    def test_system_location_as_save_folder(self, run_backup):
        result = run_backup(filesystem_root(), "unused-backups")
        assert not result.finished
        assert "system location" in result.errors.last[1]

    def test_no_archive_is_left_behind_after_a_refusal(self, save_root, backup_root, run_backup):
        run_backup(save_root, save_root / "backups")
        assert not (save_root / "backups").exists()
        assert list(backup_root.glob("*")) == []

    def test_the_save_folder_is_never_modified_by_a_refusal(self, save_root, run_backup):
        before = sorted(p.name for p in save_root.rglob("*"))
        run_backup(save_root, save_root)
        assert sorted(p.name for p in save_root.rglob("*")) == before


# ---------------------------------------------------------------------------
# failure handling
# ---------------------------------------------------------------------------

class TestFailedBackup:
    def test_a_failing_verification_deletes_the_suspect_archive(
            self, save_root, backup_root, run_backup, monkeypatch):
        import app.backup_worker as worker_module

        def rejecting(zip_path, expected, name):
            raise OSError(f"{name}: simulated corruption")

        monkeypatch.setattr(worker_module, "_verify_backup_archive", rejecting)
        result = run_backup(save_root, backup_root)

        assert not result.finished
        assert "simulated corruption" in result.errors.last[1]
        # A file that can't be trusted must not be left looking like a backup.
        assert list(backup_root.glob("*.zip")) == []

    def test_a_failing_backup_never_prunes_previous_ones(
            self, save_root, backup_root, run_backup, monkeypatch):
        import app.backup_worker as worker_module

        seed_backups(backup_root, 3)
        monkeypatch.setattr(
            worker_module, "_verify_backup_archive",
            lambda *a: (_ for _ in ()).throw(OSError("boom")),
        )
        result = run_backup(save_root, backup_root, max_backups=1)

        assert not result.finished
        # Retention deletes the oldest good archives, so doing it on the
        # strength of an unproven new one would trade a recoverable failure
        # (retry; the save folder is untouched) for an unrecoverable one.
        assert len(backup_names(backup_root)) == 3

    def test_an_unexpected_exception_becomes_an_error_signal(
            self, save_root, backup_root, run_backup, monkeypatch):
        import app.backup_worker as worker_module

        def exploding_walk(*args, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(worker_module.os, "walk", exploding_walk)
        result = run_backup(save_root, backup_root)
        assert not result.finished
        assert "unexpected" in result.errors.last[1]

    def test_a_full_disk_reports_an_error_not_a_crash(
            self, save_root, backup_root, run_backup, monkeypatch):
        import app.backup_worker as worker_module

        class FullDisk(Exception):
            pass

        def full(*args, **kwargs):
            raise FullDisk("No space left on device")

        monkeypatch.setattr(worker_module.zipfile, "ZipFile", full)
        result = run_backup(save_root, backup_root)
        assert not result.finished
        assert "No space left" in result.errors.last[1]


class TestIncompleteArchiveCleanup:
    """Which archive a failure is allowed to delete.

    The archive file exists from the moment ZipFile opens it, so its presence
    on disk says nothing about whether it holds the saves. Two things follow,
    and both went wrong: a failure after the archive verified used to delete a
    complete backup, while an interruption that skipped the Exception handler
    left a half-written one that opens cleanly and reads back as valid.
    """

    def _fail_after_the_write(self, monkeypatch, where):
        """Patch a stage that only runs once the archive is complete."""
        import app.backup_worker as worker_module

        if where == "prune":
            def boom(self):
                raise OSError("simulated prune failure")
            monkeypatch.setattr(worker_module.BackupWorker,
                                "_prune_old_backups", boom)
        else:
            def boom(path):
                raise OSError("simulated snapshot failure")
            monkeypatch.setattr(worker_module, "build_snapshot", boom)

    @pytest.mark.parametrize("where", ["prune", "snapshot"])
    def test_a_failure_after_verification_keeps_the_finished_archive(
            self, save_root, backup_root, run_backup, monkeypatch, where):
        self._fail_after_the_write(monkeypatch, where)
        result = run_backup(save_root, backup_root)

        assert not result.finished, "the failure is still reported"
        archives = list(backup_root.glob("*.zip"))
        # The saves are safely archived at this point; only the housekeeping
        # after that failed, so the backup must survive it.
        assert len(archives) == 1
        with zipfile.ZipFile(archives[0]) as zf:
            assert sorted(zf.namelist()) == [
                "SaveData/nested.sav", "settings.ini", "slot1.sav",
            ]

    def test_a_prune_failure_keeps_the_earlier_archives(
            self, save_root, backup_root, run_backup, monkeypatch):
        self._fail_after_the_write(monkeypatch, "prune")
        seed_backups(backup_root, 3)
        run_backup(save_root, backup_root, max_backups=1)

        # Pruning is what failed, so nothing was deleted at all -- and the new,
        # verified archive is still there besides.
        assert len(backup_names(backup_root)) == 4  # 3 seeded + the new one

    def test_a_snapshot_failure_still_prunes_as_configured(
            self, save_root, backup_root, run_backup, monkeypatch):
        # Retention runs *before* the fingerprint, so by the time this fails it
        # has already run -- legitimately, since the new archive had verified by
        # then. What must not happen is the failure handler treating that
        # finished archive as a casualty on top of it.
        self._fail_after_the_write(monkeypatch, "snapshot")
        seed_backups(backup_root, 3)
        run_backup(save_root, backup_root, max_backups=1)

        assert len(backup_names(backup_root)) == 1

    def test_an_interrupted_write_leaves_no_archive(self, save_root, backup_root,
                                                     run_backup, monkeypatch):
        # KeyboardInterrupt is a BaseException, so it skips an
        # `except Exception` handler entirely. zipfile still closes the file on
        # the way out, so what it leaves behind opens cleanly with only the
        # files written before the interruption -- a plausible-looking backup
        # holding a fraction of the saves.
        import app.backup_worker as worker_module

        class InterruptingZip(worker_module.zipfile.ZipFile):
            calls = 0

            def write(self, *args, **kwargs):
                InterruptingZip.calls += 1
                if InterruptingZip.calls == 2:
                    raise KeyboardInterrupt("simulated Ctrl-C")
                return super().write(*args, **kwargs)

        monkeypatch.setattr(worker_module.zipfile, "ZipFile", InterruptingZip)
        worker = BackupWorker("game-1", "Test Game", str(save_root), str(backup_root))
        errors = []
        worker.error.connect(lambda *args: errors.append(args))

        with pytest.raises(KeyboardInterrupt):
            worker.run()

        assert errors == [], "an interruption is not a backup error to report"
        assert list(backup_root.glob("*.zip")) == []

    def test_an_interrupted_write_is_still_reported_to_whoever_raised_it(
            self, save_root, backup_root, run_backup, monkeypatch):
        # The cleanup must not swallow the interruption: a caller that cancels
        # a backup needs to see that the cancellation actually happened.
        import app.backup_worker as worker_module

        class InterruptingZip(worker_module.zipfile.ZipFile):
            calls = 0

            def write(self, *args, **kwargs):
                InterruptingZip.calls += 1
                if InterruptingZip.calls == 1:
                    raise KeyboardInterrupt("simulated Ctrl-C")
                return super().write(*args, **kwargs)

        monkeypatch.setattr(worker_module.zipfile, "ZipFile", InterruptingZip)
        worker = BackupWorker("game-1", "Test Game", str(save_root), str(backup_root))
        with pytest.raises(KeyboardInterrupt):
            worker.run()
        assert list(backup_root.glob("*.zip")) == []

    def test_a_failed_verification_still_deletes_the_archive(
            self, save_root, backup_root, run_backup, monkeypatch):
        # The other side of the same coin: unproven is not proven-good, so an
        # archive that failed verification is still removed.
        import app.backup_worker as worker_module

        def rejecting(zip_path, expected, name):
            raise OSError(f"{name}: simulated corruption")

        monkeypatch.setattr(worker_module, "_verify_backup_archive", rejecting)
        result = run_backup(save_root, backup_root)

        assert not result.finished
        assert list(backup_root.glob("*.zip")) == []

    def test_undeletable_incomplete_archive_does_not_mask_the_error(
            self, save_root, backup_root, run_backup, monkeypatch):
        # Best-effort by design: failing to delete the leftover must not become
        # a second failure, and must not replace the error actually worth
        # reporting. Here that means the partial file survives on disk -- the
        # user is told what went wrong and nothing claims otherwise.
        import app.backup_worker as worker_module

        class FailingZip(worker_module.zipfile.ZipFile):
            calls = 0

            def write(self, *args, **kwargs):
                FailingZip.calls += 1
                if FailingZip.calls == 2:
                    raise OSError("simulated write failure")
                return super().write(*args, **kwargs)

        monkeypatch.setattr(worker_module.zipfile, "ZipFile", FailingZip)
        real_remove = worker_module.os.remove

        def refusing_remove(path, *args, **kwargs):
            if str(path).endswith(".zip"):
                raise PermissionError("file is open elsewhere")
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(worker_module.os, "remove", refusing_remove)
        result = run_backup(save_root, backup_root)

        assert not result.finished
        assert "simulated write failure" in result.errors.last[1]
        assert len(result.errors) == 1
        assert len(list(backup_root.glob("*.zip"))) == 1


# ---------------------------------------------------------------------------
# folders the walk could not reach
# ---------------------------------------------------------------------------

class TestIsPathLengthError:
    @pytest.mark.parametrize("winerror, expected", [
        (206, True),   # ERROR_FILENAME_EXCED_RANGE: past MAX_PATH
        (123, True),   # ERROR_INVALID_NAME: over-long single component
        (3, False),    # ERROR_PATH_NOT_FOUND: folder deleted mid-walk
        (5, False),    # ERROR_ACCESS_DENIED: user can't read it
        (32, False),   # ERROR_SHARING_VIOLATION: another process has it open
    ])
    def test_win32_codes_are_classified(self, winerror, expected):
        assert _is_path_length_error(win_error(winerror)) is expected

    def test_posix_reports_the_same_problem_as_enametoolong(self):
        # Only errno is set on POSIX, which is why it is checked as well.
        assert _is_path_length_error(OSError(errno.ENAMETOOLONG, "File name too long")) is True

    @pytest.mark.parametrize("err", [
        errno.ENOENT, errno.EACCES, errno.ENOSPC, errno.ELOOP,
    ])
    def test_unrelated_errno_values_are_not_path_length_problems(self, err):
        # Widening this predicate would fail backups over conditions that a
        # retry genuinely fixes.
        assert _is_path_length_error(OSError(err, "unrelated")) is False


class TestPathTooLongIsRefused:
    """A walk that cannot reach part of the save folder must not report success.

    _verify_backup_archive compares the archive against the list built by the
    same walk, so a silently truncated walk verifies as complete -- the one gap
    the completeness check cannot catch. Failing is the only thing that keeps
    a partial archive from being badged "Up to date" and never revisited.
    """
    SUBDIR = "SaveData"

    def _refuse(self, monkeypatch, exc=None):
        import app.backup_worker as worker_module

        monkeypatch.setattr(
            worker_module.os, "scandir",
            scandir_failing_for(
                self.SUBDIR,
                exc if exc is not None else win_error(
                    206, errno.EINVAL, "The filename or extension is too long"
                ),
            ),
        )

    def test_a_too_long_path_fails_the_backup(self, save_root, backup_root,
                                              run_backup, monkeypatch):
        self._refuse(monkeypatch)
        result = run_backup(save_root, backup_root)
        assert not result.finished
        assert "260-character limit" in result.errors.last[1]

    def test_the_message_names_the_folder_it_could_not_reach(
            self, save_root, backup_root, run_backup, monkeypatch):
        self._refuse(monkeypatch)
        result = run_backup(save_root, backup_root)
        assert self.SUBDIR in result.errors.last[1]

    def test_no_partial_archive_is_left_behind(self, save_root, backup_root,
                                               run_backup, monkeypatch):
        self._refuse(monkeypatch)
        run_backup(save_root, backup_root)
        # Refused before the archive is opened, so there is no .zip to mistake
        # for a backup of a folder that was only partly read.
        assert list(backup_root.glob("*.zip")) == []

    def test_refusing_never_prunes_previous_ones(self, save_root, backup_root,
                                                  run_backup, monkeypatch):
        self._refuse(monkeypatch)
        seed_backups(backup_root, 3)
        result = run_backup(save_root, backup_root, max_backups=1)
        assert not result.finished
        # The same reasoning as a failed verification: retention deletes the
        # oldest *good* archives, so a refusal must not cost the user the ones
        # they already have.
        assert len(backup_names(backup_root)) == 3

    def test_the_save_folder_is_never_modified(self, save_root, backup_root,
                                               run_backup, monkeypatch):
        self._refuse(monkeypatch)
        before = build_snapshot(str(save_root))
        run_backup(save_root, backup_root)
        assert build_snapshot(str(save_root)) == before


class TestOtherWalkErrorsStayTolerated:
    """Everything that isn't a path-length problem keeps today's behaviour.

    A folder deleted while the walk was running, or one the user simply cannot
    read, is worth skipping rather than failing over -- retrying fixes both,
    and a save folder with one locked subfolder should still back up.
    """
    SUBDIR = "SaveData"

    def test_a_permission_error_does_not_fail_the_backup(
            self, save_root, backup_root, run_backup, monkeypatch):
        import app.backup_worker as worker_module

        monkeypatch.setattr(
            worker_module.os, "scandir",
            scandir_failing_for(self.SUBDIR, win_error(5, errno.EACCES, "Access is denied")),
        )
        result = run_backup(save_root, backup_root)

        assert result.finished, result.errors.last
        names = archived_names(result)
        assert "slot1.sav" in names
        assert not any(name.startswith(self.SUBDIR) for name in names)

    def test_a_folder_deleted_mid_walk_does_not_fail_the_backup(
            self, save_root, backup_root, run_backup, monkeypatch):
        import app.backup_worker as worker_module

        monkeypatch.setattr(
            worker_module.os, "scandir",
            scandir_failing_for(self.SUBDIR, win_error(3, errno.ENOENT, "no such path")),
        )
        result = run_backup(save_root, backup_root)

        assert result.finished, result.errors.last
        assert "settings.ini" in archived_names(result)


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

class TestRetention:
    def test_unlimited_is_the_default_and_deletes_nothing(self, save_root, backup_root, run_backup):
        seeds = seed_backups(backup_root, 4)
        result = run_backup(save_root, backup_root)
        assert result.finished, result.errors.last
        assert result.finished.last[4] == 5
        assert set(seeds) <= set(backup_names(backup_root))

    def test_keeps_only_the_most_recent(self, save_root, backup_root, run_backup):
        seeds = seed_backups(backup_root, 4)
        result = run_backup(save_root, backup_root, max_backups=2)

        assert result.finished.last[4] == 2
        remaining = backup_names(backup_root)
        assert len(remaining) == 2
        assert result.finished.last[3].endswith(".zip")
        assert os.path.basename(result.finished.last[3]) in remaining
        # The newest pre-existing archive survives, the two oldest do not.
        assert seeds[-1] in remaining
        assert seeds[0] not in remaining
        assert seeds[1] not in remaining

    def test_limit_of_one_leaves_only_the_new_backup(self, save_root, backup_root, run_backup):
        seed_backups(backup_root, 3)
        result = run_backup(save_root, backup_root, max_backups=1)
        assert backup_names(backup_root) == [os.path.basename(result.finished.last[3])]

    def test_no_deletion_when_under_the_limit(self, save_root, backup_root, run_backup):
        seeds = seed_backups(backup_root, 2)
        result = run_backup(save_root, backup_root, max_backups=5)
        assert result.finished.last[4] == 3
        assert set(seeds) <= set(backup_names(backup_root))

    def test_never_deletes_the_backup_it_just_made(self, save_root, backup_root, run_backup):
        seed_backups(backup_root, 10)
        result = run_backup(save_root, backup_root, max_backups=1)
        assert backup_root.joinpath(os.path.basename(result.finished.last[3])).is_file()

    def test_unrelated_files_are_left_alone(self, save_root, backup_root, run_backup):
        backup_root.mkdir()
        keep = [
            backup_root / "notes.txt",
            backup_root / "2020-01-01_00-00-00.zip.bak",
            backup_root / "MyGame Save.zip",
        ]
        for path in keep:
            path.write_bytes(b"keep me")
        lookalike_dir = backup_root / "2017-06-06_12-00-00.zip"
        lookalike_dir.mkdir()

        seed_backups(backup_root, 3)
        result = run_backup(save_root, backup_root, max_backups=1)

        assert result.finished.last[4] == 1
        for path in keep:
            assert path.is_file(), f"{path.name} should not have been touched"
        assert lookalike_dir.is_dir()

    def test_an_undeletable_old_backup_does_not_fail_the_new_one(
            self, save_root, backup_root, run_backup, monkeypatch):
        import app.backup_worker as worker_module
        seeds = seed_backups(backup_root, 3)
        real_remove = worker_module.os.remove

        def refusing_remove(path, *args, **kwargs):
            if str(path).endswith(seeds[0]):
                raise PermissionError("file is open elsewhere")
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(worker_module.os, "remove", refusing_remove)
        result = run_backup(save_root, backup_root, max_backups=2)

        assert result.finished, result.errors.last
        assert seeds[0] in backup_names(backup_root)
        assert seeds[1] not in backup_names(backup_root)

    def test_prune_tolerates_an_unreadable_destination(self, save_root, backup_root,
                                                      run_backup, monkeypatch):
        import app.backup_worker as worker_module

        def failing_scandir(*args, **kwargs):
            raise OSError("device not ready")

        monkeypatch.setattr(worker_module.os, "scandir", failing_scandir)
        result = run_backup(save_root, backup_root, max_backups=3)
        # Can't even count what exists: assume just the one that was made.
        assert result.finished.last[4] == 1