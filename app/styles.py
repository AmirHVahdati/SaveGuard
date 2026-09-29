"""Central place for the app's color palette and QSS stylesheet."""

# Palette
BG = "#0e1016"
BG_ELEV = "#151822"
CARD = "#1a1e2a"
CARD_HOVER = "#1f2432"
BORDER = "#262b3a"
TEXT = "#e7e9f0"
TEXT_MUTED = "#8b90a3"
ACCENT = "#7c5cff"
ACCENT_2 = "#5cc9ff"
GREEN = "#33d17a"
AMBER = "#ffb454"
RED = "#ff6b6b"
BLUE = "#5cc9ff"
PINK = "#ff6b9d"

STYLE_SHEET = f"""
QWidget {{
    background-color: transparent;
    color: {TEXT};
    font-family: "Segoe UI", "Inter", "Helvetica Neue", "Noto Sans", "DejaVu Sans", Arial, sans-serif;
    font-size: 13px;
}}

QMainWindow {{
    background-color: {BG};
}}

#Central {{
    background-color: {BG};
}}

/* ---------- Header ---------- */
#Header {{
    background-color: {BG_ELEV};
    border-bottom: 1px solid {BORDER};
}}

#AppTitle {{
    font-size: 19px;
    font-weight: 700;
    color: {TEXT};
}}

#AppSubtitle {{
    font-size: 12px;
    color: {TEXT_MUTED};
}}

/* ---------- Buttons ---------- */
QPushButton {{
    background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover {{
    background-color: {CARD_HOVER};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: #14172080;
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    border-color: {BORDER};
}}

QPushButton#PrimaryButton {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
    color: white;
    font-weight: 600;
    padding: 9px 18px;
    border-radius: 9px;
}}
QPushButton#PrimaryButton:hover {{
    background-color: #8f74ff;
}}
QPushButton#PrimaryButton:pressed {{
    background-color: #6a4be0;
}}

QPushButton#DangerButton {{
    color: {RED};
    border: 1px solid #3a1f26;
}}
QPushButton#DangerButton:hover {{
    background-color: #2a161c;
    border-color: {RED};
}}

QPushButton#GhostButton {{
    background-color: transparent;
    border: none;
    color: {TEXT_MUTED};
    padding: 4px 8px;
}}
QPushButton#GhostButton:hover {{
    color: {TEXT};
}}

QPushButton#SupportButton {{
    background-color: transparent;
    border: 1px solid #3a1f2e;
    border-radius: 8px;
    color: {PINK};
    padding: 7px 14px;
}}
QPushButton#SupportButton:hover {{
    background-color: #2a1620;
    border-color: {PINK};
}}
QPushButton#SupportButton:pressed {{
    background-color: #24141c;
}}

/* ---------- Cards ---------- */
#GameCard {{
    background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
#GameCard:hover {{
    border-color: #3a3f52;
}}

#GameName {{
    font-size: 15px;
    font-weight: 700;
    color: {TEXT};
}}

#GamePath {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}

#MetaLabel {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}

/* ---------- Status badges ---------- */
QLabel[status="synced"] {{
    background-color: #16311f;
    color: {GREEN};
    border-radius: 9px;
    padding: 4px 10px;
    font-weight: 600;
    font-size: 11px;
}}
QLabel[status="changed"] {{
    background-color: #3a2a11;
    color: {AMBER};
    border-radius: 9px;
    padding: 4px 10px;
    font-weight: 600;
    font-size: 11px;
}}
QLabel[status="never"] {{
    background-color: #16283a;
    color: {BLUE};
    border-radius: 9px;
    padding: 4px 10px;
    font-weight: 600;
    font-size: 11px;
}}
QLabel[status="invalid"] {{
    background-color: #3a1620;
    color: {RED};
    border-radius: 9px;
    padding: 4px 10px;
    font-weight: 600;
    font-size: 11px;
}}

/* ---------- Banner ---------- */
#Banner {{
    background-color: #3a2a11;
    border: 1px solid #5a4420;
    border-radius: 10px;
    color: {AMBER};
}}

/* ---------- Inputs ---------- */
QLineEdit {{
    background-color: {BG_ELEV};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}

QCheckBox {{
    spacing: 8px;
    color: {TEXT};
    background-color: transparent;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid {BORDER};
    background-color: {BG_ELEV};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxNiAxNiI+PHBhdGggZD0iTTMgOC41TDYuNSAxMkwxMyA0IiBmaWxsPSJub25lIiBzdHJva2U9IiNmZmZmZmYiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+PC9zdmc+);
}}
QCheckBox::indicator:checked:hover {{
    background-color: #8f74ff;
}}

/* ---------- Scroll area ---------- */
QScrollArea {{
    border: none;
    background-color: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* ---------- Progress bar ---------- */
QProgressBar {{
    background-color: {BG_ELEV};
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}

/* ---------- Dialog ---------- */
QDialog {{
    background-color: {BG_ELEV};
}}
QLabel#DialogTitle {{
    font-size: 16px;
    font-weight: 700;
}}
QLabel#FieldLabel {{
    color: {TEXT_MUTED};
    font-size: 12px;
    font-weight: 600;
}}

/* ---------- Empty state ---------- */
#EmptyState {{
    color: {TEXT_MUTED};
}}

/* ---------- Status bar ---------- */
QStatusBar {{
    background-color: {BG_ELEV};
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}

QToolTip {{
    background-color: {BG_ELEV};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 8px;
    border-radius: 6px;
}}
"""
