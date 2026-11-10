"""Library views of immutable snapshots and operation-bound write evidence."""
from PySide6.QtCore import Qt, QTimer, QObject
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QComboBox, QPushButton, QProgressBar, QTableWidget, QTableWidgetItem)
from .studio_data import say
from .card_snapshot import load_revision, compare_card_snapshots
from .semantic_diff import Protocol, ScopeKind, Representation
from .operation_recovery import State, Action
from .core import DumpData
from .operations import sector_first_block, sector_trailer
from .memory_map import sector_info



from .ui import SoftButton

def label(value=''):
    item = QLabel(value)
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    return item


def button(value, slot, layout):
    item = SoftButton(value)
    item.clicked.connect(slot)
    layout.addWidget(item)
    return item


class RecoveryPanel(QWidget):
    def __init__(self, owner, bridge):
        super().__init__()
        self.owner, self.bridge = owner, bridge
        layout = QVBoxLayout(self)
        self.title, self.context, self.facts, self.saved, self.history = (label() for _ in range(5))
        self.title.setObjectName('section')
        for item in (self.title, self.context, self.facts, self.saved, self.history):layout.addWidget(item)
        self.blocks = QTableWidget(0, 2)
        self.blocks.setHorizontalHeaderLabels([say('Блок / страница', 'Block / page'), say('Подтверждение', 'Evidence')])
        self.blocks.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.blocks.horizontalHeader().setStretchLastSection(True)
        self.blocks.setColumnWidth(0, 140)
        self.blocks.setMinimumHeight(130)
        layout.addWidget(self.blocks)
        row = QHBoxLayout()
        self.check = button(say('Перечитать и сравнить', 'Read and compare'), owner.check_interrupted_write, row)
        self.prepare = button(say('Новый план записи…', 'New write plan…'), owner.prepare_recovery_write, row)
        layout.addLayout(row)
        layout.addStretch()
        bridge.result_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        result = self.bridge.decision()
        self.check.setEnabled(False);self.prepare.setEnabled(False);self.blocks.setRowCount(0)
        if result is None:
            self.title.setText(say('Результата записи пока нет', 'No write result yet'))
            self.facts.setText(say('Здесь появится результат текущей записи и безопасное следующее действие.',
                                   'The write result and the next step will appear here.'))
            self.context.clear();self.saved.clear();self.history.clear();return
        titles = {
            State.WAITING: say('Дождитесь завершения операции', 'Wait for the operation to finish'),
            State.CANCELLED_BEFORE_WRITE: say('Остановлено до записи', 'Cancelled before writing'),
            State.NOT_WRITTEN: say('Команды записи не отправлялись', 'No write commands were sent'),
            State.BACKUP_FAILED: say('Резервная копия не сохранена', 'Backup was not saved'),
            State.WRITE_UNCERTAIN: say('Результат записи не установлен', 'Write outcome is uncertain'),
            State.DIFFERENCES_CONFIRMED: say('Перечитано: есть отличия от плана', 'Readback differs from the plan'),
            State.VERIFIED: say('Данные плана подтверждены чтением', 'Planned data confirmed by readback'),
            State.INCONCLUSIVE: say('Недостаточно согласованных подтверждений', 'Insufficient consistent evidence'),
            State.OUTSIDE_SCOPE: say('Отдельная операция прошивки', 'Separate firmware operation'),
        }
        self.title.setText(titles[result.state])
        target = self.bridge._target.uid if self.bridge._target else '—'
        operation = result.binding.operation_ref[-8:] if result.binding else '—'
        self.context.setText(say('Последняя запись · UID назначения {0} · операция {1}',
                                 'Last write · target UID {0} · operation {1}').format(target, operation))
        unknown = '—' if result.uncertain_blocks is None else str(len(result.uncertain_blocks))
        untouched = '—' if result.not_attempted_blocks is None else str(len(result.not_attempted_blocks))
        self.facts.setText(say('Совпадает с планом: {0} · отличается: {1} · не установлено: {2} · не отправлялось: {3}',
                              'Matches plan: {0} · differs: {1} · uncertain: {2} · not sent: {3}').format(
                                  len(result.verified_blocks), len(result.differing_blocks), unknown, untouched))
        yes, no = say('сохранён', 'saved'), say('не подтверждён', 'unconfirmed')
        self.saved.setText(say('Отчёт: {0} · резервная копия: {1} · источник в библиотеке: {2}',
                              'Report: {0} · backup: {1} · library source: {2}').format(
                                  yes if result.saved.report else no, yes if result.saved.backup else no,
                                  yes if result.saved.source_version else no))
        self.history.setText(say('Сравнение подтверждает содержимое выбранного плана. История отправленных команд сохраняется отдельно. Старый план повторно не запускается.',
                                'Readback confirms the selected plan data. Sent-command history is separate. The old plan cannot be resumed.'))
        if self.bridge._snapshot.error_present:
            self.history.setText(self.history.text() + '\n' + say('Исходная ошибка операции сохранена.', 'The original operation failure is retained.'))
        rows = sorted(set(result.verified_blocks) | set(result.differing_blocks) |
                      set(result.uncertain_blocks or ()) | set(result.not_attempted_blocks or ()))
        self.blocks.setRowCount(len(rows))
        for row, block in enumerate(rows):
            status, color = (say('Совпадает', 'Matches'), '#70bda8') if block in result.verified_blocks else (
                (say('Отличается', 'Differs'), '#e5ad65') if block in result.differing_blocks else (
                    (say('Не отправлялось', 'Not sent'), '#9ea7b3') if block in (result.not_attempted_blocks or ()) else
                    (say('Нужно проверить', 'Needs readback'), '#dba979')))
            self.blocks.setItem(row, 0, QTableWidgetItem(str(block)))
            item = QTableWidgetItem(status);item.setForeground(QColor(color));self.blocks.setItem(row, 1, item)
        available = not self.bridge.occupied and self.bridge._capture is not None
        self.check.setEnabled(available and any(x in result.steps for x in (
            Action.RECONNECT, Action.VERIFY_CONNECTION, Action.READ_AND_COMPARE)))
        self.prepare.setEnabled(available and result.state in {
            State.NOT_WRITTEN, State.CANCELLED_BEFORE_WRITE, State.BACKUP_FAILED, State.DIFFERENCES_CONFIRMED})


class CardWorkspace(QObject):
    def __init__(self, owner, bridge):
        super().__init__(owner)
        self.owner, self.bridge = owner, bridge
        self._scheduled = False
        self._valid_source = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.passport, self.changes = QWidget(), QWidget()
        self.result = RecoveryPanel(owner, bridge)
        layout = QVBoxLayout(self.passport)
        self.identity, self.source, self.coverage, self.notice = (label() for _ in range(4))
        self.identity.setObjectName('section')
        for item in (self.identity, self.source, self.coverage):layout.addWidget(item)
        self.progress = QProgressBar();layout.addWidget(self.progress)
        self.grid = QGridLayout();layout.addLayout(self.grid)
        self.legend = label(say('Синий: известно · жёлтый: пропуски · серый: неизвестно · красный: неверные права доступа',
                               'Blue: known · amber: gaps · grey: unknown · red: invalid access bits'))
        layout.addWidget(self.legend);layout.addWidget(self.notice)
        row = QHBoxLayout()
        self.next_button = button('', self.next_action, row)
        self.map_button = button(say('Карта памяти…', 'Memory map…'), owner.open_memory_map, row)
        button(say('Редактор', 'Editor'), lambda: owner.card_tabs.setCurrentIndex(2), row)
        layout.addLayout(row);layout.addStretch()
        layout = QVBoxLayout(self.changes)
        row = QHBoxLayout()
        self.left, self.right = QComboBox(), QComboBox()
        row.addWidget(label('A'));row.addWidget(self.left, 1);row.addWidget(label('→ B'));row.addWidget(self.right, 1)
        button(say('Сравнить', 'Compare'), self.compare, row);layout.addLayout(row)
        self.diff_summary = label();layout.addWidget(self.diff_summary)
        self.diff_table = QTableWidget(0, 4)
        self.diff_table.setHorizontalHeaderLabels([say('Байт', 'Byte'), say('Блок', 'Block'),
            say('Область', 'Area'), say('Изменение', 'Change')])
        self.diff_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.diff_table.horizontalHeader().setStretchLastSection(True)
        self.diff_table.setMinimumHeight(180)
        self.diff_table.cellDoubleClicked.connect(self.jump)
        layout.addWidget(self.diff_table)
        layout.addWidget(label(say('A → B задаёт направление сравнения. Значения ключей скрыты. Изменения версий не доказывают изменения физической карты.',
                                   'A → B defines comparison direction. Key values are hidden. Snapshot changes do not prove changes to a physical card.')))
        layout.addStretch()
        bridge.source_changed.connect(self.schedule)
        bridge.result_changed.connect(self.update_actions)
        self.refresh()

    def schedule(self):
        if not self._scheduled:
            self._scheduled = True
            self.timer.start(0)

    def update_actions(self):
        self.next_button.setEnabled(not self.bridge.occupied and self._valid_source)

    def refresh(self):
        self._scheduled = False
        if self.owner.loading:return
        self._valid_source = False
        self.identity.clear();self.source.clear();self.coverage.clear();self.notice.clear()
        while self.grid.count():
            child = self.grid.takeAt(0)
            if child.widget():child.widget().deleteLater()
        self.diff_table.setRowCount(0);self.diff_summary.clear();self._diff_binding = None
        self.map_button.setEnabled(False);self.legend.hide()
        self.left.clear();self.right.clear()
        if self.owner.loaded_dump is None:
            self.identity.setText(say('Выберите карту', 'Select a card'));self.source.clear();self.coverage.clear()
            self.notice.clear();self.progress.setRange(0, 1);self.progress.setValue(0);self.next_button.setText(say('Считать карту', 'Read card'))
            self.update_actions();return
        try:
            record = self.bridge.current_source();snapshot = record.snapshot
            self.identity.setText((self.owner.protocol.text() or say('Тип не указан', 'Type unspecified')) +
                                  (' · UID / ID: '+self.owner.uid.text() if self.owner.uid.text() else ''))
            self.source.setText(say('Рабочая копия · не сохранена', 'Working copy · unsaved') if not record.persisted else
                say('Сохранённая версия: ', 'Saved revision: ') + self.owner.revisions.currentText())
            known, total = len(snapshot.known_bytes), len(snapshot.data)
            self.coverage.setText(say('Известно {0} / {1} байт', 'Known {0} / {1} bytes').format(known, total))
            self.progress.setRange(0, max(total, 1));self.progress.setValue(known)
            self.notice.setText(say('Записанные значения не подтверждают свежее чтение карты. Неизвестные заполнители данными не считаются.',
                                   'Recorded values do not establish a fresh card read. Unknown fillers are excluded.'))
            classic = snapshot.model.protocol is Protocol.MIFARE_CLASSIC and snapshot.scope.kind is ScopeKind.FULL_MODEL_MEMORY
            self.map_button.setEnabled(classic);self.legend.setVisible(classic)
            self.owner.memory_button.setEnabled(classic)
            if snapshot.representation in (Representation.IDENTIFIER, Representation.DETECTION, Representation.STRUCTURED_DOCUMENT):
                self.coverage.setText(say('Сохранена запись идентификатора или структуры · {0} байт',
                                         'Saved identifier or structured record · {0} bytes').format(total))
                self.progress.hide()
            else:self.progress.show()
            if classic:
                view = DumpData(snapshot.data, 'bin', {'partial_dump': {'known_bytes': sorted(snapshot.known_bytes)}}, 16)
                colors = {'complete':'#285568', 'partial':'#75592e', 'missing':'#41454c', 'invalid':'#713c44'}
                for sector in range(snapshot.model.geometry.sectors):
                    info = sector_info(view, sector)
                    tile = SoftButton(str(sector));tile.setMinimumSize(46, 38)
                    tile.setStyleSheet('background-color:'+colors[info['state']]+';color:#f2f4f7;border-radius:5px')
                    first, last = sector_first_block(sector), sector_trailer(sector)
                    count = sum(i in snapshot.known_bytes for i in range(first*16, (last+1)*16))
                    tile.setToolTip(say('Сектор {0} · блоки {1}–{2} · известно {3}/{4} байт',
                                        'Sector {0} · blocks {1}–{2} · known {3}/{4} bytes').format(sector, first, last, count, (last-first+1)*16))
                    tile.clicked.connect(lambda checked=False, s=sector: self.open_sector(s))
                    self.grid.addWidget(tile, sector//8, sector%8)
            self.next_button.setText(say('Сохранить версию', 'Save revision') if self.owner.dirty else (
                say('Продолжить чтение…', 'Continue reading…') if self.owner.loaded_dump.metadata.get('partial_dump', {}).get('kind') == 'mfc' else
                say('Записать на карту…', 'Write to card…')))
            revisions = self.owner.library.list_revisions(self.owner.card_id)
            for rev in revisions:
                title = self.owner.revisions.itemText(self.owner.revisions.findData(rev['id']))
                self.left.addItem(title, rev['id']);self.right.addItem(title, rev['id'])
            self.right.addItem(say('Рабочая копия', 'Working copy'), 'working')
            index = self.left.findData(self.owner.loaded_revision)
            self.right.setCurrentIndex(self.right.count()-1 if self.owner.dirty else index)
            self.left.setCurrentIndex(min(index+1, len(revisions)-1))
            self._valid_source = True
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.coverage.clear();self.progress.hide();self.notice.setText(str(exc))
        self.update_actions()

    def next_action(self):
        if self.owner.dirty:self.owner.save_card()
        elif self.owner.loaded_dump.metadata.get('partial_dump', {}).get('kind') == 'mfc':self.owner.open_resume_read()
        else:self.owner.open_selected_card_write()

    def open_sector(self, sector):
        self.owner.open_memory_map()
        if getattr(self.owner, 'memory_dialog', None):self.owner.memory_dialog.show_sector(sector)

    def compare(self):
        self.diff_table.setRowCount(0);self._diff_binding = None
        try:
            if not self.left.currentData() or not self.right.currentData():return
            a = load_revision(self.owner.library, self.owner.card_id, self.left.currentData())
            b = self.bridge.current_source() if self.right.currentData() == 'working' else load_revision(
                self.owner.library, self.owner.card_id, self.right.currentData())
            diff = compare_card_snapshots(a, b)
            if diff.generic.status != 'comparable':
                self.diff_summary.setText(say('Эта структура не поддерживает побайтовое сравнение.', 'Byte comparison is unavailable for this structure.'));return
            c = diff.generic.counts
            self.diff_summary.setText(say('Стало известно: {0} · изменилось: {1} · потеряно сведений: {2} · добавлено/удалено байт: {3}/{4}',
                'Newly known: {0} · changed: {1} · knowledge lost: {2} · bytes added/removed: {3}/{4}').format(
                    c.became_known, c.known_values_differ, c.knowledge_lost, len(diff.generic.added_offsets), len(diff.generic.removed_offsets)))
            semantic = {x.position.offset: x for x in diff.semantic.changes} if diff.semantic and diff.semantic.status.value == 'comparable' else {}
            areas = {'user_data':say('Данные', 'User data'), 'key_a':'Key A', 'key_b':'Key B', 'access_bits':say('Права доступа', 'Access bits'),
                     'gpb':'GPB', 'manufacturer_block_0':say('Блок 0', 'Block 0')}
            self._rows = diff.generic.changes
            self.diff_table.setRowCount(len(self._rows))
            for row, change in enumerate(self._rows):
                pos = change['offset'];meaning = semantic.get(pos)
                area = areas[meaning.position.category.value] if meaning else say('Байты записи', 'Record bytes')
                event = say('Добавлен', 'Added') if not change['a_present'] else say('Удалён', 'Removed') if not change['b_present'] else (
                    say('Стало известно', 'Newly known') if not change['a_known'] else say('Сведения потеряны', 'Knowledge lost') if not change['b_known'] else say('Значение изменилось', 'Value changed'))
                for col, value in enumerate((pos, pos//b.block_size, area, event)):
                    self.diff_table.setItem(row, col, QTableWidgetItem(str(value)))
            self._diff_binding = (self.owner.card_id, b.revision_id, b.fingerprint, self.right.currentData())
            if diff.semantic and not semantic and diff.semantic.status.value != 'comparable':
                self.diff_summary.setText(self.diff_summary.text()+'\n'+say('Показаны смещения файла; геометрия Classic не подтверждена или различается.',
                    'Showing record offsets; Classic geometry is unconfirmed or differs.'))
        except (ValueError, OSError, KeyError, TypeError) as exc:self.diff_summary.setText(str(exc))

    def jump(self, row, _column):
        if not self._diff_binding:return
        card, revision, fingerprint, selection = self._diff_binding
        if card != self.owner.card_id:return
        try:
            current = self.bridge.current_source()
            if selection != 'working' and (self.owner.loaded_revision != revision or self.owner.dirty):
                self.owner.revisions.setCurrentIndex(self.owner.revisions.findData(revision))
                if self.owner.loaded_revision != revision or self.owner.dirty:return
                current = self.bridge.current_source()
            if current.fingerprint != fingerprint:return
            offset = self._rows[row]['offset']
            if offset >= len(current.snapshot.data):return
            block = self.owner.hex_edit.document().findBlockByNumber(offset//16)
            cursor = QTextCursor(block);cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, (offset%16)*3)
            self.owner.card_tabs.setCurrentIndex(2);self.owner.hex_edit.setTextCursor(cursor);self.owner.hex_edit.setFocus()
        except (ValueError, OSError, IndexError):return
