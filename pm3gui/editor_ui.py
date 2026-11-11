"""A larger view of the same working HEX document, never a second data copy."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout
from .i18n import tr
from .theme import mono_font


class HexEditorDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle(tr('HEX · редактируемая копия'))
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1100, 760)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit()
        self.editor.setFont(mono_font())
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setDocument(owner.hex_edit.document())
        self.editor.setReadOnly(owner.hex_edit.isReadOnly())
        self.editor.setTextCursor(owner.hex_edit.textCursor())
        self.owner = owner
        owner.hex_edit.installEventFilter(self)
        self.sync_state()
        layout.addWidget(self.editor)

    def sync_state(self):
        original = self.owner.hex_edit
        self.editor.setReadOnly(original.isReadOnly())
        self.editor.setEnabled(original.isEnabled())
        self.editor.setExtraSelections(original.extraSelections())
        self.editor.setToolTip(original.toolTip())

    def eventFilter(self, watched, event):
        if watched is self.owner.hex_edit and event.type() in {
                QEvent.Type.ReadOnlyChange, QEvent.Type.EnabledChange, QEvent.Type.ToolTipChange}:
            self.sync_state()
        return super().eventFilter(watched, event)


def sync_expanded_hex(owner):
    dialog = getattr(owner, 'expanded_hex_editor', None)
    if dialog is not None:dialog.sync_state()


def open_hex_editor(owner):
    previous = getattr(owner, 'expanded_hex_editor', None)
    if previous is not None and previous.isVisible():
        previous.raise_(); previous.activateWindow(); return previous
    if previous is not None:
        previous.deleteLater()
    dialog = owner.expanded_hex_editor = HexEditorDialog(owner)
    dialog.show()
    return dialog
