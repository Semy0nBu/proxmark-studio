"""Shared desktop design tokens. Presentation only; no device access."""
from pathlib import Path
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

COLORS = dict(background='#202020', surface='#282828', inset='#1b1b1b',
              text='#f0f0f0', muted='#b3b3b3', border='#404040', accent='#79b8ff')
SPACING = (4, 8, 12, 16, 24)


def ui_font():
    families = set(QFontDatabase.families())
    family = next((f for f in ('Segoe UI Variable', 'Segoe UI', 'Noto Sans') if f in families), 'Sans Serif')
    return QFont(family, 10)


def mono_font():
    families = set(QFontDatabase.families())
    return QFont(next((f for f in ('Cascadia Mono', 'Consolas', 'DejaVu Sans Mono') if f in families), 'Monospace'), 10)


def stylesheet(accent=None):
    accent = accent or COLORS['accent']
    return """
QWidget { background: #202020; color: #f0f0f0; font-size: 10pt; }
QLabel { background: transparent; }
QLabel#title { font-size: 20pt; font-weight: 600; }
QLabel#section { font-size: 12pt; font-weight: 600; }
QLabel#appname { font-size: 11pt; font-weight: 600; }
QLabel#muted, QLabel#battery, QLabel#status { color: #b3b3b3; }
QLabel#metric { font-size: 16pt; font-weight: 600; }
QLabel[tone="error"] { color: #ffb4b4; }
QLabel[tone="success"] { color: #a6d9b0; }
QWidget#sidebar { background: #1c1c1c; border-right: 1px solid #333333; }
QFrame#panel { background: #282828; border: 1px solid #393939; border-radius: 6px; }
QFrame#panel QLabel, QFrame#panel QCheckBox { background: transparent; }
QFrame#classicPane { background: #252a32; border: 1px solid #3d4755; border-radius: 8px; }
QFrame#classicPane QLabel, QFrame#classicPane QCheckBox { background: transparent; }
QLabel#classicBadge { color: #9dc9fc; font-size: 9pt; font-weight: 600; }
QPushButton[classicAction="attack"] { background: #30384a; border-color: #516482; color: #c8dcff; padding: 6px 12px; }
QPushButton[classicAction="attack"]:hover { background: #3b4d69; border-color: #88acdc; }
QPushButton[classicAction="attack"]:checked { background: #355578; border-color: #92c7ff; color: #f0f7ff; }
QPushButton[classicAction="attack"]:disabled { background: #292d34; border-color: #3b414b; color: #818b99; }
QTableWidget#classicMemory, QTableWidget#classicKeys { background: #1c222b; alternate-background-color: #232b36; border-color: #3d4755; }
QTableWidget#classicMemory::item, QTableWidget#classicKeys::item { padding: 2px 6px; }
QTableWidget#classicMemory QHeaderView::section, QTableWidget#classicKeys QHeaderView::section { background: #303a48; color: #c1d0e3; padding: 6px; }
QTabWidget#classicTabs::pane { border: 1px solid #3b444f; border-radius: 6px; background: #20252c; }
QTabWidget#classicTabs QTabBar::tab { background: #292f39; color: #bac4d1; padding: 8px 14px; border: 0; border-bottom: 2px solid transparent; }
QTabWidget#classicTabs QTabBar::tab:selected { background: #333f50; color: #e9f4ff; border-bottom-color: ACCENT; }
QTabWidget#classicTabs QTabBar::tab:hover { background: #364353; }
QPushButton, QToolButton { background: #333333; border: 1px solid #484848; border-radius: 4px; padding: 6px 12px; }
QPushButton:hover, QToolButton:hover { background: #3b3b3b; border-color: #626262; }
QPushButton:pressed, QToolButton:pressed { background: #292929; }
QPushButton:focus, QToolButton:focus { border: 1px solid ACCENT; }
QPushButton:disabled, QToolButton:disabled { color: #888888; background: #292929; border-color: #383838; }
QPushButton#primary { background: ACCENT; border-color: ACCENT; color: #111111; font-weight: 600; }
QPushButton#primary:hover { background: #a0ccff; }
QPushButton#primary:pressed { background: #65a4e6; }
QPushButton#primary:disabled { background: #3a3a3a; color: #8a8a8a; border-color: #444444; }
QPushButton#nav { background: transparent; border: 1px solid transparent; text-align: left; padding: 9px 10px; }
QPushButton#nav:hover { background: #2b2b2b; }
QPushButton#nav:checked { background: #333333; border-left: 3px solid ACCENT; padding-left: 8px; }
QPushButton#nav:focus { border: 1px solid ACCENT; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #1b1b1b; border: 1px solid #484848; border-bottom-color: #777777; border-radius: 4px; padding: 5px 8px; selection-background-color: #315b83; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-bottom: 2px solid ACCENT; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #888888; border-color: #383838; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox::down-arrow { image: url(CHEVRON); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: #303030; selection-background-color: #3b5065; padding: 4px; }
QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #999999; border-radius: 3px; background: #222222; }
QCheckBox::indicator:checked { background: ACCENT; border-color: ACCENT; image: url(CHECKMARK); }
QCheckBox::indicator:disabled { background: #444444; border-color: #555555; }
QListWidget, QTableWidget { background: #232323; alternate-background-color: #272727; border: 1px solid #393939; border-radius: 4px; selection-background-color: #35485a; selection-color: #ffffff; gridline-color: #333333; }
QListWidget::item { border-bottom: 1px solid #343434; padding: 8px; }
QListWidget::item:hover { background: #2f2f2f; }
QListWidget::item:selected { background: #35485a; }
QTableWidget::item { padding: 5px 8px; border: 0; }
QTableWidget::item:focus { border: 1px solid ACCENT; }
QHeaderView::section { background: #2d2d2d; color: #bfbfbf; padding: 8px; border: 0; border-bottom: 1px solid #484848; font-weight: 600; }
QHeaderView::section:hover { background: #373737; }
QTableCornerButton::section { background: #2d2d2d; border: 0; }
QSplitter::handle { background: #202020; width: 7px; height: 7px; }
QSplitter::handle:hover { background: #555555; }
QScrollArea { border: 0; background: transparent; }
QToolButton#disclosure { color: #bdbdbd; background: transparent; border: 0; padding: 5px 0; text-align: left; }
QToolButton#disclosure:focus { border: 1px solid ACCENT; }
QProgressBar { background: #3a3a3a; border: 0; max-height: 3px; min-height: 3px; }
QProgressBar::chunk { background: ACCENT; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #666666; min-height: 30px; border-radius: 4px; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 2px; }
QScrollBar::handle:horizontal { background: #666666; min-width: 30px; border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QStatusBar { background: #1c1c1c; border-top: 1px solid #383838; font-size: 9pt; }
QStatusBar::item { border: 0; }
QDockWidget { color: #bfbfbf; }
QMenu { background: #2c2c2c; border: 1px solid #555555; padding: 4px; }
QMenu::item { padding: 7px 24px; border-radius: 3px; }
QMenu::item:selected { background: #414141; }
QToolTip { background: #333333; color: #f0f0f0; border: 1px solid #606060; padding: 6px; }
""".replace('ACCENT', accent).replace('CHECKMARK', Path(__file__).with_name('check.svg').as_posix()).replace('CHEVRON', Path(__file__).with_name('chevron.svg').as_posix())


STYLE = stylesheet()


def apply_theme(application):
    # Read the platform palette before switching to Fusion; all controls then use
    # the same accent. A light variant retains contrast on dark backgrounds.
    accent = application.palette().color(QPalette.ColorRole.Accent)
    if not accent.isValid() or accent.saturation() < 50:
        accent = QColor(COLORS['accent'])
    if accent.lightness() < 155:
        accent = accent.lighter(160)
    COLORS['accent'] = accent.name()
    application.setStyle('Fusion')
    palette = application.palette()
    for role, color in [(QPalette.ColorRole.Window, '#202020'), (QPalette.ColorRole.WindowText, '#f0f0f0'),
                        (QPalette.ColorRole.Base, '#1b1b1b'), (QPalette.ColorRole.AlternateBase, '#272727'),
                        (QPalette.ColorRole.Text, '#f0f0f0'), (QPalette.ColorRole.Button, '#333333'),
                        (QPalette.ColorRole.ButtonText, '#f0f0f0'), (QPalette.ColorRole.Highlight, '#35485a'),
                        (QPalette.ColorRole.HighlightedText, '#ffffff'), (QPalette.ColorRole.Accent, accent.name())]:
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#888888'))
    application.setPalette(palette)
    application.setFont(ui_font())
    application.setStyleSheet(stylesheet(accent.name()))
