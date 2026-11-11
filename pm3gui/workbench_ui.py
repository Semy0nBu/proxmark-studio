"""Offline inspector for the existing HEX working document.

Selection never changes bytes, a revision or authentication evidence. Recorded
key fields stay masked; access rights are decoded only from known ACL bytes.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QScrollArea,
                              QVBoxLayout, QWidget)

from .card_snapshot import adapt_dump
from .core import _checked
from .operations import CLASSIC_GEOMETRIES, sector_first_block, sector_trailer
from .semantic_diff import Protocol, Representation
from .studio_data import access_report, say
from .theme import mono_font
from .ui import SoftButton
from .writing import access_conditions, block_sector


STATE_COLOURS = {
    'complete': ('#303d52', '#c4d8f6'),
    'partial': ('#493b24', '#edc37f'),
    'missing': ('#2a2e36', '#a3adbd'),
    'invalid': ('#4a2b34', '#edabb9'),
}


@dataclass(frozen=True)
class InspectorSector:
    number: int
    first: int
    last: int
    known_bytes: int
    total_bytes: int
    key_a_known: bool
    key_b_known: bool
    acl_known: bool
    conditions: tuple[int, ...] | None

    @property
    def state(self):
        if not self.known_bytes:return 'missing'
        if self.acl_known and self.conditions is None:return 'invalid'
        return 'complete' if self.known_bytes == self.total_bytes else 'partial'


def inspect_sector(dump, mask, sector):
    """Explicit geometry is checked by the caller before decoding an address."""
    first, last = sector_first_block(sector), sector_trailer(sector)
    if len(mask) != len(dump.data) or (last + 1) * 16 > len(dump.data):
        raise ValueError('Sector is outside the selected snapshot.')
    base = last * 16
    acl_known = all(mask[base+6:base+9])
    conditions = None
    if acl_known:
        try:conditions = tuple(access_conditions(dump.data[base:base+16]))
        except ValueError:pass
    return InspectorSector(sector, first, last, sum(mask[first*16:(last+1)*16]),
                           (last-first+1)*16, all(mask[base:base+6]),
                           all(mask[base+10:base+16]), acl_known, conditions)


def _label(value='', *, name='muted'):
    result = QLabel(value)
    result.setObjectName(name)
    result.setWordWrap(True)
    result.setTextFormat(Qt.TextFormat.PlainText)
    return result


class WorkbenchInspector(QWidget):
    """Attach once after owner.hex_edit; refresh again after loading a revision.

    textChanged/protocol changes are coalesced; cursor movement reuses the parsed
    document. Call refresh() when metadata changes without a text signal (for
    example MainWindow's blocked-signal _text_changed path).
    """
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setObjectName('workbenchInspector')
        self.setMinimumWidth(180)
        self.setMaximumWidth(290)
        self.sector = 0
        self.selected_byte = 0
        self.sector_count = 0
        self.classic = False
        self.dump = None
        self.mask = ()
        self.tiles = []
        self.sectors = ()
        self._positions = ()
        self._starts = ()
        self._large_text = ''
        self._large_prefixes = ()
        self._document_revision = None
        self._refreshing = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(6)
        self.heading = _label(say('Инспектор', 'Inspector'), name='section')
        layout.addWidget(self.heading)
        self.kind = _label()
        layout.addWidget(self.kind)
        self.byte_label = self._pair(layout, say('Байт', 'Byte'))
        self.block_label = self._pair(layout, say('Блок', 'Block'))
        self.role_label = self._pair(layout, say('Область', 'Area'))

        self.classic_details = QWidget()
        details = QVBoxLayout(self.classic_details)
        details.setContentsMargins(0, 3, 0, 0)
        details.setSpacing(6)
        self.key_a = self._pair(details, 'Key A')
        self.key_b = self._pair(details, 'Key B')
        self.access_bits = self._pair(details, 'ACL')
        self.read_title, self.read_rights = self._pair(details, say('Чтение', 'Read'), with_title=True)
        self.write_title, self.write_rights = self._pair(details, say('Запись', 'Write'), with_title=True)
        self.key_note = _label(say('Поля снимка; свежая аутентификация не проверена.',
                                   'Snapshot fields; fresh authentication is not checked.'))
        self.key_note.setObjectName('inspectorNote')
        details.addWidget(self.key_note)
        layout.addWidget(self.classic_details)

        self.coverage = _label()
        layout.addWidget(self.coverage)
        self.map_scroll = QScrollArea()
        self.map_scroll.setWidgetResizable(True)
        self.map_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.map_scroll.setMinimumHeight(130)
        self.map_scroll.setMaximumHeight(220)
        self.map_widget = QWidget()
        self.grid = QGridLayout(self.map_widget)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(5)
        self.map_scroll.setWidget(self.map_widget)
        layout.addWidget(self.map_scroll)
        self.legend = _label(say('Известно · пропуски · не прочитано · ошибка ACL',
                                 'Known · gaps · unread · invalid ACL'))
        self.legend.setObjectName('inspectorNote')
        layout.addWidget(self.legend)
        self.status = _label()
        layout.addWidget(self.status)
        self.map_button = SoftButton(say('Карта памяти…', 'Memory map…'))
        self.map_button.clicked.connect(self.open_memory_map)
        layout.addWidget(self.map_button)
        layout.addStretch()

        owner.hex_edit.textChanged.connect(self.schedule)
        owner.hex_edit.cursorPositionChanged.connect(self.update_cursor)
        protocol = getattr(owner, 'protocol', None)
        if hasattr(protocol, 'textChanged'):protocol.textChanged.connect(self.schedule)
        self.refresh()

    @staticmethod
    def _pair(layout, title, *, with_title=False):
        row = QHBoxLayout()
        row.setSpacing(8)
        name, value = _label(title), _label('—', name='inspectorValue')
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(name)
        row.addWidget(value, 1)
        layout.addLayout(row)
        return (name, value) if with_title else value

    def schedule(self, *_):
        if not self.timer.isActive():self.timer.start(0)

    def _clear(self, message):
        self.dump = None
        self.mask = ()
        self.classic = False
        self.sector_count = 0
        self.sectors = ()
        self._positions = self._starts = ()
        self._large_text = ''
        self._large_prefixes = ()
        self.heading.setText(say('Инспектор', 'Inspector'))
        self.kind.setText(message)
        for item in (self.byte_label, self.block_label, self.role_label, self.key_a, self.key_b,
                     self.access_bits, self.read_rights, self.write_rights):
            item.setText('—');item.setToolTip('')
        self.kind.setToolTip('')
        self.coverage.clear()
        self.status.clear()
        self._set_tiles(0)
        self.classic_details.hide()
        self.map_scroll.hide()
        self.legend.hide()
        self.map_button.setEnabled(False)

    def refresh(self):
        self.timer.stop()
        if self._refreshing:return
        self._refreshing = True
        try:
            if getattr(self.owner, 'loaded_dump', None) is None:
                self._clear(say('Выберите снимок карты.', 'Choose a card snapshot.'))
                return
            try:
                dump = _checked(self.owner._working_dump())
                protocol_field = getattr(self.owner, 'protocol', None)
                protocol = protocol_field.text() if hasattr(protocol_field, 'text') else ''
                # Structured records are not a byte-addressed Classic memory.
                if 'structured_document' in dump.metadata:
                    self._clear(say('Структурированная запись · профильные инструменты.',
                                    'Structured record · use family tools.'))
                    self.kind.setToolTip(say('Просмотр структуры не создаёт карту секторов Classic.',
                                             'Structured inspection does not establish a Classic memory layout.'))
                    return
                # Large generic buffers need no per-byte Python set or regex
                # match list. Keep only character-count checkpoints for cursor
                # selection, without claiming known-byte coverage.
                snapshot = (adapt_dump(dump, source_ref='workbench:working', protocol=protocol)
                            if len(dump.data) <= 4096 else None)
                is_classic = bool(snapshot is not None and snapshot.snapshot.model.protocol is Protocol.MIFARE_CLASSIC
                                  and snapshot.snapshot.representation is Representation.FLAT_MEMORY)
                geometry = next((size for size, (_, length) in CLASSIC_GEOMETRIES.items()
                                 if length == len(dump.data)), None)
                if is_classic and (dump.block_size != 16 or geometry is None
                        or snapshot.snapshot.model.geometry is not None
                        and snapshot.snapshot.model.geometry.size_bytes != len(dump.data)):
                    raise ValueError('Classic extent mismatch')
                text = self.owner.hex_edit.toPlainText()
                digits = ([match for match in re.finditer(r'[0-9A-Fa-f]', text)]
                          if len(dump.data) <= 4096 else None)
                if digits is not None and len(digits) != len(dump.data)*2:
                    raise ValueError('Document byte positions do not match')
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, LookupError):
                self._clear(say('Снимок или HEX некорректен. Исправьте редактор.',
                                'Invalid snapshot or HEX. Correct the editor.'))
                return

            self.dump = dump
            self.classic = is_classic
            self.mask = (tuple(i in snapshot.snapshot.known_bytes for i in range(len(dump.data)))
                         if snapshot is not None else ())
            self._positions = (tuple((digits[i].start(), digits[i+1].end()) for i in range(0, len(digits), 2))
                               if digits is not None else ())
            self._starts = tuple(pair[0] for pair in self._positions)
            self._large_text, self._large_prefixes = '', ()
            if digits is None:
                prefixes, total = [], 0
                for start in range(0, len(text), 4096):
                    prefixes.append(total)
                    total += len(re.sub(r'[ \t\r\n\v\f]', '', text[start:start+4096]))
                prefixes.append(total)
                self._large_text, self._large_prefixes = text, tuple(prefixes)
            self._document_revision = self.owner.hex_edit.document().revision()
            self.classic_details.setVisible(self.classic)
            self.map_scroll.setVisible(self.classic)
            self.legend.setVisible(self.classic)
            self.map_button.setEnabled(self.classic and callable(getattr(self.owner, 'open_memory_map', None)))
            self.kind.setToolTip('')
            if self.classic:
                self.sector_count = CLASSIC_GEOMETRIES[geometry][0]
                self.kind.setText('MIFARE Classic · ' + geometry.upper())
                self.sectors = tuple(inspect_sector(dump, self.mask, i) for i in range(self.sector_count))
                self.coverage.setText(say('Известно {0} / {1} Б', 'Known {0} / {1} B').format(sum(self.mask), len(self.mask)))
                self._set_tiles(self.sector_count)
                for tile, info in zip(self.tiles, self.sectors):
                    tile.setProperty('memoryState', info.state)
                    background, foreground = STATE_COLOURS[info.state]
                    tile.setStyleSheet('QPushButton { background:'+background+'; color:'+foreground+
                        '; border:1px solid transparent; border-radius:4px; padding:3px; } '
                        'QPushButton:checked { border:1px solid '+foreground+'; } '
                        'QPushButton:hover { border:1px solid '+foreground+'; }')
                    tile.setToolTip(say('Сектор {0} · блоки {1}–{2} · известно {3}/{4} Б',
                                       'Sector {0} · blocks {1}–{2} · known {3}/{4} B').format(
                                           info.number, info.first, info.last, info.known_bytes, info.total_bytes))
            else:
                self.sector_count = 0
                self.sectors = ()
                self._set_tiles(0)
                identifier = dump.metadata.get('record_kind') == 'identifier'
                self.kind.setText(say('Идентификатор', 'Identifier') if identifier else say('Данные снимка', 'Snapshot data'))
                self.coverage.setText(say('{0} Б в записи', '{0} B in record').format(len(dump.data)))
                self.status.setText(say('Идентификатор не является образом памяти чипа.',
                                        'An identifier is not a chip memory image.') if identifier else
                                    say('Геометрия Classic не установлена.', 'Classic geometry is not established.'))
                self.heading.setText(say('Выбор', 'Selection'))
            self.update_cursor()
        finally:
            self._refreshing = False

    def _set_tiles(self, count):
        if len(self.tiles) == count:return
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():item.widget().deleteLater()
        self.tiles = []
        for sector in range(count):
            tile = SoftButton(str(sector))
            tile.setObjectName('sectorTile')
            tile.setFont(mono_font())
            tile.setCheckable(True)
            tile.setMinimumSize(27, 27)
            tile.setMaximumHeight(31)
            tile.clicked.connect(lambda checked=False, selected=sector:self.select_sector(selected))
            self.tiles.append(tile)
        self._reflow()

    def _reflow(self):
        columns = max(4, min(6, max(1, self.width()-28)//38))
        for index, tile in enumerate(self.tiles):self.grid.addWidget(tile, index//columns, index%columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'tiles'):self._reflow()

    def update_cursor(self):
        if self.dump is None or not (self._positions or self._large_prefixes):return
        if self.owner.hex_edit.document().revision() != self._document_revision:
            self.schedule()
            return
        cursor = self.owner.hex_edit.textCursor()
        position = cursor.selectionStart() if cursor.hasSelection() else cursor.position()
        if self._positions:
            self.selected_byte = max(0, min(len(self._positions)-1, bisect_right(self._starts, position)-1))
        else:
            checkpoint = position//4096
            count = self._large_prefixes[checkpoint]
            count += len(re.sub(r'[ \t\r\n\v\f]', '', self._large_text[checkpoint*4096:position]))
            current_hex = position < len(self._large_text) and self._large_text[position] in '0123456789abcdefABCDEF'
            self.selected_byte = max(0, min(len(self.dump.data)-1, count//2 if current_hex else (count-1)//2))
        unit = self.dump.block_size
        block = self.selected_byte // unit
        self.byte_label.setText(f'0x{self.selected_byte:04X}')
        self.block_label.setText(str(block))
        self.role_label.setText(say('Данные', 'Data'))
        if not self.classic:
            self.heading.setText(say('Выбор', 'Selection'))
            return
        self.sector = block_sector(block)
        info = self.sectors[self.sector]
        self.heading.setText(say('Сектор {0}', 'Sector {0}').format(self.sector))
        for index, tile in enumerate(self.tiles):tile.setChecked(index == self.sector)
        self.key_a.setText(say('Есть · скрыт', 'Recorded · hidden') if info.key_a_known else say('Неизвестен', 'Unknown'))
        self.key_b.setText(say('Есть · скрыт', 'Recorded · hidden') if info.key_b_known else say('Неизвестен', 'Unknown'))
        if info.conditions is None:
            value = say('Некорректны', 'Invalid') if info.acl_known else say('Неизвестны', 'Unknown')
            self.access_bits.setText(value)
            self.read_rights.setText('—');self.write_rights.setText('—')
        else:
            self.access_bits.setText(' / '.join(f'{condition:03b}' for condition in info.conditions))
            rights = access_report(info.conditions)
            trailer = block == info.last
            if trailer:
                read, write = rights['trailer'][1:3]
            else:
                group = min(2, (block-info.first)//(5 if self.sector >= 32 else 1))
                read, write = rights['data'][group][:2]
            display = lambda value:say('Запрещено', 'Denied') if value == '-' else ' / '.join(value)
            self.read_rights.setText(display(read));self.write_rights.setText(display(write))
            if block == 0:
                self.write_rights.setText(say('Зависит от карты', 'Card-specific'))
        self.write_rights.setToolTip(say('ACL не подтверждает возможность изменения UID или заводского блока.',
                                         'ACL alone does not establish UID or manufacturer-block writeability.') if block == 0 else '')
        trailer = block == info.last
        self.read_title.setText(say('Чтение ACL', 'Read ACL') if trailer else say('Чтение', 'Read'))
        self.write_title.setText(say('Запись ACL', 'Write ACL') if trailer else say('Запись', 'Write'))
        self.role_label.setText(say('Трейлер A/B/ACL', 'Trailer A/B/ACL') if trailer else
                                say('Блок UID', 'UID block') if block == 0 else say('Данные', 'Data'))
        states = {'complete':say('Данные известны', 'Recorded data'), 'partial':say('Есть пропуски', 'Contains gaps'),
                  'missing':say('Не прочитано', 'Unread'), 'invalid':say('Ошибка ACL', 'Invalid ACL')}
        self.status.setText(say('Блоки {0}–{1} · {2}/{3} Б\n{4}', 'Blocks {0}–{1} · {2}/{3} B\n{4}').format(
            info.first, info.last, info.known_bytes, info.total_bytes, states[info.state]))

    def select_sector(self, sector):
        self.refresh()
        if not self.classic or type(sector) is not int or not 0 <= sector < self.sector_count:return False
        first = sector_first_block(sector)*16
        last = first+15
        cursor = self.owner.hex_edit.textCursor()
        cursor.setPosition(self._positions[first][0])
        cursor.setPosition(self._positions[last][1], QTextCursor.MoveMode.KeepAnchor)
        self.owner.hex_edit.setTextCursor(cursor)
        self.owner.hex_edit.ensureCursorVisible()
        self.update_cursor()
        return True

    def open_memory_map(self):
        if self.classic and self.map_button.isEnabled():self.owner.open_memory_map()
