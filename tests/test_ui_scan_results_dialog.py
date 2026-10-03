"""app/ui/scan_results_dialog.py — reviewing and picking scan candidates."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLineEdit

from app.game_scanner import ScanCandidate
from app.ui.scan_results_dialog import ScanResultsDialog


CANDIDATES = [
    ScanCandidate("Hades", r"C:\saves\Hades", "My Games", "high"),
    ScanCandidate("Some Indie Game", r"C:\Documents\Some Indie Game", "Documents", "low"),
    ScanCandidate("Cyberpunk 2077", r"C:\Steam\userdata\0\1091500\remote", "Steam", "high"),
]


@pytest.fixture
def dialog(qtbot):
    widget = ScanResultsDialog(CANDIDATES)
    qtbot.addWidget(widget)
    return widget


def checkboxes(dialog):
    return [cb for cb, _candidate in dialog.checkboxes]


def check(box):
    box.setChecked(True)


class TestLayout:
    def test_title_counts_the_candidates(self, dialog):
        labels = [w.text() for w in dialog.findChildren(QLineEdit)]
        assert labels == [c.name for c in CANDIDATES]
        from PySide6.QtWidgets import QLabel
        titles = [w.text() for w in dialog.findChildren(QLabel)]
        assert "Found 3 possible games" in titles

    def test_title_is_singular_for_one_candidate(self, qtbot):
        from PySide6.QtWidgets import QLabel

        widget = ScanResultsDialog(CANDIDATES[:1])
        qtbot.addWidget(widget)
        assert "Found 1 possible game" in [w.text() for w in widget.findChildren(QLabel)]

    def test_is_modal(self, dialog):
        assert dialog.isModal() is True

    def test_shows_the_save_path_for_each_candidate(self, dialog):
        from PySide6.QtWidgets import QLabel

        paths = [w.text() for w in dialog.findChildren(QLabel)]
        for candidate in CANDIDATES:
            assert candidate.save_path in paths


class TestSelection:
    def test_nothing_is_selected_at_first(self, dialog):
        # Heuristics guess wrong sometimes, so nothing is added automatically.
        assert all(not cb.isChecked() for cb in checkboxes(dialog))

    def test_add_button_starts_disabled(self, dialog):
        assert dialog.add_btn.isEnabled() is False
        assert dialog.add_btn.text() == "Add Selected"

    def test_select_all_checks_everything(self, dialog):
        dialog._set_all_checked(True)
        assert all(cb.isChecked() for cb in checkboxes(dialog))
        assert dialog.add_btn.text() == "Add 3 Games"

    def test_select_none_clears_everything(self, dialog):
        dialog._set_all_checked(True)
        dialog._set_all_checked(False)
        assert not any(cb.isChecked() for cb in checkboxes(dialog))
        assert dialog.add_btn.isEnabled() is False

    def test_counting_follows_the_selection(self, dialog):
        check(checkboxes(dialog)[0])
        assert dialog.add_btn.text() == "Add 1 Game"
        check(checkboxes(dialog)[1])
        assert dialog.add_btn.text() == "Add 2 Games"

    def test_unchecking_again_disables_the_button(self, dialog):
        box = checkboxes(dialog)[0]
        check(box)
        box.setChecked(False)
        assert dialog.add_btn.isEnabled() is False
        assert dialog.add_btn.text() == "Add Selected"


class TestAdding:
    def test_publishes_only_the_checked_candidates(self, dialog):
        check(checkboxes(dialog)[2])
        dialog._on_add_clicked()
        assert dialog.result() == QDialog.Accepted
        assert [c.name for c in dialog.selected_candidates] == ["Cyberpunk 2077"]

    def test_keeps_each_candidate_path_and_source(self, dialog):
        dialog._set_all_checked(True)
        dialog._on_add_clicked()
        selected = {c.name: c for c in dialog.selected_candidates}
        assert selected["Hades"].save_path == CANDIDATES[0].save_path
        assert selected["Hades"].source == "My Games"
        assert selected["Hades"].confidence == "high"
        assert selected["Some Indie Game"].confidence == "low"

    def test_returns_real_scan_candidates(self, dialog):
        dialog._set_all_checked(True)
        dialog._on_add_clicked()
        assert all(isinstance(c, ScanCandidate) for c in dialog.selected_candidates)

    def test_a_renamed_candidate_keeps_its_edit(self, dialog):
        name_edits = dialog.findChildren(QLineEdit)
        name_edits[1].setText("My Renamed Game")
        check(checkboxes(dialog)[1])
        dialog._on_add_clicked()
        assert [c.name for c in dialog.selected_candidates] == ["My Renamed Game"]

    def test_editing_a_name_that_is_not_selected_does_not_add_it(self, dialog):
        name_edits = dialog.findChildren(QLineEdit)
        name_edits[1].setText("Renamed But Not Checked")
        check(checkboxes(dialog)[0])
        dialog._on_add_clicked()
        assert [c.name for c in dialog.selected_candidates] == ["Hades"]

    def test_the_button_does_it(self, dialog):
        check(checkboxes(dialog)[0])
        dialog.add_btn.click()
        assert dialog.result() == QDialog.Accepted


class TestOpeningInExplorer:
    def test_opens_an_existing_folder(self, tmp_path, monkeypatch, qtbot):
        target = tmp_path / "saves"
        target.mkdir()
        widget = ScanResultsDialog([ScanCandidate("Hades", str(target), "My Games", "high")])
        qtbot.addWidget(widget)
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toLocalFile()))
        widget._open_in_explorer(str(target))
        assert len(opened) == 1

    def test_warns_when_the_folder_is_gone(self, tmp_path, monkeypatch, message_spy):
        missing = str(tmp_path / "not_here")
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
        ScanResultsDialog._open_in_explorer(None, missing)
        assert opened == []
        assert message_spy.calls[0][2] == "Path not found"
        assert missing in message_spy.calls[0][3]


class TestCopyResults:
    def test_copies_every_candidate_to_the_clipboard(self, dialog, message_spy):
        dialog._on_copy_results()
        text = QApplication.clipboard().text()
        for candidate in CANDIDATES:
            assert candidate.name in text
            assert candidate.save_path in text
            assert candidate.source in text

    def test_says_how_many_were_copied(self, dialog, message_spy):
        dialog._on_copy_results()
        assert message_spy.calls[0][2] == "Copied"
        assert "3" in message_spy.calls[0][3]


class TestSelectionHelpers:
    def test_candidate_properties_are_read_only_wrappers(self, dialog):
        editable = dialog.candidates[0]
        assert editable.name == CANDIDATES[0].name
        assert editable.save_path == CANDIDATES[0].save_path
        assert editable.source == CANDIDATES[0].source
        assert editable.confidence == CANDIDATES[0].confidence

    def test_editable_wrappers_do_not_mutate_the_original_candidate(self, dialog):
        dialog.candidates[0].name = "Something Else"
        assert CANDIDATES[0].name == "Hades"

    def test_an_empty_override_falls_back_to_the_detected_name(self, dialog):
        dialog.candidates[0].name_override = ""
        assert dialog.candidates[0].name == CANDIDATES[0].name

    def test_checkboxes_are_unticked_by_default(self, dialog):
        assert all(cb.checkState() == Qt.Unchecked for cb in checkboxes(dialog))
        assert isinstance(checkboxes(dialog)[0], QCheckBox)