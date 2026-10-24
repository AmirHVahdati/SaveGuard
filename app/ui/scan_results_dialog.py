"""Dialog shown after a game-location scan completes. Nothing found by the
scanner is ever added automatically — the user reviews and picks which
candidates to start tracking, since folder-name heuristics (especially for
Documents and AppData) will occasionally guess wrong."""

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QCheckBox, QFrame, QMessageBox, QLineEdit
)


class _EditableCandidate:
    """Wraps a ScanCandidate with an editable name field. Used so the user
    can correct a wrong game name before adding."""

    def __init__(self, candidate):
        self.candidate = candidate
        self.name_override = None  # if set, overrides candidate.name

    @property
    def name(self):
        return self.name_override or self.candidate.name

    @name.setter
    def name(self, value):
        self.name_override = value

    @property
    def save_path(self):
        return self.candidate.save_path

    @property
    def source(self):
        return self.candidate.source

    @property
    def confidence(self):
        return self.candidate.confidence


class ScanResultsDialog(QDialog):
    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.candidates = [_EditableCandidate(c) for c in candidates]
        self.checkboxes = []  # list of (QCheckBox, candidate)

        self.setWindowTitle("Games Found")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setMinimumHeight(420)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(14)

        title = QLabel(f"Found {len(self.candidates)} possible game{'s' if len(self.candidates) != 1 else ''}")
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        subtitle = QLabel(
            "Scanned common save locations — Documents, AppData, Steam, Proton "
            "game prefixes, and Linux config/data folders. Review the list "
            "and check the games you want to start tracking."
        )
        subtitle.setStyleSheet("color: #8b90a3; font-size: 12px;")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        select_row = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.setObjectName("GhostButton")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn = QPushButton("Select None")
        select_none_btn.setObjectName("GhostButton")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        copy_btn = QPushButton("Copy Results")
        copy_btn.setObjectName("GhostButton")
        copy_btn.clicked.connect(self._on_copy_results)
        select_row.addWidget(select_all_btn)
        select_row.addWidget(select_none_btn)
        select_row.addWidget(copy_btn)
        select_row.addStretch()
        root.addLayout(select_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        list_container = QWidget()
        list_layout = QVBoxLayout(list_container)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)

        for candidate in self.candidates:
            list_layout.addWidget(self._build_row(candidate))
        list_layout.addStretch()

        scroll.setWidget(list_container)
        root.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.add_btn = QPushButton()
        self.add_btn.setObjectName("PrimaryButton")
        self.add_btn.setMinimumWidth(110)
        self.add_btn.clicked.connect(self._on_add_clicked)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.add_btn)
        root.addLayout(btn_row)

        self._update_add_button_text()

    def _build_row(self, candidate):
        row = QFrame()
        row.setStyleSheet(
            "background-color: #1a1e2a; border: 1px solid #262b3a; border-radius: 10px;"
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        checkbox = QCheckBox()
        checkbox.setChecked(False)  # let user select which games to track
        checkbox.setFixedSize(22, 22)
        checkbox.setStyleSheet("""
            QCheckBox {
                margin: 0;
                padding: 0;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1.5px solid #8b90a3;
                background: transparent;
            }
            QCheckBox::indicator:checked {
                border-color: #5cc9ff;
                background: #5cc9ff;
            }
            QCheckBox::indicator:checked:hover {
                border-color: #7ad2ff;
            }
        """)
        checkbox.stateChanged.connect(self._update_add_button_text)
        layout.addWidget(checkbox)
        layout.addSpacing(4)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)

        name_edit = QLineEdit()
        name_edit.setText(candidate.name)
        name_edit.setStyleSheet("""
            QLineEdit {
                background-color: #0d111d;
                border: 1px solid #262b3a;
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 13px;
                color: #e0e0e0;
            }
            QLineEdit:focus {
                border-color: #5cc9ff;
            }
        """)
        name_edit.textChanged.connect(lambda text, ec=candidate: setattr(ec, 'name', text))
        text_col.addWidget(name_edit)

        path_label = QLabel(candidate.save_path)
        path_label.setStyleSheet("color: #8b90a3; font-size: 11px;")
        path_label.setWordWrap(True)
        text_col.addWidget(path_label)
        layout.addLayout(text_col, 1)

        source_badge = QLabel(candidate.source)
        badge_color = "#5cc9ff" if candidate.confidence == "high" else "#8b90a3"
        source_badge.setStyleSheet(
            f"color: {badge_color}; font-size: 11px; font-weight: 600; "
            f"background-color: #22{badge_color[1:]}; border-radius: 8px; padding: 3px 8px;"
        )
        layout.addWidget(source_badge, 0, Qt.AlignVCenter)
        layout.addSpacing(6)

        open_btn = QPushButton()
        open_btn.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DirOpenIcon))
        open_btn.setToolTip("Open save folder in Explorer")
        open_btn.setFixedSize(24, 24)
        open_btn.setStyleSheet("""
            QPushButton {
                background-color: #0d111d;
                border: 1px solid #262b3a;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #222a3a;
                border-color: #5cc9ff;
            }
        """)
        open_btn.clicked.connect(lambda checked, path=candidate.save_path: self._open_in_explorer(path))
        layout.addWidget(open_btn, 0, Qt.AlignVCenter)

        self.checkboxes.append((checkbox, candidate))
        return row

    def _set_all_checked(self, checked: bool):
        for checkbox, _candidate in self.checkboxes:
            checkbox.setChecked(checked)

    def _update_add_button_text(self, *_args):
        count = sum(1 for cb, _c in self.checkboxes if cb.isChecked())
        self.add_btn.setEnabled(count > 0)
        self.add_btn.setText(f"Add {count} Game{'s' if count != 1 else ''}" if count else "Add Selected")

    def _on_add_clicked(self):
        self.selected_candidates = [
            self._to_scan_candidate(c)
            for cb, c in self.checkboxes
            if cb.isChecked()
        ]
        self.accept()

    @staticmethod
    def _to_scan_candidate(ec):
        """Convert an _EditableCandidate back to a ScanCandidate with
        the user's name edit applied."""
        from app.game_scanner import ScanCandidate
        return ScanCandidate(
            name=ec.name,
            save_path=ec.save_path,
            source=ec.source,
            confidence=ec.confidence,
        )

    def _open_in_explorer(self, path: str):
        """Open the given folder in the system file explorer."""
        if os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            QMessageBox.warning(self, "Path not found", f"The folder doesn't exist:\n{path}")

    def _on_copy_results(self):
        """Copy all scan results to clipboard as text that can be pasted
        into a bug report or chat message."""
        from PySide6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        lines = []
        for c in self.candidates:
            lines.append(f"{c.name} | {c.save_path} | {c.source} | {c.confidence}")
        text = f"Scan Results ({len(self.candidates)} candidates):\n\n" + "\n".join(lines)
        clipboard.setText(text)
        QMessageBox.information(self, "Copied", f"Copied {len(self.candidates)} scan results to clipboard.")
