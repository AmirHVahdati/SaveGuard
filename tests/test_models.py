"""app/models.py — the tracked-game record and its JSON representation."""

import json
import uuid

import pytest

from app.models import Game


class TestNew:
    def test_fills_in_every_supplied_field(self):
        game = Game.new("Hades", r"C:\saves\hades", r"D:\backups\hades")
        assert game.name == "Hades"
        assert game.save_path == r"C:\saves\hades"
        assert game.backup_root == r"D:\backups\hades"

    def test_starts_with_no_backup_history(self):
        game = Game.new("Hades", "/saves", "/backups")
        assert game.last_backup_time is None
        assert game.last_snapshot is None
        assert game.backup_count == 0
        assert game.color is None

    def test_generates_a_unique_id(self):
        ids = {Game.new(f"Game {i}", "/s", "/b").id for i in range(50)}
        assert len(ids) == 50
        for game_id in ids:
            uuid.UUID(game_id)  # parses as a real UUID

    def test_does_not_share_state_between_instances(self):
        first = Game.new("A", "/s", "/b")
        second = Game.new("B", "/s", "/b")
        first.backup_count = 7
        assert second.backup_count == 0


class TestSerialization:
    def test_round_trip_preserves_every_field(self):
        game = Game.new("Elden Ring", "/saves/elden", "/backups/elden")
        game.last_backup_time = "2026-09-11_20-30-00"
        game.last_snapshot = {"slot.sav": [1024, 1757623800123456789]}
        game.backup_count = 3
        game.color = "#7c5cff"

        restored = Game.from_dict(game.to_dict())
        assert restored == game

    def test_to_dict_is_plain_json_types(self):
        payload = Game.new("Hades", "/s", "/b").to_dict()
        assert isinstance(payload, dict)
        assert payload["last_snapshot"] is None
        assert isinstance(payload["id"], str)

    def test_snapshot_survives_the_json_round_trip_exactly(self):
        # Change detection compares mtimes exactly, so a float-typed snapshot
        # must not lose a nanosecond on the way through disk.
        game = Game.new("Hades", "/s", "/b")
        game.last_snapshot = {"slot.sav": [1024, 1757623800123456789]}
        restored = Game.from_dict(json.loads(json.dumps(game.to_dict())))
        assert restored.last_snapshot == {"slot.sav": [1024, 1757623800123456789]}


class TestFromDict:
    def test_minimal_dict_gets_usable_defaults(self):
        game = Game.from_dict({})
        assert game.name == "Unknown Game"
        assert game.save_path == ""
        assert game.backup_root == ""
        assert game.last_backup_time is None
        assert game.last_snapshot is None
        assert game.backup_count == 0
        assert game.color is None
        uuid.UUID(game.id)

    def test_existing_id_is_kept(self):
        game = Game.from_dict({"id": "fixed-id", "name": "Hades"})
        assert game.id == "fixed-id"

    @pytest.mark.parametrize("falsy_id", [None, "", 0, False])
    def test_missing_or_empty_id_gets_a_fresh_one(self, falsy_id):
        game = Game.from_dict({"id": falsy_id, "name": "Hades"})
        assert game.id
        assert game.id != falsy_id
        uuid.UUID(game.id)

    def test_unknown_keys_are_ignored(self):
        # Forward compatibility: a games.json written by a newer version must
        # still load rather than blowing up on an unrecognised key.
        game = Game.from_dict({
            "id": "1", "name": "Hades", "future_option": {"a": 1},
        })
        assert game.name == "Hades"

    def test_backup_timestamp_without_a_snapshot_is_kept_as_is(self):
        # Both keys persist independently, so this combination is reachable
        # from a truncated or hand-edited file. scanner.snapshots_differ is
        # what turns it into "changed" rather than a permanent "synced".
        game = Game.from_dict({"id": "1", "last_backup_time": "2026-01-01_00-00-00"})
        assert game.last_backup_time == "2026-01-01_00-00-00"
        assert game.last_snapshot is None