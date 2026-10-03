"""app/ui/settings_dialog.py — compression, retention, and the default backup
location."""

import os

import pytest
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from app.config import DEFAULT_SETTINGS
from app.ui.settings_dialog import SettingsDialog


def button_named(widget, text):
    from PySide6.QtWidgets import QPushButton
    return next(b for b in widget.findChildren(QPushButton) if b.text() == text)


def make_dialog(qtbot, settings=None):
    widget = SettingsDialog(dict(DEFAULT_SETTINGS if settings is None else settings))
    qtbot.addWidget(widget)
    return widget


class TestCompressionSlider:
    def test_starts_at_the_stored_level(self, qtbot):
        assert make_dialog(qtbot, {"compression_level": 8}).slider.value() == 8

    def test_defaults_to_the_app_default(self, qtbot):
        assert make_dialog(qtbot).slider.value() == DEFAULT_SETTINGS["compression_level"]

    @pytest.mark.parametrize("stored, shown", [
        (99, 9), (-5, 0), ("junk", 6), (None, 6), ("7", 7),
    ])
    def test_an_out_of_range_stored_value_is_clamped(self, qtbot, stored, shown):
        # A hand-edited settings.json must not be able to make every backup
        # fail deep inside zlib.
        assert make_dialog(qtbot, {"compression_level": stored}).slider.value() == shown

    def test_shows_the_level_and_its_description(self, qtbot):
        dialog = make_dialog(qtbot)
        dialog.slider.setValue(4)
        assert dialog.value_label.text() == "4"
        assert "Balanced" in dialog.description_label.text()

    def test_describes_the_no_compression_tier(self, qtbot):
        dialog = make_dialog(qtbot)
        dialog.slider.setValue(0)
        assert "No compression" in dialog.description_label.text()

    def test_explains_that_the_top_tier_uses_a_different_algorithm(self, qtbot):
        dialog = make_dialog(qtbot)
        dialog.slider.setValue(9)
        description = dialog.description_label.text()
        assert "Maximum" in description
        assert "LZMA" in description

    def test_every_level_has_a_description(self, qtbot):
        dialog = make_dialog(qtbot)
        for level in range(10):
            dialog.slider.setValue(level)
            assert dialog.description_label.text().strip()


class TestRetentionSpinBox:
    def test_starts_at_the_stored_limit(self, qtbot):
        assert make_dialog(qtbot, {"max_backups": 7}).max_backups_spin.value() == 7

    def test_unlimited_is_the_default(self, qtbot):
        assert make_dialog(qtbot).max_backups_spin.value() == 0

    def test_zero_reads_as_unlimited(self, qtbot):
        dialog = make_dialog(qtbot, {"max_backups": 0})
        assert dialog.max_backups_spin.specialValueText() == "Unlimited"

    def test_the_control_cannot_exceed_its_own_maximum(self, qtbot):
        # A larger stored value would be accepted by the clamp and then
        # silently reduced by the widget, so the saved value has to match what
        # the user actually sees.
        assert make_dialog(qtbot, {"max_backups": 100_000}).max_backups_spin.maximum() == 999

    @pytest.mark.parametrize("stored", [-1, "junk", None])
    def test_an_unusable_stored_value_falls_back_to_unlimited(self, qtbot, stored):
        assert make_dialog(qtbot, {"max_backups": stored}).max_backups_spin.value() == 0


class TestDefaultBackupLocation:
    def test_shows_the_stored_path(self, qtbot):
        dialog = make_dialog(qtbot, {"default_scan_backup_root": "/tmp/backups"})
        assert dialog.scan_backup_root_edit.text() == "/tmp/backups"

    def test_browsing_fills_the_field(self, qtbot, backup_root, monkeypatch):
        backup_root.mkdir()
        dialog = make_dialog(qtbot)
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda *a, **k: str(backup_root)
        )
        dialog._on_browse_scan_backup_root()
        assert dialog.scan_backup_root_edit.text() == str(backup_root)

    def test_cancelling_the_browser_changes_nothing(self, qtbot, monkeypatch):
        dialog = make_dialog(qtbot)
        before = dialog.scan_backup_root_edit.text()
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
        dialog._on_browse_scan_backup_root()
        assert dialog.scan_backup_root_edit.text() == before


class TestSave:
    def test_publishes_the_edited_settings_and_accepts(self, qtbot, backup_root):
        backup_root.mkdir()
        dialog = make_dialog(qtbot)
        dialog.slider.setValue(2)
        dialog.max_backups_spin.setValue(4)
        dialog.scan_backup_root_edit.setText(str(backup_root))
        dialog._on_save()

        assert dialog.result() == QDialog.Accepted
        assert dialog.result_settings["compression_level"] == 2
        assert dialog.result_settings["max_backups"] == 4
        assert dialog.result_settings["default_scan_backup_root"] == str(backup_root)

    def test_keeps_settings_the_dialog_does_not_expose(self, qtbot):
        dialog = make_dialog(qtbot, {"some_future_setting": "keep me"})
        dialog._on_save()
        assert dialog.result_settings["some_future_setting"] == "keep me"

    def test_does_not_edit_the_dictionary_it_was_given(self, qtbot):
        original = dict(DEFAULT_SETTINGS)
        dialog = SettingsDialog(original)
        qtbot.addWidget(dialog)
        dialog.slider.setValue(9)
        dialog._on_save()
        # The app only adopts the result after Save is accepted.
        assert original["compression_level"] == DEFAULT_SETTINGS["compression_level"]

    def test_an_existing_folder_is_accepted(self, qtbot, tmp_path):
        dialog = make_dialog(qtbot)
        dialog.scan_backup_root_edit.setText(str(tmp_path))
        dialog._on_save()
        assert dialog.result() == QDialog.Accepted

    def test_a_missing_folder_is_refused(self, qtbot, tmp_path, message_spy):
        dialog = make_dialog(qtbot)
        dialog.scan_backup_root_edit.setText(str(tmp_path / "not_here"))
        dialog._on_save()
        # message_spy stubs question() to None, i.e. declining the offer.
        assert message_spy.calls[0][0] == "question"
        assert message_spy.calls[0][2] == "Folder doesn't exist"
        assert dialog.result() != QDialog.Accepted
        assert dialog.result_settings is None
        # Declining must not leave the folder behind either.
        assert not (tmp_path / "not_here").exists()


class TestCreatingAMissingBackupFolder:
    """A backup root that doesn't exist is never itself a failure.

    BackupWorker.run() calls os.makedirs on the backup root and
    _add_scanned_games only joins paths onto it, so the folder gets created on
    demand regardless. Offering to create it now turns a dead end into one
    click, and does it before any game is pointed at the path.
    """

    def missing(self, tmp_path):
        return str(tmp_path / "not_here")

    def test_accepting_creates_the_folder_and_saves(
            self, qtbot, tmp_path, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.Yes)
        dialog = make_dialog(qtbot)
        dialog.scan_backup_root_edit.setText(self.missing(tmp_path))

        dialog._on_save()

        assert dialog.result() == QDialog.Accepted
        assert dialog.result_settings["default_scan_backup_root"] == self.missing(tmp_path)
        assert (tmp_path / "not_here").is_dir()

    def test_the_offer_defaults_to_declining(self, qtbot, tmp_path, monkeypatch):
        # A mistyped path should leave no stray folder behind, so the button
        # that a stray Enter would trigger is the safe one.
        seen = {}

        def question(*args, **kwargs):
            seen["default"] = kwargs.get("defaultButton", args[4] if len(args) > 4 else None)
            return QMessageBox.No

        monkeypatch.setattr(QMessageBox, "question", question)
        dialog = make_dialog(qtbot)
        dialog.scan_backup_root_edit.setText(self.missing(tmp_path))
        dialog._on_save()

        assert seen["default"] == QMessageBox.No
        assert not (tmp_path / "not_here").exists()

    def test_a_file_in_the_way_is_reported_rather_than_overwritten(
            self, qtbot, tmp_path, message_spy, monkeypatch):
        # os.path.isdir is False for a plain file, and makedirs then raises
        # FileExistsError. The user must be told, not left guessing. message_spy
        # is required here: the autouse fixture only stubs question(), so
        # warning() would be a real modal box and hang the run.
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.Yes)
        blocker = tmp_path / "not_here"
        blocker.write_text("I am a file, not a folder", encoding="utf-8")
        dialog = make_dialog(qtbot)
        dialog.scan_backup_root_edit.setText(str(blocker))

        dialog._on_save()

        assert dialog.result() != QDialog.Accepted
        assert dialog.result_settings is None
        assert any(call[2] == "Couldn't create folder" for call in message_spy.calls)
        # The file is left exactly as it was.
        assert blocker.read_text(encoding="utf-8") == "I am a file, not a folder"

    def test_a_failure_to_create_is_surfaced(self, qtbot, tmp_path,
                                            message_spy, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.Yes)

        def refuse(*args, **kwargs):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(os, "makedirs", refuse)
        dialog = make_dialog(qtbot)
        dialog.scan_backup_root_edit.setText(self.missing(tmp_path))

        dialog._on_save()

        assert dialog.result() != QDialog.Accepted
        assert any(call[2] == "Couldn't create folder" for call in message_spy.calls)


class TestUnchangedStoredPathIsNotRevalidated:
    """The field is pre-filled from the stored setting, and that folder can stop
    existing on its own -- deleted, an external drive unplugged, a home
    directory that moved. Re-checking a value the user never entered would let
    it block every other setting in the dialog."""

    def gone(self, tmp_path):
        root = tmp_path / "Backups"
        root.mkdir()
        return root

    def test_a_missing_stored_path_does_not_block_saving(
            self, qtbot, tmp_path, message_spy):
        root = self.gone(tmp_path)
        import shutil
        shutil.rmtree(root)  # the setting now points at a folder that isn't there
        dialog = make_dialog(qtbot, {"default_scan_backup_root": str(root)})
        dialog.slider.setValue(1)

        dialog._on_save()

        assert dialog.result() == QDialog.Accepted
        assert dialog.result_settings["compression_level"] == 1
        # Nothing was asked, because the user changed no path.
        assert message_spy.calls == []

    def test_a_path_the_user_types_is_still_validated(
            self, qtbot, tmp_path, message_spy):
        dialog = make_dialog(qtbot, {"default_scan_backup_root": str(tmp_path)})
        dialog.scan_backup_root_edit.setText(str(tmp_path / "typo"))

        dialog._on_save()

        # Otherwise the exemption would quietly accept any mistyped path.
        assert dialog.result() != QDialog.Accepted
        assert message_spy.calls[0][2] == "Folder doesn't exist"

    def test_an_existing_stored_path_is_still_accepted(
            self, qtbot, tmp_path, message_spy):
        dialog = make_dialog(qtbot, {"default_scan_backup_root": str(tmp_path)})
        dialog._on_save()
        assert dialog.result() == QDialog.Accepted
        assert message_spy.calls == []

    def test_emptying_the_field_keeps_the_previous_value(self, qtbot, tmp_path):
        dialog = make_dialog(qtbot, {"default_scan_backup_root": str(tmp_path)})
        dialog.scan_backup_root_edit.setText("")
        dialog._on_save()
        # Deliberately not written as an empty path: MainWindow has a sane
        # built-in fallback anyway.
        assert dialog.result_settings["default_scan_backup_root"] == str(tmp_path)

    def test_saving_the_save_button(self, qtbot):
        dialog = make_dialog(qtbot)
        button_named(dialog, "Save").click()
        assert dialog.result() == QDialog.Accepted
        assert dialog.result_settings is not None

    def test_cancelling_saves_nothing(self, qtbot):
        dialog = make_dialog(qtbot)
        dialog.slider.setValue(9)
        button_named(dialog, "Cancel").click()
        assert dialog.result() == QDialog.Rejected
        assert dialog.result_settings is None


class TestUpdateCheck:
    def test_an_up_to_date_app_says_so(self, qtbot, monkeypatch, message_spy):
        from PySide6.QtWidgets import QPushButton

        monkeypatch.setattr(
            "app.ui.settings_dialog.check_for_updates", lambda force=False: None
        )
        dialog = make_dialog(qtbot)
        update_button = next(
            b for b in dialog.findChildren(QPushButton)
            if b.text() == "Check for Updates"
        )
        update_button.click()
        qtbot.waitUntil(lambda: bool(message_spy.calls), timeout=5000)
        assert message_spy.calls[0][2] == "Up to Date"

    def test_a_newer_release_is_announced_with_its_notes(self, qtbot, monkeypatch, message_spy):
        from PySide6.QtWidgets import QPushButton
        from PySide6.QtWidgets import QMessageBox

        from app.updater import ReleaseInfo

        monkeypatch.setattr(
            "app.ui.settings_dialog.check_for_updates",
            lambda force=False: ReleaseInfo("9.9.9", "https://example.invalid/r", "", False),
        )
        monkeypatch.setattr(
            "app.ui.settings_dialog.fetch_release_notes", lambda version: "Faster backups"
        )
        # Answering the question is all this test needs; the browser is stubbed
        # by message_spy's replacement of QMessageBox.question.
        dialog = make_dialog(qtbot)
        next(b for b in dialog.findChildren(QPushButton)
             if b.text() == "Check for Updates").click()
        qtbot.waitUntil(lambda: any(c[0] == "question" for c in message_spy.calls),
                        timeout=5000)
        question = next(c for c in message_spy.calls if c[0] == "question")
        assert question[2] == "Update Available"
        assert "9.9.9" in question[3]
        assert "Faster backups" in question[3]
        assert QMessageBox.Yes in question[4]

    def test_the_button_is_re_enabled_afterwards(self, qtbot, monkeypatch, message_spy):
        from PySide6.QtWidgets import QPushButton

        monkeypatch.setattr(
            "app.ui.settings_dialog.check_for_updates", lambda force=False: None
        )
        dialog = make_dialog(qtbot)
        button = next(b for b in dialog.findChildren(QPushButton)
                      if b.text() == "Check for Updates")
        button.click()
        qtbot.waitUntil(lambda: button.text() == "Check for Updates", timeout=5000)
        assert button.isEnabled() is True


class TestAboutTab:
    def test_shows_the_version_and_config_location(self, qtbot, monkeypatch, app_dir):
        from app import __version__
        from app.ui import settings_dialog as module

        # The dialog reads APP_DIR straight from its own module namespace.
        monkeypatch.setattr(module, "APP_DIR", app_dir)
        dialog = make_dialog(qtbot)
        text = " ".join(
            label.text() for label in dialog.findChildren(type(dialog.value_label))
        )
        assert __version__ in text
        assert str(app_dir) in text

    def test_has_a_backups_tab_and_an_about_tab(self, qtbot):
        from PySide6.QtWidgets import QTabWidget

        dialog = make_dialog(qtbot)
        tabs = dialog.findChildren(QTabWidget)[0]
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        assert titles == ["Backups", "About"]