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
import threading
import time

import pytest
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QResizeEvent
from PySide6.QtWidgets import QMessageBox, QPushButton

from app import config
from app import game_scanner
from app.models import Game
from app.scanner import build_snapshot
from app.ui import main_window as main_window_module
from app.ui.game_card import GameCard
from app.ui.main_window import MAX_CONCURRENT_BACKUPS, MainWindow, SUPPORT_URL

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
    """Stands in for a worker that is still doing its job.

    Finishes when something waits on it, which is what a real thread does and
    what lets closeEvent's shutdown drain terminate instead of running out its
    grace period. Pass finishes_on_wait=False for the one case worth testing
    separately: work that never ends however long you wait for it, so the drain
    has to refuse the close rather than hang.
    """

    def __init__(self, finishes_on_wait=True):
        self._running = True
        self.finishes_on_wait = finishes_on_wait
        self.wait_calls = 0
        self.cancel_calls = 0
        self.cancelled = False

    def isRunning(self):
        return self._running

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.finishes_on_wait:
            self._running = False
        return self.finishes_on_wait

    def cancel(self):
        # Mirrors BackupWorker.cancel(): it is a request, not an instant stop
        # -- the worker keeps running until it next checks the flag or is
        # waited on, which is what lets the drain's wait() observe the
        # cancellation the way a real thread's would.
        self.cancel_calls += 1
        self.cancelled = True
        return True


class FinishedWorker:
    """Stands in for a worker whose thread has already ended."""

    def __init__(self):
        self.cancel_calls = 0

    def isRunning(self):
        return False

    def wait(self, timeout=None):
        return True

    def cancel(self):
        self.cancel_calls += 1


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

    def wait(self, timeout=None):
        # The drain calls wait() on every in-flight thread; a fake worker
        # just stops immediately.
        self.started = False
        return True


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
    window._start_backup = requested.append
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


# The real class, captured at import time -- before the autouse fixture below
# substitutes its inline subclass into the main_window namespace.
_REAL_STATUS_WORKER = main_window_module.StatusWorker


class _InlineStatusWorker(_REAL_STATUS_WORKER):
    """A StatusWorker that runs on the calling thread and never spawns one.

    finished() is emitted explicitly because that is what the real thread
    machinery emits, and it is what releases MainWindow's in-flight guard; a
    direct run() alone would leave the window permanently refusing sweeps.
    """

    def start(self):
        self.run()
        self.finished.emit()


@pytest.fixture(autouse=True)
def inline_status_sweeps(monkeypatch):
    """Runs every status sweep on the calling thread, for this whole file.

    The sweep is asynchronous in the app so the event loop keeps running while
    it walks save folders. Two things follow for tests. A sweep started by
    _on_startup_status_ready is scheduled by a QTimer in __init__, so it fires
    whenever the event loop happens to turn -- mid-test, unpredictably, and
    landing results computed from whatever the game looked like then. And a
    sweep started inside a test body would still be running when the assertions
    run, leaving status_cache empty.

    Substituting a subclass whose start() runs inline makes the whole file
    deterministic and restores the pre-worker semantics: by the time _rescan_all
    returns, the cache is filled. The module-level reference to the real class
    is kept below so the threading itself can still be tested directly --
    see TestStatusSweepIsOffThread.
    """
    monkeypatch.setattr(main_window_module, "StatusWorker", _InlineStatusWorker)


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
        main_window._start_status_sweep()
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
            self, tracked, main_window, monkeypatch, save_root, backup_root, tmp_path):
        # Its own folder: sharing save_root with the tracked game would be a
        # second game on one folder, which the add/edit guard now asks about.
        make_game(main_window, "AAA Game", tmp_path / "aaa_saves")
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


class TestDuplicateSaveFolderWarning:
    """Adding or editing a game onto a folder another game already has.

    Warned about, not refused. Two games can share a save folder on purpose
    (one folder deliberately tracked under a second name), so the choice is the
    user's -- but left unmentioned it is also how a duplicate appears by
    accident, with two backup histories over one folder that drift apart and a
    restore that silently ignores whichever one was not chosen.
    """

    def fail_if_asked(self, monkeypatch):
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: pytest.fail("should not have asked about a duplicate"),
        )

    def answer(self, monkeypatch, reply):
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda parent, title, text, *a, **k: asked.append((title, text)) or reply,
        )
        return asked

    # ---- adding ----

    def test_adding_over_a_tracked_folder_asks_first(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        asked = self.answer(monkeypatch, QMessageBox.No)
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root, name="Hades Again"))

        main_window._on_add_game()

        assert len(asked) == 1
        assert tracked.id in main_window.games
        assert [g.name for g in main_window.games.values()] == ["Hades"]

    def test_declining_adds_nothing_at_all(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        self.answer(monkeypatch, QMessageBox.No)
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root, name="Hades Again"))

        main_window._on_add_game()

        assert main_window.games == {tracked.id: tracked}
        assert config.load_games() == []
        assert card_order(main_window) == ["Hades"]

    def test_confirming_adds_the_second_game(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        self.answer(monkeypatch, QMessageBox.Yes)
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root, name="Hades Again"))

        main_window._on_add_game()

        assert sorted(g.name for g in main_window.games.values()) == [
            "Hades", "Hades Again",
        ]
        assert len(config.load_games()) == 2

    def test_a_genuinely_new_folder_is_added_without_asking(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        self.fail_if_asked(monkeypatch)
        other = write_files(save_root.parent / "Celeste", {"slot.sav": "a"})
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(other, backup_root, name="Celeste"))

        main_window._on_add_game()

        assert sorted(g.name for g in main_window.games.values()) == [
            "Celeste", "Hades",
        ]

    def test_the_warning_names_the_game_already_there(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        asked = self.answer(monkeypatch, QMessageBox.No)
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root, name="Hades Again"))

        main_window._on_add_game()

        title, text = asked[0]
        assert "already tracked" in title
        assert "Hades" in text
        assert str(save_root) in text

    def test_asking_defaults_to_no(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        defaults = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda parent, title, text, buttons, default: (
                defaults.append(default) or QMessageBox.No
            ),
        )
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root, name="Hades Again"))

        main_window._on_add_game()

        # Enter on a prompt about a duplicate must not create one.
        assert defaults == [QMessageBox.No]

    def test_a_link_to_a_tracked_folder_asks_too(
            self, tracked, main_window, monkeypatch, save_root, backup_root,
            tmp_path, junction):
        asked = self.answer(monkeypatch, QMessageBox.No)
        junction(save_root, "HadesLink", parent=tmp_path)
        stub_dialog(monkeypatch, "AddGameDialog", result_data=new_game_data(
            tmp_path / "HadesLink", backup_root, name="Hades Link"))

        main_window._on_add_game()

        assert len(asked) == 1
        assert len(main_window.games) == 1

    # ---- editing ----

    def test_moving_a_game_onto_another_asks_first(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        other = write_files(save_root.parent / "Celeste", {"slot.sav": "a"})
        celeste = make_game(main_window, "Celeste", other)
        asked = self.answer(monkeypatch, QMessageBox.No)
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(other, backup_root, name="Hades"))

        main_window._on_edit_game(tracked.id)

        assert len(asked) == 1
        # Declined, so the move is not applied: the game keeps its own folder.
        assert main_window.games[tracked.id].save_path == str(save_root)
        assert main_window.games[celeste.id].save_path == str(other)

    def test_confirming_the_move_applies_it(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        other = write_files(save_root.parent / "Celeste", {"slot.sav": "a"})
        make_game(main_window, "Celeste", other)
        self.answer(monkeypatch, QMessageBox.Yes)
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(other, backup_root, name="Hades"))

        main_window._on_edit_game(tracked.id)

        assert main_window.games[tracked.id].save_path == str(other)

    def test_re_saving_the_same_folder_does_not_ask(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        # The game being edited never conflicts with itself, so an ordinary
        # rename or backup-folder change stays silent.
        self.fail_if_asked(monkeypatch)
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root, name="Hades II"))

        main_window._on_edit_game(tracked.id)

        assert main_window.games[tracked.id].name == "Hades II"

    def test_a_rename_is_not_treated_as_a_duplicate_name(
            self, tracked, main_window, monkeypatch, save_root, backup_root):
        self.fail_if_asked(monkeypatch)
        make_game(main_window, "Hades II", save_root.parent / "hades2")
        stub_dialog(monkeypatch, "AddGameDialog",
                    result_data=new_game_data(save_root, backup_root, name="Hades II"))

        main_window._on_edit_game(tracked.id)

        assert main_window.games[tracked.id].name == "Hades II"


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

    def test_each_backup_is_started_by_a_separate_call(self, main_window, tmp_path,
                                                       monkeypatch, qtbot):
        # What this actually pins: the queue drains one game per _start_backup
        # call, so a backup that completes instantly cannot re-enter the loop
        # that started it. It is *not* a claim that backups run one at a time --
        # they run concurrently, bounded by MAX_CONCURRENT_BACKUPS. The old
        # version of this test was named "one_at_a_time" while asserting only
        # this, which read as a serialization guarantee the code never made.
        games, _requested = stage_never_backed_up_games(main_window, tmp_path)
        in_flight = []
        batches = []

        def one_at_a_time(game_id):
            batches.append(list(in_flight))
            in_flight.append(game_id)

        main_window._start_backup = one_at_a_time
        main_window._on_backup_all()
        qtbot.waitUntil(lambda: len(in_flight) == 3, timeout=2000)
        assert all(len(batch) == index for index, batch in enumerate(batches))

    def test_skips_games_that_are_already_up_to_date(self, main_window, tmp_path,
                                                     monkeypatch, qtbot):
        requested = []
        changed = make_game(main_window, "Changed", tmp_path / "a")
        make_game(main_window, "Synced", tmp_path / "b")
        main_window.status_cache = {gid: "changed" for gid in main_window.games}
        for gid in list(main_window.games)[1:]:
            main_window.status_cache[gid] = "synced"
        main_window._start_backup = requested.append
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


class TestConcurrentBackupCeiling:
    """The ceiling is the point of this: "Backup All" used to start one thread
    per game needing a backup, all live at once (measured 12 concurrent for a
    12-game library), with every one of them walking a different save folder
    and writing then re-reading an archive."""

    def stage(self, main_window, tmp_path, count):
        games = [make_game(main_window, f"Game {index}", tmp_path / f"g{index}")
                 for index in range(count)]
        for game in games:
            main_window.status_cache[game.id] = "never"
        return games

    def test_no_more_than_the_ceiling_start_at_once(self, main_window, tmp_path):
        games = self.stage(main_window, tmp_path, 9)
        started = []
        main_window._start_backup = lambda gid: (
            started.append(gid), main_window.workers.__setitem__(gid, RunningWorker())
        )

        main_window._on_backup_all()

        assert len(started) == MAX_CONCURRENT_BACKUPS
        assert len(main_window._backup_queue) == 9 - MAX_CONCURRENT_BACKUPS
        # And the ones that started are exactly the ceiling's worth, not all nine.
        assert set(started) == {game.id for game in games[:MAX_CONCURRENT_BACKUPS]}

    def test_a_freed_slot_starts_the_next_waiting_backup(self, main_window, tmp_path):
        self.stage(main_window, tmp_path, 6)
        started = []
        main_window._start_backup = lambda gid: (
            started.append(gid), main_window.workers.__setitem__(gid, RunningWorker())
        )
        main_window._on_backup_all()
        assert len(started) == MAX_CONCURRENT_BACKUPS

        # One finishes the way a real worker would: QThread's finished signal,
        # which is what drops the reference.
        finished_id = started[0]
        main_window._on_backup_worker_finished(finished_id)

        assert len(started) == MAX_CONCURRENT_BACKUPS + 1
        assert finished_id not in main_window.workers
        assert len(main_window._backup_queue) == 6 - len(started)

    def test_the_queue_drains_one_per_completion(self, main_window, tmp_path):
        total = 9
        self.stage(main_window, tmp_path, total)
        started = []
        main_window._start_backup = lambda gid: (
            started.append(gid), main_window.workers.__setitem__(gid, RunningWorker())
        )
        main_window._on_backup_all()

        while main_window._backup_queue or main_window.workers:
            running = list(main_window.workers)
            assert len(running) <= MAX_CONCURRENT_BACKUPS
            main_window._on_backup_worker_finished(running[0])

        assert len(started) == total
        assert len(set(started)) == total      # nothing queued or started twice

    def test_a_card_button_press_shares_the_same_budget(self, main_window, tmp_path):
        # The ceiling is enforced in the shared path, so it holds however the
        # backup was asked for rather than only for the batch button.
        self.stage(main_window, tmp_path, 10)
        started = []
        main_window._start_backup = lambda gid: (
            started.append(gid), main_window.workers.__setitem__(gid, RunningWorker())
        )

        for game in main_window.games.values():
            main_window._on_backup_requested(game.id)

        assert len(started) == MAX_CONCURRENT_BACKUPS

    def test_a_game_already_backing_up_is_not_queued_again(self, main_window, tmp_path):
        game = make_game(main_window, "Running", tmp_path / "running")
        main_window.workers[game.id] = RunningWorker()

        main_window._on_backup_requested(game.id)
        main_window._on_backup_requested(game.id)

        assert main_window._backup_queue == []

    def test_a_game_already_waiting_is_not_queued_twice(self, main_window, tmp_path):
        self.stage(main_window, tmp_path, 6)
        main_window._start_backup = lambda gid: main_window.workers.__setitem__(
            gid, RunningWorker())
        main_window._on_backup_all()
        waiting = list(main_window._backup_queue)
        assert waiting

        main_window._on_backup_requested(waiting[0])

        assert main_window._backup_queue == waiting

    def test_the_button_is_re_enabled_once_the_queue_is_empty(self, main_window, tmp_path):
        # Nothing is waiting, so there is nothing for the button to be blocked
        # on -- games already in flight are filtered out of the next Backup All
        # anyway.
        self.stage(main_window, tmp_path, 2)
        main_window._start_backup = lambda gid: main_window.workers.__setitem__(
            gid, RunningWorker())
        main_window.backup_all_btn.setEnabled(False)

        main_window._on_backup_all()

        assert main_window.backup_all_btn.isEnabled() is True


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
        # Cleared first, or this passes on the status the `tracked` fixture
        # left behind rather than on anything the rescan did.
        main_window.status_cache.clear()
        button_named(main_window, "Rescan").click()
        assert tracked.id in main_window.status_cache


# ---------------------------------------------------------------------------
# the whole-library sweep runs off the main thread
# ---------------------------------------------------------------------------

class TestStatusSweepIsOffThread:
    """The bug: walking every save folder inline starved the event loop
    outright -- a 5ms timer received zero deliveries across a 461ms sweep. So
    what matters is not only that the statuses are right, but that the walk
    happens on another thread.

    The file-wide inline_status_sweeps fixture keeps MainWindow's own sweeps
    synchronous for determinism, so the threading is exercised here against the
    real class directly.
    """

    def test_the_walk_runs_on_another_thread(self, save_root, qtbot, monkeypatch):
        walked_on = []

        def record(save_path, last_backup_time, last_snapshot):
            walked_on.append(threading.get_ident())
            return "synced"

        monkeypatch.setattr(main_window_module, "_status_for", record)
        results = []
        worker = _REAL_STATUS_WORKER([("game-1", str(save_root), "2026-01-01", None)])
        worker.status_finished.connect(results.append)
        try:
            worker.start()
            qtbot.waitUntil(lambda: bool(results), timeout=5000)
        finally:
            worker.wait()   # never leave the OS thread running past the test
        assert walked_on
        assert walked_on[0] != threading.get_ident()

    def test_the_worker_reports_every_game_it_was_given(self, save_root, qtbot):
        results = []
        worker = _REAL_STATUS_WORKER([
            ("a", str(save_root), "2026-01-01", None),
            ("b", str(save_root), None, None),
            ("c", str(save_root), "2026-01-01", {}),
        ])
        worker.status_finished.connect(results.append)
        try:
            worker.start()
            qtbot.waitUntil(lambda: bool(results), timeout=5000)
        finally:
            worker.wait()
        assert set(results[0]) == {"a", "b", "c"}
        assert results[0]["b"] == "never"   # never backed up: no disk work at all

    def test_an_unreadable_folder_is_reported_without_losing_the_rest(
            self, save_root, monkeypatch):
        def record(save_path, last_backup_time, last_snapshot):
            if save_path == "denied":
                raise OSError(5, "Access is denied")
            return "synced"

        monkeypatch.setattr(main_window_module, "_status_for", record)
        errors, results = [], []
        worker = _REAL_STATUS_WORKER([
            ("a", str(save_root), "2026-01-01", None),
            ("b", "denied", "2026-01-01", None),
        ])
        worker.status_error.connect(errors.append)
        worker.status_finished.connect(results.append)
        worker.run()

        assert errors and "denied" in errors[0]
        # The other game's status still arrives: one unreadable folder must not
        # abandon the rest of the library. The failed one is simply absent, so
        # it keeps whatever status it already had rather than being given a
        # guess.
        assert set(results[0]) == {"a"}

    def test_the_worker_does_not_shadow_qthreads_own_finished_signal(self):
        # Callers wait on `finished` knowing the OS thread has really ended.
        worker = _REAL_STATUS_WORKER([])
        assert "finished" not in type(worker).__dict__
        assert hasattr(worker.finished, "connect")

    def test_a_game_deleted_mid_sweep_is_not_resurrected(self, main_window, tracked):
        # The `tracked` fixture fills the cache, so start from empty.
        main_window.status_cache.clear()
        main_window.games.pop(tracked.id)
        main_window._start_status_sweep()
        # The worker still returns an answer for it; applying that would put a
        # status in the cache for a game that no longer exists.
        assert tracked.id not in main_window.status_cache

    def test_the_in_flight_guard_is_released_after_a_sweep(self, main_window, tracked):
        main_window._start_status_sweep()
        assert main_window.status_worker is None   # cleared once finished fires

    def test_a_second_sweep_while_one_is_running_is_refused(self, main_window,
                                                            tracked, monkeypatch):
        released = []
        monkeypatch.setattr(
            main_window_module.StatusWorker, "start",
            lambda self: released.append(self),   # never finishes
        )
        main_window._start_status_sweep()
        first = main_window.status_worker

        main_window._rescan_all()

        # Both cover the whole library, so the running one already answers it.
        assert main_window.status_worker is first
        assert len(released) == 1

    def test_the_guard_is_reported_rather_than_silently_ignored(self, main_window,
                                                               tracked, monkeypatch):
        monkeypatch.setattr(
            main_window_module.StatusWorker, "start", lambda self: None)
        main_window._start_status_sweep()
        main_window.status_bar.clearMessage()

        main_window._rescan_all()

        assert "already running" in main_window.status_bar.currentMessage()

    def test_startup_sweeps_through_the_worker(self, main_window, tracked):
        started = []
        monkey_target = main_window_module.StatusWorker

        class Recording(monkey_target):
            def start(self):
                started.append(self)
                self.run()
                self.finished.emit()

        main_window_module.StatusWorker = Recording
        try:
            main_window.status_cache.clear()
            main_window._on_startup_status_ready()
        finally:
            main_window_module.StatusWorker = monkey_target

        assert len(started) == 1
        assert tracked.id in main_window.status_cache

    def test_the_welcome_summary_follows_the_startup_sweep(self, main_window, tracked):
        main_window.status_bar.clearMessage()
        main_window._on_startup_status_ready()
        assert "need a backup" in main_window.status_bar.currentMessage()

    def test_nothing_to_check_still_shows_the_summary(self, main_window):
        # No games means no worker and therefore no signal; the caller must not
        # be left waiting for one that will never arrive.
        main_window.status_bar.clearMessage()
        main_window._on_startup_status_ready()
        assert main_window.status_worker is None

    def test_a_single_game_update_stays_synchronous(self, main_window, tracked):
        # One folder is a few milliseconds and its answer is needed right now,
        # so this path deliberately does not go through a worker -- the result
        # is in the cache by the time the call returns, with nothing started.
        started = []
        monkey_target = main_window_module.StatusWorker
        main_window_module.StatusWorker = type(
            "Recording", (monkey_target,), {"start": lambda self: started.append(self)}
        )
        try:
            main_window.status_cache.clear()
            main_window._recompute_statuses(game_ids=[tracked.id])
        finally:
            main_window_module.StatusWorker = monkey_target

        assert tracked.id in main_window.status_cache
        assert started == []

    def test_closing_waits_for_a_running_sweep(self, main_window, tracked, monkeypatch):
        # A QThread destroyed while it is running is a fatal Qt error, and the
        # worker is deliberately not parented to the window, so closeEvent has
        # to let it finish rather than drop the last reference to it.
        def slow(save_path, last_backup_time, last_snapshot):
            time.sleep(0.05)
            return "synced"

        monkeypatch.setattr(main_window_module, "_status_for", slow)
        monkeypatch.setattr(main_window_module, "StatusWorker", _REAL_STATUS_WORKER)
        main_window._start_status_sweep()
        worker = main_window.status_worker
        assert worker.isRunning()

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is True
        # No prompt for a read-only sweep, but it was waited out rather than
        # abandoned -- so the thread object is safe to destroy.
        assert not worker.isRunning()


# ---------------------------------------------------------------------------
# scanning for games
# ---------------------------------------------------------------------------

class TestFirstScan:
    """The first-launch offer.

    The scan reads Documents, AppData and every Steam library on the machine,
    so it is asked about rather than started: nothing here may walk the disk
    before the user has agreed to it.
    """

    def scan_stub(self, monkeypatch):
        started = []
        monkeypatch.setattr(MainWindow, "_start_scan", lambda self: started.append(1))
        return started

    def answer_prompt(self, monkeypatch, reply=QMessageBox.Yes):
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda parent, title, text, *a, **k: asked.append((title, text)) or reply,
        )
        return asked

    def test_offers_the_scan_once_then_never_again(self, app_dir, monkeypatch, qtbot):
        started = self.scan_stub(monkeypatch)
        asked = self.answer_prompt(monkeypatch)
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            qtbot.waitUntil(lambda: bool(started), timeout=2000)
            state = json.loads((app_dir / "state.json").read_text(encoding="utf-8"))
            assert state["first_scan_done"] is True
            assert len(asked) == 1

            window._maybe_run_first_scan()  # a second launch must not ask again
            qtbot.wait(50)
            assert len(started) == 1
            assert len(asked) == 1
        finally:
            window.close()

    def test_declining_scans_nothing_and_is_not_asked_again(
            self, app_dir, monkeypatch, qtbot):
        started = self.scan_stub(monkeypatch)
        asked = self.answer_prompt(monkeypatch, QMessageBox.No)
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            qtbot.waitUntil(lambda: bool(asked), timeout=2000)
            qtbot.wait(50)

            assert started == [], "a declined offer must not scan"
            state = json.loads((app_dir / "state.json").read_text(encoding="utf-8"))
            assert state["first_scan_declined"] is True

            window._maybe_run_first_scan()  # and it must not come back
            qtbot.wait(50)
            assert len(asked) == 1
        finally:
            window.close()

    def test_the_prompt_says_what_would_be_read(self, app_dir, monkeypatch, qtbot):
        started = self.scan_stub(monkeypatch)
        asked = self.answer_prompt(monkeypatch, QMessageBox.No)
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            qtbot.waitUntil(lambda: bool(asked), timeout=2000)
        finally:
            window.close()
        title, text = asked[0]
        # The user is being asked permission to read their disk; say which
        # folders and that nothing is added without them.
        assert "Documents" in text and "Steam" in text
        assert "Scan Games" in text
        assert title == "Look for installed games?"

    def test_the_prompt_defaults_to_no(self, app_dir, monkeypatch, qtbot):
        self.scan_stub(monkeypatch)
        defaults = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda parent, title, text, buttons, default: (
                defaults.append(default) or QMessageBox.No
            ),
        )
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            qtbot.waitUntil(lambda: bool(defaults), timeout=2000)
        finally:
            window.close()
        # Enter on a permission prompt must not grant it.
        assert defaults == [QMessageBox.No]

    def test_no_prompt_and_no_scan_when_the_setting_is_off(
            self, app_dir, monkeypatch, qtbot):
        started = self.scan_stub(monkeypatch)
        asked = self.answer_prompt(monkeypatch)
        # MainWindow reads its settings through load_settings() in __init__, so
        # this is what turns the offer off for a window built from here on.
        monkeypatch.setattr(main_window_module, "load_settings",
                            lambda: {"auto_scan_on_first_run": False})
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            assert window.settings["auto_scan_on_first_run"] is False
            window._maybe_run_first_scan()
            # Past the 500ms delay the offer would have fired by now, so this
            # fails rather than passes by waiting less than the timer.
            qtbot.wait(900)
            assert asked == []
            assert started == []
        finally:
            window.close()

    def test_is_skipped_once_it_has_run(self, app_dir, monkeypatch, qtbot):
        started = self.scan_stub(monkeypatch)
        asked = self.answer_prompt(monkeypatch)
        mark_first_scan_done()
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            qtbot.wait(50)
            assert started == []
            assert asked == []
        finally:
            window.close()

    def test_the_offer_is_delayed_so_the_window_can_paint(self, app_dir, monkeypatch, qtbot):
        started = self.scan_stub(monkeypatch)
        asked = self.answer_prompt(monkeypatch)
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._maybe_run_first_scan()
            # Still nothing: a dialog popping up before the window is visible
            # looks like a crash.
            assert started == []
            assert asked == []
            qtbot.waitUntil(lambda: bool(asked), timeout=2000)
            qtbot.waitUntil(lambda: bool(started), timeout=2000)
        finally:
            window.close()

    def test_turning_the_setting_back_on_re_arms_a_declined_offer(
            self, app_dir, monkeypatch, qtbot):
        # Otherwise the switch is inert for the one user who needs it: the one
        # who said no. first_scan_done is what silences the prompt, so it has
        # to be cleared along with the record of the refusal.
        config.save_state({"first_scan_done": True, "first_scan_declined": True})
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._rearm_first_scan_offer()
            state = json.loads((app_dir / "state.json").read_text(encoding="utf-8"))
            assert "first_scan_done" not in state
            assert "first_scan_declined" not in state
        finally:
            window.close()

    def test_re_arming_leaves_an_accepted_offer_alone(self, app_dir, monkeypatch, qtbot):
        # Someone who already scanned had the question settled; re-asking
        # would undo an answer they gave.
        config.save_state({"first_scan_done": True})
        window = MainWindow()
        qtbot.addWidget(window)
        try:
            window._rearm_first_scan_offer()
            state = json.loads((app_dir / "state.json").read_text(encoding="utf-8"))
            assert state["first_scan_done"] is True
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
        # Two distinct folders: two candidates naming one folder is a duplicate,
        # not a second game (see TestAddScannedGamesDeduplication).
        second = write_files(tmp_path / "saves" / "Celeste", {"slot.sav": "a"})
        self.scan(main_window,
                   self.candidate(save_root, "Hades"),
                   self.candidate(second, "Celeste"))
        assert len(config.load_games()) == 2

    def test_reports_how_many_were_added(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        second = write_files(tmp_path / "saves" / "Celeste", {"slot.sav": "a"})
        self.scan(main_window,
                   self.candidate(save_root, "Hades"),
                   self.candidate(second, "Celeste"))
        assert "Added 2 games from scan" in main_window.status_bar.currentMessage()

    def test_one_game_is_reported_in_the_singular(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        self.scan(main_window, self.candidate(save_root, "Hades"))
        assert "Added 1 game from scan" in main_window.status_bar.currentMessage()

    def test_cards_are_added_in_sorted_order(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        second = write_files(tmp_path / "saves" / "Zelda", {"slot.sav": "a"})
        self.scan(main_window,
                   self.candidate(save_root, "Zelda"),
                   self.candidate(second, "Another Game"))
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


class TestAddScannedGamesDeduplication:
    """Adding from a scan must never produce a second game for one folder.

    The scanner filters candidates against the tracked paths, but that check
    lives in another module and runs at scan time. _add_scanned_games is the
    point where a Game -- with its own id, its own backup folder and its own
    row in games.json -- actually comes into existence, so it is the last
    place a duplicate can be stopped, and the only one that sees both the
    candidates and the games they would duplicate.
    """

    def candidate(self, save_root, name="Hades"):
        return game_scanner.ScanCandidate(name, str(save_root), "My Games", "high")

    def test_a_folder_already_tracked_is_not_added_again(
            self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        make_game(main_window, "Hades", save_root)

        main_window._add_scanned_games([self.candidate(save_root)])

        assert len(main_window.games) == 1
        # Nothing was added, so nothing was written: no second entry in
        # games.json, and the tracked game's own record is untouched.
        assert config.load_games() == []

    def test_the_same_folder_twice_in_one_batch_is_added_once(
            self, main_window, save_root, tmp_path):
        # Two scanners describing one folder: a publisher folder and the game
        # inside it, or two sources reaching the same saves.
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}

        main_window._add_scanned_games([
            self.candidate(save_root, "Hades"),
            self.candidate(save_root, "Hades (Publisher)"),
        ])

        assert len(main_window.games) == 1
        assert len(config.load_games()) == 1

    def test_the_newest_spelling_of_a_duplicated_folder_is_dropped(
            self, main_window, save_root, tmp_path):
        # Two spellings of one folder inside a single batch: the later one must
        # not claim the folder, or the user is left with whichever entry
        # happened to be added last and no idea which is which.
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        spellings = [
            str(save_root),
            str(save_root) + os.sep + "SaveData" + os.sep + os.pardir,
        ]

        main_window._add_scanned_games([
            self.candidate(save_root, "Hades"),
            game_scanner.ScanCandidate("Hades", spellings[1], "My Games", "high"),
        ])

        assert len(main_window.games) == 1
        assert next(iter(main_window.games.values())).save_path == str(save_root)

    def test_a_link_to_a_tracked_folder_is_not_a_second_game(
            self, main_window, save_root, tmp_path, junction):
        # Same folder, reached through a junction. The rest of the app already
        # treats that as the same folder -- paths_conflict and the backup
        # containment check both compare with path_key, which resolves links --
        # so tracking it twice would give one game two save folders the app
        # believes are different and one of them a backup root that conflicts
        # with its own save folder.
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        make_game(main_window, "Hades", save_root)
        link = tmp_path / "HadesLink"
        junction(save_root, link.name, parent=tmp_path)

        main_window._add_scanned_games([self.candidate(link, "Hades")])

        assert len(main_window.games) == 1
        assert config.load_games() == []

    def test_the_report_counts_only_what_was_actually_added(
            self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        make_game(main_window, "Celeste", tmp_path / "celeste")

        main_window._add_scanned_games([self.candidate(save_root, "Hades")])

        assert "Added 1 game from scan" in main_window.status_bar.currentMessage()

    def test_nothing_added_is_reported_rather_than_counted(
            self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        make_game(main_window, "Hades", save_root)

        main_window._add_scanned_games([self.candidate(save_root)])

        assert "Nothing new to add" in main_window.status_bar.currentMessage()
        assert "1 folder already tracked" in main_window.status_bar.currentMessage()

    def test_a_mixed_batch_reports_both_halves(self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        other = write_files(tmp_path / "saves" / "Celeste", {"slot.sav": "a"})
        make_game(main_window, "Celeste", other)

        main_window._add_scanned_games([
            self.candidate(save_root, "Hades"),
            self.candidate(other, "Celeste"),
        ])

        message = main_window.status_bar.currentMessage()
        assert "Added 1 game from scan" in message
        assert "Skipped 1 already-tracked folder" in message

    def test_a_genuinely_new_folder_is_still_added_alongside_a_duplicate(
            self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        other = write_files(tmp_path / "saves" / "Celeste", {"slot.sav": "a"})
        make_game(main_window, "Celeste", other)

        main_window._add_scanned_games([
            self.candidate(save_root, "Hades"),
            self.candidate(other, "Celeste"),
        ])

        assert sorted(g.name for g in main_window.games.values()) == [
            "Celeste", "Hades",
        ]

    def test_a_batch_of_only_duplicates_adds_no_cards(
            self, main_window, save_root, tmp_path):
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        make_game(main_window, "Hades", save_root)
        before = card_order(main_window)

        main_window._add_scanned_games([self.candidate(save_root, "Hades")])

        assert card_order(main_window) == before

    def test_case_differences_are_not_a_new_game(self, main_window, save_root, tmp_path):
        # Only meaningful where the filesystem has case to fold; on POSIX
        # "Hades" and "HADES" really are two folders and both are legitimate.
        different_case = str(save_root).upper()
        if different_case == str(save_root):
            pytest.skip("filesystem path has no case to fold on this platform")
        main_window.settings = {"default_scan_backup_root": str(tmp_path / "backups")}
        make_game(main_window, "Hades", save_root)

        main_window._add_scanned_games([
            game_scanner.ScanCandidate("Hades", different_case, "My Games", "high")
        ])

        assert len(main_window.games) == 1


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
        assert "backing up game" in asked[0]
        assert "cancelled" in asked[0]

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

    def test_a_running_scan_is_waited_out_without_a_prompt(self, main_window, monkeypatch):
        # A scan only reads -- see closeEvent -- so it isn't confirmed, and it
        # isn't cancelled either: it simply finishes and the close proceeds.
        main_window.scan_worker = RunningWorker()
        asked = []

        def trap(parent, title, text, *a, **k):
            asked.append(text)
            return QMessageBox.No

        monkeypatch.setattr(QMessageBox, "question", trap)
        event = FakeEvent()
        main_window.closeEvent(event)

        assert asked == []          # no prompt for read-only work
        assert event.accepted is True
        assert not main_window.scan_worker.isRunning()  # ...but it was waited out


class TestCloseWaitsOutRunningThreads:
    """Quitting has to mean every background thread has ended, not that the
    user said so.

    A QThread destroyed while it is still running is a fatal Qt error, not an
    exception: the process aborts with 0xC0000409 and no traceback (confirmed
    by dropping a running unparented QThread). None of this app's workers is
    parented to the window -- deliberately, since closeEvent runs underneath
    them -- so accepting the close while one is live hands its last Python
    reference to the garbage collector mid-run. Quitting during a backup used
    to do exactly that, on the Yes path of the prompt.
    """

    def _confirm_quit(self, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    def test_a_backup_in_flight_is_waited_out_before_closing(self, main_window,
                                                              monkeypatch):
        worker = RunningWorker()
        main_window.workers["hades"] = worker
        self._confirm_quit(monkeypatch)

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is True
        # Not merely accepted after a token wait -- the thread was waited on
        # until it actually ended, which is what makes its destruction safe.
        assert worker.wait_calls > 0
        assert not worker.isRunning()

    def test_a_scan_in_flight_is_waited_out_before_closing(self, main_window,
                                                           monkeypatch):
        main_window.scan_worker = RunningWorker()
        self._confirm_quit(monkeypatch)

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is True
        assert not main_window.scan_worker.isRunning()

    def test_a_backup_and_a_sweep_are_both_waited_out(self, main_window, monkeypatch):
        backup = RunningWorker()
        sweep = RunningWorker()
        main_window.workers["hades"] = backup
        main_window.status_worker = sweep
        self._confirm_quit(monkeypatch)

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is True
        assert not backup.isRunning()
        assert not sweep.isRunning()

    def test_work_that_never_finishes_refuses_the_close(self, main_window, monkeypatch):
        # A share that stopped answering is the realistic case. Closing anyway
        # would mean either an aborted process or a half-written archive left
        # to pass for a backup, so the window stays open and keeps waiting.
        monkeypatch.setattr(main_window_module, "_SHUTDOWN_GRACE_MS", 0)
        warned = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda parent, title, text, *a, **k: warned.append(text),
        )
        stuck = RunningWorker(finishes_on_wait=False)
        main_window.workers["hades"] = stuck
        self._confirm_quit(monkeypatch)

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is False
        assert stuck.isRunning()          # left alone, not killed
        assert warned
        # And the window is fully usable again afterwards -- the next close has
        # to be allowed to try once the thread has finished.
        assert main_window._closing is False

    def test_declining_leaves_the_window_fully_usable(self, main_window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        main_window.workers["hades"] = RunningWorker()

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is False
        assert main_window._closing is False

    def test_a_second_close_mid_drain_is_ignored(self, main_window, monkeypatch):
        # The event loop is pumped while draining, so the title bar's X is live
        # and can be clicked again. A second close must not start a second drain
        # over the same threads; the first one accepts on their behalf.
        main_window._closing = True
        event = FakeEvent()
        main_window.closeEvent(event)
        assert event.accepted is False

    def test_a_backup_result_still_lands_while_draining(self, main_window, monkeypatch):
        # The drain pumps the event loop on purpose: a backup that finishes
        # during shutdown reports through a queued signal on this thread, and
        # discarding it would leave a good archive on disk that games.json never
        # recorded -- so the next launch would report the game as changed and
        # take the same backup again.
        delivered = []
        monkeypatch.setattr(
            main_window, "_on_backup_finished",
            lambda *a: delivered.append(a[0]),
        )
        worker = RunningWorker()
        main_window.workers["hades"] = worker
        self._confirm_quit(monkeypatch)

        # Stands in for the queued signal arriving while wait() blocks.
        original_wait = worker.wait

        def wait_then_report(timeout=None):
            result = original_wait(timeout)
            QTimer.singleShot(0, lambda: main_window._on_backup_finished("hades"))
            return result

        worker.wait = wait_then_report

        main_window.closeEvent(FakeEvent())

        assert delivered == ["hades"]


class TestShutdownSuppressesIrreversibleWork:
    """The shutdown drain pumps the event loop, so the handlers it delivers to
    run while the window is on its way out. Anything irreversible in them has
    to stand down, or closing would start new work and put modals on screen
    over the drain that is waiting on the very thread reporting the problem.
    """

    def test_no_new_backups_start_while_shutting_down(self, main_window):
        started = []
        main_window._start_backup = started.append
        main_window._backup_queue = ["a", "b", "c"]
        main_window._closing = True

        main_window._pump_backup_queue()

        assert started == []

    def test_a_backup_error_during_shutdown_does_not_pop_a_modal(self, main_window,
                                                                monkeypatch):
        popped = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda *a, **k: popped.append(a[1]),
        )
        main_window._closing = True

        main_window._on_backup_error("hades", "the disk went away")

        assert popped == []
        assert "the disk went away" in main_window.status_bar.currentMessage()

    def test_a_failed_backup_still_pops_a_modal_normally(self, main_window, monkeypatch):
        popped = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda *a, **k: popped.append(a[1]),
        )
        main_window._closing = False

        main_window._on_backup_error("hades", "the disk went away")

        assert popped == ["Backup failed"]

    def test_a_scan_landing_mid_shutdown_is_dropped(self, main_window, monkeypatch):
        shown = []
        monkeypatch.setattr(
            main_window_module, "ScanResultsDialog",
            lambda *a, **k: shown.append(a),
        )
        main_window._closing = True

        main_window._on_scan_finished([game_scanner.ScanCandidate(
            name="Hades", save_path="C:/saves/hades", source="Steam",
            confidence="high",
        )])

        assert shown == []


class TestCloseCancelsBackups:
    """"Quit anyway" means quit now, not "wait for the backup to finish".

    The user clicking Yes to the shutdown prompt has, by definition, decided
    that getting out is more important than the in-flight backup. Rather than
    wait for a long archive to complete (which is what the naive fix for the
    quit-during-backup crash does), SaveGuard cancels running backups at the
    next file boundary and lets the existing discard path throw away whatever
    partial archive was started -- so the user leaves promptly with no corrupt
    archive left behind. Read-only scans and status sweeps are not cancelled,
    only waited out.
    """

    def test_confirming_quit_cancels_in_flight_backups(self, main_window, monkeypatch):
        worker = RunningWorker()
        main_window.workers["hades"] = worker
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is True
        assert worker.cancel_calls == 1
        assert worker.cancelled is True

    def test_declining_quit_does_not_cancel_backups(self, main_window, monkeypatch):
        worker = RunningWorker()
        main_window.workers["hades"] = worker
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)

        event = FakeEvent()
        main_window.closeEvent(event)

        assert event.accepted is False
        assert worker.cancel_calls == 0
        assert worker.cancelled is False

    def test_a_backup_in_flight_is_cancelled_before_the_close_is_accepted(
            self, main_window, qtbot, tmp_path, monkeypatch):
        # End-to-end with a real BackupWorker: the worker is on a different OS
        # thread, cancel() is a flag set from this one, and the thread must
        # notice it at the next file boundary, raise BackupCancelled, discard
        # any partial archive and terminate -- so the only references left are
        # ones that are safe to destroy.
        from app.backup_worker import BackupWorker
        import app.backup_worker as bwmod

        saves = tmp_path / "saves"
        saves.mkdir()
        out = tmp_path / "out"
        # Enough files to make the walk span a few milliseconds, and a patched
        # islink that adds a tiny delay widens the cancellation window so the
        # test is deterministic rather than a race against a fast SSD.
        big = 3000
        for i in range(big):
            (saves / f"save{i}.dat").write_text("x")
        real_islink = bwmod.os.path.islink
        def slow_islink(p):
            time.sleep(0.0002)
            return real_islink(p)
        monkeypatch.setattr(bwmod.os.path, "islink", slow_islink)

        worker = BackupWorker("hades", "Hades", str(saves), str(out))
        worker.start()
        assert worker.isRunning()
        # The walk is deliberately slow; cancelling now must be observed while
        # it is still collecting files, before any archive is opened.
        worker.cancel()
        assert worker.wait(5000)

        assert not worker.isRunning()
        # The partial archive -- if one was even opened -- is gone.
        assert not out.exists() or not any(out.iterdir())

    def test_a_finished_backup_is_not_cancelled_on_quit(self, main_window, monkeypatch):
        worker = FinishedWorker()
        main_window.workers["hades"] = worker
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

        main_window.closeEvent(FakeEvent())

        assert worker.cancel_calls == 0


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