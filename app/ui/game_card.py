"""A single 'card' widget representing one tracked game inside the main list."""

import os
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QMessageBox
)

from app.ui.color_picker_popup import ColorPickerPopup

STATUS_TEXT = {
    "synced": "Up to date",
    "changed": "Changes detected",
    "never": "Never backed up",
    "invalid": "Path missing",
}

# A small set of muted accent colors used for the initials avatar by default.
# Picked per-game (by name hash) so each card is easy to tell apart at a glance,
# unless the user has manually assigned a color tag.
AVATAR_COLORS = [
    "#7c5cff",  # violet
    "#5cc9ff",  # sky
    "#33d17a",  # green
    "#ffb454",  # amber
    "#ff6b9d",  # pink
    "#5ce0c7",  # teal
]


def _initials_for(name: str) -> str:
    words = [w for w in name.strip().split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _default_color_for(name: str) -> str:
    return AVATAR_COLORS[sum(ord(c) for c in name) % len(AVATAR_COLORS)]


def _format_time(iso_ts: str) -> str:
    if not iso_ts:
        return "Never"
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%d_%H-%M-%S")
        return dt.strftime("%b %d, %Y · %I:%M %p")
    except ValueError:
        return iso_ts


class ColorTagSwatch(QLabel):
    """The clickable initials avatar. Click opens a small popup with a
    curated color palette, plus quick "Automatic" and "Custom…" options —
    see app.ui.color_picker_popup for why this replaces the native OS
    color dialog here."""

    color_picked = Signal(str)   # emits new hex color, e.g. "#ff6b9d"
    color_reset = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to set a tag color")
        self._popup = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            current = self.property("current_color") or "#7c5cff"
            # Keep a reference so the popup isn't garbage-collected before
            # it can show; it cleans itself up (WA_DeleteOnClose) once closed.
            self._popup = ColorPickerPopup(current, self)
            self._popup.color_picked.connect(self.color_picked)
            self._popup.color_reset.connect(self.color_reset)
            self._popup.show()
        super().mousePressEvent(event)


class GameCard(QFrame):
    backup_requested = Signal(str)
    edit_requested = Signal(str)
    remove_requested = Signal(str)
    color_changed = Signal(str, object)  # game_id, new_color (str or None for auto)

    def __init__(self, game, status: str, parent=None):
        super().__init__(parent)
        self.game = game
        self.status = status
        self.setObjectName("GameCard")
        self._build_ui()
        # Note: intentionally no QGraphicsDropShadowEffect here — profiling
        # showed it makes every repaint ~8x more expensive (software blur
        # recalculated per card), which adds up fast when scrolling a long
        # game list. The border + background contrast from the stylesheet
        # already reads clearly as a card without that cost.

    # ---------- UI construction ----------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        icon_label = ColorTagSwatch()
        icon_label.setText(_initials_for(self.game.name))
        icon_label.setFixedSize(44, 44)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.color_picked.connect(self._on_color_picked)
        icon_label.color_reset.connect(self._on_color_reset)
        self.icon_label = icon_label
        self._apply_avatar_color()
        top_row.addWidget(icon_label)

        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        self.name_label = QLabel(self.game.name)
        self.name_label.setObjectName("GameName")
        name_col.addWidget(self.name_label)

        self.path_label = QLabel(self._elide(self.game.save_path))
        self.path_label.setObjectName("GamePath")
        self.path_label.setToolTip(self.game.save_path)
        name_col.addWidget(self.path_label)

        top_row.addLayout(name_col, 1)

        self.status_badge = QLabel(STATUS_TEXT.get(self.status, self.status))
        self.status_badge.setProperty("status", self.status)
        self.status_badge.setAlignment(Qt.AlignCenter)
        top_row.addWidget(self.status_badge, 0, Qt.AlignTop)

        outer.addLayout(top_row)

        # Meta row: last backup + backup count
        meta_row = QHBoxLayout()
        self.meta_label = QLabel(self._meta_text())
        self.meta_label.setObjectName("MetaLabel")
        meta_row.addWidget(self.meta_label)
        meta_row.addStretch()
        outer.addLayout(meta_row)

        # Progress bar (hidden until a backup is running)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        outer.addWidget(self.progress_bar)

        # Actions row
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)

        self.backup_btn = QPushButton("Backup Now")
        self.backup_btn.setObjectName("PrimaryButton")
        self.backup_btn.clicked.connect(lambda: self.backup_requested.emit(self.game.id))
        actions_row.addWidget(self.backup_btn)

        open_btn = QPushButton("Open Backups")
        open_btn.clicked.connect(self._open_backup_folder)
        actions_row.addWidget(open_btn)

        actions_row.addStretch()

        edit_btn = QPushButton("Edit")
        edit_btn.setObjectName("GhostButton")
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.game.id))
        actions_row.addWidget(edit_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.setObjectName("DangerButton")
        remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.game.id))
        actions_row.addWidget(remove_btn)

        outer.addLayout(actions_row)

    def _apply_avatar_color(self):
        color = self.game.color or _default_color_for(self.game.name)
        self.icon_label.setProperty("current_color", color)
        self.icon_label.setStyleSheet(
            f"background-color: #22{color[1:]}; color: {color}; "
            f"border-radius: 10px; font-size: 15px; font-weight: 700;"
        )

    def _on_color_picked(self, hex_color: str):
        self.game.color = hex_color
        self._apply_avatar_color()
        self.color_changed.emit(self.game.id, hex_color)

    def _on_color_reset(self):
        self.game.color = None
        self._apply_avatar_color()
        self.color_changed.emit(self.game.id, None)

    # ---------- helpers ----------

    def _elide(self, path: str, max_len: int = 58) -> str:
        if not path:
            return "No save folder set"
        if len(path) <= max_len:
            return path
        head = path[: max_len // 2 - 2]
        tail = path[-(max_len // 2 - 2):]
        return f"{head}…{tail}"

    def _meta_text(self) -> str:
        last = _format_time(self.game.last_backup_time)
        count = self.game.backup_count
        plural = "backup" if count == 1 else "backups"
        return f"Last backup: {last}   ·   {count} {plural}"

    def _open_backup_folder(self):
        if self.game.backup_root and os.path.isdir(self.game.backup_root):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.game.backup_root))
        elif self.game.backup_count > 0:
            # Previously this silently did nothing -- clicking the button
            # gave zero feedback either way, whether there was no backup
            # yet or the destination had simply been moved/deleted.
            QMessageBox.information(
                self, "Backup folder not found",
                "The backup folder couldn't be found — it may have been moved or deleted.",
            )
        else:
            QMessageBox.information(
                self, "No backups yet",
                f"“{self.game.name}” hasn't been backed up yet, so there's "
                f"no backup folder to open. Click “Backup Now” first.",
            )

    # ---------- external updates ----------

    def set_status(self, status: str):
        self.status = status
        self.status_badge.setText(STATUS_TEXT.get(status, status))
        self.status_badge.setProperty("status", status)
        self.status_badge.style().polish(self.status_badge)
        self.status_badge.update()

    def refresh_meta(self):
        self.meta_label.setText(self._meta_text())
        self.path_label.setText(self._elide(self.game.save_path))
        self.path_label.setToolTip(self.game.save_path)
        self.name_label.setText(self.game.name)
        self.icon_label.setText(_initials_for(self.game.name))
        self._apply_avatar_color()

    def set_backing_up(self, active: bool):
        self.progress_bar.setVisible(active)
        self.progress_bar.setValue(0)
        self.backup_btn.setEnabled(not active)
        self.backup_btn.setText("Backing up…" if active else "Backup Now")

    def set_progress(self, current: int, total: int):
        pct = int((current / total) * 100) if total else 100
        self.progress_bar.setValue(pct)
