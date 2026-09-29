"""Main window: header, banner for pending changes, scrollable card list.

Performance note: computing a game's backup status requires walking its save
folder on disk. That work is done in one pass per rescan and cached in
`self.status_cache`, then reused by the banner, status bar, and the
"Backup All" button — so refreshing the UI never re-walks the filesystem
more than once per game per user action (see _recompute_statuses).
"""

import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QLineEdit, QMessageBox, QFrame
)

from app.config import load_games, save_games, load_state, save_state, load_settings, save_settings
from app.models import Game
from app.scanner import build_snapshot, snapshots_differ
from app.backup_worker import BackupWorker
from app.game_scanner import GameScanWorker
from app.assets import load_icon_pixmap, load_app_icon
from app.utils import sanitize_folder_name, clamp_compression_level, clamp_max_backups
from app.ui.game_card import GameCard
from app.ui.add_game_dialog import AddGameDialog
from app.ui.scan_results_dialog import ScanResultsDialog
from app.ui.settings_dialog import SettingsDialog

# TODO: replace with the real donation page once it exists. Wired up now so
# the button ships in 1.0 and just needs this one string updated later —
# nothing else in the app depends on this being a placeholder.
SUPPORT_URL = "https://donatr.ee/REPLACE_WITH_REAL_LINK"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SaveGuard — Game Save Backup Utility")
        self.setWindowIcon(load_app_icon())
        self.resize(1080, 680)
        self.setMinimumSize(1010, 480)

        self.games: dict[str, Game] = {}
        self.cards: dict[str, GameCard] = {}
        self.workers: dict[str, BackupWorker] = {}
        self.status_cache: dict[str, str] = {}  # game_id -> status, refreshed on demand
        self.scan_worker: GameScanWorker = None
        self.settings: dict = load_settings()

        self._load_games()
        self._recompute_statuses()   # one disk walk per game, populates the cache
        self._build_ui()             # cards, banner, status bar all read from the cache
        self._show_startup_summary()
        self._maybe_run_first_scan()

    # ---------------- data ----------------

    def _load_games(self):
        raw = load_games()
        self.games = {g["id"]: Game.from_dict(g) for g in raw} if raw else {}

    def _persist(self):
        if not save_games([g.to_dict() for g in self.games.values()]):
            self.status_bar.showMessage(
                "Couldn't save your changes to disk — check that SaveGuard has "
                "write access and there's free disk space.", 8000
            )

    # ---------------- UI construction ----------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("Central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self.banner = self._build_banner()
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        # Scroll area with card list
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(24, 18, 24, 24)
        self.list_layout.setSpacing(14)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_container)
        root.addWidget(self.scroll, 1)

        self.status_bar = self.statusBar()

        self._rebuild_cards()
        self._update_banner_from_cache()
        self._refresh_status_bar()
        self._update_backup_all_button()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)

        logo = QLabel()
        logo.setFixedSize(36, 36)
        logo.setPixmap(load_icon_pixmap(72))
        logo.setScaledContents(True)
        layout.addWidget(logo)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("SaveGuard")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Manual game save backups — no launcher required")
        subtitle.setObjectName("AppSubtitle")
        # Word-wrap lets this label shrink well below its full-text width
        # instead of forcing the whole header to stay that wide.
        subtitle.setWordWrap(True)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        title_col_widget = QWidget()
        title_col_widget.setLayout(title_col)
        title_col_widget.setMaximumWidth(230)
        layout.addWidget(title_col_widget)

        # The search box is the one flexible element in the header: it
        # grows to fill extra space and shrinks (down to a sane floor)
        # instead of staying a fixed width and forcing everything else to
        # collide when the window is narrower than the header's old,
        # rigidly-computed minimum size.
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search games…")
        self.search_edit.setMinimumWidth(120)
        self.search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_edit, 1)

        refresh_btn = QPushButton("Rescan")
        refresh_btn.clicked.connect(self._rescan_all)
        layout.addWidget(refresh_btn)

        self.scan_games_btn = QPushButton("Scan Games")
        self.scan_games_btn.setToolTip("Scan common save locations (My Games, Saved Games, Documents, AppData, Steam) for games to add")
        self.scan_games_btn.clicked.connect(self._on_scan_for_games)
        layout.addWidget(self.scan_games_btn)

        self.backup_all_btn = QPushButton("Backup All")
        self.backup_all_btn.clicked.connect(self._on_backup_all)
        layout.addWidget(self.backup_all_btn)

        add_btn = QPushButton("Add Game")
        add_btn.setObjectName("PrimaryButton")
        add_btn.clicked.connect(self._on_add_game)
        layout.addWidget(add_btn)

        settings_btn = QPushButton("Settings")
        settings_btn.setObjectName("GhostButton")
        settings_btn.clicked.connect(self._on_open_settings)
        layout.addWidget(settings_btn)

        support_btn = QPushButton("♥ Support")
        support_btn.setObjectName("SupportButton")
        support_btn.setToolTip("Support SaveGuard's development")
        support_btn.setCursor(Qt.PointingHandCursor)
        support_btn.clicked.connect(self._on_support_clicked)
        layout.addWidget(support_btn)

        return header

    def _build_banner(self) -> QWidget:
        banner = QFrame()
        banner.setObjectName("Banner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)

        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background-color: #ffb454; border-radius: 4px;")
        layout.addWidget(dot, 0, Qt.AlignVCenter)

        self.banner_label = QLabel("")
        layout.addWidget(self.banner_label)
        layout.addStretch()

        banner_backup_btn = QPushButton("Backup All")
        banner_backup_btn.setObjectName("PrimaryButton")
        banner_backup_btn.clicked.connect(self._on_backup_all)
        layout.addWidget(banner_backup_btn)

        dismiss = QPushButton("Dismiss")
        dismiss.setObjectName("GhostButton")
        dismiss.clicked.connect(lambda: self.banner.setVisible(False))
        layout.addWidget(dismiss)
        # wrap in a margin container so it doesn't touch the header
        wrapper = QFrame()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(24, 12, 24, 0)
        wl.addWidget(banner)
        return wrapper

    # ---------------- card list management ----------------

    def _rebuild_cards(self):
        # clear existing widgets (except the trailing stretch)
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.cards.clear()

        if not self.games:
            empty = QLabel(
                "No games added yet.\nClick “+ Add Game” to start tracking a save folder."
            )
            empty.setObjectName("EmptyState")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("padding: 60px; font-size: 14px;")
            self.list_layout.insertWidget(0, empty)
            return

        for game in sorted(self.games.values(), key=lambda g: g.name.lower()):
            status = self.status_cache.get(game.id, "never")
            card = GameCard(game, status)
            card.backup_requested.connect(self._on_backup_requested)
            card.edit_requested.connect(self._on_edit_game)
            card.remove_requested.connect(self._on_remove_game)
            card.color_changed.connect(self._on_color_changed)
            self.cards[game.id] = card
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    # ---------------- status / scanning ----------------
    #
    # _compute_status() is the only place that touches the filesystem. Every
    # other method below reads from self.status_cache instead of calling it
    # directly, so a UI refresh never re-walks a save folder that hasn't
    # actually changed.

    def _compute_status(self, game: Game) -> str:
        if not game.save_path or not os.path.isdir(game.save_path):
            return "invalid"
        if game.last_backup_time is None:
            return "never"
        current = build_snapshot(game.save_path)
        if snapshots_differ(game.last_snapshot, current):
            return "changed"
        return "synced"

    def _recompute_statuses(self, game_ids=None):
        """Walk save folders and refresh the cache. Pass game_ids to limit
        the (potentially slow) disk work to just the games that need it."""
        targets = (
            [self.games[gid] for gid in game_ids if gid in self.games]
            if game_ids is not None
            else list(self.games.values())
        )
        for game in targets:
            self.status_cache[game.id] = self._compute_status(game)

    def _rescan_all(self):
        """Full manual rescan — used by the 'Rescan' button. Walks every
        tracked save folder exactly once."""
        self._recompute_statuses()
        for game_id, card in self.cards.items():
            card.set_status(self.status_cache.get(game_id, "invalid"))
        self._update_banner_from_cache()
        self._refresh_status_bar()
        self._update_backup_all_button()

    def _show_startup_summary(self):
        changed = [g.name for g in self.games.values() if self.status_cache.get(g.id) == "changed"]
        if changed:
            self.status_bar.showMessage(
                f"Welcome back — {len(changed)} game(s) have new save data.", 6000
            )

    def _update_backup_all_button(self):
        changed_count = sum(1 for s in self.status_cache.values() if s == "changed")
        self.backup_all_btn.setEnabled(changed_count > 0)
        self.backup_all_btn.setText(
            f"Backup All ({changed_count})" if changed_count > 0 else "Backup All"
        )

    def _update_banner_from_cache(self):
        changed_names = [g.name for g in self.games.values() if self.status_cache.get(g.id) == "changed"]
        self._update_banner(changed_names)

    def _update_banner(self, changed_games: list):
        if changed_games:
            names = ", ".join(changed_games[:4])
            extra = f" and {len(changed_games) - 4} more" if len(changed_games) > 4 else ""
            self.banner_label.setText(
                f"Unsaved progress detected in: {names}{extra}. Consider backing up now."
            )
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    def _refresh_status_bar(self):
        total = len(self.games)
        needing = sum(1 for s in self.status_cache.values() if s in ("changed", "never"))
        if total == 0:
            self.status_bar.showMessage("No games tracked yet.")
        elif needing == 0:
            self.status_bar.showMessage(f"{total} game(s) tracked — all backups up to date.")
        else:
            self.status_bar.showMessage(f"{total} game(s) tracked — {needing} need a backup.")

    # ---------------- add / edit / remove ----------------

    def _on_add_game(self):
        dialog = AddGameDialog(self)
        if dialog.exec():
            data = dialog.result_data
            game = Game.new(data["name"], data["save_path"], data["backup_root"])
            self.games[game.id] = game
            self._persist()
            self._recompute_statuses(game_ids=[game.id])  # only walk the new game's folder
            self._rebuild_cards()
            self._update_banner_from_cache()
            self._refresh_status_bar()
            self._update_backup_all_button()

    def _on_edit_game(self, game_id: str):
        game = self.games.get(game_id)
        if not game:
            return
        dialog = AddGameDialog(self, existing=game)
        if dialog.exec():
            data = dialog.result_data
            game.name = data["name"]
            game.save_path = data["save_path"]
            game.backup_root = data["backup_root"]
            self._persist()
            self._recompute_statuses(game_ids=[game.id])  # path may have changed, re-check just this one
            self._rebuild_cards()
            self._update_banner_from_cache()
            self._refresh_status_bar()
            self._update_backup_all_button()

    def _on_remove_game(self, game_id: str):
        game = self.games.get(game_id)
        if not game:
            return
        reply = QMessageBox.question(
            self,
            "Remove Game",
            f"Stop tracking “{game.name}”?\n\nThis will not delete any existing backup files.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            del self.games[game_id]
            self.status_cache.pop(game_id, None)  # no disk access needed at all
            self._persist()
            self._rebuild_cards()
            self._update_banner_from_cache()
            self._refresh_status_bar()
            self._update_backup_all_button()

    def _on_color_changed(self, game_id: str, new_color):
        game = self.games.get(game_id)
        if not game:
            return
        game.color = new_color  # the card widget already updated its own display
        self._persist()

    # ---------------- settings ----------------

    def _on_open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.result_settings
            if not save_settings(self.settings):
                self.status_bar.showMessage(
                    "Settings applied for this session, but couldn't be saved to disk "
                    "— they may not persist after closing SaveGuard.", 8000
                )

    def _on_support_clicked(self):
        QDesktopServices.openUrl(QUrl(SUPPORT_URL))

    # ---------------- game discovery (scan common save locations) ----------------

    def _maybe_run_first_scan(self):
        """Runs the location scan automatically the very first time the app
        is opened. A short delay lets the window finish painting first so
        the results dialog (if anything is found) doesn't pop up before the
        main window is even visible."""
        state = load_state()
        if state.get("first_scan_done"):
            return
        state["first_scan_done"] = True
        save_state(state)
        QTimer.singleShot(500, self._start_scan)

    def _on_scan_for_games(self):
        if self.scan_worker is not None:
            self.status_bar.showMessage("A scan is already in progress…", 3000)
            return
        self._start_scan()

    def _start_scan(self):
        if self.scan_worker is not None:
            return
        self.scan_games_btn.setEnabled(False)
        self.scan_games_btn.setText("Scanning…")
        self.status_bar.showMessage("Scanning common save locations…")

        existing_paths = {g.save_path for g in self.games.values()}
        worker = GameScanWorker(existing_paths)
        worker.scan_finished.connect(self._on_scan_finished)
        worker.scan_error.connect(self._on_scan_error)
        # Same pattern as BackupWorker: only drop our reference once QThread's
        # own built-in `finished` signal confirms the OS thread has actually
        # terminated, not the moment our custom result signal arrives.
        worker.finished.connect(self._on_scan_thread_finished)
        self.scan_worker = worker
        worker.start()

    def _on_scan_thread_finished(self):
        self.scan_games_btn.setEnabled(True)
        self.scan_games_btn.setText("Scan Games")
        self.scan_worker = None

    def _on_scan_finished(self, candidates):
        if not candidates:
            self.status_bar.showMessage("No new games found in common save locations.", 5000)
            return
        dialog = ScanResultsDialog(candidates, self)
        if dialog.exec():
            self._add_scanned_games(dialog.selected_candidates)

    def _on_scan_error(self, message: str):
        self.status_bar.showMessage(f"Scan couldn't finish: {message}", 6000)

    def _add_scanned_games(self, candidates):
        if not candidates:
            return
        backups_root = self.settings.get("default_scan_backup_root") or os.path.join(
            os.path.expanduser("~"), "SaveGuard Backups"
        )
        new_ids = []
        for candidate in candidates:
            backup_root = os.path.join(backups_root, sanitize_folder_name(candidate.name))
            game = Game.new(candidate.name, candidate.save_path, backup_root)
            self.games[game.id] = game
            new_ids.append(game.id)

        self._persist()
        self._recompute_statuses(game_ids=new_ids)  # only walk the newly added games
        self._rebuild_cards()
        self._update_banner_from_cache()
        self._refresh_status_bar()
        self._update_backup_all_button()
        self.status_bar.showMessage(
            f"Added {len(candidates)} game{'s' if len(candidates) != 1 else ''} from scan.", 5000
        )

    # ---------------- shutdown ----------------

    def closeEvent(self, event):
        # Give any in-flight background thread a moment to finish cleanly
        # instead of yanking the process out from under it.
        running = list(self.workers.values())
        if self.scan_worker is not None:
            running.append(self.scan_worker)

        in_flight = [w for w in running if w.isRunning()]
        if in_flight:
            reply = QMessageBox.question(
                self,
                "Backups in progress",
                "SaveGuard is currently backing up game(s). "
                "Quitting now may leave those backups incomplete.\n\n"
                "Do you want to quit anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                # Don't quit - let the backups finish. The window stays open
                # and the user can continue using the app while backups run.
                event.ignore()
                return

        event.accept()

    # ---------------- search ----------------

    def _on_search_changed(self, text: str):
        text = text.strip().lower()
        for game_id, card in self.cards.items():
            game = self.games.get(game_id)
            visible = (not text) or (text in game.name.lower())
            card.setVisible(visible)

    # ---------------- backup flow ----------------

    def _on_backup_all(self):
        changed_ids = [
            game_id for game_id, status in self.status_cache.items()
            if status == "changed" and game_id not in self.workers
        ]
        if not changed_ids:
            self.status_bar.showMessage("No changed saves to back up right now.", 4000)
            return

        for game_id in changed_ids:
            self._on_backup_requested(game_id)

        self.status_bar.showMessage(f"Backing up {len(changed_ids)} game(s)…", 4000)

    def _on_backup_requested(self, game_id: str):
        game = self.games.get(game_id)
        card = self.cards.get(game_id)
        if not game or not card:
            return
        if game_id in self.workers:
            return  # already backing up
        if not os.path.isdir(game.save_path):
            QMessageBox.warning(self, "Save folder missing",
                                 f"The save folder for “{game.name}” could not be found:\n{game.save_path}")
            return

        card.set_backing_up(True)
        worker = BackupWorker(
            game.id, game.name, game.save_path, game.backup_root,
            compression_level=clamp_compression_level(self.settings.get("compression_level")),
            max_backups=clamp_max_backups(self.settings.get("max_backups")),
        )
        worker.progress.connect(self._on_backup_progress)
        worker.backup_finished.connect(self._on_backup_finished)
        worker.error.connect(self._on_backup_error)
        # QThread's own built-in `finished` signal only fires once the OS
        # thread has actually terminated — that's the safe point to drop our
        # last reference, not the moment our custom `backup_finished`/`error`
        # signal arrives (which is emitted just before run() returns).
        worker.finished.connect(lambda gid=game_id: self.workers.pop(gid, None))
        self.workers[game_id] = worker
        worker.start()

    def _on_backup_progress(self, game_id: str, current: int, total: int):
        card = self.cards.get(game_id)
        if card:
            card.set_progress(current, total)

    def _on_backup_finished(self, game_id: str, new_snapshot: dict, timestamp: str,
                             zip_path: str, backups_remaining: int):
        game = self.games.get(game_id)
        card = self.cards.get(game_id)
        if not game:
            return
        game.last_snapshot = new_snapshot
        game.last_backup_time = timestamp
        # Reflects what's actually on disk right now, not a lifetime count —
        # matters once backup retention pruning is in play (Settings >
        # Backups > "Backups to keep per game"), otherwise "N backups" would
        # keep climbing even as old ones get deleted.
        game.backup_count = backups_remaining
        self._persist()

        # We already know the outcome (we just captured the post-backup
        # snapshot), so update the cache directly instead of re-walking disk.
        self.status_cache[game_id] = "synced"

        if card:
            card.set_backing_up(False)
            card.set_status("synced")
            card.refresh_meta()

        self.status_bar.showMessage(f"Backed up “{game.name}” to {zip_path}", 6000)
        self._update_banner_from_cache()
        self._refresh_status_bar()
        self._update_backup_all_button()

    def _on_backup_error(self, game_id: str, message: str):
        card = self.cards.get(game_id)
        if card:
            card.set_backing_up(False)
        QMessageBox.critical(self, "Backup failed", message)
