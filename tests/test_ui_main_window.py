"""app/ui/main_window.py — the window that ties the app together.

The window is exercised directly rather than through its widgets wherever
possible: these tests care about what the user is told (banner, status bar,
button labels) and what gets written to games.json, not about pixel layout.
Dialogs and background workers are stubbed so no test has to click through a
modal flow or wait on a real thread.

Every window here comes from conftest's main_window fixture, which points the
config directory at a temp folder and pre-marks the one-time first scan as
done, so nothing here touches the real ~/.saveguard or scans the real disk.
"""

import json
import os

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QDesktopServices, QResizeEvent
from PySide6.QtWidgets import QMessageBox, QPushButton

from app import config
from app import game_scanner
from app.models import Game
from app.scanner import build_snapshot
from app.ui import main_window as main_window_module
from app.ui.game_card import GameCard
from app.ui.main_window import MainWindow, SUPPORT_URL

from helpers import write_files


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------

class FakeBackupWorker(QObject):
    """A BackupWorker that records how it was built and never starts."""

    progress = Signal(str, int, int)
    backup_finished = Signal(str, dict, str, str, int)
    error = Signal(str)
    # The window relies on QThread's own `finished` signal to know when it is
    # safe to drop its last reference to a worker, so the fake has one too.
    finished = Signal()

    instances = []

    def __init__(self, game_id, game_name, save_path, backup_root,
                 compression_level=6, max_backups=0):
        super().__init__()
        self.game_id = game_id
        self.game_name = game_name
        self.save_path = save_path
        self.backup_root = backup_root
        self.compression_level = compression_level
        self.max_backups = max_backups
        FakeBackupWorker.instances.append(self)

    def start(self):
        pass

    def isRunning(self):
        return False


class FakeEvent:
    def __init__(self):
        self.accepted = None

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


class RunningWorker:
    """Stands in for a worker that is still doing its job."""

    def isRunning(self):
        return True


class FinishedWorker:
    """Stands in for a worker whose thread has already ended."""

    def isRunning(self):
        return False


class FakeScanWorker(QObject):
    """A GameScanWorker stand-in: records its roots and never scans."""

    scan_finished = Signal(list)
    scan_error = Signal(str)
    finished = Signal()

    instances = []

    def __init__(self, existing_paths):
        super().__init__()
        self.existing_paths = existing_paths
        self.started = False
        FakeScanWorker.instances.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return self.started


def stub_dialog(monkeypatch, name, result_data=None, result_settings=None, accepted=True):
    """Replace a dialog class with one that returns a canned result."""
    class Stub:
        def __init__(self, *args, **kwargs):
            self.result_data = result_data
            self.result_settings = result_settings

        def exec(self):
            return 1 if accepted else 0

    monkeypatch.setattr(main_window_module, name, Stub)


def buttons(window):
    return window.findChildren(QPushButton)


def button_named(window, text):
    return next(b for b in buttons(window) if b.text() == text)


def card_order(window):
    """The game names in the order the card list currently displays them."""
    order = []
    for index in range(window.list_layout.count()):
        widget = window.list_layout.itemAt(index).widget()
        if isinstance(widget, GameCard):
            order.append(widget.game.name)
    return order


def visible_cards(window):
    """The ids of the cards currently shown.

    Visibility is read from the widget's hidden flag rather than isVisible():
    the window itself is never shown in an offscreen test run, so isVisible()
    would report False for every card whether or not a search had filtered it.
    """
    return [game_id for game_id, card in window.cards.items() if not card.isHidden()]


def make_game(window, name="Hades", save_path=None, backup_root=None):
    game = Game.new(name, str(save_path or "/nowhere"), str(backup_root or "/backups"))
    window.games[game.id] = game
    return game


def mark_first_scan_done():
    """Pretend the one-time first scan already happened on an earlier launch."""
    config.save_state({"first_scan_done": True})


def stage_never_backed_up_games(window, tmp_path, names=("A", "B", "C")):
    """Track a few games that have never been backed up, and capture requests.

    This is a plain function rather than a fixture on purpose: the window
    recomputes every game's status as soon as the event loop turns, so a status
    written during fixture setup would be overwritten before the test body ran.
    """
    games = [make_game(window, name, tmp_path / name) for name in names]
    for game in games:
        window.status_cache[game.id] = "never"
    requested = []
    window._on_backup_requested = requested.append
    return games, requested


@pytest.fixture
def tracked(main_window, save_root, backup_root):
    """A window with one tracked game whose save folder really exists."""
    game = Game.new("Hades", str(save_root), str(backup_root))
    main_window.games[game.id] = game
    main_window._recompute_statuses()
    main_window._rebuild_cards()
    return game


@pytest.fixture
def fake_workers(monkeypatch):
    FakeBackupWorker.instances.clear()
    monkeypatch.setattr(main_window_module, "BackupWorker", FakeBackupWorker)
    return FakeBackupWorker


@pytest.fixture
def fake_scans(monkeypatch):
    FakeScanWorker.instances.clear()
    monkeypatch.setattr(main_window_module, "GameScanWorker", FakeScanWorker)
    return FakeScanWorker


# ---------------------------------------------------------------------------
# statuses
# ---------------------------------------------------------------------------

class TestComputeStatus:
    def test_no_save_folder_at_all(self, main_window, tmp_path):
        game = make_game(main_window, "Hades", tmp_path / "saves")
        game.save_path = ""
        assert main_window._compute_status(game) == "invalid"

    def test_save_folder_that_no_longer_exists(self, main_window, tmp_path):
        game = make_game(main_window, "Hades", tmp_path / "not_here")
        assert main_window._compute_status(game) == "invalid"

    def test_never_backed_up(self, main_window, save_root, backup_root):
        game = Game.new("Hades", str(save_root), str(backup_root))
        assert main_window._compute_status(game) == "never"

    def test_up_to_date_when_the_folder_matches_the_last_snapshot(
            self, main_window, save_root, backup_root):
        game = Game.new("Hades", str(save_root), str(backup_root))
        game.last_backup_time = "2026-09-11_20-30-00"
        game.last_snapshot = build_snapshot(game.save_path)
        assert main_window._compute_status(game) == "synced"

    def test_changes_when_the_save_folder_moved_on(
            self, main_window, save_root, backup_root):
        game = Game.new("Hades", str(save_root), str(backup_root))
        game.last_backup_time = "2026-09-11_20-30-00"
        game.last_snapshot = {"slot1.sav": [999, 1]}
        assert main_window._compute_status(game) == "changed"

    def test_a_backup_timestamp_without_a_snapshot_counts_as_changed(
            self, main_window, save_root, backup_root):
        # Both fields persist separately, so a truncated games.json can leave a
        # game that looks backed up with nothing to compare against. Calling it
        # up to date would mean never backing it up again.
        game = Game.new("Hades", str(save_root), str(backup_root))
        game.last_backup_time = "2026-09-11_20-30-00"
        game.last_snapshot = None
        assert main_window._compute_status(game) == "changed"

    def test_a_missing_save_folder_wins_over_a_backup_timestamp(self, main_window, tmp_path):
        game = make_game(main_window, "Hades", tmp_path / "gone")
        game.last_backup_time = "2026-09-11_20-30-00"
        assert main_window._compute_status(game) == "invalid"


class TestStatusCache:
    def test_recomputes_every_game_by_default(self, main_window, tmp_path, save_root):
        make_game(main_window, "A", tmp_path / "gone")
        make_game(main_window, "B", save_root)
        main_window._recompute_statuses()
        assert sorted(main_window.status_cache.values()) == ["invalid", "never"]

    def test_can_be_limited_to_specific_games(self, main_window, tmp_path, save_root):
        first = make_game(main_window, "A", tmp_path / "gone")
        second = make_game(main_window, "B", save_root)
        main_window._recompute_statuses(game_ids=[second.id])
        assert first.id not in main_window.status_cache
        assert main_window.status_cache[second.id] == "never"

    def test_unknown_ids_are_ignored(self, main_window):
        main_window._recompute_statuses(game_ids=["not-a-game"])
        assert main_window.status_cache == {}

    def test_cards_are_updated_by_the_refresh_pass(self, tracked, main_window):
        card = main_window.cards[tracked.id]
        card.set_status("never")
        main_window._recompute_and_refresh()
        assert card.status == main_window.status_cache[tracked.id]


# ---------------------------------------------------------------------------
# banner, status bar, backup-all button
# ---------------------------------------------------------------------------

class TestBanner:
    def test_hidden_when_everything_is_up_to_date(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "synced"
        main_window._update_banner_from_cache()
        assert main_window.banner.isHidden() is True

    def test_shown_with_the_names_that_need_a_backup(self, main_window, tmp_path):
        make_game(main_window, "Hades", tmp_path / "a")
        make_game(main_window, "Hollow Knight", tmp_path / "b")
        main_window.status_cache = {gid: "never" for gid in main_window.games}
        main_window._update_banner_from_cache()
        assert main_window.banner.isHidden() is False
        assert "Hades" in main_window.banner_label.text()
        assert "Hollow Knight" in main_window.banner_label.text()

    def test_summarises_the_tail_when_there_are_many(self, main_window, tmp_path):
        for index in range(6):
            make_game(main_window, f"Game {index}", tmp_path / f"s{index}")
        main_window.status_cache = {gid: "never" for gid in main_window.games}
        main_window._update_banner_from_cache()
        assert "and 2 more" in main_window.banner_label.text()

    def test_shows_four_names_before_summarising(self, main_window, tmp_path):
        for index in range(5):
            make_game(main_window, f"Game {index}", tmp_path / f"s{index}")
        main_window.status_cache = {gid: "never" for gid in main_window.games}
        main_window._update_banner_from_cache()
        assert "Game 3" in main_window.banner_label.text()
        assert "and 1 more" in main_window.banner_label.text()

    def test_only_changed_and_never_games_need_a_backup(self, main_window, tmp_path):
        good = make_game(main_window, "Fine", tmp_path / "fine")
        broken = make_game(main_window, "Missing Folder", tmp_path / "gone")
        main_window.status_cache = {good.id: "synced", broken.id: "invalid"}
        main_window._update_banner_from_cache()
        assert main_window.banner.isHidden() is True


class TestStatusBar:
    def test_no_games_at_all(self, main_window):
        main_window._refresh_status_bar()
        assert main_window.status_bar.currentMessage() == "No games tracked yet."

    def test_everything_up_to_date(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "synced"
        main_window._refresh_status_bar()
        assert "all backups up to date" in main_window.status_bar.currentMessage()

    def test_counts_the_games_that_need_one(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "changed"
        main_window._refresh_status_bar()
        message = main_window.status_bar.currentMessage()
        assert "1 game(s) tracked" in message
        assert "1 need a backup" in message

    def test_welcome_summary_on_startup(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "never"
        main_window._show_startup_summary()
        assert "1 game(s) need a backup" in main_window.status_bar.currentMessage()

    def test_no_summary_when_everything_is_fine(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "synced"
        main_window.status_bar.clearMessage()
        main_window._show_startup_summary()
        assert "need a backup" not in main_window.status_bar.currentMessage()


class TestBackupAllButton:
    def test_disabled_with_no_games(self, main_window):
        main_window._update_backup_all_button()
        assert main_window.backup_all_btn.isEnabled() is False
        assert main_window.backup_all_btn.text() == "Backup All"

    def test_disabled_when_everything_is_up_to_date(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "synced"
        main_window._update_backup_all_button()
        assert main_window.backup_all_btn.isEnabled() is False

    def test_enabled_with_a_count_when_work_is_waiting(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "changed"
        main_window._update_backup_all_button()
        assert main_window.backup_all_btn.isEnabled() is True
        assert main_window.backup_all_btn.text() == "Backup All (1)"

    def test_counts_only_the_games_that_need_a_backup(self, main_window, tmp_path):
        make_game(main_window, "A", tmp_path / "a")
        make_game(main_window, "B", tmp_path / "b")
        main_window.status_cache = {gid: "synced" for gid in main_window.games}
        main_window._update_backup_all_button()
        assert main_window.backup_all_btn.text() == "Backup All"


# ---------------------------------------------------------------------------
# the card list
# ---------------------------------------------------------------------------

class TestCardList:
    def test_empty_state_message(self, main_window):
        main_window._rebuild_cards()
        assert card_order(main_window) == []
        assert main_window.cards == {}

    def test_cards_are_sorted_by_name(self, main_window, tmp_path):
        for name in ["Zelda", "Another Game", "midas"]:
            make_game(main_window, name, tmp_path / name)
        main_window._rebuild_cards()
        assert card_order(main_window) == ["Another Game", "midas", "Zelda"]

    def test_a_new_game_is_inserted_in_order(self, main_window, tmp_path):
        make_game(main_window, "Another Game", tmp_path / "a")
        main_window._rebuild_cards()
        main_window._add_card_for_game(make_game(main_window, "AAA Game", tmp_path / "b"))
        assert card_order(main_window) == ["AAA Game", "Another Game"]

    def test_removing_a_game_removes_its_card(self, main_window, tmp_path):
        game = make_game(main_window, "Hades", tmp_path / "a")
        main_window._rebuild_cards()
        main_window._remove_card_for_game(game.id)
        assert card_order(main_window) == []
        assert game.id not in main_window.cards

    def test_rebuilding_does_not_duplicate_cards(self, main_window, tmp_path):
        make_game(main_window, "A", tmp_path / "a")
        main_window._rebuild_cards()
        main_window._rebuild_cards()
        assert card_order(main_window) == ["A"]
        assert len(main_window.cards) == 1

    def test_a_short_list_creates_every_card(self, main_window, tmp_path):
        for index in range(5):
            make_game(main_window, f"Game {index}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        assert len(main_window.cards) == 5

    def test_a_long_list_only_creates_the_visible_cards(self, main_window, tmp_path):
        for index in range(60):
            make_game(main_window, f"Game {index:02d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        # All of them are tracked, but only the ones in view exist as widgets.
        assert len(main_window._all_game_ids) == 60
        assert 0 < len(main_window.cards) < 60

    def test_scrolling_creates_the_cards_that_came_into_view(
            self, main_window, tmp_path, monkeypatch):
        for index in range(120):
            make_game(main_window, f"Game {index:03d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        assert len(main_window.cards) < 120

        # The viewport is simulated rather than scrolled for real: the widget
        # has no laid-out geometry in an offscreen test run.
        monkeypatch.setattr(main_window, "_get_visible_range", lambda: (100, 110))
        main_window._sync_visible_cards()

        shown = set(main_window.cards)
        assert set(main_window._all_game_ids[100:110]) <= shown
        assert len(shown) < 120

    def test_cards_far_outside_the_viewport_are_dropped(
            self, main_window, tmp_path, monkeypatch):
        for index in range(120):
            make_game(main_window, f"Game {index:03d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        monkeypatch.setattr(main_window, "_get_visible_range", lambda: (100, 110))
        main_window._sync_visible_cards()
        near_the_end = set(main_window._all_game_ids[95:115])
        assert not any(gid not in near_the_end for gid in main_window.cards)
        assert len(main_window.cards) <= len(near_the_end)

    def test_sync_is_a_no_op_for_a_short_list(self, main_window, tmp_path, monkeypatch):
        make_game(main_window, "A", tmp_path / "a")
        main_window._rebuild_cards()
        monkeypatch.setattr(
            main_window, "_get_visible_range",
            lambda: pytest.fail("should not even look at the viewport"))
        main_window._sync_visible_cards()

    def test_visible_range_stays_inside_the_list(self, main_window, tmp_path):
        for index in range(60):
            make_game(main_window, f"Game {index:02d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        start, end = main_window._get_visible_range()
        assert 0 <= start <= end <= 60

    def test_a_resize_does_not_break_the_long_list(self, main_window, tmp_path):
        for index in range(60):
            make_game(main_window, f"Game {index:02d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        main_window.scroll.resize(400, 400)
        main_window._sync_visible_cards()  # must not raise or recurse forever

    def test_every_game_holds_a_slot_even_without_a_card(self, main_window, tmp_path):
        # Layout position N has to correspond to _all_game_ids[N], whether that
        # slot holds a card or a spacer. If it doesn't, cards get inserted in the
        # wrong place as you scroll.
        for index in range(120):
            make_game(main_window, f"Game {index:03d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        assert len(main_window.cards) < 120
        # one item per game, plus the trailing stretch
        assert main_window.list_layout.count() == len(main_window._all_game_ids) + 1

    def test_slots_and_cards_stay_in_step_when_scrolling(self, main_window, tmp_path,
                                                          monkeypatch):
        for index in range(120):
            make_game(main_window, f"Game {index:03d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        monkeypatch.setattr(main_window, "_get_visible_range", lambda: (100, 110))
        main_window._sync_visible_cards()
        assert main_window.list_layout.count() == len(main_window._all_game_ids) + 1

    def test_removing_a_game_frees_its_slot(self, main_window, tmp_path):
        for index in range(120):
            make_game(main_window, f"Game {index:03d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        victim = main_window._all_game_ids[-1]
        main_window._remove_card_for_game(victim)
        assert victim not in main_window._all_game_ids
        assert victim not in main_window._slot_ids
        assert main_window.list_layout.count() == len(main_window._all_game_ids) + 1

    def test_the_last_game_is_in_view_at_the_bottom_of_the_scroll(self, main_window,
                                                                  tmp_path, qtbot):
        # The original bug: the scroll area was only as tall as the handful of
        # cards that existed, so the offset needed to bring the tail of a long
        # list into view was never reachable and those games could not be seen.
        for index in range(120):
            make_game(main_window, f"Game {index:03d}", tmp_path / f"s{index}")
        main_window._rebuild_cards()
        main_window.show()
        qtbot.wait(50)

        scrollbar = main_window.scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        main_window._sync_visible_cards()

        start, end = main_window._get_visible_range()
        assert start < len(main_window._all_game_ids)
        assert end == len(main_window._all_game_ids)
        assert main_window._all_game_ids[-1] in main_window.cards


class TestSearch:
    @pytest.fixture
    def populated(self, main_window, tmp_path):
        for name in ["Hades", "Hollow Knight", "Celeste"]:
            make_game(main_window, name, tmp_path / name)
        main_window._rebuild_cards()
        return main_window

    def test_no_query_shows_everything(self, populated):
        populated._on_search_changed("")
        assert len(visible_cards(populated)) == 3

    def test_matches_a_substring(self, populated):
        populated._on_search_changed("hollow")
        shown = visible_cards(populated)
        assert len(shown) == 1
        assert populated.games[shown[0]].name == "Hollow Knight"

    def test_matching_is_case_insensitive(self, populated):
        populated._on_search_changed("HADES")
        assert len(visible_cards(populated)) == 1

    def test_clearing_the_query_brings_everything_back(self, populated):
        populated._on_search_changed("hades")
        populated._on_search_changed("")
        assert len(visible_cards(populated)) == 3

    def test_a_query_matching_nothing_hides_everything(self, populated):
        populated._on_search_changed("no such game")
        assert visible_cards(populated) == []

    def test_surrounding_whitespace_is_ignored(self, populated):
        populated._on_search_changed("   ")
        assert len(visible_cards(populated)) == 3

    def test_a_partial_word_still_matches(self, populated):
        populated._on_search_changed("hol")
        assert len(visible_cards(populated)) == 1

    def test_typing_in_the_search_box_filters_the_cards(self, populated):
        populated.search_edit.setText("celeste")
        assert len(visible_cards(populated)) == 1


# ---------------------------------------------------------------------------
# adding, editing, removing
# ---------------------------------------------------------------------------

def new_game_data(save_root, backup_root, name="Hades"):
    return {"name": name, "save_path": str(save_root), "backup_root": str(backup_root)}


class TestAddGame:
    def test_adding_tracks_and_persists(self, main_window, monkeypatch, save_root, backup_root):
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root))
        main_window._on_add_game()
        assert [g.name for g in main_window.games.values()] == ["Hades"]
        assert [g["name"] for g in config.load_games()] == ["Hades"]

    def test_adding_creates_a_card(self, main_window, monkeypatch, save_root, backup_root):
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root))
        main_window._on_add_game()
        assert card_order(main_window) == ["Hades"]

    def test_cancelling_adds_nothing(self, main_window, monkeypatch, save_root, backup_root):
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root), accepted=False)
        main_window._on_add_game()
        assert main_window.games == {}
        assert config.load_games() == []

    def test_the_new_games_status_is_computed(self, main_window, monkeypatch,
                                              save_root, backup_root):
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root))
        main_window._on_add_game()
        game = next(iter(main_window.games.values()))
        assert main_window.status_cache[game.id] == "never"

    def test_only_the_new_games_folder_is_walked(self, main_window, monkeypatch,
                                                 tmp_path, save_root, backup_root):
        existing = make_game(main_window, "Existing", tmp_path / "existing")
        walked = []
        monkeypatch.setattr(
            main_window, "_recompute_statuses",
            lambda game_ids=None: walked.append(game_ids),
        )
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root))
        main_window._on_add_game()
        # The other games' folders are not touched: adding one game must not
        # cost a walk of every save folder already tracked.
        assert len(walked) == 1
        assert len(walked[0]) == 1
        assert walked[0][0] not in {existing.id}


class TestEditGame:
    def test_editing_updates_the_game_and_the_file(
            self, tracked, main_window, monkeypatch, tmp_path):
        new_save = write_files(tmp_path / "new_saves", {"slot.sav": "x"})
        stub_dialog(monkeypatch, "AddGameDialog", result_data=new_game_data(
            new_save, tmp_path / "backups", name="Hades II"))
        main_window._on_edit_game(tracked.id)
        assert main_window.games[tracked.id].name == "Hades II"
        assert main_window.games[tracked.id].save_path == str(new_save)
        assert config.load_games()[0]["name"] == "Hades II"

    def test_a_rename_repositions_the_card(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        make_game(main_window, "AAA Game", save_root)
        main_window._rebuild_cards()
        stub_dialog(monkeypatch, "AddGameDialog", result_data=new_game_data(
            save_root, backup_root, name="ZZZ Renamed"))
        main_window._on_edit_game(tracked.id)
        assert card_order(main_window) == ["AAA Game", "ZZZ Renamed"]

    def test_editing_without_a_rename_keeps_the_card_in_place(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root))
        main_window._on_edit_game(tracked.id)
        assert card_order(main_window) == ["Hades"]

    def test_cancelling_changes_nothing(self, tracked, main_window, monkeypatch):
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data("/x", "/y", name="Something Else"),
                    accepted=False)
        main_window._on_edit_game(tracked.id)
        assert main_window.games[tracked.id].name == "Hades"

    def test_editing_an_unknown_game_does_nothing(self, main_window, monkeypatch):
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data("/x", "/y"))
        main_window._on_edit_game("not-a-game")
        assert main_window.games == {}

    def test_a_moved_save_folder_is_re_statused(self, tracked, main_window,
                                                monkeypatch, tmp_path):
        stub_dialog(monkeypatch, "AddGameDialog", result_data=new_game_data(
            tmp_path / "gone", tmp_path / "backups"))
        main_window._on_edit_game(tracked.id)
        assert main_window.status_cache[tracked.id] == "invalid"


class TestRemoveGame:
    def test_confirming_removes_and_persists(self, tracked, main_window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        main_window._on_remove_game(tracked.id)
        assert main_window.games == {}
        assert config.load_games() == []

    def test_declining_keeps_the_game(self, tracked, main_window, monkeypatch):
        main_window._persist()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        main_window._on_remove_game(tracked.id)
        assert tracked.id in main_window.games
        assert len(config.load_games()) == 1

    def test_the_confirmation_says_backups_are_kept(self, tracked, main_window, monkeypatch):
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda parent, title, text, *a, **k: asked.append(text) or QMessageBox.No,
        )
        main_window._on_remove_game(tracked.id)
        assert "not delete any existing backup files" in asked[0]

    def test_the_cached_status_is_dropped(self, tracked, main_window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        main_window._on_remove_game(tracked.id)
        assert tracked.id not in main_window.status_cache

    def test_removing_an_unknown_game_does_nothing(self, main_window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: pytest.fail("should not have asked"))
        main_window._on_remove_game("not-a-game")
        assert main_window.games == {}


class TestColorChange:
    def test_the_new_colour_is_stored_and_persisted(self, tracked, main_window):
        main_window._on_color_changed(tracked.id, "#ff6b9d")
        assert main_window.games[tracked.id].color == "#ff6b9d"
        assert config.load_games()[0]["color"] == "#ff6b9d"

    def test_automatic_is_stored_as_no_colour(self, tracked, main_window):
        main_window._on_color_changed(tracked.id, "#ff6b9d")
        main_window._on_color_changed(tracked.id, None)
        assert main_window.games[tracked.id].color is None

    def test_an_unknown_game_is_ignored(self, main_window):
        main_window._on_color_changed("not-a-game", "#ff6b9d")  # must not raise


class TestPersistence:
    def test_a_failed_save_is_reported_in_the_status_bar(self, main_window, monkeypatch):
        make_game(main_window, "Hades")
        monkeypatch.setattr(main_window_module, "save_games", lambda games: False)
        main_window._persist()
        assert "write access" in main_window.status_bar.currentMessage()

    def test_tracked_games_survive_a_restart(self, main_window, save_root, backup_root,
                                              app_dir, qtbot):
        game = Game.new("Hades", str(save_root), str(backup_root))
        main_window.games[game.id] = game
        main_window._persist()

        mark_first_scan_done()
        reopened = MainWindow()
        qtbot.addWidget(reopened)
        try:
            assert [g.name for g in reopened.games.values()] == ["Hades"]
        finally:
            reopened.close()

    def test_a_corrupt_games_file_starts_empty(self, app_dir, qtbot):
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "games.json").write_text("{ not json", encoding="utf-8")
        mark_first_scan_done()
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            assert window.games == {}
        finally:
            window.close()


# ---------------------------------------------------------------------------
# backups
# ---------------------------------------------------------------------------

class TestBackupRequest:
    def test_a_missing_save_folder_is_reported_not_backed_up(
            self, main_window, monkeypatch, tmp_path, message_spy):
        game = make_game(main_window, "Hades", tmp_path / "gone")
        main_window._add_card_for_game(game)
        main_window._on_backup_requested(game.id)
        assert message_spy.calls[0][2] == "Save folder missing"
        assert game.id not in main_window.workers

    def test_an_unknown_game_id_is_ignored(self, main_window):
        main_window._on_backup_requested("not-a-game")  # must not raise

    def test_a_game_with_no_card_is_still_backed_up(self, main_window, save_root,
                                                    fake_workers):
        # Above the virtualization threshold most of the library has no card at
        # any given moment. That must not stop a backup: "Backup All" drives
        # every game through here.
        game = make_game(main_window, "Hades", save_root)
        assert game.id not in main_window.cards
        main_window._on_backup_requested(game.id)
        assert game.id in main_window.workers
        assert fake_workers.instances[-1].game_id == game.id

    def test_settings_are_clamped_before_the_worker_starts(
            self, tracked, main_window, fake_workers):
        main_window.settings = {"compression_level": 99, "max_backups": -1}
        main_window._on_backup_requested(tracked.id)
        worker = fake_workers.instances[-1]
        assert worker.compression_level == 9
        assert worker.max_backups == 0

    def test_the_worker_gets_the_games_paths(self, tracked, main_window, fake_workers):
        main_window._on_backup_requested(tracked.id)
        worker = fake_workers.instances[-1]
        assert worker.save_path == tracked.save_path
        assert worker.backup_root == tracked.backup_root

    def test_the_card_is_marked_as_backing_up(self, tracked, main_window, fake_workers):
        main_window._on_backup_requested(tracked.id)
        assert main_window.cards[tracked.id].backup_btn.text() == "Backing up…"

    def test_the_worker_is_tracked_so_progress_can_find_it(
            self, tracked, main_window, fake_workers):
        main_window._on_backup_requested(tracked.id)
        assert tracked.id in main_window.workers

    def test_a_second_request_while_running_is_ignored(
            self, tracked, main_window, fake_workers):
        main_window._on_backup_requested(tracked.id)
        first = main_window.workers[tracked.id]
        main_window._on_backup_requested(tracked.id)
        assert main_window.workers[tracked.id] is first
        assert len(fake_workers.instances) == 1


class TestBackupFinished:
    def finish(self, window, game, backups_remaining=1, zip_path=r"D:\backups\b.zip"):
        window._on_backup_finished(
            game.id,
            {"slot1.sav": [10, 20]},
            "2026-09-11_20-30-00",
            zip_path,
            backups_remaining,
        )

    def test_records_what_was_backed_up(self, tracked, main_window):
        self.finish(main_window, tracked, backups_remaining=3)
        assert main_window.games[tracked.id].last_snapshot == {"slot1.sav": [10, 20]}
        assert main_window.games[tracked.id].last_backup_time == "2026-09-11_20-30-00"
        assert main_window.games[tracked.id].backup_count == 3

    def test_persists_the_new_state(self, tracked, main_window):
        self.finish(main_window, tracked)
        saved = config.load_games()[0]
        assert saved["last_backup_time"] == "2026-09-11_20-30-00"
        assert saved["last_snapshot"] == {"slot1.sav": [10, 20]}

    def test_marks_the_game_as_up_to_date_without_re_reading_disk(
            self, tracked, main_window):
        # The outcome is already known from the snapshot the worker returned.
        main_window._compute_status = pytest.fail
        self.finish(main_window, tracked)
        assert main_window.status_cache[tracked.id] == "synced"

    def test_updates_the_card(self, tracked, main_window):
        self.finish(main_window, tracked)
        card = main_window.cards[tracked.id]
        assert card.status == "synced"
        assert card.backup_btn.text() == "Backup Now"

    def test_reports_where_the_backup_landed(self, tracked, main_window):
        self.finish(main_window, tracked)
        message = main_window.status_bar.currentMessage()
        assert "Hades" in message
        assert "b.zip" in message

    def test_backup_count_reflects_what_is_on_disk(self, tracked, main_window):
        # Retention deletes old archives, so the number shown has to come from
        # the worker rather than accumulating for the lifetime of the app.
        main_window.games[tracked.id].backup_count = 17
        self.finish(main_window, tracked, backups_remaining=3)
        assert main_window.games[tracked.id].backup_count == 3

    def test_an_unknown_game_is_ignored(self, main_window):
        main_window._on_backup_finished("not-a-game", {}, "t", "z", 1)  # must not raise


class TestBackupError:
    def test_clears_the_progress_state(self, tracked, main_window, message_spy):
        main_window.cards[tracked.id].set_backing_up(True)
        main_window._on_backup_error(tracked.id, "disk full")
        assert main_window.cards[tracked.id].backup_btn.text() == "Backup Now"

    def test_shows_the_message(self, tracked, main_window, message_spy):
        main_window._on_backup_error(tracked.id, "disk full")
        assert message_spy.calls[0][2] == "Backup failed"
        assert "disk full" in message_spy.calls[0][3]

    def test_a_failure_leaves_the_status_alone(self, tracked, main_window, message_spy):
        # The save folder is untouched, so the game still needs a backup.
        main_window.status_cache[tracked.id] = "changed"
        main_window._on_backup_error(tracked.id, "disk full")
        assert main_window.status_cache[tracked.id] == "changed"


class TestBackupAll:
    def test_says_so_when_nothing_needs_backing_up(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "synced"
        main_window._on_backup_all()
        assert "No saves need backing up" in main_window.status_bar.currentMessage()

    def test_queues_one_backup_per_game_needing_it(self, main_window, tmp_path, qtbot):
        games, requested = stage_never_backed_up_games(main_window, tmp_path)
        main_window._on_backup_all()
        qtbot.waitUntil(lambda: len(requested) == 3, timeout=2000)
        assert sorted(requested) == sorted(game.id for game in games)

    def test_backups_run_one_at_a_time(self, main_window, tmp_path, monkeypatch, qtbot):
        games, _requested = stage_never_backed_up_games(main_window, tmp_path)
        in_flight = []
        batches = []

        def one_at_a_time(game_id):
            batches.append(list(in_flight))
            in_flight.append(game_id)

        main_window._on_backup_requested = one_at_a_time
        main_window._on_backup_all()
        qtbot.waitUntil(lambda: len(in_flight) == 3, timeout=2000)
        # Each is started on its own event loop tick, so a fast completion
        # can't re-enter the loop that started it.
        assert all(len(batch) == index for index, batch in enumerate(batches))

    def test_skips_games_that_are_already_up_to_date(self, main_window, tmp_path,
                                                     monkeypatch, qtbot):
        requested = []
        changed = make_game(main_window, "Changed", tmp_path / "a")
        make_game(main_window, "Synced", tmp_path / "b")
        main_window.status_cache = {gid: "changed" for gid in main_window.games}
        for gid in list(main_window.games)[1:]:
            main_window.status_cache[gid] = "synced"
        main_window._on_backup_requested = requested.append
        main_window._on_backup_all()
        qtbot.waitUntil(lambda: requested == [changed.id], timeout=2000)

    def test_skips_games_that_are_already_being_backed_up(
            self, main_window, tmp_path, qtbot):
        running = make_game(main_window, "Running", tmp_path / "running")
        main_window.workers[running.id] = RunningWorker()
        _games, requested = stage_never_backed_up_games(main_window, tmp_path,
                                                       names=("Waiting",))
        main_window.status_cache[running.id] = "never"
        main_window._on_backup_all()
        qtbot.waitUntil(lambda: bool(requested), timeout=2000)
        # It is already in flight, so queueing it again would double up.
        assert running.id not in requested

    def test_covers_games_whose_cards_are_virtualized(self, main_window, tmp_path,
                                                      save_root, qtbot, fake_workers):
        # The regression this guards: a game scrolled out of view has no card,
        # and Backup All used to skip every one of those — reporting a count
        # that included them while backing up almost nothing.
        games = []
        for index in range(3):
            game = make_game(main_window, f"Game {index}", save_root)
            main_window.status_cache[game.id] = "changed"
            games.append(game)
        assert not any(game.id in main_window.cards for game in games)

        main_window._on_backup_all()
        qtbot.waitUntil(lambda: len(fake_workers.instances) == 3, timeout=2000)
        assert {worker.game_id for worker in fake_workers.instances} == {
            game.id for game in games
        }
        assert main_window.backup_all_btn.isEnabled() is True

    def test_the_button_comes_back_afterwards(self, main_window, tmp_path, qtbot):
        stage_never_backed_up_games(main_window, tmp_path)
        main_window._on_backup_all()
        qtbot.waitUntil(lambda: main_window.backup_all_btn.isEnabled() is True, timeout=2000)


class TestBackupProgress:
    def test_progress_reaches_the_card(self, tracked, main_window):
        main_window._on_backup_progress(tracked.id, 2, 4)
        assert main_window.cards[tracked.id].progress_bar.value() == 50

    def test_progress_for_an_unknown_game_is_ignored(self, main_window):
        main_window._on_backup_progress("not-a-game", 1, 2)  # must not raise


class TestStatusUpdateBatching:
    def test_nothing_happens_until_the_event_loop_turns(self, main_window, monkeypatch):
        calls = []
        monkeypatch.setattr(main_window, "_flush_status_update", lambda: calls.append(1))
        for _ in range(3):
            main_window._schedule_status_update()
        assert calls == []

    def test_several_updates_collapse_into_one_refresh(self, main_window, qtbot):
        calls = []
        main_window._flush_status_update = lambda: calls.append(1)
        for _ in range(3):
            main_window._schedule_status_update()
        qtbot.waitUntil(lambda: bool(calls), timeout=1000)
        assert len(calls) == 1

    def test_the_flag_is_cleared_after_a_flush(self, main_window):
        main_window._status_update_pending = True
        main_window._flush_status_update()
        assert main_window._status_update_pending is False

    def test_a_flush_refreshes_banner_status_bar_and_button(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "changed"
        main_window._flush_status_update()
        assert main_window.backup_all_btn.isEnabled() is True
        assert main_window.banner.isHidden() is False


class TestRescan:
    def test_recomputes_every_game_and_updates_the_cards(self, tracked, main_window):
        game = main_window.games[tracked.id]
        game.last_backup_time = "2026-09-11_20-30-00"
        game.last_snapshot = build_snapshot(game.save_path)
        main_window._rescan_all()
        assert main_window.status_cache[tracked.id] == "synced"
        assert main_window.cards[tracked.id].status == "synced"

    def test_a_changed_save_folder_is_reported(self, tracked, main_window):
        game = main_window.games[tracked.id]
        game.last_backup_time = "2026-09-11_20-30-00"
        game.last_snapshot = build_snapshot(game.save_path)
        write_files(game.save_path, {"new_slot.sav": "brand new"})
        main_window._rescan_all()
        assert main_window.status_cache[tracked.id] == "changed"

    def test_the_button_does_it(self, main_window, tracked, fake_workers):
        button_named(main_window, "Rescan").click()
        assert tracked.id in main_window.status_cache


# ---------------------------------------------------------------------------
# scanning for games
# ---------------------------------------------------------------------------

class TestFirstScan:
    def test_runs_once_then_never_again(self, app_dir, monkeypatch, qtbot):
        started = []
        monkeypatch.setattr(MainWindow, "_start_scan", lambda self: started.append(1))
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            qtbot.waitUntil(lambda: bool(started), timeout=2000)
            state = json.loads((app_dir / "state.json").read_text(encoding="utf-8"))
            assert state["first_scan_done"] is True

            window._maybe_run_first_scan()  # a second launch must not scan
            qtbot.wait(50)
            assert len(started) == 1
        finally:
            window.close()

    def test_is_skipped_once_it_has_run(self, app_dir, monkeypatch, qtbot):
        started = []
        monkeypatch.setattr(MainWindow, "_start_scan", lambda self: started.append(1))
        mark_first_scan_done()
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            qtbot.wait(50)
            assert started == []
        finally:
            window.close()

    def test_the_scan_is_delayed_so_the_window_can_paint(self, app_dir, monkeypatch, qtbot):
        started = []
        monkeypatch.setattr(MainWindow, "_start_scan", lambda self: started.append(1))
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            # Still nothing: a dialog popping up before the window is visible
            # looks like a crash.
            assert started == []
            qtbot.waitUntil(lambda: bool(started), timeout=2000)
        finally:
            window.close()


class TestScanWorkerWiring:
    def test_the_button_is_disabled_while_scanning(self, main_window, fake_scans):
        main_window._start_scan()
        assert main_window.scan_games_btn.isEnabled() is False
        assert main_window.scan_games_btn.text() == "Scanning…"
        assert main_window.scan_worker is not None
        assert main_window.scan_worker.started is True

    def test_status_bar_says_what_is_happening(self, main_window, fake_scans):
        main_window._start_scan()
        assert "Scanning" in main_window.status_bar.currentMessage()

    def test_only_one_scan_runs_at_a_time(self, main_window, fake_scans):
        main_window._start_scan()
        main_window._on_scan_for_games()
        assert "already in progress" in main_window.status_bar.currentMessage()

    def test_the_button_returns_when_the_thread_really_finishes(self, main_window,
                                                                fake_scans):
        main_window._start_scan()
        main_window._on_scan_thread_finished()
        assert main_window.scan_games_btn.isEnabled() is True
        assert main_window.scan_games_btn.text() == "Scan Games"
        assert main_window.scan_worker is None

    def test_already_tracked_save_paths_are_handed_to_the_scanner(
            self, tracked, main_window, fake_scans):
        main_window._start_scan()
        assert tracked.save_path in fake_scans.instances[-1].existing_paths

    def test_the_button_does_it(self, main_window, fake_scans):
        button_named(main_window, "Scan Games").click()
        assert main_window.scan_worker is not None


class TestScanResults:
    def candidate(self, save_root, name="Hades"):
        return game_scanner.ScanCandidate(name, str(save_root), "My Games", "high")

    def test_nothing_found_is_reported_quietly(self, main_window):
        main_window._on_scan_finished([])
        assert "No new games found" in main_window.status_bar.currentMessage()

    def test_candidates_are_offered_for_review(self, main_window, monkeypatch, save_root):
        candidate = self.candidate(save_root)
        shown = []

        class StubDialog:
            def __init__(self, candidates, parent=None):
                shown.append(list(candidates))

            def exec(self):
                return 0  # user cancelled

        monkeypatch.setattr(main_window_module, "ScanResultsDialog", StubDialog)
        main_window._on_scan_finished([candidate])
        assert shown == [[candidate]]
        assert main_window.games == {}

    def test_accepted_candidates_become_tracked_games(
            self, main_window, monkeypatch, save_root):
        candidate = self.candidate(save_root)

        class StubDialog:
            def __init__(self, candidates, parent=None):
                self.selected_candidates = candidates

            def exec(self):
                return 1

        monkeypatch.setattr(main_window_module, "ScanResultsDialog", StubDialog)
        main_window._on_scan_finished([candidate])
        assert [g.name for g in main_window.games.values()] == ["Hades"]
        assert len(config.load_games()) == 1

    def test_a_scan_error_is_reported(self, main_window):
        main_window._on_scan_error("permission denied")
        assert "permission denied" in main_window.status_bar.currentMessage()


class TestAddScannedGames:
    def scan(self, window, *candidates):
        window._add_scanned_games(list(candidates))

    def candidate(self, save_root, name):
        return game_scanner.ScanCandidate(name, str(save_root), "My Games", "high")

    def test_each_game_gets_its_own_backup_folder(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        self.scan(main_window, self.candidate(save_root, "Hades"))
        game = next(iter(main_window.games.values()))
        assert game.backup_root == os.path.join(str(tmp_path / "backups"), "Hades")

    def test_unsafe_names_are_sanitised(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        self.scan(main_window, self.candidate(save_root, "Half-Life: Source"))
        game = next(iter(main_window.games.values()))
        assert os.path.basename(game.backup_root) == "Half-Life Source"

    def test_falls_back_to_a_sensible_default_root(self, main_window, save_root, monkeypatch):
        main_window.settings = {}
        monkeypatch.setattr(os.path, "expanduser", lambda path: "C:/Users/x")
        self.scan(main_window, self.candidate(save_root, "Hades"))
        game = next(iter(main_window.games.values()))
        assert "SaveGuard Backups" in game.backup_root

    def test_persists_every_game(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        self.scan(main_window,
                   self.candidate(save_root, "Hades"),
                   self.candidate(save_root, "Celeste"))
        assert len(config.load_games()) == 2

    def test_reports_how_many_were_added(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        self.scan(main_window,
                   self.candidate(save_root, "Hades"),
                   self.candidate(save_root, "Celeste"))
        assert "Added 2 games from scan" in main_window.status_bar.currentMessage()

    def test_one_game_is_reported_in_the_singular(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        self.scan(main_window, self.candidate(save_root, "Hades"))
        assert "Added 1 game from scan" in main_window.status_bar.currentMessage()

    def test_cards_are_added_in_sorted_order(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        self.scan(main_window,
                   self.candidate(save_root, "Zelda"),
                   self.candidate(save_root, "Another Game"))
        assert card_order(main_window) == ["Another Game", "Zelda"]

    def test_only_the_new_folders_are_walked(self, main_window, save_root, tmp_path,
                                            monkeypatch):
        existing = make_game(main_window, "Existing", tmp_path / "existing")
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        walked = []
        monkeypatch.setattr(
            main_window, "_recompute_statuses",
            lambda game_ids=None: walked.append(game_ids),
        )
        self.scan(main_window, self.candidate(save_root, "Hades"))
        # The other games' folders are not walked: adding a game must not cost a
        # fresh walk of every save folder already tracked.
        assert len(walked) == 1
        assert len(walked[0]) == 1
        assert existing.id not in walked[0]

    def test_nothing_added_means_nothing_happens(self, main_window):
        self.scan(main_window)
        assert main_window.games == {}


# ---------------------------------------------------------------------------
# settings dialog and support link
# ---------------------------------------------------------------------------

class TestSettings:
    def test_saved_settings_are_adopted(self, main_window, monkeypatch):
        stub_dialog(monkeypatch, "SettingsDialog",
                    result_settings={"compression_level": 9, "max_backups": 4})
        main_window._on_open_settings()
        assert main_window.settings["compression_level"] == 9
        assert config.load_settings()["max_backups"] == 4

    def test_cancelling_keeps_the_current_settings(self, main_window, monkeypatch):
        before = dict(main_window.settings)
        stub_dialog(monkeypatch, "SettingsDialog",
                    result_settings={"compression_level": 9}, accepted=False)
        main_window._on_open_settings()
        assert main_window.settings == before

    def test_a_failed_save_is_reported(self, main_window, monkeypatch):
        stub_dialog(monkeypatch, "SettingsDialog",
                    result_settings={"compression_level": 9, "max_backups": 4})
        monkeypatch.setattr(main_window_module, "save_settings", lambda settings: False)
        main_window._on_open_settings()
        assert "couldn't be saved to disk" in main_window.status_bar.currentMessage()

    def test_the_button_does_it(self, main_window, monkeypatch):
        stub_dialog(monkeypatch, "SettingsDialog",
                    result_settings={"compression_level": 9})
        button_named(main_window, "Settings").click()
        assert main_window.settings["compression_level"] == 9


class TestSupportButton:
    def test_opens_the_support_page(self, main_window, monkeypatch):
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toString()))
        main_window._on_support_clicked()
        assert opened == [SUPPORT_URL]

    def test_the_button_does_it(self, main_window, monkeypatch):
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toString()))
        button_named(main_window, "♥ Support").click()
        assert opened == [SUPPORT_URL]

    def test_the_url_is_a_real_link(self):
        assert SUPPORT_URL.startswith("https://")


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

class TestCloseEvent:
    def test_closes_normally_when_nothing_is_running(self, main_window):
        event = FakeEvent()
        main_window.closeEvent(event)
        assert event.accepted is True

    def test_asks_before_quitting_with_a_backup_in_flight(self, main_window, monkeypatch):
        main_window.workers["hades"] = RunningWorker()
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda parent, title, text, *a, **k: asked.append(text) or QMessageBox.No,
        )
        event = FakeEvent()
        main_window.closeEvent(event)
        assert asked
        assert "may leave those backups incomplete" in asked[0]

    def test_declining_keeps_the_window_open(self, main_window, monkeypatch):
        main_window.workers["hades"] = RunningWorker()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        event = FakeEvent()
        main_window.closeEvent(event)
        assert event.accepted is False

    def test_confirming_quits_anyway(self, main_window, monkeypatch):
        main_window.workers["hades"] = RunningWorker()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        event = FakeEvent()
        main_window.closeEvent(event)
        assert event.accepted is True

    def test_a_finished_worker_does_not_block_closing(self, main_window):
        main_window.workers["hades"] = FinishedWorker()
        event = FakeEvent()
        main_window.closeEvent(event)
        assert event.accepted is True

    def test_a_running_scan_counts_as_work_in_flight(self, main_window, monkeypatch):
        main_window.scan_worker = RunningWorker()
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda parent, title, text, *a, **k: asked.append(text) or QMessageBox.No,
        )
        event = FakeEvent()
        main_window.closeEvent(event)
        assert asked
        assert event.accepted is False


# ---------------------------------------------------------------------------
# window basics
# ---------------------------------------------------------------------------

class TestWindowSetup:
    def test_has_a_sensible_title_and_size(self, main_window):
        assert "SaveGuard" in main_window.windowTitle()
        assert main_window.minimumWidth() <= main_window.width()

    def test_offers_the_main_actions(self, main_window):
        labels = {b.text() for b in buttons(main_window)}
        for expected in ["Rescan", "Scan Games", "Backup All", "Add Game",
                         "Settings", "♥ Support"]:
            assert expected in labels

    def test_the_support_button_explains_itself(self, main_window):
        assert button_named(main_window, "♥ Support").toolTip()

    def test_the_search_box_explains_itself(self, main_window):
        assert main_window.search_edit.placeholderText() == "Search games…"

    def test_the_scan_button_says_what_it_scans(self, main_window):
        assert "My Games" in button_named(main_window, "Scan Games").toolTip()

    def test_the_banner_starts_hidden(self, main_window):
        assert main_window.banner.isHidden() is True

    def test_dismissing_the_banner(self, main_window, tracked):
        main_window.status_cache[tracked.id] = "changed"
        main_window._update_banner_from_cache()
        main_window.banner.setVisible(True)
        button_named(main_window.banner, "Dismiss").click()
        assert main_window.banner.isVisible() is False

    def test_resize_events_pass_through_the_filter(self, main_window):
        event = QResizeEvent(main_window.scroll.size(), main_window.scroll.size())
        assert main_window.eventFilter(main_window.scroll, event) is False