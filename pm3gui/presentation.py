"""Window layout and interaction conventions, independent of device commands."""
from __future__ import annotations
from .i18n import tr

import json
import re
from PySide6.QtCore import QByteArray, Qt, Signal, QRect, QSize
from datetime import datetime, timezone
from PySide6.QtGui import QAction, QKeySequence, QShortcut,QColor,QTextCursor
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDockWidget,
    QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMenu, QPlainTextEdit, QProgressBar, QSizePolicy,
    QSpinBox, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,QTextEdit,QTabWidget,QTabBar,
)
from .antenna import AntennaPlot
from .theme import mono_font
from .ui import Disclosure, SmoothScrollArea, SoftButton, line_icon
from .i18n import LANGUAGES
from .studio_data import say
from .interface_mode import Guidance

ENCODINGS = [('UTF-8', 'utf-8'), ('Windows-1251 · кириллица', 'cp1251'),
             ('CP866 · DOS', 'cp866'), ('KOI8-R', 'koi8-r'), ('UTF-16 LE', 'utf-16-le'),
             ('UTF-16 BE', 'utf-16-be'), ('ASCII', 'ascii'), ('Latin-1', 'latin-1')]


class ResultLabel(QLabel):
    changed = Signal(str)

    def setText(self, text):
        super().setText(text)
        self.changed.emit(text)


class CardList(QListWidget):
    """Wrap names to the actual pane width instead of the longest item width."""
    def __init__(self):
        super().__init__()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def fit_items(self):
        width = max(100, self.viewport().width() - 24)
        for i in range(self.count()):
            item = self.item(i)
            bounds = self.fontMetrics().boundingRect(QRect(0, 0, width, 10000), Qt.TextFlag.TextWordWrap, item.text())
            item.setSizeHint(QSize(0, bounds.height() + 20))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_items()


class SortableItem(QTableWidgetItem):
    def __lt__(self, other):
        first, second = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        return first < second if first is not None and second is not None else self.text().casefold() < other.text().casefold()


def timestamp_order(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return float('-inf')


def text(value='', kind='muted', wrap=True):
    widget = QLabel(value)
    widget.setObjectName(kind)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def command(title, callback, primary=False):
    widget = SoftButton(title)
    widget.clicked.connect(callback)
    if primary:
        widget.setObjectName('primary')
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def notice(widget, message, error=False):
    widget.setProperty('tone', 'error' if error else '')
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.setText(message)


def column(parent=None, margins=0, spacing=12):
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return layout


def surface():
    frame = QFrame()
    frame.setObjectName('panel')
    return frame, column(frame, 16)


def codecs():
    combo = QComboBox()
    for title, codec in ENCODINGS:
        combo.addItem(tr(title), codec)
    combo.setMinimumContentsLength(8)
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    return combo


def editor(title, widget):
    frame = QWidget()
    layout = column(frame, spacing=8)
    layout.addWidget(text(title, 'section'))
    widget.setFont(mono_font())
    layout.addWidget(widget, 1)
    return frame


def configure_table(table, widths):
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(36)
    header = table.horizontalHeader()
    header.setMinimumSectionSize(60)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    for col, width in enumerate(widths):
        table.setColumnWidth(col, width)


def install_table_copy(table):
    def copy_values():
        indexes = table.selectedIndexes()
        if not indexes:
            return
        rows = sorted({index.row() for index in indexes})
        cols = sorted({index.column() for index in indexes})
        selected = {(index.row(), index.column()) for index in indexes}
        QApplication.clipboard().setText('\n'.join('\t'.join(
            table.item(row, col).text() if (row, col) in selected and table.item(row, col) else ''
            for col in cols) for row in rows))
    action = QAction(tr('Копировать'), table)
    action.setShortcut(QKeySequence.StandardKey.Copy)
    action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    action.triggered.connect(copy_values)
    table.addAction(action)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    def menu_at(point):
        menu = QMenu(table)
        menu.addAction(tr('Копировать'), copy_values).setEnabled(bool(table.selectedIndexes()))
        item = table.itemAt(point)
        if item:
            menu.addAction(tr('Копировать значение'), lambda: QApplication.clipboard().setText(item.text()))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(table.viewport().mapToGlobal(point))
    table.customContextMenuRequested.connect(menu_at)


class PresentationMixin:
    def _annotate_partial_bytes(self):
        partial=self.loaded_dump.metadata.get('partial_dump') if self.loaded_dump else None
        selections=[]
        if partial:
            value=self.hex_edit.toPlainText()
            from .core import parse_hex
            try:parse_hex(value)
            except ValueError:
                self.hex_edit.setExtraSelections([])
                from .editor_ui import sync_expanded_hex
                sync_expanded_hex(self)
                return
            known=set(partial['known_bytes'])
            start,end=None,None
            spans=[]
            for index,match in enumerate(re.finditer(r'[0-9A-Fa-f]{2}',value)):
                if index not in known:
                    if start is None:start=match.start()
                    end=match.end()
                elif start is not None:
                    spans.append((start,end));start=None
            if start is not None:spans.append((start,end))
            for first,last in spans:
                selection=QTextEdit.ExtraSelection()
                selection.cursor=QTextCursor(self.hex_edit.document())
                selection.cursor.setPosition(first)
                selection.cursor.setPosition(last,QTextCursor.MoveMode.KeepAnchor)
                selection.format.setBackground(QColor('#4a3827'))
                selection.format.setForeground(QColor('#f2c99d'))
                selections.append(selection)
        self.hex_edit.setExtraSelections(selections)
        self.hex_edit.setToolTip(tr('Подсвеченные байты не были прочитаны с карты. При сохранении дампа пропуски заполнены нулями; ручное редактирование не делает их подтверждёнными чтением.') if partial else '')
        from .editor_ui import sync_expanded_hex
        sync_expanded_hex(self)

    def _build(self):
        host = QWidget()
        self.setCentralWidget(host)
        outer = QHBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(192)
        side = column(sidebar, 12, 4)
        name = text('Proxmark Studio', 'appname', False)
        side.addWidget(name)
        side.addSpacing(20)
        self.nav_buttons = []
        self.page_names = [tr('Рабочая область'), tr('Карты'), tr('Ключи MIFARE'), tr('Антенны'), tr('Кодировки'), tr('Лаборатория')]
        for index, (title, icon) in enumerate(zip(self.page_names, ['device', 'cards', 'keys', 'antenna', 'encoding', 'settings'])):
            nav = command(title, lambda checked=False, i=index: self.navigate(i))
            nav.setObjectName('nav')
            nav.setCheckable(True)
            nav.setIcon(line_icon(icon, '#bfbfbf'))
            nav.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.nav_buttons.append(nav)
            side.addWidget(nav)
        side.addStretch()
        side.addWidget(text(tr('Режим интерфейса'),wrap=False))
        self.interface_mode_selector=QComboBox()
        self.interface_mode_selector.addItem(tr('Пошаговый'),'guided')
        self.interface_mode_selector.addItem(tr('Компактный'),'compact')
        self.interface_mode_selector.setCurrentIndex(self.interface_mode_selector.findData(self.interface_mode))
        self.interface_mode_selector.setToolTip(tr('Пошаговый: пояснения на экране. Компактный: подробности по запросу. Проверки записи одинаковы.'))
        self.interface_mode_selector.currentIndexChanged.connect(self.choose_interface_mode)
        side.addWidget(self.interface_mode_selector)
        self.interface_mode_notice=text('');self.interface_mode_notice.hide();side.addWidget(self.interface_mode_notice)
        side.addWidget(text(tr('Язык / Language'),wrap=False))
        self.language_selector=QComboBox()
        for code,title in LANGUAGES.items():self.language_selector.addItem(title,code)
        self.language_selector.setCurrentIndex(self.language_selector.findData(self.ui_language))
        self.language_selector.setToolTip(tr('Выбор сохраняется и применяется при следующем запуске.'))
        self.language_selector.currentIndexChanged.connect(self.choose_language);side.addWidget(self.language_selector)
        self.language_notice=text('');self.language_notice.hide();side.addWidget(self.language_notice)
        side.addWidget(text('RDV4 / RDV4.01', wrap=False))
        self.backup_button = command(tr('Библиотека…'), self.show_library_backup)
        self.backup_button.setToolTip(tr('Резервная копия, восстановление и выбор профиля'))
        side.addWidget(self.backup_button)
        from .about import AboutDialog
        side.addWidget(command(tr('О программе'), lambda: AboutDialog(self).exec()))
        outer.addWidget(sidebar)
        body = QWidget()
        layout = column(body, 20, 14)
        heading = QHBoxLayout()
        self.page_title = text('', 'title', False)
        heading.addWidget(self.page_title, 1)
        self.operation_badge = text(tr('Ожидание действия'), 'status', False)
        heading.addWidget(self.operation_badge)
        self.stop_button = command(tr('Остановить'), self.cancel_operation)
        self.stop_button.setToolTip(tr('Остановить текущую операцию устройства'))
        self.stop_button.setEnabled(False)
        heading.addWidget(self.stop_button)
        layout.addLayout(heading)
        self.operation_progress = QProgressBar()
        self.operation_progress.setRange(0, 0)
        self.operation_progress.setTextVisible(False)
        self.operation_progress.hide()
        layout.addWidget(self.operation_progress)
        self.stack = QStackedWidget()
        self.ui_has_operation = False
        for build in (self._device_page, self._library_page, self._keys_page, self._antenna_page, self._encoding_page, self._build_studio_page):
            self.stack.addWidget(build())
        layout.addWidget(self.stack, 1)
        outer.addWidget(body, 1)
        self._build_journal()
        self.global_connection = text(tr('Нет подключения'), wrap=False)
        self.global_connection.setMinimumWidth(170)
        self.statusBar().addWidget(self.global_connection)
        self.global_operation = text(tr('Ожидание действия'), wrap=False)
        self.global_operation.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.statusBar().addWidget(self.global_operation, 1)
        self.journal_toggle = QToolButton()
        self.journal_toggle.setText(tr('Журнал клиента'))
        self.journal_toggle.setCheckable(True)
        self.journal_toggle.toggled.connect(self.journal.setVisible)
        self.journal.visibilityChanged.connect(self.journal_toggle.setChecked)
        self.statusBar().addPermanentWidget(self.journal_toggle)
        self.device_info.changed.connect(self._result_changed)
        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.activated.connect(self._focus_search)
        self.log_shortcut = QShortcut(QKeySequence('Ctrl+J'), self)
        self.log_shortcut.activated.connect(lambda: self.journal_toggle.toggle())
        self._restore_presentation()

    def _build_journal(self):
        self.journal = QDockWidget(tr('Журнал клиента'), self)
        self.journal.setObjectName('clientJournal')
        self.journal.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.journal.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        title = QWidget()
        row = QHBoxLayout(title)
        row.setContentsMargins(16, 6, 16, 6)
        row.addWidget(text(tr('Журнал клиента'), 'section'), 1)
        row.addWidget(command(tr('Сохранить…'), self.save_log))
        row.addWidget(command(tr('Очистить'), lambda: self.log.clear()))
        row.addWidget(command(tr('Скрыть'), self.journal.hide))
        self.journal.setTitleBarWidget(title)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font())
        self.log.setMinimumHeight(120)
        self.log.document().setMaximumBlockCount(6000)
        self.log.setPlaceholderText(tr('Технический вывод клиента появится здесь'))
        self.journal.setWidget(self.log)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.journal)
        self.resizeDocks([self.journal], [220], Qt.Orientation.Vertical)
        self.journal.hide()

    def navigate(self, index):
        if not 0 <= index < self.stack.count():
            return
        self.stack.setCurrentIndex(index)
        self.page_title.setText(self.page_names[index])
        for i, nav in enumerate(self.nav_buttons):
            nav.setChecked(i == index)

    def _focus_search(self):
        target = {1: self.search, 2: self.key_search}.get(self.stack.currentIndex())
        if target:
            target.setFocus()
            target.selectAll()
        else:
            self.navigate(1)
            self.search.setFocus()

    def _device_page(self):
        from .studio_ui import FlowLayout
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = column(page, spacing=16)
        connection, content = surface()
        row = QHBoxLayout()
        row.addWidget(text(tr('Подключение'), 'section'))
        self.transport_combo = QComboBox()
        self.transport_combo.addItem('USB', 'usb')
        self.transport_combo.addItem('Blue Shark', 'blueshark')
        self.transport_combo.addItem('TCP', 'tcp')
        self.transport_combo.setCurrentIndex(self.transport_combo.findData(self._active_transport))
        row.addWidget(self.transport_combo)
        self.ports = QComboBox()
        self.ports.setEditable(True)
        self.ports.setMinimumWidth(160)
        self.ports.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.ports, 1)
        self.session_button = command(tr('Подключить'), self.toggle_connection, True)
        row.addWidget(self.session_button)
        self.refresh_button = command(tr('Обновить'), self.refresh_ports)
        self.refresh_button.setToolTip(tr('Обновить список COM-портов'))
        row.addWidget(self.refresh_button)
        content.addLayout(row)
        self.connection_status = text(tr('Соединение не проверено.'))
        content.addWidget(self.connection_status)
        self.connection_details = Disclosure(tr('Параметры подключения'), expanded=not bool(self.settings.get('client_path')))
        path = QHBoxLayout()
        self.client_path = QLineEdit(str(self.settings.get('client_path') or self._suggest_client()))
        self.client_path.setPlaceholderText(tr('Путь к proxmark3.exe'))
        path.addWidget(self.client_path, 1)
        self.browse_button = command(tr('Обзор…'), self.browse_client)
        path.addWidget(self.browse_button)
        self.connection_details.body.addLayout(path)
        row = QHBoxLayout()
        self.keep_connection = QCheckBox(tr('Держать соединение'))
        self.keep_connection.setChecked(self.settings.get('keep_connection', True) is not False)
        self.keep_connection.setToolTip(tr('Один клиент между командами, проверка связи при простое'))
        row.addWidget(self.keep_connection)
        row.addStretch()
        self.connection_test_button = command(tr('Проверить версии'), lambda: self.run_command('hw version', 45000))
        row.addWidget(self.connection_test_button)
        self.bluetooth_settings_button = command('Bluetooth Windows…', self.open_bluetooth_settings)
        row.addWidget(self.bluetooth_settings_button)
        self.settings_button = command(tr('Сохранить'), self.save_settings)
        row.addWidget(self.settings_button)
        self.connection_details.body.addLayout(row)
        self.connection_help = text('')
        self.connection_help.hide()
        self.connection_details.body.addWidget(self.connection_help)
        self.battery_status = text(tr('Заряд недоступен'), 'battery')
        self.battery_status.setToolTip(tr('Blue Shark RDV4 не передаёт процент заряда. Красный LED показывает зарядку и гаснет после её завершения.'))
        self.connection_details.body.addWidget(self.battery_status)
        content.addWidget(self.connection_details)
        self.transport_combo.currentIndexChanged.connect(self._transport_changed)
        self.ports.currentTextChanged.connect(self._connection_input_changed)
        self.client_path.textChanged.connect(self._connection_input_changed)
        self.keep_connection.toggled.connect(self._keep_connection_changed)
        self._update_connection_help()
        layout.addWidget(connection)
        self.workspace_bands=QTabWidget()
        hf_page,lf_page=QWidget(),QWidget()
        hf_layout,lf_layout=column(hf_page,8,10),column(lf_page,8,10)
        self.workspace_bands.addTab(hf_page,tr('HF · 13,56 МГц'));self.workspace_bands.addTab(lf_page,tr('LF · 125 / 134 кГц'))
        actions = FlowLayout()
        self.read_button = command(tr('Считать и сохранить'), self.read_hf_card, True)
        self.overview_read_button = self.read_button  # Existing action has a single owner and binding.
        self.hf_search_button = command(tr('Поиск HF'), lambda: self.run_command('hf search', 90000))
        self.lf_search_button = command(tr('Поиск LF'), lambda: self.run_command('lf search', 120000))
        self.lf_read_button=command(tr('Считать и сохранить ID'),self.read_lf_card,True)
        self.lf_read_button.setToolTip(tr('Чтение LF без предварительного поиска HF. Для EM410x сохраняется идентификатор, не полный образ памяти.'))
        self.check_keys_button = command(tr('Найти ключи'), self.check_card_keys)
        for item in (self.read_button, self.hf_search_button):
            actions.addWidget(item)
        self.device_write_button=command(tr('Автозапись…'),self.open_write_dialog)
        self.device_write_button.setToolTip(tr('Выберите дамп из библиотеки или файла. Сценарий проверит карту, сохранит копию и выполнит запись после просмотра плана.'))
        self.device_write_button.setEnabled(True)
        hf_layout.addLayout(actions)
        classic=QFrame();classic.setObjectName('classicPane');classic_layout=column(classic,12,8)
        classic_heading=QHBoxLayout();classic_heading.addWidget(text('MIFARE CLASSIC','classicBadge',False));classic_heading.addStretch()
        classic_heading.addWidget(command(say('Панель Classic…','Classic workbench…'),self.open_classic_workbench))
        classic_layout.addLayout(classic_heading)
        classic_actions=FlowLayout();self.classic_method_buttons={}
        for method in ('Nested','Hardnested','Darkside'):
            button=command(method,lambda checked=False,method=method:self.open_classic_attack(method))
            button.setProperty('classicAction','attack');classic_actions.addWidget(button);self.classic_method_buttons[method]=button
        classic_actions.addWidget(self.check_keys_button);classic_layout.addLayout(classic_actions);hf_layout.addWidget(classic)
        lf_actions=QHBoxLayout()
        lf_actions.addWidget(self.lf_read_button);lf_actions.addWidget(self.lf_search_button);lf_actions.addStretch();lf_layout.addLayout(lf_actions)
        self.hardware_buttons = [self.connection_test_button, self.read_button, self.hf_search_button, self.lf_search_button, self.lf_read_button,self.check_keys_button]
        result, output = surface()
        output.addWidget(text(tr('Обнаруженная карта'), 'section'))
        self.detection_label = text(tr('Карта ещё не определена'))
        self.detection_label.setFont(mono_font())
        output.addWidget(self.detection_label)
        output.addSpacing(8)
        output.addWidget(text(tr('Результат операции'), 'section'))
        self.device_info = ResultLabel(tr('Выберите действие. Результат появится здесь.'))
        self.device_info.setWordWrap(True)
        self.device_info.setTextFormat(Qt.TextFormat.PlainText)
        self.device_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        output.addWidget(self.device_info)
        self.read_options = Disclosure(tr('Параметры чтения'))
        self.recovery_enabled = QCheckBox(tr('Искать недостающие ключи MIFARE'))
        self.recovery_enabled.setChecked(True)
        self.recovery_enabled.setToolTip(tr('Адресный поиск A/B; ограничение времени и остановка повторяющихся ошибок сохраняются'))
        self.read_options.body.addWidget(self.recovery_enabled)
        row = QHBoxLayout()
        row.addWidget(text('NTAG / Ultralight', wrap=False))
        self.mfu_auth = QLineEdit()
        self.mfu_auth.setPlaceholderText(tr('Необязательный пароль или ключ: PWD 8 HEX / UL-C 32 HEX'))
        row.addWidget(self.mfu_auth, 1)
        self.read_options.body.addLayout(row)
        hf_layout.addWidget(self.read_options)
        hf_tools=FlowLayout()
        hf_tools.addWidget(self.device_write_button)
        self.hf_tools_button=command(tr('Инструменты HF…'),self.open_hf_tools)
        self.hf_tools_button.setToolTip(tr('Полный HF-каталог и справка установленного клиента. Открытие не обращается к устройству.'))
        hf_tools.addWidget(self.hf_tools_button)
        hf_tools.addWidget(command(say('Мастера карт…','Card wizards…'),lambda:self.open_family_wizard()))
        hf_tools.addWidget(command(tr('Эмуляция…'),self.open_emulation))
        hf_tools.addWidget(command(tr('Продолжить чтение…'),self.open_resume_read))
        hf_layout.addLayout(hf_tools)
        lf_tools=FlowLayout()
        self.lf_write_button=command(tr('Мастер записи LF…'),self.open_lf_write)
        self.lf_tools_button=command(tr('Инструменты LF…'),self.open_lf_tools)
        self.lf_tools_button.setToolTip(tr('Каталог LF: EM4xxx, HID, Indala, T55xx, Hitag и другие. Просмотр не запускает устройство.'))
        lf_tools.addWidget(self.lf_write_button);lf_tools.addWidget(self.lf_tools_button)
        lf_tools.addWidget(command(say('Мастера карт…','Card wizards…'),lambda:self.open_family_wizard(profile='EM410x')))
        lf_tools.addWidget(command(tr('Эмуляция…'),self.open_emulation));lf_layout.addLayout(lf_tools)
        auto_panel, auto_layout = surface()
        auto_row = QHBoxLayout()
        self.auto_capture_button = command(say('Автоприём HF + LF', 'Automatic intake HF + LF'), lambda:None, True)
        self.auto_capture_button.setCheckable(True)
        self.auto_capture_button.toggled.connect(self.toggle_auto_capture)
        self.auto_capture_button.setToolTip(say('Ищет оба диапазона и сохраняет доступные данные. Неполные дампы сохраняются с отметкой, без повторов, пока карта на антенне. Поиск ключей — из параметров чтения HF.',
            'Scans both bands and saves available data. Partial dumps are marked; a card left on the antenna is saved once. Key recovery follows the HF read settings.'))
        auto_row.addWidget(self.auto_capture_button)
        self.auto_capture_status = text(say('Приложите карту — тип определится автоматически.', 'Present a card — its type is detected automatically.'))
        auto_row.addWidget(self.auto_capture_status, 1)
        auto_layout.addLayout(auto_row)
        self.auto_reset_button = command(say('Питание сброшено…', 'Power was reset…'), self.acknowledge_auto_reset)
        self.auto_reset_button.hide();auto_layout.addWidget(self.auto_reset_button)
        layout.addWidget(auto_panel)
        layout.addWidget(self.workspace_bands);layout.addWidget(result)
        recent_header = QHBoxLayout()
        self.recent_heading=text(tr('Последние HF'), 'section')
        recent_header.addWidget(self.recent_heading, 1)
        recent_header.addWidget(command(tr('Открыть библиотеку'), self.open_band_library))
        recent_header.addWidget(command(tr('Импорт дампа…'), self.import_card))
        layout.addLayout(recent_header)
        self.recent_cards = QListWidget()
        self.recent_cards.setMaximumHeight(116)
        self.recent_cards.setMinimumHeight(85)
        self.recent_cards.itemActivated.connect(self._open_recent)
        layout.addWidget(self.recent_cards)
        self.recent_empty = text(tr('Библиотека пуста. Считайте карту или импортируйте дамп.'))
        layout.addWidget(self.recent_empty)
        layout.addWidget(self.build_firmware_tile())
        self.workspace_bands.currentChanged.connect(self._workspace_band_changed)
        layout.addStretch()
        scroll.setWidget(page)
        return scroll

    def _open_recent(self, item):
        if self._discard_allowed():
            self.load_card(item.data(Qt.ItemDataRole.UserRole))
            self.refresh_library()
            self.navigate(1)

    def _library_page(self):
        page = QWidget()
        layout = column(page)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Поиск карт · название, UID, коллекция, метки  (Ctrl+F)'))
        self.search.textChanged.connect(self.refresh_library)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(command(tr('Импорт…'), self.import_card, True))
        toolbar.addWidget(command(tr('Новая запись'), self.new_card))
        layout.addLayout(toolbar)
        self.library_bands=QTabBar();self.library_bands.setExpanding(False)
        for title,band in [(tr('Все'),'all'),('HF','hf'),('LF','lf'),(tr('Без типа'),'unknown')]:
            index=self.library_bands.addTab(title);self.library_bands.setTabData(index,band)
        self.library_bands.currentChanged.connect(self.refresh_library)
        layout.addWidget(self.library_bands)
        filters = QHBoxLayout()
        self.library_collection_filter = QComboBox()
        self.library_collection_filter.setToolTip(tr('Фильтр по коллекции'))
        self.library_collection_filter.addItem(tr('Все коллекции'), None)
        self.library_collection_filter.addItem(tr('Без коллекции'), '')
        self.library_tag_filter = QComboBox()
        self.library_tag_filter.setToolTip(tr('Фильтр по метке'))
        self.library_tag_filter.addItem(tr('Все метки'), None)
        for widget in (self.library_collection_filter, self.library_tag_filter):
            widget.setMinimumContentsLength(12)
            widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            filters.addWidget(widget, 1)
            widget.currentIndexChanged.connect(self.refresh_library)
        layout.addLayout(filters)
        self.library_count = text('')
        layout.addWidget(self.library_count)
        self.library_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.library_splitter.setChildrenCollapsible(False)
        self.cards = CardList()
        self.cards.setMinimumWidth(190)
        self.cards.setWordWrap(True)
        self.cards.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.cards.currentItemChanged.connect(self._card_selected)
        self.cards.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.cards.customContextMenuRequested.connect(self._card_context)
        self.library_splitter.addWidget(self.cards)
        self.detail_stack = QStackedWidget()
        empty = QWidget()
        empty_layout = column(empty, 24)
        empty_layout.addStretch()
        empty_layout.addWidget(text(tr('Выберите карту'), 'section'))
        empty_layout.addWidget(text(tr('Считайте карту в рабочей области или импортируйте BIN, MFD, EML или JSON.')))
        empty_layout.addStretch()
        self.detail_stack.addWidget(empty)
        detail = QWidget()
        details = column(detail, 8, 8)
        self.card_summary = text('', 'section')
        details.addWidget(self.card_summary)
        self.card_fields = Disclosure(tr('Сведения о карте'), expanded=False)
        self.card_name, self.uid, self.protocol, self.notes = (QLineEdit() for _ in range(4))
        self.uid.setFont(mono_font())
        form = QFormLayout()
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for title, widget in [(tr('Название'), self.card_name), ('UID / ID', self.uid), (tr('Тип'), self.protocol), (tr('Заметки'), self.notes)]:
            form.addRow(title, widget)
            widget.textChanged.connect(self._mark_dirty)
        self.card_collection = QComboBox()
        self.card_collection.setEditable(True)
        self.card_collection.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.card_collection.lineEdit().setMaxLength(96)
        self.card_collection.lineEdit().setPlaceholderText(tr('Выберите или создайте коллекцию'))
        self.card_collection.currentTextChanged.connect(self._mark_dirty)
        self.card_tags = QLineEdit()
        self.card_tags.setMaxLength(32 * 50)
        self.card_tags.setPlaceholderText(tr('Например: тестовая, офис'))
        self.card_tags.setToolTip(tr('До 32 меток, разделённых запятыми. Метки относятся к записи карты.'))
        self.card_tags.textChanged.connect(self._mark_dirty)
        form.addRow(tr('Коллекция'), self.card_collection)
        form.addRow(tr('Метки'), self.card_tags)
        self.card_fields.body.addLayout(form)
        details.addWidget(self.card_fields)
        row = QHBoxLayout()
        self.revisions = QComboBox()
        self.revisions.setMinimumContentsLength(12)
        self.revisions.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.revisions.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.revisions.setToolTip(tr('Версия дампа'))
        self.revisions.currentIndexChanged.connect(self._revision_selected)
        row.addWidget(self.revisions, 1)
        row.addWidget(command(tr('Экспорт…'), self.export_card))
        self.card_keys_button=command(tr('Ключи A/B…'), self.show_card_keys)
        row.addWidget(self.card_keys_button)
        details.addLayout(row)
        row = QHBoxLayout()
        self.dump_info = text('')
        row.addWidget(self.dump_info, 1)
        self.save_button = command(tr('Сохранить'), self.save_card, True)
        row.addWidget(self.save_button)
        self.write_card_button=command(tr('Записать на карту…'),self.open_selected_card_write)
        self.write_card_button.setEnabled(False)
        row.addWidget(self.write_card_button)
        self.save_button.setEnabled(False)
        details.addLayout(row)
        self.card_tabs = QTabWidget()
        editor_page = QWidget()
        details.addWidget(self.card_tabs, 1)
        self.card_tabs.addTab(editor_page, tr('Редактор'))
        details = column(editor_page, 0, 8)
        row = QHBoxLayout()
        self.encoding = codecs()
        self.encoding.currentIndexChanged.connect(self._update_decoded)
        row.addWidget(self.encoding)
        row.addStretch()
        row.addWidget(command(tr('В конвертер'), self.copy_to_converter))
        details.addLayout(row)
        row=QHBoxLayout()
        row.addWidget(command(tr('Эмуляция…'),self.open_emulation))
        self.memory_button=command(tr('Карта памяти…'),self.open_memory_map);row.addWidget(self.memory_button)
        self.memory_button.setToolTip(tr('Цветовая карта секторов MIFARE Classic Mini/1K/2K/4K'))
        self.resume_card_button=command(tr('Продолжить чтение…'),self.open_resume_read);row.addWidget(self.resume_card_button);row.addStretch();details.addLayout(row)
        self.card_editors = QSplitter(Qt.Orientation.Horizontal)
        self.card_editors.setChildrenCollapsible(False)
        self.hex_edit, self.decoded = QPlainTextEdit(), QPlainTextEdit()
        self.hex_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.hex_edit.setPlaceholderText('00 01 02 03 …')
        self.decoded.setPlaceholderText(tr('Текст выбранной кодировки'))
        self.hex_edit.textChanged.connect(self._hex_changed)
        self.decoded.textChanged.connect(self._text_changed)
        for title, widget in [(tr('HEX · редактируемая копия'), self.hex_edit), (tr('Текст'), self.decoded)]:
            pane = editor(title, widget)
            pane.setMinimumWidth(130)
            self.card_editors.addWidget(pane)
        self.card_editors.setSizes([420, 320])
        from .editor_ui import open_hex_editor
        details.addWidget(command(tr('Развернуть HEX…'), lambda:open_hex_editor(self)))
        details.addWidget(self.card_editors, 1)
        self.decode_info = text('')
        details.addWidget(self.decode_info)
        hint = text(tr('Изменения сохраняются новой версией.'))
        hint.setToolTip(tr('Правки текста обновляют HEX. Название, UID и тип — поля библиотеки; служебные Card-поля JSON сохраняются из оригинала и не пересчитываются автоматически. BIN/MFD содержит исходные байты, включая возможные заголовки.'))
        details.addWidget(Guidance(self,hint))
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        self.card_editors.setMinimumHeight(140)
        scroll.setWidget(detail)
        self.detail_stack.addWidget(scroll)
        self.library_splitter.addWidget(self.detail_stack)
        self.library_splitter.setStretchFactor(0, 1)
        self.library_splitter.setStretchFactor(1, 3)
        self.library_splitter.setSizes([350, 850])
        layout.addWidget(self.library_splitter, 1)
        return page

    def _card_context(self, point):
        item = self.cards.itemAt(point)
        if not item:
            return
        card = self.library.get_card(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self.cards)
        menu.addAction(tr('Копировать название'), lambda: QApplication.clipboard().setText(card['name']))
        menu.addAction(tr('Копировать UID'), lambda: QApplication.clipboard().setText(card['uid']))
        menu.exec(self.cards.viewport().mapToGlobal(point))

    def _keys_page(self):
        page = QWidget()
        layout = column(page)
        row = QHBoxLayout()
        self.key_search = QLineEdit()
        self.key_search.setPlaceholderText(tr('Поиск ключей · значение, название, источник  (Ctrl+F)'))
        self.key_search.textChanged.connect(self.refresh_keys)
        row.addWidget(self.key_search, 1)
        row.addWidget(command(tr('Добавить'), self.add_key))
        row.addWidget(command(tr('Импорт…'), self.import_keys))
        row.addWidget(command(tr('Экспорт…'), self.export_keys))
        self.flipper_button = command('Flipper USB', self.import_flipper_usb)
        self.flipper_button.setToolTip(tr('Импортировать словарь, кэши и сохранённые карты с подключённого Flipper Zero'))
        row.addWidget(self.flipper_button)
        layout.addLayout(row)
        info = QHBoxLayout()
        self.key_count = text('')
        info.addWidget(self.key_count, 1)
        self.flipper_cancel = command(tr('Остановить загрузку'), self.cancel_flipper_usb)
        self.flipper_cancel.setEnabled(False)
        info.addWidget(self.flipper_cancel)
        layout.addLayout(info)
        self.flipper_info = text('')
        self.flipper_info.hide()
        layout.addWidget(self.flipper_info)
        self.key_table = QTableWidget(0, 5)
        self.key_table.setHorizontalHeaderLabels([tr('Ключ'), tr('Проверка'), tr('Название'), tr('Источник'), tr('Добавлен')])
        configure_table(self.key_table, [150, 110, 200, 240, 195])
        self.key_table.horizontalHeader().setStretchLastSection(False)
        self.key_table.horizontalHeader().setMinimumSectionSize(100)
        self.key_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        install_table_copy(self.key_table)
        layout.addWidget(self.key_table, 1)
        self.keys_empty = text(tr('Ключей пока нет. Добавьте ключ, импортируйте словарь или подключите Flipper.'))
        layout.addWidget(self.keys_empty)
        guide = text(tr('Импортированные ключи — кандидаты.'))
        guide.setToolTip(tr('Импорт: .dic / .dict / .txt, Flipper .nfc / .shd, .cache/<UID>.keys. Экспортирует словарь без привязок UID, секторов и дат. Повторный импорт сохраняет дату первого добавления.'))
        layout.addWidget(Guidance(self,guide))
        return page

    def _antenna_page(self):
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = column(page)
        layout.addWidget(Guidance(self,text(tr('Перед базовым замером снимите карты с антенн и уберите близкий металл.'))))
        row = QHBoxLayout()
        self.antenna_mode = QComboBox()
        for title, data in [(tr('HF · 13,56 МГц'), 'hf'), (tr('LF · 125 кГц'), 95), (tr('LF · 134,83 кГц'), 88), (tr('LF · другая частота'), 'custom')]:
            self.antenna_mode.addItem(title, data)
        self.antenna_mode.currentIndexChanged.connect(self._antenna_mode_changed)
        self.antenna_frequency = QDoubleSpinBox()
        self.antenna_frequency.setRange(47, 600)
        self.antenna_frequency.setDecimals(2)
        self.antenna_frequency.setValue(125)
        self.antenna_frequency.setSuffix(tr(' кГц'))
        self.antenna_frequency.hide()
        self.antenna_samples = QSpinBox()
        self.antenna_samples.setRange(1, 20)
        self.antenna_samples.setValue(5)
        self.antenna_samples.setSuffix(tr(' замеров'))
        self.antenna_measure_button = command(tr('Измерить'), self.start_antenna_measurement, True)
        self.antenna_sweep_button = command(tr('Проверить HF / LF'), lambda: self.start_antenna_measurement(sweep=True))
        self.antenna_stop_button = command(tr('Остановить'), self.cancel_antenna_measurement)
        self.antenna_stop_button.setEnabled(False)
        for widget in (self.antenna_mode, self.antenna_frequency, self.antenna_samples, self.antenna_measure_button, self.antenna_stop_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        self.antenna_repeat = QCheckBox(tr('Наблюдать до 2 минут'))
        row.addWidget(self.antenna_sweep_button)
        row.addWidget(self.antenna_repeat)
        self.antenna_note = QLineEdit()
        self.antenna_note.setPlaceholderText(tr('Заметка: антенна, F / Q, условия'))
        row.addWidget(self.antenna_note, 1)
        layout.addLayout(row)
        metrics = QHBoxLayout()
        self.antenna_metrics = {}
        for name, key in [(tr('HF · 13,56 МГц'), 'hf_v'), (tr('LF · 125 кГц'), 'lf125_v'), (tr('LF · 134,83 кГц'), 'lf134_v')]:
            group = QWidget()
            group_layout = column(group, 8, 4)
            group_layout.addWidget(text(name))
            value = text('—', 'metric')
            self.antenna_metrics[key] = value
            group_layout.addWidget(value)
            metrics.addWidget(group, 1)
        layout.addLayout(metrics)
        self.antenna_details = text(tr('Результаты HF / LF и оценки Q появятся после измерения.'))
        layout.addWidget(self.antenna_details)
        self.antenna_plot = AntennaPlot()
        self.antenna_plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.antenna_plot.setMinimumHeight(260)
        layout.addWidget(self.antenna_plot, 1)
        self.antenna_status = text(tr('Измерение ещё не выполнялось.'))
        layout.addWidget(self.antenna_status)
        row = QHBoxLayout()
        self.antenna_full_range = QCheckBox(tr('Весь диапазон LF · 46,88–600 кГц'))
        self.antenna_full_range.toggled.connect(self.antenna_plot.set_full_range)
        row.addWidget(self.antenna_full_range)
        row.addStretch()
        self.antenna_baseline_button = command(tr('Сохранить эталон'), self.save_antenna_baseline)
        self.antenna_baseline_button.setEnabled(False)
        row.addWidget(self.antenna_baseline_button)
        layout.addLayout(row)
        self.antenna_baseline_label = text(tr('Эталон ещё не сохранён.'))
        layout.addWidget(self.antenna_baseline_label)
        try:
            saved = json.loads((self.data_dir / 'antenna-baseline.json').read_text(encoding='utf-8'))
            if saved.get('version') == 1 and isinstance(saved.get('measurement'), dict):
                self.antenna_baseline = saved
                self.antenna_baseline_label.setText(tr('Эталон: ') + saved.get('created_at', ''))
        except (OSError, ValueError, AttributeError):
            pass
        help_box = Guidance(self,text(tr('Переключатели F (125 / 134) и Q (7 / 14) на LF-плате RDV4.01 меняются вручную. Частота возбуждения не меняет резонанс антенны. HF измеряется на 13,56 МГц. Оценки Q приблизительны; больший Q не всегда улучшает чтение. Нагрузку карты сравнивайте отдельно с замером без карты.')),tr('О частотах и сравнении'))
        layout.addWidget(help_box)
        scroll.setWidget(page)
        return scroll

    def _antenna_mode_changed(self):
        self.antenna_frequency.setVisible(self.antenna_mode.currentData() == 'custom')

    def _encoding_page(self):
        page = QWidget()
        layout = column(page)
        row = QHBoxLayout()
        self.converter_encoding = codecs()
        row.addWidget(text(tr('Кодировка'), wrap=False))
        row.addWidget(self.converter_encoding)
        row.addWidget(command(tr('HEX → текст'), self.convert_hex, True))
        self.converter_encode_button=command(tr('Текст → HEX'), self.convert_text)
        row.addWidget(self.converter_encode_button)
        row.addStretch()
        layout.addLayout(row)
        row=QHBoxLayout()
        row.addWidget(command(tr('Из редактора карты'),self.copy_to_converter))
        self.converter_hide_keys=QCheckBox(tr('Без ключей MF'))
        self.converter_hide_keys.setToolTip(tr('Только просмотр: скрыть поля A/B в трейлерах Classic. При выключении исходный HEX и текст восстанавливаются.'))
        self.converter_hide_keys.toggled.connect(self._toggle_converter_keys)
        row.addWidget(self.converter_hide_keys);row.addStretch()
        self.converter_text_mode=QComboBox()
        for title,mode in [(tr('Строго'),'strict'),(tr('По блокам'),'blocks'),(tr('Текстовые строки'),'strings')]:self.converter_text_mode.addItem(title,mode)
        self.converter_text_mode.setToolTip(tr('По блокам: непечатные байты показаны точками. Текстовые строки: печатные фрагменты от 4 символов. Адреса относятся к исходному дампу; это только просмотр.'))
        self.converter_text_mode.currentIndexChanged.connect(self._converter_mode_changed)
        row.addWidget(self.converter_text_mode);layout.addLayout(row)
        self.converter_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.converter_splitter.setChildrenCollapsible(False)
        self.converter_hex, self.converter_text = QPlainTextEdit(), QPlainTextEdit()
        self.converter_hex.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.converter_hex.setPlaceholderText(tr('Вставьте HEX-байты, например D0 9F D1 80'))
        self.converter_text.setPlaceholderText(tr('Текст выбранной кодировки'))
        for title, widget in [('HEX', self.converter_hex), (tr('Текст'), self.converter_text)]:
            pane = editor(title, widget)
            pane.setMinimumWidth(150)
            self.converter_splitter.addWidget(pane)
        self.converter_splitter.setSizes([520, 520])
        self.converter_hex.textChanged.connect(self._converter_hex_changed)
        self.converter_encoding.currentIndexChanged.connect(self._converter_codec_changed)
        layout.addWidget(self.converter_splitter, 1)
        self.converter_info = text(tr('Переключение кодировки не меняет данные. Преобразование выполняется кнопками.'))
        layout.addWidget(self.converter_info)
        return page

    def _result_changed(self, value):
        self.device_info.setToolTip(value)
        self.global_operation.setToolTip(value)
        active = self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy
        if active:
            self.ui_has_operation = True
        if not active and not self.ui_has_operation:
            self.global_operation.setText(tr('Ожидание действия'))
            self.operation_badge.setText(tr('Ожидание действия'))
            return
        self.global_operation.setText(value if len(value) < 100 else value[:97] + '…')
        # Status text can include a saved result or an external error in either language.
        failure = any(part in value.lower() for part in ('не заверш', 'не удалось', 'ошибка', 'не запущена',
                                                        'incomplete', 'could not', 'error', 'not started', 'failed'))
        self.device_info.setProperty('tone', 'error' if failure else '')
        self.device_info.style().unpolish(self.device_info)
        self.device_info.style().polish(self.device_info)
        self.operation_badge.setText(tr('Операция выполняется') if active else tr('Завершено с ошибкой') if failure else tr('Результат обновлён'))

    def _sync_busy_presentation(self, active):
        for widget in self.hardware_buttons + [self.firmware_button, self.session_button]:
            if widget.property('idle_tip') is None:
                widget.setProperty('idle_tip', widget.toolTip())
            widget.setToolTip(tr('Дождитесь завершения или остановите текущую операцию.') if active else widget.property('idle_tip'))

    def _sync_connection_view(self):
        if not hasattr(self, 'global_connection'):
            return
        profile = 'TCP' if self._transport() == 'tcp' else 'Blue Shark' if self._transport() == 'blueshark' else 'USB'
        port = self._port() or tr('порт не выбран')
        connected = self.runner.session_open
        self.global_connection.setText(f'{tr('Подключено') if connected else tr('Нет подключения')} · {profile} · {port}')
        self.global_connection.setToolTip(self.connection_status.text())

    def _restore_presentation(self):
        self.geometry_restored = False
        state = self.settings.get('ui', {})
        if not isinstance(state, dict):
            return
        for name, target in [('geometry', self), ('docks', self), ('library', self.library_splitter), ('card_editors', self.card_editors), ('converter', self.converter_splitter), ('key_header', self.key_table.horizontalHeader())]:
            value = state.get(name)
            if isinstance(value, str) and len(value) < 20000:
                try:
                    data = QByteArray.fromHex(value.encode('ascii'))
                    if name == 'geometry':
                        self.geometry_restored = target.restoreGeometry(data)
                    else:
                        target.restoreState(data)
                except (ValueError, UnicodeError):
                    pass

    def _save_presentation(self):
        state = dict(self.settings.get('ui', {})) if isinstance(self.settings.get('ui'), dict) else {}
        for name, target in [('geometry', self), ('docks', self), ('library', self.library_splitter), ('card_editors', self.card_editors), ('converter', self.converter_splitter), ('key_header', self.key_table.horizontalHeader())]:
            state[name] = bytes(target.saveGeometry() if name == 'geometry' else target.saveState()).hex()
        # Only UI fields are persisted here. Unsaved connection edits remain unsaved.
        try:
            current = json.loads(self.settings_path.read_text(encoding='utf-8')) if self.settings_path.exists() else {}
            current = current if isinstance(current, dict) else {}
            current['ui'] = state
            target = self.settings_path.with_suffix('.ui.tmp')
            target.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
            target.replace(self.settings_path)
            self.settings['ui'] = state
        except (OSError, ValueError):
            pass
