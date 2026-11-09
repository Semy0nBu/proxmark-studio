"""Compact Classic instruments, colored bytes and explicit recovery controls."""
from __future__ import annotations

import sqlite3
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStyledItemDelegate, QStyle, QStyleOptionViewItem,
    QVBoxLayout, QWidget)
from .classic_tools import METHODS, manual_recovery, probe_classic
from .operations import CLASSIC_GEOMETRIES
from .studio_data import key_entries, say
from .theme import mono_font


BYTE_COLORS = {'uid': '#f2bc78', 'data': '#d8e1ec', 'a': '#77d7bf',
               'acl': '#bfacf4', 'b': '#87baff', 'unknown': '#a3a9b5'}


class ClassicSplit(QSplitter):
    def resizeEvent(self, event):
        wanted = Qt.Orientation.Horizontal if self.width() >= 790 else Qt.Orientation.Vertical
        if self.orientation() != wanted:
            self.setOrientation(wanted)
            self.setSizes([580, 350] if wanted == Qt.Orientation.Horizontal else [350, 320])
        super().resizeEvent(event)


class ClassicHexDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option);self.initStyleOption(opt, index)
        tokens = opt.text.split();opt.text = ''
        style = opt.widget.style() if opt.widget else None
        if style is None:return super().paint(painter, option, index)
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save();painter.setClipRect(opt.rect);painter.setFont(mono_font())
        metrics = painter.fontMetrics();step = metrics.horizontalAdvance('00 ')
        baseline = opt.rect.top() + (opt.rect.height() - metrics.height()) // 2 + metrics.ascent()
        role = index.data(Qt.ItemDataRole.UserRole) or 'data'
        uid_bytes = index.data(Qt.ItemDataRole.UserRole + 1) or 4
        for i, token in enumerate(tokens):
            kind = ('unknown' if token == '??' else
                    ('a' if i < 6 else 'acl' if i < 10 else 'b') if role == 'trailer' else
                    'uid' if role == 'uid' and i < uid_bytes else 'data')
            painter.setPen(QColor(BYTE_COLORS[kind]))
            painter.drawText(opt.rect.left() + 8 + i * step, baseline, token)
        painter.restore()


def key_slots(dump, library_entries):
    """Keep unknown fields unknown and surface conflicting candidates explicitly."""
    slots = {}
    for entry in key_entries(dump, False):
        slots.setdefault((entry['sector'], entry['type']), {})[entry['key_hex']] = {
            **entry, 'verified': False, 'source': say('Поле дампа', 'Dump field')}
    sectors = next((sectors for sectors, length in CLASSIC_GEOMETRIES.values() if length == len(dump.data)), 0)
    for entry in library_entries:
        if not 0 <= entry['sector'] < sectors:continue
        slot = slots.setdefault((entry['sector'], entry.get('type', entry.get('key_type'))), {})
        old = slot.get(entry['key_hex'], {})
        if not old.get('verified') or entry.get('verified'):slot[entry['key_hex']] = dict(entry)
    return slots


class ClassicAttackDialog(QDialog):
    def __init__(self, owner, method='Nested', *, uid='', size='1k', dump=None):
        super().__init__(owner);self.owner = owner;self.snapshot = dump
        self._snapshot_uid = ''.join(uid.split()).upper()
        self.closing = False;self.last_session = None;self.action = ''
        self.setWindowTitle(say('MIFARE Classic · поиск ключей', 'MIFARE Classic · key recovery'))
        self.resize(720, 640);self.setMinimumWidth(520)
        layout = QVBoxLayout(self);layout.setSpacing(12)
        title = QLabel(say('Поиск ключа', 'Recover a key'));title.setObjectName('title');layout.addWidget(title)
        methods = QHBoxLayout();self.methods = {};self.group = QButtonGroup(self)
        for name in METHODS:
            button = QPushButton(name);button.setCheckable(True);button.setProperty('classicAction', 'attack')
            self.group.addButton(button);self.methods[name] = button;methods.addWidget(button)
        self.group.buttonClicked.connect(self.method_changed);layout.addLayout(methods)
        self.hint = QLabel();self.hint.setWordWrap(True);self.hint.setObjectName('muted');layout.addWidget(self.hint)
        self.scroll = QScrollArea();self.scroll.setWidgetResizable(True);layout.addWidget(self.scroll, 1)
        self.form_widget = QWidget();body = QVBoxLayout(self.form_widget);body.setContentsMargins(0, 0, 6, 0)
        common = QFormLayout();body.addLayout(common)
        self.uid = QLineEdit(uid);self.uid.setFont(mono_font())
        self.uid.setPlaceholderText(say('Определится при запуске', 'Detected when the run starts'));common.addRow('UID', self.uid)
        self.geometry = QComboBox()
        for value in CLASSIC_GEOMETRIES:self.geometry.addItem(value.upper(), value)
        self.geometry.setCurrentIndex(self.geometry.findData(size));common.addRow(say('Память', 'Memory'), self.geometry)
        self.source_frame, source = self.frame(body, say('Исходный ключ', 'Source key'))
        self.seeds = QComboBox();self.seeds.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.seeds.setMinimumContentsLength(18);source.addRow(say('Из снимка / библиотеки', 'From snapshot / library'), self.seeds)
        self.source_sector = QSpinBox();self.source_type = QComboBox();self.source_type.addItems(['A', 'B'])
        source_row = QHBoxLayout();source_row.addWidget(self.source_sector, 1);source_row.addWidget(self.source_type)
        source.addRow(say('Сектор и тип', 'Sector and type'), source_row)
        self.key = QLineEdit();self.key.setMaxLength(12);self.key.setFont(mono_font());self.key.setPlaceholderText('12 HEX');source.addRow('HEX', self.key)
        self.target_frame, target = self.frame(body, say('Искомый ключ', 'Target key'))
        self.target_sector = QSpinBox();self.target_type = QComboBox();self.target_type.addItems(['A', 'B']);self.target_type.setCurrentText('B')
        target_row = QHBoxLayout();target_row.addWidget(self.target_sector, 1);target_row.addWidget(self.target_type)
        target.addRow(say('Сектор и тип', 'Sector and type'), target_row)
        body.addStretch();self.scroll.setWidget(self.form_widget)
        self.status = QLabel(say('Параметры работают без подключения. Поиск начинается кнопкой внизу.',
                                'Parameters work offline. Start recovery using the button below.'))
        self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        self.reset = QPushButton(say('После сброса питания…', 'After power reset…'));self.reset.clicked.connect(owner.acknowledge_studio_reset);self.reset.hide();layout.addWidget(self.reset)
        footer = QHBoxLayout();layout.addLayout(footer)
        self.probe = QPushButton(say('Определить карту', 'Identify card'));self.probe.clicked.connect(lambda:self.guard(self.identify));footer.addWidget(self.probe)
        self.start = QPushButton();self.start.setObjectName('primary');self.start.clicked.connect(lambda:self.guard(self.begin));footer.addWidget(self.start)
        self.stop = QPushButton(say('Остановить', 'Stop'));self.stop.clicked.connect(owner.cancel_operation);footer.addWidget(self.stop)
        self.close_button = QPushButton(say('Закрыть', 'Close'));self.close_button.clicked.connect(self.reject);footer.addWidget(self.close_button)
        self.geometry.currentIndexChanged.connect(self.geometry_changed);self.uid.editingFinished.connect(self.load_seeds)
        self.seeds.currentIndexChanged.connect(self.seed_changed)
        self.methods[method].setChecked(True);self.geometry_changed();self.method_changed();self.sync()
        if self.seeds.count() > 1:self.seeds.setCurrentIndex(1)
        for button in self.findChildren(QPushButton):button.setAutoDefault(False)
        self.uid.setFocus()

    @staticmethod
    def frame(layout, title):
        frame = QFrame();frame.setObjectName('classicPane');body = QVBoxLayout(frame)
        heading = QLabel(title);heading.setObjectName('section');body.addWidget(heading)
        form = QFormLayout();body.addLayout(form);layout.addWidget(frame)
        return frame, form

    def method(self):return self.group.checkedButton().text()

    def method_changed(self, *_):
        dark = self.method() == 'Darkside'
        self.source_frame.setVisible(not dark);self.target_frame.setVisible(not dark)
        self.hint.setText(say('Один запуск для ключа A сектора 0. Наличие уязвимости определяется картой.',
                             'One run for sector 0 key A. The card must support the vulnerability.') if dark else
                          say('Один выбранный сектор A/B. Исходный ключ проверяется до атаки, найденный — отдельной аутентификацией.',
                              'One selected sector A/B. The source key is checked before the attack; a recovered key is authenticated separately.'))
        self.start.setText(say('Запустить ', 'Run ') + self.method())

    def geometry_changed(self, *_):
        maximum = CLASSIC_GEOMETRIES[self.geometry.currentData()][0] - 1
        self.source_sector.setRange(0, maximum);self.target_sector.setRange(0, maximum);self.load_seeds()

    def load_seeds(self):
        selected = self.seeds.currentData();self.seeds.blockSignals(True);self.seeds.clear()
        self.seeds.addItem(say('Ввести вручную', 'Enter manually'), None)
        try:
            uid = ''.join(self.uid.text().split()).upper()
            entries = self.owner.key_library.list_card_keys(uid) if uid else []
            if self.snapshot is not None and uid and uid == self._snapshot_uid:
                entries += [{**e, 'verified': False} for e in key_entries(self.snapshot, False)]
            seen = set()
            for entry in sorted(entries, key=lambda e:not e.get('verified')):
                data = (entry['sector'], entry.get('type', entry.get('key_type')), entry['key_hex'])
                if data in seen or data[0] > self.source_sector.maximum():continue
                seen.add(data)
                state = say('подтверждён', 'verified') if entry.get('verified') else say('кандидат', 'candidate')
                self.seeds.addItem(f'{data[0]}{data[1]} · {data[2]} · {state}', data)
            index = self.seeds.findData(selected) if selected is not None else -1
            self.seeds.setCurrentIndex(index if index >= 0 else 0)
        except ValueError:pass
        finally:self.seeds.blockSignals(False)

    def seed_changed(self, *_):
        data = self.seeds.currentData()
        if data:self.source_sector.setValue(data[0]);self.source_type.setCurrentText(data[1]);self.key.setText(data[2])

    def guard(self, callback):
        try:callback()
        except (ValueError, OSError, sqlite3.Error) as exc:self.status.setText(str(exc))

    def owned(self):
        job = self.owner.read_job
        return bool(job and job.get('job_kind') == 'studio' and job.get('on_finished') == self.operation_finished)

    def sync(self):
        active = self.owner.runner.busy or self.owner.read_job is not None or self.owner.antenna_job is not None or self.owner.firmware_busy
        self.form_widget.setEnabled(not active)
        for button in self.methods.values():button.setEnabled(not active)
        self.start.setEnabled(not active);self.probe.setEnabled(not active)
        self.stop.setEnabled(self.owned())
        self.reset.setVisible(self.owned() and self.owner.read_job['stage'] == 'uncertain')
        if self.reset.isVisible():self.status.setText(say('Остановка платы не подтверждена. Другие операции заблокированы до восстановления связи или сброса питания.',
                                                       'Device stop was not confirmed. Other operations remain locked until recovery or a power reset.'))
        elif self.owned():
            job = self.owner.read_job
            if job['stage'].startswith('cleanup_'):
                self.status.setText(say('Завершаем операцию: выключаем RF и проверяем отклик платы…',
                                        'Finishing: turning RF off and checking the device response…'))
            elif job.get('request'):
                kind = job['request'].operation().kind
                self.status.setText(say('Проверяем UID и профиль карты…', 'Checking card UID and geometry…') if kind == 'hf14a_select' else
                    say('Проверяем ключ аутентификацией…', 'Authenticating the key…') if kind == 'mfc_check_candidate' else
                    self.method() + say(' выполняется. Карта должна оставаться на антенне.', ' is running. Keep the card on the antenna.'))

    def identify(self):
        self.owner._lab_available();self.action = 'probe'
        self.status.setText(say('Определяем карту…', 'Identifying the card…'))
        self.owner.start_studio(probe_classic, say('Определение Classic', 'Identify Classic'), rf=True, on_finished=self.operation_finished);self.sync()
        if not self.owned():self.status.setText(self.owner.studio.status.text())

    def begin(self):
        self.owner._lab_available()
        flow = manual_recovery(self.method(), self.geometry.currentData(), self.uid.text(),
            source_sector=self.source_sector.value(), source_type=self.source_type.currentText(), key=self.key.text(),
            target_sector=self.target_sector.value(), target_type=self.target_type.currentText())
        self.action = 'attack';self.status.setText(say('Проверяем карту и запускаем ', 'Checking the card and starting ') + self.method() + '…')
        self.owner.start_studio(flow, 'Classic · ' + self.method(), rf=True, on_finished=self.operation_finished);self.sync()
        if not self.owned():self.status.setText(self.owner.studio.status.text())

    def operation_finished(self, outcome):
        self.last_session = outcome['session'];result = outcome['result']
        message = str(outcome['error']) or say('Остановлено. Журнал сохранён.', 'Stopped. The journal was saved.')
        if outcome['status'] == 'completed':
            self.uid.setText(result['uid'])
            self.geometry.setCurrentIndex(self.geometry.findData(result['classic_size']))
            if self.action == 'probe':message = result['uid'] + ' · ' + result['classic_size'].upper()
            else:
                try:
                    self.owner.key_library.record_verified_keys(result['uid'], result['keys'], source='Proxmark3: manual ' + result['method'])
                    self.owner.refresh_keys();self.load_seeds()
                    entry = result['keys'][0]
                    message = say('Подтверждён и сохранён: ', 'Verified and saved: ') + f"{entry['sector']}{entry['type']} · {entry['key_hex']}"
                except (ValueError, OSError, sqlite3.Error) as exc:message = say('Ключ найден, но библиотека не сохранена: ', 'Key found, but library save failed: ') + str(exc)
        self.status.setText(message);self.sync()
        if self.closing:QTimer.singleShot(0, self.accept)

    def closeEvent(self, event):
        if self.owned():self.closing = True;self.owner.cancel_operation();event.ignore();return
        event.accept()

    def reject(self):
        if self.owned():self.closing = True;self.owner.cancel_operation();return
        super().reject()
