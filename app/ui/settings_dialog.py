"""App settings dialog. Uses a QTabWidget so future settings can simply be
added as new tabs (or new rows within an existing tab) without needing to
redesign this dialog — today there's a "Backups" tab and an "About" tab."""

import os

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QWidget, QSlider, QLineEdit, QFileDialog, QFrame, QSpinBox,
    QMessageBox
)

from app import __version__
from app.assets import load_icon_pixmap
from app.config import APP_DIR
from app.updater import check_for_updates, fetch_release_notes
from app.utils import clamp_compression_level, clamp_max_backups


_LEVEL_DESCRIPTIONS = {
    0: "No compression — fastest backups, largest files on disk.",
    1: "Low compression — very fast, modest size savings.",
    2: "Low compression — very fast, modest size savings.",
    3: "Light compression — fast with decent size savings.",
    4: "Balanced — good mix of speed and file size.",
    5: "Balanced — good mix of speed and file size.",
    6: "Balanced (recommended) — good mix of speed and file size.",
    7: "High compression — smaller files, a bit slower.",
    8: "High compression — smaller files, a bit slower.",
    9: "Maximum — switches to a stronger algorithm (LZMA) for noticeably "
       "smaller files than level 8, at the cost of slower backups. Standard "
       "zip format, but a few older or minimal zip tools may need 7-Zip (or "
       "similar) to open it manually.",
}


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = dict(settings)  # work on a copy until Save is clicked
        self.result_settings = None

        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(14)

        title = QLabel("Settings")
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        tabs = QTabWidget()
        tabs.addTab(self._build_backups_tab(), "Backups")
        tabs.addTab(self._build_about_tab(), "About")
        root.addWidget(tabs, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    def _build_backups_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 16, 4, 4)
        layout.setSpacing(6)

        label_row = QHBoxLayout()
        name_label = QLabel("Compression level")
        name_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        label_row.addWidget(name_label)
        label_row.addStretch()
        self.value_label = QLabel()
        self.value_label.setStyleSheet("color: #8b90a3; font-size: 12px;")
        label_row.addWidget(self.value_label)
        layout.addLayout(label_row)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 9)
        self.slider.setValue(clamp_compression_level(self.settings.get("compression_level")))
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setTickInterval(1)
        self.slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self.slider)

        scale_row = QHBoxLayout()
        fastest = QLabel("Fastest")
        fastest.setStyleSheet("color: #8b90a3; font-size: 11px;")
        smallest = QLabel("Smallest")
        smallest.setStyleSheet("color: #8b90a3; font-size: 11px;")
        scale_row.addWidget(fastest)
        scale_row.addStretch()
        scale_row.addWidget(smallest)
        layout.addLayout(scale_row)

        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        self.description_label.setStyleSheet("color: #8b90a3; font-size: 12px; margin-top: 6px;")
        layout.addWidget(self.description_label)

        note = QLabel("Applies to backups made from now on — existing backups aren't recompressed.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8b90a3; font-size: 11px; margin-top: 10px;")
        layout.addWidget(note)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet("background-color: #262b3a; max-height: 1px; border: none; margin: 16px 0;")
        layout.addWidget(divider)

        retention_label = QLabel("Backups to keep per game")
        retention_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        layout.addWidget(retention_label)

        retention_hint = QLabel(
            "Once a game has more backups than this, the oldest ones are deleted "
            "automatically right after each new backup. Set to Unlimited to keep "
            "every backup forever."
        )
        retention_hint.setWordWrap(True)
        retention_hint.setStyleSheet("color: #8b90a3; font-size: 12px; margin-bottom: 4px;")
        layout.addWidget(retention_hint)

        self.max_backups_spin = QSpinBox()
        self.max_backups_spin.setRange(0, 999)
        self.max_backups_spin.setSpecialValueText("Unlimited")
        self.max_backups_spin.setValue(clamp_max_backups(self.settings.get("max_backups")))
        self.max_backups_spin.setFixedWidth(130)
        layout.addWidget(self.max_backups_spin)

        divider2 = QFrame()
        divider2.setFrameShape(QFrame.HLine)
        divider2.setStyleSheet("background-color: #262b3a; max-height: 1px; border: none; margin: 16px 0;")
        layout.addWidget(divider2)

        location_label = QLabel("Default save location for games")
        location_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        layout.addWidget(location_label)

        location_hint = QLabel(
            "When \"Scan for Games\" finds a new game, its backups go in a subfolder here by default. "
            "You can still change it per-game afterward from Edit."
        )
        location_hint.setWordWrap(True)
        location_hint.setStyleSheet("color: #8b90a3; font-size: 12px; margin-bottom: 4px;")
        layout.addWidget(location_hint)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.scan_backup_root_edit = QLineEdit()
        self.scan_backup_root_edit.setText(self.settings.get("default_scan_backup_root", ""))
        self.scan_backup_root_edit.setPlaceholderText("e.g. C:\\Users\\You\\SaveGuard Backups")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse_scan_backup_root)
        path_row.addWidget(self.scan_backup_root_edit)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        layout.addStretch()
        self._on_slider_changed(self.slider.value())
        return tab

    def _on_browse_scan_backup_root(self):
        path = QFileDialog.getExistingDirectory(self, "Select Default Backup Location")
        if path:
            self.scan_backup_root_edit.setText(path)

    def _build_about_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 20, 4, 4)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignTop)

        icon_label = QLabel()
        icon_label.setPixmap(load_icon_pixmap(56))
        icon_label.setFixedSize(56, 56)
        icon_label.setScaledContents(True)
        layout.addWidget(icon_label, 0, Qt.AlignHCenter)
        layout.addSpacing(10)

        name_label = QLabel("SaveGuard")
        name_label.setAlignment(Qt.AlignHCenter)
        name_label.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(name_label)

        version_label = QLabel(f"Version {__version__}")
        version_label.setAlignment(Qt.AlignHCenter)
        version_label.setStyleSheet("color: #8b90a3; font-size: 12px;")
        layout.addWidget(version_label)

        layout.addSpacing(24)

        update_row = QHBoxLayout()
        update_row.addStretch()
        self.update_btn = QPushButton("Check for Updates")
        self.update_btn.setMinimumHeight(32)
        self.update_btn.setMinimumWidth(140)
        self.update_btn.clicked.connect(self._on_check_updates)
        update_row.addWidget(self.update_btn)
        update_row.addStretch()
        layout.addLayout(update_row)
        layout.addSpacing(14)

        tagline = QLabel("A backup utility for gamers who don't use Steam Cloud "
                          "or any other launcher's save sync — or want a local "
                          "backup on top of it.")
        tagline.setAlignment(Qt.AlignHCenter)
        tagline.setWordWrap(True)
        tagline.setStyleSheet("color: #8b90a3; font-size: 12px;")
        layout.addWidget(tagline)
        layout.addSpacing(16)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet("background-color: #262b3a; max-height: 1px; border: none;")
        layout.addWidget(divider)
        layout.addSpacing(12)

        config_row = QVBoxLayout()
        config_row.setSpacing(2)
        config_label = QLabel("Config & data location")
        config_label.setStyleSheet("font-weight: 600; font-size: 12px;")
        config_path = QLabel(str(APP_DIR))
        config_path.setWordWrap(True)
        config_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        config_path.setStyleSheet("color: #8b90a3; font-size: 11px;")
        config_row.addWidget(config_label)
        config_row.addWidget(config_path)
        layout.addLayout(config_row)
        layout.addSpacing(10)

        disclaimer = QLabel(
            "SaveGuard is an independent tool and isn't affiliated with or "
            "endorsed by Valve, Steam, or any game publisher."
        )
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet("color: #8b90a3; font-size: 10.5px;")
        layout.addWidget(disclaimer)

        layout.addStretch()
        return tab

    def _on_slider_changed(self, value: int):
        self.value_label.setText(str(value))
        self.description_label.setText(_LEVEL_DESCRIPTIONS.get(value, ""))

    def _on_save(self):
        self.settings["compression_level"] = self.slider.value()
        self.settings["max_backups"] = self.max_backups_spin.value()

        path = self.scan_backup_root_edit.text().strip()
        if path:
            if not os.path.isdir(path):
                QMessageBox.warning(
                    self,
                    "Invalid path",
                    f"The folder you entered doesn't exist:\n{path}\n\n"
                    "Please choose an existing folder, or clear the field to use "
                    "the default location.",
                )
                return
            self.settings["default_scan_backup_root"] = path
        # if left blank, silently keep whatever was there before rather than
        # writing an empty path — MainWindow always has a sane built-in
        # fallback anyway, so this only matters if the field is left empty
        # on purpose after clearing it, which we treat as "no change".

        self.result_settings = self.settings
        self.accept()

    def _on_check_updates(self):
        """Run an update check in a background thread and show the result."""
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Checking…")

        worker = _UpdateCheckWorker()
        worker.result.connect(self._handle_update_check_result)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._update_worker = worker

    def _handle_update_check_result(self, release_info):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Check for Updates")

        if release_info is None:
            QMessageBox.information(
                self,
                "Up to Date",
                f"SaveGuard {__version__} is the latest version.",
            )
            return

        notes = fetch_release_notes(release_info.version)
        msg = (
            f"A new version (v{release_info.version}) is available.\n\n"
            f"You're currently on v{__version__}.\n\n"
            "Would you like to open the release page in your browser?"
        )
        if notes:
            msg += f"\n\nRelease notes:\n{notes[:500]}"
        reply = QMessageBox.question(
            self,
            "Update Available",
            msg,
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl(release_info.url))


class _UpdateCheckWorker(QThread):
    """Runs check_for_updates() on a background thread so the dialog
    doesn't freeze during the network request."""
    result = Signal(object)

    def run(self):
        try:
            release = check_for_updates(force=True)
        except Exception:
            release = None
        self.result.emit(release)
