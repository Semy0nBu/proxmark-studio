"""Offline catalog inspector. Its worker never uses the device runner or a port."""
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel,
                              QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                              QSplitter, QVBoxLayout, QWidget)

from .catalog_snapshot import (CatalogCancelled, CatalogError, capture_catalog,
                               compare_catalogs, load_catalog, save_catalog,
                               syntax_fingerprint)
from .client_catalog import COMMANDS
from .studio_data import say
from .theme import mono_font
from .ui import SoftButton


class CatalogWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(str)

    def __init__(self, executable, cache_path, parent=None):
        super().__init__(parent)
        self.executable, self.cache_path = executable, Path(cache_path)

    def run(self):
        try:
            snapshot = capture_catalog(self.executable, cancelled=self.isInterruptionRequested,
                                       progress=self.progress.emit)
            if self.isInterruptionRequested():
                raise CatalogCancelled('Client catalog update cancelled.')
            save_catalog(snapshot, self.cache_path)
            self.completed.emit(snapshot)
        except CatalogCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class CatalogDialog(QDialog):
    snapshot_updated = Signal(object)
    open_command = Signal(str)

    def __init__(self, owner, *, executable=None, profile=None, snapshot=None, on_open=None):
        super().__init__(owner)
        self.owner, self.worker, self.snapshot = owner, None, snapshot
        self._fixed_executable = str(executable) if executable is not None else None
        self.cache_path = Path(profile if profile is not None else owner.data_dir)/'client-catalog.json'
        self._close_requested = False
        self._previous = None
        self._rows = {}
        self._states = {}
        if on_open is not None:
            self.open_command.connect(on_open)
        self.setWindowTitle(say('Возможности выбранного клиента', 'Selected client capabilities'))
        self.resize(960, 680)
        layout = QVBoxLayout(self)
        self.identity = QLabel();self.identity.setWordWrap(True);self.identity.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.identity)
        controls = QHBoxLayout()
        self.update_button = SoftButton(say('Обновить из клиента', 'Update from client'))
        self.update_button.clicked.connect(self.update_catalog);controls.addWidget(self.update_button)
        self.cancel_button = SoftButton(say('Отменить', 'Cancel'))
        self.cancel_button.clicked.connect(self.cancel_update);self.cancel_button.setEnabled(False);controls.addWidget(self.cancel_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.notice = QLabel(say(
            'Список и справка получены без подключения к плате. Наличие команды не подтверждает поддержку устройством. Новые и изменённые команды доступны для просмотра; их выполнение через старые формы заблокировано.',
            'Help is collected without connecting to a reader. Advertised commands do not prove device support. New or changed commands can be inspected; existing forms cannot execute unconfirmed syntax.'))
        self.notice.setWordWrap(True);layout.addWidget(self.notice)
        filters = QHBoxLayout()
        self.search = QLineEdit();self.search.setPlaceholderText(say('Найти команду', 'Find a command'))
        self.search.textChanged.connect(self.filter_commands);filters.addWidget(self.search, 1)
        self.state_filter = QComboBox()
        for title, state in [(say('Все', 'All'), ''), (say('Добавлены', 'Added'), 'added'),
                             (say('Исчезли', 'Removed'), 'removed'), (say('Синтаксис изменён', 'Syntax changed'), 'changed')]:
            self.state_filter.addItem(title, state)
        self.state_filter.currentIndexChanged.connect(self.filter_commands);filters.addWidget(self.state_filter)
        layout.addLayout(filters)
        self.summary = QLabel();self.summary.setWordWrap(True);layout.addWidget(self.summary)
        splitter = QSplitter(Qt.Orientation.Horizontal);splitter.setChildrenCollapsible(False)
        self.commands = QListWidget();self.commands.currentItemChanged.connect(self.selected)
        self.commands.setMinimumWidth(220);splitter.addWidget(self.commands)
        detail = QWidget();body = QVBoxLayout(detail);body.setContentsMargins(6, 0, 0, 0)
        self.command_state = QLabel();self.command_state.setWordWrap(True);body.addWidget(self.command_state)
        self.help = QPlainTextEdit();self.help.setReadOnly(True);self.help.setFont(mono_font());body.addWidget(self.help)
        buttons = QHBoxLayout()
        self.copy_button = SoftButton(say('Скопировать справку', 'Copy help'))
        self.copy_button.clicked.connect(lambda:QApplication.clipboard().setText(self.help.toPlainText()))
        buttons.addWidget(self.copy_button)
        self.open_button = SoftButton(say('Открыть проверенную форму', 'Open confirmed form'))
        self.open_button.clicked.connect(self.open_selected);buttons.addWidget(self.open_button)
        body.addLayout(buttons);splitter.addWidget(detail);splitter.setSizes([320, 620]);layout.addWidget(splitter, 1)
        self.status = QLabel();self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        if self.snapshot is None:
            try:self.snapshot = load_catalog(self.cache_path)
            except CatalogError:pass
        self._display(self.snapshot)
        selected_client = getattr(owner, 'client_path', None)
        if self._fixed_executable is None and hasattr(selected_client, 'textChanged'):
            selected_client.textChanged.connect(self._client_changed)

    def executable(self):
        return self._fixed_executable if self._fixed_executable is not None else self.owner.client_path.text().strip()

    def _client_changed(self, *_):
        if self.worker is not None:self.cancel_update()
        self._display(self.snapshot)

    def _display(self, snapshot, previous=None):
        self._rows, self._states = {}, {}
        executable = self.executable()
        if snapshot is None:
            self.identity.setText(executable)
            self.summary.setText(say('Каталог ещё не проверен.', 'The catalog has not been checked.'))
            self.status.setText(say('Обновите список возможностей выбранного клиента.', 'Update the capabilities of the selected client.'))
        else:
            trusted = snapshot.matches(executable)
            self.identity.setText(snapshot.version+'\nSHA256: '+snapshot.client_sha256+'\n'+snapshot.client_path)
            baseline = previous.commands if previous is not None else COMMANDS
            changes = compare_catalogs(baseline, snapshot)
            self._rows = dict(snapshot.commands)
            for name in changes.removed:
                self._rows[name] = baseline[name]
            self._states = {name:'added' for name in changes.added}
            self._states.update({name:'removed' for name in changes.removed})
            self._states.update({name:'changed' for name in changes.changed})
            scope = say('с предыдущим каталогом', 'with the previous catalog') if previous else say('со встроенным каталогом', 'with the bundled catalog')
            self.summary.setText(say('Команд: {0} · Добавлены: {1} · Исчезли: {2} · Синтаксис изменён: {3}',
                                     'Commands: {0} · Added: {1} · Removed: {2} · Syntax changed: {3}').format(
                                     len(snapshot.commands), len(changes.added), len(changes.removed), len(changes.changed))+' · '+scope)
            self.status.setText(say('Каталог подтверждён для выбранного клиента.', 'The catalog matches the selected client.') if trusted else
                                say('Клиент изменился. Каталог устарел; обновите его. Формы не подтверждены.', 'The client changed. This catalog is stale; update it. Forms are not confirmed.'))
        self.filter_commands()

    def filter_commands(self, *_):
        selected = self.commands.currentItem()
        selected = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.commands.clear()
        query, state = self.search.text().casefold(), self.state_filter.currentData()
        for name, row in sorted(self._rows.items()):
            if state and self._states.get(name) != state:continue
            if query and query not in (name+' '+row['description']).casefold():continue
            item = QListWidgetItem(name);item.setData(Qt.ItemDataRole.UserRole, name)
            if self._states.get(name):item.setToolTip(self._states[name])
            self.commands.addItem(item)
            if name == selected:self.commands.setCurrentItem(item)
        if self.commands.currentItem() is None and self.commands.count():self.commands.setCurrentRow(0)
        if not self.commands.count():self.selected(None)

    def _confirmed_form(self, name):
        if self.worker is not None or self.snapshot is None or not self.snapshot.matches(self.executable()):return False
        live, shipped = self.snapshot.commands.get(name), COMMANDS.get(name)
        return bool(live and shipped and live['help'].strip() and live['offline'] == shipped['offline']
                    and syntax_fingerprint(live['help']) == syntax_fingerprint(shipped['help']))

    def selected(self, item, *_):
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.help.setPlainText(self._rows[name]['help'] if name else '')
        self.copy_button.setEnabled(bool(name))
        confirmed = bool(name and self._confirmed_form(name))
        self.open_button.setEnabled(confirmed)
        states = {'added':say('Добавлена', 'Added'), 'removed':say('Исчезла', 'Removed'),
                  'changed':say('Синтаксис изменён', 'Syntax changed')}
        status = states.get(self._states.get(name), say('Без изменений синтаксиса', 'Syntax unchanged'))
        self.command_state.setText((name+' · '+status+'\n'+(say('Существующая форма подтверждена.', 'The existing form is confirmed.') if confirmed else
                                   say('Только справка; существующая форма не подтверждена.', 'Help only; the existing form is not confirmed.'))) if name else '')

    def open_selected(self):
        item = self.commands.currentItem()
        if item is None:return
        name = item.data(Qt.ItemDataRole.UserRole)
        if self._confirmed_form(name):self.open_command.emit(name)

    def update_catalog(self):
        if self.worker is not None:return
        self._previous = self.snapshot
        self.update_button.setEnabled(False);self.cancel_button.setEnabled(True)
        self.open_button.setEnabled(False)
        self.worker = worker = CatalogWorker(self.executable(), self.cache_path, self)
        worker.completed.connect(self._completed);worker.failed.connect(self._failed)
        worker.cancelled.connect(self._cancelled);worker.finished.connect(self._finished)
        worker.progress.connect(lambda flag:self.status.setText(say('Чтение справки клиента: ', 'Reading client help: ')+flag))
        worker.start()

    def cancel_update(self):
        if self.worker is not None:
            self.worker.requestInterruption();self.cancel_button.setEnabled(False)
            self.status.setText(say('Отмена обновления…', 'Cancelling update…'))

    def _completed(self, snapshot):
        self.snapshot = snapshot
        self._display(snapshot, self._previous)
        # A settings edit during an export cannot activate the previous binary.
        if snapshot.matches(self.executable()) and not self._close_requested:self.snapshot_updated.emit(snapshot)

    def _failed(self, message):
        self.status.setText(message)

    def _cancelled(self):
        self.status.setText(say('Обновление отменено. Предыдущий каталог сохранён.', 'Update cancelled. The previous catalog was kept.'))

    def _finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:worker.deleteLater()
        self.update_button.setEnabled(True);self.cancel_button.setEnabled(False)
        self.selected(self.commands.currentItem())
        if self._close_requested:QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.worker is not None:
            self._close_requested = True;self.cancel_update();event.ignore();return
        super().closeEvent(event)

    def reject(self):
        if self.worker is not None:
            self._close_requested = True;self.cancel_update();return
        super().reject()

    def showEvent(self, event):
        if self.worker is None:self._close_requested = False
        super().showEvent(event)
