"""Application wide Qt stylesheet (dark 'scientific instrument' theme).

Keeping the stylesheet in one module makes it trivial to add a light theme
later: expose a second constant and let :func:`app.gui.main_window` pick it
from :mod:`app.utils.settings`.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
COLOR_BG = "#14181d"
COLOR_SURFACE = "#1c2229"
COLOR_SURFACE_ALT = "#232b34"
COLOR_BORDER = "#2f3944"
COLOR_TEXT = "#e6edf3"
COLOR_TEXT_MUTED = "#93a1b1"
COLOR_ACCENT = "#3d9bfd"
COLOR_ACCENT_DARK = "#2a7fd4"
COLOR_SUCCESS = "#3fb950"
COLOR_WARNING = "#d29922"
COLOR_DANGER = "#f85149"

RADIUS = "6px"


def dark_palette():
    """Return a dark :class:`QPalette`.

    The palette - not a blanket ``QWidget { background: ... }`` rule - is what
    gives every *unstyled* surface (menus, message boxes, scroll-area
    viewports, plain container widgets) a dark background.  Painting the
    background through the stylesheet instead would make child containers
    opaque and produce visible dark rectangles inside the panels.
    """
    from PySide6.QtGui import QColor, QPalette

    palette = QPalette()
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    palette.setColor(role.Window, QColor(COLOR_BG))
    palette.setColor(role.WindowText, QColor(COLOR_TEXT))
    palette.setColor(role.Base, QColor(COLOR_SURFACE_ALT))
    palette.setColor(role.AlternateBase, QColor(COLOR_SURFACE))
    palette.setColor(role.Text, QColor(COLOR_TEXT))
    palette.setColor(role.Button, QColor(COLOR_SURFACE_ALT))
    palette.setColor(role.ButtonText, QColor(COLOR_TEXT))
    palette.setColor(role.BrightText, QColor("#ffffff"))
    palette.setColor(role.Highlight, QColor(COLOR_ACCENT_DARK))
    palette.setColor(role.HighlightedText, QColor("#ffffff"))
    palette.setColor(role.ToolTipBase, QColor(COLOR_SURFACE_ALT))
    palette.setColor(role.ToolTipText, QColor(COLOR_TEXT))
    palette.setColor(role.PlaceholderText, QColor(COLOR_TEXT_MUTED))
    palette.setColor(role.Link, QColor(COLOR_ACCENT))

    for disabled_role in (role.Text, role.ButtonText, role.WindowText):
        palette.setColor(group.Disabled, disabled_role, QColor("#5b6672"))
    return palette


def apply_theme(app) -> None:
    """Apply the dark palette and stylesheet to ``app`` in one call."""
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(DARK_STYLESHEET)


DARK_STYLESHEET = f"""
/* ---------- base ----------
   Only text/font here: backgrounds come from the palette (see
   :func:`dark_palette`) so that plain container widgets stay transparent. */
QWidget {{
    color: {COLOR_TEXT};
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
    /* Transparent keeps plain container widgets from painting the palette
       window colour over the lighter panel background. Widgets that need a
       surface (group boxes, tables, buttons ...) declare one below. */
    background-color: transparent;
}}
QMainWindow, QDialog {{ background-color: {COLOR_BG}; }}
QMenuBar {{ background-color: {COLOR_SURFACE}; }}
QMenuBar::item:selected {{ background-color: {COLOR_ACCENT_DARK}; }}
QMenu {{ background-color: {COLOR_SURFACE_ALT}; border: 1px solid {COLOR_BORDER}; }}
QMenu::item:selected {{ background-color: {COLOR_ACCENT_DARK}; color: #ffffff; }}
QToolTip {{
    background-color: {COLOR_SURFACE_ALT};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    padding: 3px 6px;
}}

/* ---------- grouping ---------- */
QGroupBox#panel {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS};
    margin-top: 18px;
    padding: 10px 12px 12px 12px;
    font-weight: 600;
}}
QGroupBox#panel::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 2px 8px;
    color: {COLOR_ACCENT};
    background-color: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
}}

QWidget#runbar {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS};
}}

/* Labels and check boxes inside a panel must not paint the palette window
   colour over the panel surface. A descendant selector is needed because
   plain labels otherwise resolve their background from the palette. */
QGroupBox#panel QLabel,
QGroupBox#panel QCheckBox,
QWidget#runbar QLabel {{
    background-color: transparent;
    border: none;
}}

/* ---------- inputs ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background-color: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 5px 7px;
    selection-background-color: {COLOR_ACCENT_DARK};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {COLOR_ACCENT};
}}
QLineEdit[readOnly="true"] {{ color: {COLOR_TEXT_MUTED}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_ACCENT_DARK};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background-color: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 6px 12px;
    color: {COLOR_TEXT};
}}
QPushButton:hover {{ background-color: #2c3641; border-color: {COLOR_ACCENT}; }}
QPushButton:pressed {{ background-color: {COLOR_ACCENT_DARK}; }}
QPushButton:disabled {{
    color: #5b6672;
    background-color: {COLOR_SURFACE};
    border-color: {COLOR_BORDER};
}}
QPushButton#primary {{
    background-color: {COLOR_ACCENT_DARK};
    border: 1px solid {COLOR_ACCENT};
    font-weight: 700;
    letter-spacing: 0.5px;
}}
QPushButton#primary:hover {{ background-color: {COLOR_ACCENT}; }}
QPushButton#run {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2ea043, stop:1 #1f7a33);
    border: 1px solid #3fb950;
    border-radius: {RADIUS};
    color: #ffffff;
    font-size: 15px;
    font-weight: 700;
    padding: 12px;
}}
QPushButton#run:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3fb950, stop:1 #2ea043);
}}
QPushButton#run:disabled {{ background-color: #2a3038; color: #6b7681; border-color: {COLOR_BORDER}; }}
QPushButton#danger {{ border: 1px solid {COLOR_DANGER}; color: {COLOR_DANGER}; }}
QPushButton#danger:hover {{ background-color: #47262a; }}

/* ---------- labels ---------- */
QLabel#muted {{ color: {COLOR_TEXT_MUTED}; }}
QLabel#title {{
    font-size: 22px;
    font-weight: 700;
    color: {COLOR_TEXT};
    letter-spacing: 0.5px;
}}
QLabel#subtitle {{ color: {COLOR_TEXT_MUTED}; font-size: 12px; }}
QLabel#metric {{
    font-size: 26px;
    font-weight: 700;
    color: {COLOR_SUCCESS};
}}
QLabel#sectionHint {{ color: {COLOR_TEXT_MUTED}; font-size: 11px; }}

/* ---------- tables / lists ---------- */
QTableWidget, QTreeWidget, QListWidget {{
    background-color: {COLOR_SURFACE_ALT};
    alternate-background-color: #202831;
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    gridline-color: {COLOR_BORDER};
}}
QHeaderView::section {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT_MUTED};
    border: none;
    border-right: 1px solid {COLOR_BORDER};
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 5px 8px;
    font-weight: 600;
}}
QTableWidget::item:selected {{ background-color: {COLOR_ACCENT_DARK}; color: #ffffff; }}

/* ---------- tabs (Result panel: Poses / Interactions) ---------- */
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    background-color: {COLOR_SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT_MUTED};
    border: 1px solid {COLOR_BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 5px 12px;
    margin-right: 2px;
}}
QTabBar::tab:hover {{ color: {COLOR_TEXT}; }}
QTabBar::tab:selected {{
    background-color: {COLOR_SURFACE_ALT};
    color: {COLOR_TEXT};
    border-color: {COLOR_ACCENT};
}}

/* ---------- other ---------- */
QProgressBar {{
    background-color: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    text-align: center;
    height: 20px;
}}
QProgressBar::chunk {{ background-color: {COLOR_ACCENT}; border-radius: 3px; }}
QProgressBar#busy::chunk {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {COLOR_ACCENT}, stop:0.5 #7ee0ff, stop:1 {COLOR_ACCENT});
}}
QScrollBar:vertical {{
    background: {COLOR_SURFACE};
    width: 11px; margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: #39424e; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: #4a5563; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar:horizontal {{ background: {COLOR_SURFACE}; height: 11px; }}
QScrollBar::handle:horizontal {{ background: #39424e; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
QSplitter::handle {{ background-color: {COLOR_BORDER}; }}
QCheckBox::indicator {{
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid {COLOR_BORDER}; background-color: {COLOR_SURFACE_ALT};
}}
QCheckBox::indicator:checked {{ background-color: {COLOR_ACCENT}; border-color: {COLOR_ACCENT}; }}
QStatusBar {{ background-color: {COLOR_SURFACE}; color: {COLOR_TEXT_MUTED}; }}
"""
