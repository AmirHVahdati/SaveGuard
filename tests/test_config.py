"""app/config.py — JSON persistence, atomic writes, and settings defaults.

config resolves its paths once at import time from Path.home(), so every test
here runs against the redirected throwaway directory from conftest's app_dir
fixture: nothing in this file can read or write the real ~/.saveguard.
"""

import json
import os

import pytest

from app import config


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestEnsureAppDir:
    def test_creates_the_directory(self, app_dir):
        assert not app_dir.exists()
        config.ensure_app_dir()
        assert app_dir.is_dir()

    def test_is_idempotent(self, app_dir):
        config.ensure_app_dir()
        marker = app_dir / "keep.txt"
        marker.write_text("kept", encoding="utf-8")
        config.ensure_app_dir()
        assert marker.read_text(encoding="utf-8") == "kept"

    def test_load_creates_it_even_when_nothing_exists_yet(self, app_dir):
        assert config.load_games() == []
        assert config.load_state() == {}
        assert config.load_settings() == config.DEFAULT_SETTINGS
        assert app_dir.is_dir()


class TestLoadGames:
    def test_no_file_yet_returns_empty_list(self, app_dir):
        assert config.load_games() == []

    def test_returns_saved_dicts_untouched(self, app_dir):
        games = [{"id": "1", "name": "Hades", "save_path": "/a", "backup_root": "/b"}]
        assert config.save_games(games) is True
        assert config.load_games() == games

    @pytest.mark.parametrize("payload", ['{"not": "a list"}', "42", '"text"', "null"])
    def test_non_list_json_is_treated_as_nothing_saved(self, app_dir, payload):
        config.ensure_app_dir()
        config.CONFIG_FILE.write_text(payload, encoding="utf-8")
        assert config.load_games() == []

    @pytest.mark.parametrize("payload", ["", "{", '{"id": "1"', "[{,}]", "not json at all"])
    def test_corrupt_json_is_treated_as_nothing_saved(self, app_dir, payload):
        # A crash mid-write used to silently read as "no games saved", which is
        # exactly why writes are atomic.
        config.ensure_app_dir()
        config.CONFIG_FILE.write_text(payload, encoding="utf-8")
        assert config.load_games() == []

    def test_unreadable_file_is_treated_as_nothing_saved(self, app_dir):
        config.ensure_app_dir()
        config.CONFIG_FILE.mkdir()  # open() on a directory raises OSError
        assert config.load_games() == []


class TestSaveGames:
    def test_round_trips_unicode_names_unescaped(self, app_dir):
        games = [{"id": "1", "name": "Pokémon — Version Épique"}]
        config.save_games(games)
        raw = config.CONFIG_FILE.read_text(encoding="utf-8")
        assert "Pokémon — Version Épique" in raw
        assert "\\u" not in raw

    def test_writes_readable_indented_json(self, app_dir):
        config.save_games([{"id": "1"}])
        assert "\n" in config.CONFIG_FILE.read_text(encoding="utf-8")

    def test_replaces_previous_contents(self, app_dir):
        config.save_games([{"id": "1", "name": "First"}])
        config.save_games([{"id": "2", "name": "Second"}])
        assert [g["name"] for g in config.load_games()] == ["Second"]

    def test_leaves_no_temporary_files_behind(self, app_dir):
        config.save_games([{"id": "1"}])
        config.save_games([{"id": "2"}])
        assert [p.name for p in app_dir.iterdir()] == ["games.json"]

    def test_returns_true_on_success(self, app_dir):
        assert config.save_games([]) is True


class TestAtomicWriteJson:
    def test_failure_leaves_the_previous_file_intact(self, app_dir, monkeypatch):
        config.save_games([{"id": "1", "name": "Safe"}])

        def failing_replace(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(config.os, "replace", failing_replace)
        assert config.save_games([{"id": "2", "name": "Lost"}]) is False

        # The point of the atomic replace: the old content is still fully there.
        assert read_json(config.CONFIG_FILE) == [{"id": "1", "name": "Safe"}]

    def test_failure_removes_the_partial_temporary_file(self, app_dir, monkeypatch):
        def failing_replace(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(config.os, "replace", failing_replace)
        assert config.save_games([{"id": "1"}]) is False
        assert list(app_dir.iterdir()) == []

    def test_temp_file_is_created_beside_the_target(self, app_dir, monkeypatch):
        # Same directory, therefore same filesystem, therefore os.replace() is
        # a real atomic rename rather than a copy.
        seen = {}
        real_mkstemp = config.tempfile.mkstemp

        def recording_mkstemp(**kwargs):
            seen.update(kwargs)
            return real_mkstemp(**kwargs)

        monkeypatch.setattr(config.tempfile, "mkstemp", recording_mkstemp)
        config.save_games([{"id": "1"}])
        assert os.path.normcase(seen["dir"]) == \
            os.path.normcase(str(config.CONFIG_FILE.parent))
        assert seen["suffix"] == ".tmp"

    def test_error_creating_the_temp_file_is_reported_not_raised(self, app_dir, monkeypatch):
        def failing_mkstemp(**kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(config.tempfile, "mkstemp", failing_mkstemp)
        assert config.save_games([{"id": "1"}]) is False


class TestState:
    def test_round_trips(self, app_dir):
        assert config.save_state({"first_scan_done": True}) is True
        assert config.load_state() == {"first_scan_done": True}

    def test_missing_file_returns_empty_dict(self, app_dir):
        assert config.load_state() == {}

    @pytest.mark.parametrize("payload", ["[1, 2, 3]", '"text"', "null", "{oops"])
    def test_non_dict_or_corrupt_content_returns_empty_dict(self, app_dir, payload):
        config.ensure_app_dir()
        config.STATE_FILE.write_text(payload, encoding="utf-8")
        assert config.load_state() == {}

    def test_unrelated_state_keys_survive(self, app_dir):
        config.save_state({"first_scan_done": True, "future_flag": 3})
        assert config.load_state()["future_flag"] == 3


class TestSettings:
    def test_defaults_when_no_file_exists(self, app_dir):
        assert config.load_settings() == config.DEFAULT_SETTINGS

    def test_saved_values_win(self, app_dir):
        saved = {
            "compression_level": 9,
            "max_backups": 5,
            "default_scan_backup_root": "/tmp/backups",
        }
        config.save_settings(saved)
        # Exactly the defaults with the saved values on top: a key the file
        # doesn't mention still comes from DEFAULT_SETTINGS.
        assert config.load_settings() == {**config.DEFAULT_SETTINGS, **saved}

    def test_missing_keys_fall_back_to_defaults(self, app_dir):
        # A settings.json written by an older version must keep working
        # without migration code.
        config.ensure_app_dir()
        config.SETTINGS_FILE.write_text(
            json.dumps({"compression_level": 3}), encoding="utf-8"
        )
        settings = config.load_settings()
        assert settings["compression_level"] == 3
        assert settings["max_backups"] == config.DEFAULT_SETTINGS["max_backups"]
        assert settings["default_scan_backup_root"] == \
            config.DEFAULT_SETTINGS["default_scan_backup_root"]

    def test_corrupt_file_falls_back_to_defaults(self, app_dir):
        config.ensure_app_dir()
        config.SETTINGS_FILE.write_text("{ this is not json", encoding="utf-8")
        assert config.load_settings() == config.DEFAULT_SETTINGS

    @pytest.mark.parametrize("payload", ["[1,2]", '"text"', "null"])
    def test_non_dict_payload_falls_back_to_defaults(self, app_dir, payload):
        config.ensure_app_dir()
        config.SETTINGS_FILE.write_text(payload, encoding="utf-8")
        assert config.load_settings() == config.DEFAULT_SETTINGS

    def test_returns_a_copy_so_callers_cannot_corrupt_defaults(self, app_dir):
        settings = config.load_settings()
        settings["compression_level"] = 0
        settings["invented_key"] = True
        assert config.load_settings() == config.DEFAULT_SETTINGS
        assert "invented_key" not in config.DEFAULT_SETTINGS

    def test_unknown_keys_are_preserved_for_forward_compatibility(self, app_dir):
        config.save_settings({"some_future_setting": "keep me"})
        assert config.load_settings()["some_future_setting"] == "keep me"

    def test_save_reports_failure_instead_of_raising(self, app_dir, monkeypatch):
        def failing_replace(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(config.os, "replace", failing_replace)
        assert config.save_settings({"max_backups": 3}) is False


class TestDefaultSettings:
    def test_documented_defaults(self):
        assert config.DEFAULT_SETTINGS["compression_level"] == 6
        assert config.DEFAULT_SETTINGS["max_backups"] == 0  # unlimited
        assert config.DEFAULT_SETTINGS["default_scan_backup_root"].endswith(
            os.path.join("SaveGuard Backups")
        )

    def test_defaults_are_json_serialisable(self):
        json.dumps(config.DEFAULT_SETTINGS)