"""Main window: header, banner for pending changes, scrollable card list.

Performance note: computing a game's backup status requires walking its save
folder on disk. That work is done in one pass per rescan and cached in
`self.status_cache`, then reused by the banner, status bar, and the
"Backup All" button — so refreshing the UI never re-walks the filesystem
more than once per game per user action (see _recompute_statuses).
"""

import os

from PySide6.QtCore import Qt, QTimer, QUrl, QEvent, QThread, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QLineEdit, QMessageBox, QFrame, QSpacerItem, QSizePolicy
)

from app.config import load_games, save_games, load_state, save_state, load_settings, save_settings
from app.models import Game
from app.scanner import build_snapshot, snapshots_differ
from app.backup_worker import BackupWorker
from app.game_scanner import GameScanWorker
from app.assets import load_icon_pixmap, load_app_icon
from app.utils import (
    clamp_compression_level, clamp_max_backups, comparable_path_key,
    sanitize_folder_name,
)
from app.ui.game_card import GameCard
from app.ui.add_game_dialog import AddGameDialog
from app.ui.scan_results_dialog import ScanResultsDialog
from app.ui.settings_dialog import SettingsDialog

# TODO: replace with the real donation page once it exists. Wired up now so
# the button ships in 1.0 and just needs this one string updated later —
# nothing else in the app depends on this being a placeholder.
SUPPORT_URL = "https://donatr.ee/amirvahdati/"

# Number of extra items to render above/below the visible viewport as a buffer
# so cards don't pop in/out abruptly while scrolling.
VIRTUAL_BUFFER = 3

# Nominal height of a GameCard in pixels (including vertical margin/spacing).
# Only a starting estimate: once the list has been laid out, the real slot pitch
# comes from the scrollbar's own content height (see _slot_pitch). It is
# deliberately on the generous side, because guessing too small risks showing
# fewer cards than fit rather than more.
CARD_HEIGHT = 268  # a card is closer to 128px; this is a conservative bound

# Ceiling on backups running at the same time.
#
# Each backup is its own QThread doing a folder walk, a zip write, and then a
# full re-read of that zip to verify it, so "Backup All" used to start one
# thread per game needing a backup -- measured 12 simultaneously for a 12-game
# library. The contention is not just for CPU: every one of those threads is
# walking a different save folder of small files and writing and re-reading an
# archive, which is exactly the access pattern a single mechanical disk handles
# worst.
#
# A fixed number rather than one derived from cpu_count, so the ceiling is the
# same on every machine and can be asserted in a test. Bounding to 4 measured
# no slower than unbounded on this machine (0.80s vs 0.89s for 12 games of 300
# files), so the ceiling costs no throughput here while keeping a large library
# off the disk's limit -- where the benefit grows, since the same 12 backups on
# an HDD are bound by seeks rather than by cores.
MAX_CONCURRENT_BACKUPS = 4


def _status_for(save_path, last_backup_time, last_snapshot) -> str:
    """The backup status of one game, from its fields alone.

    Split out of MainWindow._compute_status so the off-thread sweep and the
    synchronous single-game path cannot drift apart: both must agree on what
    "changed" means, or a game could read one way right after a backup and the
    other way on the next rescan.
    """
    if not save_path or not os.path.isdir(save_path):
        return "invalid"
    if last_backup_time is None:
        return "never"
    current = build_snapshot(save_path)
    if snapshots_differ(last_snapshot, current):
        return "changed"
    return "synced"


class StatusWorker(QThread):
    """Computes backup statuses for a batch of games, off the main thread.

    Walking a save folder is filesystem I/O, and doing it inline does not merely
    slow the window down, it starves the event loop completely: measured with a
    5ms QTimer running, a sweep of 60 already-backed-up games produced zero
    timer deliveries across its 461ms. The window is already painted by the time
    a startup sweep runs, so there is no partial frame to suggest progress —
    the window simply stops responding until it finishes, which on a slow drive
    or a large library is long enough to read as a hang.

    Takes plain tuples rather than Game objects deliberately. The main thread
    adds, edits and removes games while this runs, so reading a Game's fields
    from here would race those edits; everything a status depends on is copied
    out first, and the answer comes back as a plain {game_id: status} dict for
    the main thread to apply, skipping any game that has since been deleted.

    Only games that already have a backup reach the filesystem at all (see
    _status_for), so the sweep is cheap for a library that has never been
    backed up and proportional to total file count otherwise.
    """

    # Named status_* rather than `finished`, which QThread already defines and
    # which fires only once the OS thread has actually terminated. Same
    # reasoning as BackupWorker.backup_finished.
    status_finished = Signal(dict)   # {game_id: "invalid"|"never"|"changed"|"synced"}
    status_error = Signal(str)

    def __init__(self, targets, parent=None):
        """targets: iterable of (game_id, save_path, last_backup_time, last_snapshot)."""
        super().__init__(parent)
        self.targets = list(targets)

    def run(self):
        statuses = {}
        for game_id, save_path, last_backup_time, last_snapshot in self.targets:
            try:
                statuses[game_id] = _status_for(save_path, last_backup_time, last_snapshot)
            except OSError as exc:
                # One unreadable folder must not abandon the rest of the
                # library's statuses; report it and carry on.
                self.status_error.emit(f"{game_id}: {exc}")
        self.status_finished.emit(statuses)


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
        self.status_worker: StatusWorker = None
        self._backup_queue: list[str] = []
        self._show_summary_after_statuses: bool = False
        self.settings: dict = load_settings()
        self._status_update_pending: bool = False  # for batching UI updates during backup completions

        self._load_games()
        self._build_ui()
        self._maybe_run_first_scan()
        
        QTimer.singleShot(0, self._on_startup_status_ready)

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
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(24, 18, 24, 24)
        self.list_layout.setSpacing(14)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_container)
        self.scroll.installEventFilter(self)
        root.addWidget(self.scroll, 1)

        # Vertical space one list slot occupies, excluding the layout spacing
        # that follows it. A slot is either a real GameCard or a spacer of this
        # height, so a pixel offset maps to the same list index whether or not
        # that game currently has a card. Filled in from a real card as soon as
        # one is built. See _sync_visible_cards.
        self._slot_height = 0
        self._slot_ids: set = set()  # games holding a layout slot, card or spacer

        self.status_bar = self.statusBar()

        self._all_game_ids: list = []  # sorted list of all game IDs
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

    # Threshold for virtual scrolling — above this, we lazily create cards
    # only when they come into view rather than creating all at once.
    _VIRTUAL_SCROLL_THRESHOLD = 50

    def _rebuild_cards(self):
        """Rebuild the complete card list from the games dict. Used during
        initialization and when the game list is fundamentally restructured."""
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.cards.clear()
        self._all_game_ids.clear()
        self._slot_ids.clear()
        self._slot_height = 0

        if not self.games:
            empty = QLabel(
                "No games added yet.\nClick \u201c+ Add Game\u201d to start tracking a save folder."
            )
            empty.setObjectName("EmptyState")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("padding: 60px; font-size: 14px;")
            self.list_layout.insertWidget(0, empty)
            return

        sorted_games = sorted(self.games.values(), key=lambda g: g.name.lower())
        self._all_game_ids = [g.id for g in sorted_games]

        if len(sorted_games) <= self._VIRTUAL_SCROLL_THRESHOLD:
            # Small list: create all cards immediately
            for game in sorted_games:
                self._add_card_for_game(game)
        else:
            # Large list: build the cards in view, then leave a slot of the
            # same height behind for every game that doesn't have one. Those
            # slots are what keep the scroll area as tall as the whole list --
            # without them the scrollbar's range shrinks to the handful of
            # cards that exist, and the tail of a long list becomes impossible
            # to scroll to.
            self._sync_visible_cards()
            self._fill_missing_slots()

    def _slot_spacer(self) -> QSpacerItem:
        """A placeholder standing in for a game that has no card right now.

        Sized from a real card so that materialising a card doesn't change how
        much list there is below it. Before any card exists, fall back to the
        estimate -- _get_visible_range() reads real geometry, so an off-by-a-bit
        fallback can't hide games from the tail, it can only cost a small jump.
        """
        height = self._slot_height or max(1, CARD_HEIGHT - self.list_layout.spacing())
        return QSpacerItem(0, height, QSizePolicy.Minimum, QSizePolicy.Fixed)

    def _fill_missing_slots(self):
        """Give every tracked game a layout slot, so layout position N always
        corresponds to _all_game_ids[N]."""
        for game_id in self._all_game_ids:
            if game_id in self._slot_ids:
                continue
            index = self._all_game_ids.index(game_id)
            self.list_layout.insertItem(index, self._slot_spacer())
            self._slot_ids.add(game_id)

    def _take_slot(self, index: int):
        """Remove and return the layout item at `index`, destroying its widget.

        Used both when a card takes over a spacer's place and when a game
        leaves the list entirely, so that layout position N always corresponds
        to _all_game_ids[N].
        """
        if index < 0 or index >= self.list_layout.count():
            return None
        item = self.list_layout.takeAt(index)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        return item

    def _add_card_for_game(self, game: Game):
        """Create and insert a single GameCard for the given game at its
        position in the list.

        The layout holds exactly one item per entry of _all_game_ids — a card
        where the game has one, a spacer where it doesn't — so a game's position
        is simply its index, and adding a card never disturbs the games around
        it.
        """
        status = self.status_cache.get(game.id, "never")
        card = GameCard(game, status)
        card.backup_requested.connect(self._on_backup_requested)
        card.edit_requested.connect(self._on_edit_game)
        card.remove_requested.connect(self._on_remove_game)
        card.color_changed.connect(self._on_color_changed)
        self._slot_height = card.sizeHint().height() or self._slot_height

        if game.id in self._all_game_ids:
            index = self._all_game_ids.index(game.id)
            if game.id in self._slot_ids:
                # Scrolled back into view: the game already holds a spacer that
                # the card takes over. A game with no slot yet is the first
                # build of a long list — there is nothing to take.
                self._take_slot(index)
        else:
            # Newly tracked game: claim its sorted position and let the card
            # occupy the slot that position implies.
            index = len(self._all_game_ids)
            target_name = game.name.lower()
            for position, existing_id in enumerate(self._all_game_ids):
                existing_game = self.games.get(existing_id)
                if existing_game and existing_game.name.lower() > target_name:
                    index = position
                    break
            self._all_game_ids.insert(index, game.id)

        self.cards[game.id] = card
        self._slot_ids.add(game.id)
        self.list_layout.insertWidget(index, card)

    def _remove_card_for_game(self, game_id: str):
        """Remove and clean up the GameCard for the given game.

        The slot goes too: a game that is no longer tracked must not keep
        reserving a card's worth of scroll height."""
        self.cards.pop(game_id, None)
        if game_id in self._all_game_ids:
            index = self._all_game_ids.index(game_id)
            self._all_game_ids.pop(index)
            self._take_slot(index)
            self._slot_ids.discard(game_id)

    def _slot_pitch(self) -> float:
        """Average vertical distance from one slot to the next.

        Derived from the scrollbar's own numbers -- the full content height is
        its maximum plus its page step -- rather than from the layout's item
        geometry. Item geometry is only trustworthy after Qt has laid the list
        out, and the sync that asks for it is usually the very thing that
        invalidated it, so reading positions mid-sync falls back to estimates
        that disagree with the real layout. The scrollbar always agrees with
        it, which is what keeps the tail of a long list reachable: at maximum
        scroll the last slot's position is inside the range by construction.

        Before the first layout there are no numbers to divide, so fall back to
        the nominal card height. That estimate can only cost a slightly wrong
        fetch window; it cannot hide games, because the scrollbar's range is
        what decides how far the list can be scrolled either way.
        """
        total = len(self._all_game_ids)
        if not total:
            return 0.0
        fallback = self._slot_height or max(1, CARD_HEIGHT - self.list_layout.spacing())
        scrollbar = self.scroll.verticalScrollBar()
        content = (scrollbar.maximum() + scrollbar.pageStep()) if scrollbar else 0
        # A scrollbar that has never been laid out reports a page step but no
        # range. Dividing that stub height by the game count would call every
        # slot visible and quietly un-virtualize the whole list.
        if content > total * fallback:
            return content / total
        return fallback + self.list_layout.spacing()

    def _slot_edges(self) -> list[tuple[int, int]]:
        """(top, bottom) of every tracked slot, in list order."""
        pitch = self._slot_pitch()
        fallback = self._slot_height or max(1, CARD_HEIGHT - self.list_layout.spacing())
        edges = []
        top = 0.0
        for _ in self._all_game_ids:
            edges.append((int(top), int(top) + fallback))
            top += pitch
        return edges

    def _get_visible_range(self) -> tuple[int, int]:
        """Return (start_index, end_index) of the slots currently in view.

        Measured against the slots' real positions rather than by dividing the
        scroll offset by an assumed card height, and with the buffer applied in
        pixels for the same reason.
        """
        total = len(self._all_game_ids)
        if total == 0:
            return 0, 0

        edges = self._slot_edges()
        scrollbar = self.scroll.verticalScrollBar()
        if not scrollbar or not edges:
            return 0, total

        pad = self._slot_height or max(1, CARD_HEIGHT - self.list_layout.spacing())
        viewport_top = scrollbar.value() - VIRTUAL_BUFFER * pad
        viewport_bottom = viewport_top + scrollbar.pageStep() + 2 * VIRTUAL_BUFFER * pad

        start = 0
        while start < total and edges[start][1] <= viewport_top:
            start += 1
        end = start
        while end < total and edges[end][0] < viewport_bottom:
            end += 1

        return start, max(start, end)

    def eventFilter(self, obj, event):
        """Intercept resize events on the scroll area to trigger card sync."""
        if obj is self.scroll and event.type() == QEvent.Resize:
            if len(self._all_game_ids) > self._VIRTUAL_SCROLL_THRESHOLD:
                QTimer.singleShot(0, self._sync_visible_cards)
        return super().eventFilter(obj, event)

    def _sync_visible_cards(self):
        """Sync cards to match current viewport visibility."""
        if len(self._all_game_ids) <= self._VIRTUAL_SCROLL_THRESHOLD:
            return

        visible_start, visible_end = self._get_visible_range()

        # Create cards for newly visible items
        for i in range(visible_start, min(visible_end, len(self._all_game_ids))):
            game_id = self._all_game_ids[i]
            if game_id not in self.cards:
                game = self.games[game_id]
                self._add_card_for_game(game)

        # Clean up cards that are far out of view
        buffer_zone = VIRTUAL_BUFFER + 5
        keep_start = max(0, visible_start - buffer_zone)
        keep_end = min(len(self._all_game_ids), visible_end + buffer_zone)
        ids_to_keep = set(self._all_game_ids[keep_start:keep_end])

        ids_to_remove = [
            gid for gid in self.cards.keys() if gid not in ids_to_keep
        ]
        for game_id in ids_to_remove:
            self._virtualize_card(game_id)

    def _virtualize_card(self, game_id: str):
        """Drop a card that has scrolled too far out of view, leaving its slot
        behind so the game's position in the list doesn't move."""
        self.cards.pop(game_id, None)
        index = self._all_game_ids.index(game_id)
        self._take_slot(index)
        self.list_layout.insertItem(index, self._slot_spacer())

    def _on_scroll(self, value: int):
        """Handle scroll events for virtual scrolling."""
        self._sync_visible_cards()

    # ---------------- status / scanning ----------------
    #
    # _compute_status() is the only place that touches the filesystem. Every
    # other method below reads from self.status_cache instead of calling it
    # directly, so a UI refresh never re-walks a save folder that hasn't
    # actually changed.

    def _compute_status(self, game: Game) -> str:
        return _status_for(game.save_path, game.last_backup_time, game.last_snapshot)

    def _recompute_statuses(self, game_ids=None):
        """Walk save folders and refresh the cache. Pass game_ids to limit
        the (potentially slow) disk work to just the games that need it.

        Synchronous on purpose. This is the path taken when one game is added,
        edited or discovered, where the answer is needed immediately and there
        is exactly one folder to walk -- a few milliseconds. The whole-library
        sweeps go through _start_status_sweep instead.
        """
        targets = (
            [self.games[gid] for gid in game_ids if gid in self.games]
            if game_ids is not None
            else list(self.games.values())
        )
        for game in targets:
            self.status_cache[game.id] = self._compute_status(game)

    def _status_sweep_targets(self) -> list:
        """Everything a status depends on, copied out for the worker.

        Tuples rather than Game objects: the main thread keeps editing games
        while the worker walks, and handing it live references would race those
        edits (see StatusWorker).
        """
        return [
            (game.id, game.save_path, game.last_backup_time, game.last_snapshot)
            for game in self.games.values()
        ]

    def _start_status_sweep(self, show_summary: bool = False):
        """Recompute every game's status on a worker thread, then refresh the UI.

        Used by the startup path and by Rescan. Refuses to start a second sweep
        while one is in flight: both cover the whole library, so a running one
        already produces the answer a new request would, and overlapping walks
        would only contend for the same disks.
        """
        if self.status_worker is not None:
            return
        targets = self._status_sweep_targets()
        if not targets:
            # Nothing tracked: still let the caller run whatever it does after
            # a sweep, rather than leaving it waiting on a signal that will
            # never come.
            if show_summary:
                self._show_startup_summary()
            return

        self._show_summary_after_statuses = show_summary
        # No parent, matching GameScanWorker: a QThread destroyed while it is
        # still running is a fatal Qt error, so the thread must not be a child
        # of a widget that can be closed underneath it. closeEvent waits for it.
        worker = StatusWorker(targets)
        worker.status_finished.connect(self._on_status_finished)
        worker.status_error.connect(self._on_status_error)
        # Drop the reference only once QThread's own `finished` confirms the OS
        # thread has terminated, not when the result signal arrives -- same
        # pattern as GameScanWorker.
        worker.finished.connect(self._on_status_thread_finished)
        self.status_worker = worker
        worker.start()

    def _on_status_thread_finished(self):
        self.status_worker = None

    def _on_status_error(self, message: str):
        self.status_bar.showMessage(f"Couldn't check one save folder: {message}", 6000)

    def _on_status_finished(self, statuses: dict):
        for game_id, status in statuses.items():
            # A game deleted while the sweep was walking is simply not in
            # self.games any more, so its status has nowhere to go.
            if game_id in self.games:
                self.status_cache[game_id] = status
        for game_id, card in self.cards.items():
            card.set_status(self.status_cache.get(game_id, "invalid"))
        self._flush_status_update()
        if self._show_summary_after_statuses:
            self._show_summary_after_statuses = False
            self._show_startup_summary()

    def _on_startup_status_ready(self):
        """Called via QTimer after the window is first shown.

        Defers the sweep rather than skipping it: the walk is real I/O over
        every tracked save folder, and running it here without a worker froze
        the event loop outright (see StatusWorker). The window is painted by
        the time this runs, which is what the timer was for; the freeze it was
        never going to prevent is what the worker is for.
        """
        self._start_status_sweep(show_summary=True)

    def _rescan_all(self):
        """Full manual rescan — used by the 'Rescan' button. Walks every
        tracked save folder exactly once, on a worker thread so the window
        stays responsive while it does."""
        if self.status_worker is not None:
            self.status_bar.showMessage("A status check is already running…", 3000)
            return
        self.status_bar.showMessage("Checking save folders…")
        self._start_status_sweep()

    def _show_startup_summary(self):
        needing = [g.name for g in self.games.values() if self.status_cache.get(g.id) in ("changed", "never")]
        if needing:
            self.status_bar.showMessage(
                f"Welcome back — {len(needing)} game(s) need a backup.", 6000
            )

    def _update_backup_all_button(self):
        # Games needing backup: either changed saves or never backed up
        needing = sum(1 for s in self.status_cache.values() if s in ("changed", "never"))
        self.backup_all_btn.setEnabled(needing > 0)
        self.backup_all_btn.setText(
            f"Backup All ({needing})" if needing > 0 else "Backup All"
        )

    def _update_banner_from_cache(self):
        needing_names = [
            g.name for g in self.games.values()
            if self.status_cache.get(g.id) in ("changed", "never")
        ]
        self._update_banner(needing_names)

    def _update_banner(self, changed_games: list):
        if changed_games:
            names = ", ".join(changed_games[:4])
            extra = f" and {len(changed_games) - 4} more" if len(changed_games) > 4 else ""
            self.banner_label.setText(
                f"Games needing backup: {names}{extra}. Consider backing up now."
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
            self._add_card_for_game(game)
            self._flush_status_update()

    def _on_edit_game(self, game_id: str):
        game = self.games.get(game_id)
        if not game:
            return
        dialog = AddGameDialog(self, existing=game)
        if dialog.exec():
            data = dialog.result_data
            old_name = game.name  # for detecting if card needs repositioning
            game.name = data["name"]
            game.save_path = data["save_path"]
            game.backup_root = data["backup_root"]
            self._persist()
            self._recompute_statuses(game_ids=[game.id])  # path may have changed, re-check just this one
            # Refresh card in place (name change may affect sort order)
            if game.name.lower() != old_name.lower():
                self._remove_card_for_game(game_id)
                self._add_card_for_game(game)
            else:
                card = self.cards.get(game_id)
                if card:
                    card.refresh_meta()
                    card.set_status(self.status_cache.get(game_id, "invalid"))
            self._flush_status_update()

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
            self._remove_card_for_game(game_id)
            self.status_cache.pop(game_id, None)  # no disk access needed at all
            self._persist()
            self._flush_status_update()

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
        # The scanner already filters candidates against the tracked paths, but
        # that check belongs to another module, runs at scan time, and compares
        # whatever spelling the folder had then. This is the point where a Game
        # -- its own id, its own backup folder, its own row in games.json --
        # comes into existence, so it is the last place a duplicate can be
        # stopped and the only one that sees both the candidates and the games
        # they would duplicate. A duplicate is not cosmetic: two games on one
        # folder means two independent backup histories over the same saves,
        # and the second one's backup folder conflicts with the save folder the
        # app otherwise treats as the same directory.
        #
        # Comparison is by resolved identity, not by text, and claimed as each
        # folder is accepted so a batch describing one folder twice (directly
        # and through a publisher folder, say) keeps only the first.
        tracked = {comparable_path_key(g.save_path) for g in self.games.values()}
        new_ids = []
        duplicates = 0
        for candidate in candidates:
            key = comparable_path_key(candidate.save_path)
            if key in tracked:
                duplicates += 1
                continue
            tracked.add(key)
            backup_root = os.path.join(backups_root, sanitize_folder_name(candidate.name))
            game = Game.new(candidate.name, candidate.save_path, backup_root)
            self.games[game.id] = game
            new_ids.append(game.id)

        if not new_ids:
            self.status_bar.showMessage(
                f"Nothing new to add — {duplicates} folder"
                f"{'s' if duplicates != 1 else ''} already tracked.", 5000
            )
            return

        self._persist()
        self._recompute_statuses(game_ids=new_ids)  # only walk the newly added games
        # Add cards for the newly discovered games in sorted order
        new_games = [self.games[gid] for gid in new_ids]
        for game in sorted(new_games, key=lambda g: g.name.lower()):
            self._add_card_for_game(game)
        self._flush_status_update()
        message = (
            f"Added {len(new_ids)} game{'s' if len(new_ids) != 1 else ''} from scan."
        )
        if duplicates:
            message += (
                f" Skipped {duplicates} already-tracked folder"
                f"{'s' if duplicates != 1 else ''}."
            )
        self.status_bar.showMessage(message, 5000)

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

        # A status sweep only reads, so quitting during one costs nothing --
        # unlike a backup, there is no half-written archive to worry about, and
        # it is deliberately kept out of the prompt above. But the thread object
        # cannot be destroyed while it is running, which Qt treats as a fatal
        # error, so it has to be waited out rather than abandoned. The walk is
        # bounded and off the critical path, so this is a formality.
        if self.status_worker is not None and self.status_worker.isRunning():
            self.status_worker.wait(5000)

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
        # Back up games that have new changes OR have never been backed up
        needing = [
            game_id for game_id, status in self.status_cache.items()
            if status in ("changed", "never") and game_id not in self.workers
        ]
        if not needing:
            self.status_bar.showMessage("No saves need backing up right now.", 4000)
            return

        self.backup_all_btn.setEnabled(False)
        for game_id in needing:
            if game_id not in self._backup_queue:
                self._backup_queue.append(game_id)
        self.status_bar.showMessage(f"Backing up {len(needing)} game(s)…", 4000)
        self._pump_backup_queue()

    def _pump_backup_queue(self):
        """Start queued backups until the concurrency ceiling is reached.

        This is what keeps a library-wide "Backup All" from becoming one thread
        per game. Starting is spread over separate event loop iterations --
        deliberately, since a backup that completes instantly would otherwise
        re-enter the loop that started it -- but that is reentrancy safety, not
        a concurrency limit: every thread stayed live for the whole walk-and-zip,
        so they all ran at once.
        """
        while self._backup_queue and len(self.workers) < MAX_CONCURRENT_BACKUPS:
            self._start_backup(self._backup_queue.pop(0))
        if not self._backup_queue:
            # Everything has been handed to a worker, so nothing is waiting.
            # Games already in flight are filtered out of the next "Backup All"
            # anyway, so this does not need to wait for them to finish.
            self.backup_all_btn.setEnabled(True)
            self._update_backup_all_button()

    def _on_backup_requested(self, game_id: str):
        """Queue a backup, starting one now if the budget has room.

        Shared by "Backup All" and each card's own button, so the ceiling holds
        however the backup was asked for rather than only on the batch path.
        """
        if game_id in self.workers:
            return  # already backing up
        if game_id in self._backup_queue:
            return  # already waiting for a slot
        self._backup_queue.append(game_id)
        self._pump_backup_queue()

    def _start_backup(self, game_id: str):
        game = self.games.get(game_id)
        if not game:
            return
        if not os.path.isdir(game.save_path):
            QMessageBox.warning(self, "Save folder missing",
                                 f"The save folder for “{game.name}” could not be found:\n{game.save_path}")
            return

        # A game scrolled out of view has no card right now. That is not a
        # reason to refuse the backup: "Backup All" drives every game in the
        # library through here, and above the virtualization threshold most of
        # them have no card at any given moment. The card is only for showing
        # progress, and _on_backup_finished puts it back when it returns.
        card = self.cards.get(game_id)
        if card:
            card.set_backing_up(True)
        worker = BackupWorker(
            game.id, game.name, game.save_path, game.backup_root,
            compression_level=clamp_compression_level(self.settings.get("compression_level")),
            max_backups=clamp_max_backups(self.settings.get("max_backups")),
        )
        worker.progress.connect(self._on_backup_progress)
        worker.backup_finished.connect(self._on_backup_finished)
        worker.error.connect(self._on_backup_error)
        worker.finished.connect(lambda gid=game_id: self._on_backup_worker_finished(gid))
        self.workers[game_id] = worker
        worker.start()

    def _on_backup_worker_finished(self, game_id: str):
        """A slot has freed up: drop the worker and start whoever is waiting.

        QThread's own built-in `finished` signal only fires once the OS thread
        has actually terminated — that's the safe point to drop our last
        reference, not the moment our custom `backup_finished`/`error` signal
        arrives (which is emitted just before run() returns).
        """
        self.workers.pop(game_id, None)
        self._pump_backup_queue()

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
        # Batch UI updates during rapid backup completions to avoid redundant
        # refreshes when multiple backups finish close together
        self._schedule_status_update()

    def _on_backup_error(self, game_id: str, message: str):
        card = self.cards.get(game_id)
        if card:
            card.set_backing_up(False)
        QMessageBox.critical(self, "Backup failed", message)
        # Also update UI in case other backups were waiting to refresh
        self._schedule_status_update()

    def _schedule_status_update(self):
        """Debounce status UI updates (.update_banner_from_cache, refresh
        status bar, update backup button). When multiple backups complete in
        quick succession (Backup All with several games), refreshing the UI
        each time is redundant — this batches them into a single refresh by
        coalescing multiple calls within the same event loop iteration."""
        if not self._status_update_pending:
            self._status_update_pending = True
            QTimer.singleShot(0, self._flush_status_update)

    def _flush_status_update(self):
        """Perform the actual batched UI refresh. Called once via QTimer after
        all pending backup completions have been processed."""
        self._status_update_pending = False
        self._update_banner_from_cache()
        self._refresh_status_bar()
        self._update_backup_all_button()
