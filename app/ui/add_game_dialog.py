"""Modal dialog used both to add a new game and to edit an existing one."""
import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QFrame
)

from app.utils import paths_conflict


class AddGameDialog(QDialog):
    def __init__(self, parent=None, existing=None):
        super().__init__(parent)
        self.existing = existing  # Game or None
        self.setWindowTitle("Edit Game" if existing else "Add Game")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._build_ui()
        if existing:
            self._prefill(existing)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(16)

        title = QLabel("Edit Game" if self.existing else "Add a Game")
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        subtitle = QLabel("Point SaveGuard at a save folder and where backups should go.")
        subtitle.setStyleSheet("color: #8b90a3; font-size: 12px;")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        root.addWidget(self._divider())

        # Game name
        root.addWidget(self._field_label("GAME NAME"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Cyberpunk 2077")
        root.addWidget(self.name_edit)

        # Save path
        root.addWidget(self._field_label("SAVE FOLDER"))
        self.save_path_edit, save_row = self._path_row(
            "Folder where the game stores its save files", self._browse_save_path
        )
        root.addLayout(save_row)

        # Backup destination
        root.addWidget(self._field_label("BACKUP DESTINATION"))
        self.backup_path_edit, backup_row = self._path_row(
            "Folder where backups should be copied to", self._browse_backup_path
        )
        root.addLayout(backup_row)

        root.addSpacing(8)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save Game" if self.existing else "Add Game")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    def _divider(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: #262b3a; max-height: 1px; border: none;")
        return line

    def _field_label(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("FieldLabel")
        return lbl

    def _path_row(self, placeholder, browse_slot):
        row = QHBoxLayout()
        row.setSpacing(8)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        browse = QPushButton("Browse…")
        browse.clicked.connect(browse_slot)
        row.addWidget(edit)
        row.addWidget(browse)
        return edit, row

    def _browse_save_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Save Folder")
        if path:
            self.save_path_edit.setText(path)

    def _browse_backup_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Backup Destination")
        if path:
            self.backup_path_edit.setText(path)

    def _prefill(self, game):
        self.name_edit.setText(game.name)
        self.save_path_edit.setText(game.save_path)
        self.backup_path_edit.setText(game.backup_root)

    def _on_save(self):
        name = self.name_edit.text().strip()
        save_path = self.save_path_edit.text().strip()
        backup_path = self.backup_path_edit.text().strip()

        if not name:
            QMessageBox.warning(self, "Missing info", "Please enter a game name.")
            return
        if not save_path:
            QMessageBox.warning(self, "Missing info", "Please choose the save folder.")
            return
        if not os.path.isdir(save_path):
            QMessageBox.warning(
                self, "Save folder not found",
                f"The save folder doesn't exist:\n{save_path}\n\n"
                "Please browse to select a folder that exists.",
            )
            return
        if not backup_path:
            QMessageBox.warning(self, "Missing info", "Please choose a backup destination.")
            return
        if paths_conflict(save_path, backup_path):
            QMessageBox.warning(
                self, "Overlapping folders",
                "The backup destination can't be the same as, or inside, the "
                "save folder — each backup would end up including all the "
                "previous ones. Please choose a separate folder for backups."
            )
            return

        self.result_data = {
            "name": name,
            "save_path": save_path,
            "backup_root": backup_path,
        }
        self.accept()
