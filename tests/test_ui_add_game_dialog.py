"""app/ui/add_game_dialog.py — the add/edit game form and its validation."""

import pytest
from PySide6.QtWidgets import QDialog, QFileDialog, QPushButton

from app.models import Game
from app.ui.add_game_dialog import AddGameDialog


def button_named(widget, text):
    return next(b for b in widget.findChildren(QPushButton) if b.text() == text)


@pytest.fixture
def dialog(qtbot):
    widget = AddGameDialog()
    qtbot.addWidget(widget)
    return widget


def fill(dialog, name, save_path, backup_path):
    dialog.name_edit.setText(name)
    dialog.save_path_edit.setText(str(save_path))
    dialog.backup_path_edit.setText(str(backup_path))


class TestTitle:
    def test_adding_a_game(self, dialog):
        assert dialog.windowTitle() == "Add Game"

    def test_editing_an_existing_game(self, qtbot, save_root, backup_root):
        existing = Game.new("Hades", str(save_root), str(backup_root))
        widget = AddGameDialog(existing=existing)
        qtbot.addWidget(widget)
        assert widget.windowTitle() == "Edit Game"

    def test_is_modal(self, dialog):
        assert dialog.isModal() is True


class TestPrefill:
    def test_starts_empty(self, dialog):
        assert dialog.name_edit.text() == ""
        assert dialog.save_path_edit.text() == ""
        assert dialog.backup_path_edit.text() == ""

    def test_an_existing_game_fills_the_fields_in(self, qtbot, save_root, backup_root):
        existing = Game.new("Hades", str(save_root), str(backup_root))
        widget = AddGameDialog(existing=existing)
        qtbot.addWidget(widget)
        assert widget.name_edit.text() == "Hades"
        assert widget.save_path_edit.text() == str(save_root)
        assert widget.backup_path_edit.text() == str(backup_root)


class TestSuccessfulSave:
    def test_publishes_the_entered_values_and_accepts(self, dialog, save_root, backup_root):
        fill(dialog, "Cyberpunk 2077", save_root, backup_root)
        dialog._on_save()
        assert dialog.result() == QDialog.Accepted
        assert dialog.result_data == {
            "name": "Cyberpunk 2077",
            "save_path": str(save_root),
            "backup_root": str(backup_root),
        }

    def test_surrounding_whitespace_is_trimmed(self, dialog, save_root, backup_root):
        fill(dialog, "  Hades  ", save_root, backup_root)
        dialog.name_edit.setText("  Hades  ")
        dialog.save_path_edit.setText(f"  {save_root}  ")
        dialog.backup_path_edit.setText(f"  {backup_root}  ")
        dialog._on_save()
        assert dialog.result_data["name"] == "Hades"
        assert dialog.result_data["save_path"] == str(save_root)
        assert dialog.result_data["backup_root"] == str(backup_root)

    def test_a_backup_destination_that_does_not_exist_yet_is_fine(
            self, dialog, save_root, backup_root):
        assert not backup_root.exists()
        fill(dialog, "Hades", save_root, backup_root)
        dialog._on_save()
        assert dialog.result() == QDialog.Accepted

    def test_a_save_folder_nested_in_the_backup_destination_is_allowed(
            self, dialog, save_root, tmp_path):
        # Only the dangerous direction is refused: a backup destination inside
        # the save folder would end up backing itself up.
        backup = tmp_path / "everything"
        backup.mkdir()
        fill(dialog, "Hades", backup, save_root)
        dialog._on_save()
        assert dialog.result() == QDialog.Accepted


class TestValidation:
    def test_a_name_is_required(self, dialog, save_root, backup_root, message_spy):
        fill(dialog, "   ", save_root, backup_root)
        dialog._on_save()
        assert message_spy.calls[0][0] == "warning"
        assert message_spy.calls[0][2] == "Missing info"
        assert dialog.result() != QDialog.Accepted
        assert not hasattr(dialog, "result_data")

    def test_a_save_folder_is_required(self, dialog, backup_root, message_spy):
        fill(dialog, "Hades", "", backup_root)
        dialog._on_save()
        assert message_spy.calls[0][2] == "Missing info"
        assert dialog.result() != QDialog.Accepted

    def test_the_save_folder_has_to_exist(self, dialog, tmp_path, backup_root, message_spy):
        missing = tmp_path / "not_here"
        fill(dialog, "Hades", missing, backup_root)
        dialog._on_save()
        assert message_spy.calls[0][2] == "Save folder not found"
        assert str(missing) in message_spy.calls[0][3]
        assert dialog.result() != QDialog.Accepted

    def test_a_file_is_not_a_save_folder(self, dialog, save_root, backup_root, message_spy):
        not_a_folder = save_root / "slot1.sav"
        fill(dialog, "Hades", not_a_folder, backup_root)
        dialog._on_save()
        assert message_spy.calls[0][2] == "Save folder not found"

    def test_a_backup_destination_is_required(self, dialog, save_root, message_spy):
        fill(dialog, "Hades", save_root, "  ")
        dialog._on_save()
        assert message_spy.calls[0][2] == "Missing info"
        assert dialog.result() != QDialog.Accepted

    def test_a_backup_destination_inside_the_save_folder_is_refused(
            self, dialog, save_root, message_spy):
        # Each backup would zip the previous one, growing without bound.
        fill(dialog, "Hades", save_root, save_root / "backups")
        dialog._on_save()
        assert message_spy.calls[0][2] == "Overlapping folders"
        assert dialog.result() != QDialog.Accepted

    def test_the_same_folder_for_both_is_refused(self, dialog, save_root, message_spy):
        fill(dialog, "Hades", save_root, save_root)
        dialog._on_save()
        assert message_spy.calls[0][2] == "Overlapping folders"

    def test_a_backup_destination_reached_through_a_link_is_refused(
            self, dialog, save_root, tmp_path, junction, message_spy):
        link = junction(save_root, "savelink")
        fill(dialog, "Hades", save_root, link / "backups")
        dialog._on_save()
        assert message_spy.calls[0][2] == "Overlapping folders"

    def test_a_nested_but_unrelated_sibling_is_allowed(
            self, dialog, save_root, tmp_path):
        fill(dialog, "Hades", save_root, tmp_path / "backups")
        dialog._on_save()
        assert dialog.result() == QDialog.Accepted

    def test_validation_stops_at_the_first_problem(
            self, dialog, tmp_path, message_spy):
        # Nothing filled in: the name is reported, not all three at once.
        dialog._on_save()
        assert len(message_spy.calls) == 1


class TestBrowseButtons:
    def test_browsing_for_the_save_folder_fills_the_field(
            self, dialog, save_root, monkeypatch):
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            lambda *args, **kwargs: str(save_root),
        )
        dialog._browse_save_path()
        assert dialog.save_path_edit.text() == str(save_root)

    def test_browsing_for_the_backup_folder_fills_the_field(
            self, dialog, backup_root, monkeypatch):
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            lambda *args, **kwargs: str(backup_root),
        )
        dialog._browse_backup_path()
        assert dialog.backup_path_edit.text() == str(backup_root)

    def test_cancelling_the_browser_changes_nothing(self, dialog, monkeypatch):
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda *a, **k: ""
        )
        dialog._browse_save_path()
        assert dialog.save_path_edit.text() == ""


class TestButtons:
    def test_cancel_rejects_without_saving(self, dialog):
        button_named(dialog, "Cancel").click()
        assert dialog.result() == QDialog.Rejected
        assert not hasattr(dialog, "result_data")

    def test_no_result_data_before_saving(self, dialog):
        assert not hasattr(dialog, "result_data")