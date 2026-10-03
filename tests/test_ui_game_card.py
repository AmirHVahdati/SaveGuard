"""app/ui/game_card.py — the per-game card widget and its formatting helpers."""

from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QDesktopServices, QMouseEvent
from PySide6.QtWidgets import QPushButton

from app.models import Game
from app.ui import game_card as game_card_module
from app.ui.game_card import (
    AVATAR_COLORS, GameCard, STATUS_TEXT, _default_color_for,
)


def elide(path, max_len=58):
    """The card's path-shortening helper, called without a widget instance."""
    return GameCard._elide(None, path, max_len)


@pytest.fixture
def game(save_root, backup_root):
    return Game.new("Cyberpunk 2077", str(save_root), str(backup_root))


@pytest.fixture
def card(qtbot, game):
    widget = GameCard(game, "never")
    qtbot.addWidget(widget)
    return widget


def button_named(card, text):
    return next(b for b in card.findChildren(QPushButton) if b.text() == text)


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

class TestInitials:
    @pytest.mark.parametrize("name, expected", [
        ("Cyberpunk 2077", "C2"),
        ("The Witcher 3", "TW"),
        ("Hades", "HA"),
        ("hades", "HA"),
        ("Baldur's Gate", "BG"),
        ("S.T.A.L.K.E.R.", "S."),
    ])
    def test_two_letters_is_the_goal(self, name, expected):
        assert game_card_module._initials_for(name) == expected

    @pytest.mark.parametrize("name", ["", "   ", "\t"])
    def test_nothing_to_work_with(self, name):
        assert game_card_module._initials_for(name) == "?"

    def test_extra_whitespace_between_words_is_ignored(self):
        assert game_card_module._initials_for("Hollow   Knight") == "HK"


class TestDefaultColor:
    def test_comes_from_the_palette(self):
        for name in ["Hades", "Hollow Knight", "Some Other Game", "2077"]:
            assert _default_color_for(name) in AVATAR_COLORS

    def test_is_stable_for_the_same_name(self):
        assert _default_color_for("Hades") == _default_color_for("Hades")

    def test_differs_between_games(self):
        colors = {_default_color_for(f"Game {i}") for i in range(20)}
        assert len(colors) > 1

    def test_a_user_colour_wins_over_the_automatic_one(self):
        game = Game.new("Hades", "/s", "/b")
        game.color = "#ff0000"
        assert game.color != _default_color_for("Hades")


class TestFormatTime:
    def test_formats_a_backup_timestamp(self):
        assert game_card_module._format_time("2026-09-11_20-30-00") == \
            "Sep 11, 2026 · 08:30 PM"

    @pytest.mark.parametrize("value", ["", None])
    def test_never_backed_up(self, value):
        assert game_card_module._format_time(value) == "Never"

    @pytest.mark.parametrize("value", ["not a timestamp", "2026-13-45", "12345"])
    def test_unrecognised_values_are_shown_as_they_are(self, value):
        # Better than showing "Never" for a timestamp the app wrote itself.
        assert game_card_module._format_time(value) == value


class TestElide:
    def test_short_paths_are_shown_in_full(self):
        assert elide(r"C:\saves\hades") == r"C:\saves\hades"

    def test_long_paths_are_shortened_with_an_ellipsis(self):
        long_path = r"C:\Users\someone\Documents\My Games\CD Projekt Red\Cyberpunk 2077"
        shortened = elide(long_path)
        assert len(shortened) < len(long_path)
        assert "…" in shortened

    def test_the_tail_survives_so_the_folder_is_still_identifiable(self):
        long_path = r"C:\Users\someone\Documents\My Games\CD Projekt Red\Cyberpunk 2077"
        assert elide(long_path).endswith("2077")

    @pytest.mark.parametrize("value", ["", None])
    def test_no_path_at_all(self, value):
        assert elide(value) == "No save folder set"


# ---------------------------------------------------------------------------
# the card itself
# ---------------------------------------------------------------------------

class TestStatusBadge:
    @pytest.mark.parametrize("status, text", [
        ("synced", "Up to date"),
        ("changed", "Changes detected"),
        ("never", "Never backed up"),
        ("invalid", "Path missing"),
    ])
    def test_every_status_has_a_label(self, status, text):
        assert STATUS_TEXT[status] == text

    @pytest.mark.parametrize("status", ["synced", "changed", "never", "invalid"])
    def test_badge_shows_the_label_for_the_initial_status(self, qtbot, card, status):
        widget = GameCard(card.game, status)
        qtbot.addWidget(widget)
        assert widget.status_badge.text() == STATUS_TEXT[status]

    def test_set_status_updates_label_and_styling_hook(self, card):
        card.set_status("changed")
        assert card.status == "changed"
        assert card.status_badge.text() == "Changes detected"
        # The status is exposed as a property so the stylesheet can colour it.
        assert card.status_badge.property("status") == "changed"

    def test_an_unknown_status_falls_back_to_itself(self, card):
        card.set_status("something-new")
        assert card.status_badge.text() == "something-new"


class TestProgress:
    def test_progress_bar_starts_hidden(self, card):
        assert card.progress_bar.isHidden() is True

    def test_set_backing_up_shows_progress_and_disables_the_button(self, card):
        card.set_backing_up(True)
        assert card.progress_bar.isHidden() is False
        assert card.backup_btn.isEnabled() is False
        assert card.backup_btn.text() == "Backing up…"

    def test_clearing_backing_up_restores_the_button(self, card):
        card.set_backing_up(True)
        card.set_backing_up(False)
        assert card.backup_btn.isEnabled() is True
        assert card.backup_btn.text() == "Backup Now"

    @pytest.mark.parametrize("current, total, expected", [
        (1, 4, 25), (2, 4, 50), (4, 4, 100), (1, 3, 33),
    ])
    def test_progress_percentage(self, card, current, total, expected):
        card.set_progress(current, total)
        assert card.progress_bar.value() == expected

    def test_a_zero_total_does_not_divide_by_zero(self, card):
        card.set_progress(0, 0)
        assert card.progress_bar.value() == 100


class TestMetaLine:
    def test_never_backed_up(self, card):
        assert card.meta_label.text() == "Last backup: Never   ·   0 backups"

    def test_shows_last_backup_and_count(self, card):
        card.game.last_backup_time = "2026-09-11_20-30-00"
        card.game.backup_count = 1
        card.refresh_meta()
        assert "Sep 11, 2026" in card.meta_label.text()
        assert "·   1 backup" in card.meta_label.text()  # singular

    def test_plural_for_more_than_one(self, card):
        card.game.backup_count = 5
        card.refresh_meta()
        assert "5 backups" in card.meta_label.text()


class TestRefreshMeta:
    def test_picks_up_a_renamed_game(self, card):
        card.game.name = "Cyberpunk: Phantom Liberty"
        card.refresh_meta()
        assert card.name_label.text() == "Cyberpunk: Phantom Liberty"
        assert card.icon_label.text() == "CP"

    def test_picks_up_a_moved_save_folder(self, card):
        card.game.save_path = r"D:\Games\Cyberpunk 2077"
        card.refresh_meta()
        assert card.path_label.text() == r"D:\Games\Cyberpunk 2077"
        assert card.path_label.toolTip() == r"D:\Games\Cyberpunk 2077"

    def test_picks_up_a_longer_save_folder(self, card):
        card.game.save_path = r"C:\Users\someone\Documents\My Games\CD Projekt Red\Cyberpunk 2077"
        card.refresh_meta()
        assert "…" in card.path_label.text()
        assert card.path_label.toolTip() == card.game.save_path


class TestAvatarColor:
    def test_uses_the_automatic_colour_by_default(self, card):
        assert card.icon_label.property("current_color") == \
            _default_color_for(card.game.name)

    def test_uses_a_user_chosen_colour(self, qtbot, game):
        game.color = "#ff6b9d"
        widget = GameCard(game, "never")
        qtbot.addWidget(widget)
        assert widget.icon_label.property("current_color") == "#ff6b9d"

    @pytest.mark.parametrize("bad_color", ["red", "#fff", "#gggggg", "rgb(1,2,3)", ""])
    def test_a_malformed_stored_colour_falls_back_instead_of_producing_bad_css(
            self, qtbot, game, bad_color):
        game.color = bad_color
        widget = GameCard(game, "never")
        qtbot.addWidget(widget)
        expected = _default_color_for(game.name)
        assert widget.icon_label.property("current_color") == expected
        assert expected in widget.icon_label.styleSheet()

    def test_picking_a_colour_stores_it_and_announces_it(self, card, qtbot):
        received = []
        card.color_changed.connect(lambda *args: received.append(args))
        card._on_color_picked("#5cc9ff")
        assert card.game.color == "#5cc9ff"
        assert card.icon_label.property("current_color") == "#5cc9ff"
        assert received == [(card.game.id, "#5cc9ff")]

    def test_resetting_returns_to_the_automatic_colour(self, card, qtbot):
        card._on_color_picked("#5cc9ff")
        received = []
        card.color_changed.connect(lambda *args: received.append(args))
        card._on_color_reset()
        assert card.game.color is None
        assert card.icon_label.property("current_color") == \
            _default_color_for(card.game.name)
        assert received == [(card.game.id, None)]


class TestSignals:
    def test_backup_button_requests_a_backup(self, card):
        received = []
        card.backup_requested.connect(received.append)
        card.backup_btn.click()
        assert received == [card.game.id]

    def test_edit_button(self, card):
        received = []
        card.edit_requested.connect(received.append)
        button_named(card, "Edit").click()
        assert received == [card.game.id]

    def test_remove_button(self, card):
        received = []
        card.remove_requested.connect(received.append)
        button_named(card, "Remove").click()
        assert received == [card.game.id]

    def test_a_click_on_the_swatch_opens_the_colour_popup_for_the_current_colour(self, card):
        position = QPointF(5, 5)
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, position, position, position,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        card.icon_label.mousePressEvent(event)
        popup = card.icon_label._popup
        assert popup is not None
        # The popup is opened on the colour currently in use, so the right
        # swatch shows as selected.
        assert popup.current_color == card.icon_label.property("current_color")


class TestOpenBackupFolder:
    def test_opens_the_folder_when_it_exists(self, card, backup_root, monkeypatch):
        backup_root.mkdir()
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toLocalFile()))
        card._open_backup_folder()
        # A local file URL, so a file manager opens the folder itself.
        assert len(opened) == 1
        assert Path(opened[0]) == backup_root

    def test_explains_a_missing_folder_instead_of_doing_nothing(
            self, card, message_spy):
        card.game.backup_count = 3  # backups exist, but the folder is gone
        card._open_backup_folder()
        assert message_spy.calls
        assert message_spy.calls[0][0] == "information"
        assert message_spy.calls[0][2] == "Backup folder not found"

    def test_explains_that_there_is_nothing_to_open_yet(self, card, message_spy):
        card.game.backup_count = 0
        card._open_backup_folder()
        assert message_spy.calls
        assert message_spy.calls[0][0] == "information"
        assert "hasn't been backed up yet" in message_spy.calls[0][3]