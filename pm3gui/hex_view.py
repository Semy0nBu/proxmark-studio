"""Native HEX editor with a painted offset gutter, outside the text document.

Only a validated, complete text cache supplies offsets. A pending or malformed
document never displays guessed byte addresses. Rebuilding is debounced and
split across event-loop turns; painting visits visible QTextBlocks only.
"""
from __future__ import annotations

from array import array
import re
import time

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from .studio_data import say
from .theme import mono_font

_VALID_LINE = re.compile(r'[0-9a-fA-F \t\r\n\v\f]*\Z')
_REMOVE_WHITESPACE = {ord(char): None for char in ' \t\r\n\v\f'}


class _OffsetGutter(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setObjectName('hexOffsetGutter')
        self.setAccessibleName(say('Смещения байтов', 'Byte offsets'))

    def sizeHint(self):
        return QSize(self.editor.offset_gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_offsets(event)


class WorkbenchHexEdit(QPlainTextEdit):
    """QPlainTextEdit semantics, with byte offsets drawn in a viewport margin."""

    offsets_ready = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('workbenchHex')
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._bytes_per_block = 16
        self._offsets = array('q')
        self._pending_offsets = array('q')
        self._offset_status = 'pending'
        self._total_bytes = 0
        self._nibbles = 0
        self._scan_block = None
        self._generation = 0
        self._scan_generation = -1
        self._bound_document = None
        self._offset_timer = QTimer(self)
        self._offset_timer.setSingleShot(True)
        self._offset_timer.timeout.connect(self._scan_offsets)
        self.gutter = _OffsetGutter(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self.gutter.update)
        self._bind_document()
        self.set_bytes_per_block(16)
        self._schedule_offsets()

    @property
    def bytes_per_block(self):
        return self._bytes_per_block

    def set_bytes_per_block(self, value):
        if type(value) is not int or not 1 <= value <= 65536:
            raise ValueError('Block size must be an integer from 1 to 65536.')
        self._bytes_per_block = value
        self.gutter.setToolTip(say('Смещение байта · блок {0} Б', 'Byte offset · {0}-byte block').format(value))
        self._update_gutter_width()

    @property
    def offset_status(self):
        """'pending', 'valid' or 'invalid'; independent of card authenticity."""
        return self._offset_status

    def byte_offset_for_block(self, number):
        """Offset of a physical text line, or None without a complete boundary."""
        if (self._offset_status != 'valid' or type(number) is not int
                or not 0 <= number < len(self._offsets)):
            return None
        value = self._offsets[number]
        return None if value < 0 else value

    def offset_label(self, number):
        value = self.byte_offset_for_block(number)
        return '—' if value is None else f'{value:0{self._offset_digits()}X}'

    def _bind_document(self):
        self._bound_document = self.document()
        self._bound_document.contentsChange.connect(self._document_changed)

    def setDocument(self, document):
        old = getattr(self, '_bound_document', None)
        if old is not None:
            try:old.contentsChange.disconnect(self._document_changed)
            except (RuntimeError, TypeError):pass
        super().setDocument(document)
        if hasattr(self, '_offset_timer'):
            self._bind_document()
            self._schedule_offsets()

    def _document_changed(self, position, removed, added):
        if removed or added:
            self._schedule_offsets()

    def _schedule_offsets(self):
        self._generation += 1
        self._offset_status = 'pending'
        self._scan_generation = -1
        self._scan_block = None
        self._pending_offsets = array('q')
        self._nibbles = 0
        self._offset_timer.start(20)
        self._update_gutter_width()
        self.gutter.update()

    def _scan_offsets(self):
        if self._scan_generation != self._generation:
            self._scan_generation = self._generation
            self._scan_block = self.document().firstBlock()
        generation = self._scan_generation
        deadline = time.monotonic() + .008
        visited = 0
        while self._scan_block.isValid():
            value = self._scan_block.text()
            if _VALID_LINE.fullmatch(value) is None:
                self._finish_offsets(False)
                return
            digits = len(value.translate(_REMOVE_WHITESPACE))
            self._pending_offsets.append(self._nibbles // 2 if digits and not self._nibbles % 2 else -1)
            self._nibbles += digits
            self._scan_block = self._scan_block.next()
            visited += 1
            if visited >= 4096 or time.monotonic() >= deadline:
                # Changes can invalidate this generation before the next turn.
                if self._scan_block.isValid():
                    self._offset_timer.start(0)
                    return
        if generation == self._generation:
            self._finish_offsets(not self._nibbles % 2)

    def _finish_offsets(self, valid):
        self._offset_status = 'valid' if valid else 'invalid'
        self._offsets = self._pending_offsets if valid else array('q')
        self._pending_offsets = array('q')
        self._total_bytes = self._nibbles // 2 if valid else 0
        self._scan_block = None
        self._update_gutter_width()
        self.gutter.update()
        self.offsets_ready.emit(valid)

    def _offset_digits(self):
        largest = self._total_bytes if self._offset_status == 'valid' else self.document().characterCount() // 2
        return max(4, len(f'{largest:X}'))

    def offset_gutter_width(self):
        return 18 + self.fontMetrics().horizontalAdvance('0') * self._offset_digits()

    def _update_gutter_width(self, *_):
        if not hasattr(self, 'gutter'):
            return
        self.setViewportMargins(self.offset_gutter_width(), 0, 0, 0)
        rect = self.contentsRect()
        self.gutter.setGeometry(QRect(rect.left(), rect.top(), self.offset_gutter_width(), rect.height()))

    def _update_gutter(self, rect, dy):
        if dy:self.gutter.scroll(0, dy)
        else:self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_gutter_width()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange, QEvent.Type.PaletteChange}:
            self._update_gutter_width()
            if hasattr(self, 'gutter'):self.gutter.update()

    def paint_offsets(self, event):
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.Base))
        painter.setFont(self.font())
        painter.setPen(QColor('#353a44'))
        painter.drawLine(self.gutter.width()-1, event.rect().top(), self.gutter.width()-1, event.rect().bottom())
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        current = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            height = round(self.blockBoundingRect(block).height())
            if block.isVisible() and top + height >= event.rect().top():
                painter.setPen(QColor('#9abcf8') if block.blockNumber() == current else QColor('#818e9f'))
                painter.drawText(0, top, self.gutter.width()-9, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, self.offset_label(block.blockNumber()))
            top += height
            block = block.next()
