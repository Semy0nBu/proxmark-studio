from __future__ import annotations
from .i18n import tr, display_key_source, display_key_label, display_revision_note

import argparse
import json
import sys
import sqlite3
import re
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QInputDialog, QLabel, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QTableWidget,
    QAbstractItemView, QDialog, QDialogButtonBox,
)

from .core import DumpData, Library, decode_bytes, encode_text, export_dump, parse_dump, parse_hex
from .device import (ClientRunner, DeviceSettings, available_ports, discover_client,
                     parse_connection_status, operation_requires_resync, operation_supports_bulk_cancel)
from .keys import KeyLibrary, format_key_timestamp
from .operations import (Operation, CLASSIC_GEOMETRIES, identity_dump, parse_detection,
                         parse_found_keys, key_file_for_session, validate_key_file,
                         OPERATION_TIMEOUT_MS, RECOVERY_OPERATION_KINDS, validate_read_dump)
from .antenna import AntennaControllerMixin
from .recovery import RecoveryPlan, write_complete_key_file
from .flipper import available_flippers, fetch_key_files
from .firmware_ui import FirmwareControllerMixin
from .presentation import PresentationMixin, configure_table, install_table_copy, SortableItem, timestamp_order, notice
from .theme import STYLE, apply_theme, mono_font
from .partial import partial_classic, partial_key_file, partial_ultralight, partial_prompt
from .write_ui import HFWriteControllerMixin
from .hf_tools_ui import HFToolsControllerMixin
from .read_ui import PartialReadControllerMixin
from .encoding_view import without_classic_keys,classic_view_positions,readable_preview
from .card_band import card_band
from .lab_ui import LabControllerMixin
from .studio_controller import StudioControllerMixin
from .auto_capture_ui import AutoCaptureMixin
from .auto_capture import transport_failed as search_transport_failed
from .connection_checks import transport_never_opened, stop_cleanup_version_checks
from .i18n import LanguageControllerMixin,set_language,LANGUAGES
from .paths import application_root, default_data_dir, is_frozen
from .version import APP_NAME, APP_VERSION
from .library_backup_ui import LibraryBackupMixin
from .interface_mode import InterfaceModeMixin,MODES

ROOT = application_root()



def label(text: str, kind: str = "muted", wrap: bool = True) -> QLabel:
    result = QLabel(text)
    result.setObjectName(kind)
    result.setWordWrap(wrap)
    result.setTextFormat(Qt.TextFormat.PlainText)
    return result


def format_hex(data: bytes, block_size: int = 16) -> str:
    width = max(1, min(block_size, 32))
    return "\n".join(data[i:i + width].hex(" ").upper() for i in range(0, len(data), width))


class FlipperFetchWorker(QThread):
    progress = Signal(str)
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, port: str, destination: Path, parent=None):
        super().__init__(parent)
        self.port = port
        self.destination = destination

    def run(self):
        try:
            report = fetch_key_files(self.port, self.destination, progress=self.progress.emit,
                                    cancelled=self.isInterruptionRequested)
            self.completed.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(InterfaceModeMixin, LibraryBackupMixin, PresentationMixin, LanguageControllerMixin, AutoCaptureMixin, StudioControllerMixin, LabControllerMixin, PartialReadControllerMixin, HFWriteControllerMixin, HFToolsControllerMixin, FirmwareControllerMixin, AntennaControllerMixin, QMainWindow):
    interface_mode_changed=Signal(str)
    def __init__(self, data_dir: Path,language=None):
        super().__init__()
        self.data_dir = data_dir
        self.project_root = ROOT
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.library = Library(self.data_dir / "library.sqlite3")
        self.key_library = KeyLibrary(self.data_dir / "library.sqlite3")
        self.settings_path = self.data_dir / "settings.json"
        self.settings = self._load_settings()
        self.interface_mode=self.settings['interface_mode']
        self.ui_language=set_language(language or self.settings.get('language','ru'))
        self._active_transport = self.settings['transport']
        self.port_profiles = {'usb': self.settings['usb_port'], 'blueshark': self.settings['bluetooth_port'], 'tcp': self.settings.get('tcp_endpoint', '')}
        self.runner = ClientRunner(self)
        self.card_id: str | None = None
        self.loaded_dump: DumpData | None = None
        self.loaded_revision: str | None = None
        self.dirty = False
        self.loading = False
        self.text_error = ""
        self.converter_key_view=None
        self.converter_origin=None
        self.converter_text_snapshot=None
        self.current_command = ""
        self.diagnostic_error_codes = []
        self.operation_output = ""
        self.current_session: Path | None = None
        self.read_job: dict | None = None
        self.auto_intake = None
        self.antenna_job: dict | None = None
        self.antenna_last_result = None
        self.antenna_series = []
        self.antenna_baseline = None
        self.antenna_timer = QTimer(self)
        self.antenna_timer.setSingleShot(True)
        self.antenna_timer.timeout.connect(self._run_antenna_batch)
        self.detected = None
        self.flipper_worker: FlipperFetchWorker | None = None
        self.setWindowTitle("Proxmark Studio · RDV4.01")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name('app-icon.svg'))))
        self.resize(1440, 930)
        self.setMinimumSize(920, 480)
        self._build()
        from .workspace_bridge import StudioAdapterBridge
        from .card_workspace import CardWorkspace
        self.workspace_bridge = StudioAdapterBridge(self)
        self.card_workspace = CardWorkspace(self, self.workspace_bridge)
        self.card_tabs.insertTab(0, self.card_workspace.passport, tr('Паспорт'))
        self.card_tabs.insertTab(1, self.card_workspace.changes, tr('Изменения'))
        self.card_tabs.addTab(self.card_workspace.result, tr('Результат операции'))
        self.card_tabs.setCurrentIndex(0)
        self.runner.output.connect(self._log)
        self.runner.busy_changed.connect(self._busy_changed)
        self.runner.completed.connect(self._step_completed)
        self.runner.session_changed.connect(self._session_changed)
        from .reconnect import IdleReconnect
        self.reconnector=IdleReconnect(self)
        self.runner.idle_link_lost.connect(self.reconnector.lost)
        self.refresh_ports()
        self.refresh_library()
        self.refresh_keys()
        self.navigate(0)
        self._sync_connection_view()
        self.statusBar().setToolTip(str(self.data_dir))

    def _suggest_client(self):
        return discover_client(ROOT)

    def _load_settings(self) -> dict:
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
            value = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            value = {}
        # Migrate in memory. Existing files are written only by Save settings.
        def port(key, default=''):
            result = value.get(key, default)
            return result.strip() if isinstance(result, str) else ''
        value['transport'] = value.get('transport') if value.get('transport') in ('usb', 'blueshark', 'tcp') else 'usb'
        legacy_port = port('port')
        value['usb_port'] = port('usb_port', legacy_port if value['transport'] == 'usb' else '')
        value['bluetooth_port'] = port('bluetooth_port', legacy_port if value['transport'] == 'blueshark' else '')
        value['language']=value.get('language') if isinstance(value.get('language'),str) and value.get('language') in LANGUAGES else 'ru'
        value['interface_mode']=value.get('interface_mode') if value.get('interface_mode') in MODES else 'guided'
        if is_frozen() and value.get('client_path') and not Path(str(value['client_path'])).is_file():
            value['client_path'] = discover_client(ROOT)
        return value

    def refresh_keys(self, *_):
        all_rows = self.key_library.list_keys()
        query = self.key_search.text().strip().casefold()
        def matches(entry):
            values = [entry['key_hex']]
            for origin in (entry, *entry.get('origins', [])):
                values.extend((origin['label'], origin['source'],
                               display_key_label(origin['label'], origin['source']),
                               display_key_source(origin['source'])))
            return query in ' '.join(values).casefold()
        rows = [entry for entry in all_rows if not query or matches(entry)]
        total = len(all_rows)
        self._key_count_text = tr('Ключей: {0} · найдено по фильтру: {1}', f'{total}', f'{len(rows)}')
        self.key_count.setText(self._key_count_text)
        if not hasattr(self, '_key_fill_timer'):
            self._key_fill_timer = QTimer(self)
            self._key_fill_timer.timeout.connect(self._fill_key_rows)
        self._key_fill_timer.stop()
        self._key_rows, self._key_fill_index = rows, 0
        self.key_table.setSortingEnabled(False)
        self.key_table.setRowCount(0)
        self.keys_empty.setVisible(not rows)
        self.keys_empty.setText(tr('По этому запросу ключей нет.') if total else tr('Ключей пока нет. Добавьте ключ или импортируйте словарь.'))
        self._fill_key_rows()
        if hasattr(self, 'studio') and self.studio.dump is not None:self.studio.show_key_overview()

    def _fill_key_rows(self):
        rows = self._key_rows
        start = self._key_fill_index
        end = min(start + 250, len(rows))
        mono = mono_font()
        self.key_table.setUpdatesEnabled(False)
        self.key_table.setRowCount(end)
        try:
            for index in range(start, end):
                entry = rows[index]
                values = [entry['key_hex'], tr('С карты') if entry.get('verified') else tr('Кандидат'),
                          display_key_label(entry['label'], entry['source']), display_key_source(entry['source']),
                          format_key_timestamp(entry.get('created_at'))]
                for col, value in enumerate(values):
                    item = SortableItem(str(value))
                    if col == 0:
                        item.setFont(mono)
                    if col == 1:
                        item.setToolTip(tr('Клиент подтвердил значение на карте: аутентификация или чтение доступного поля.') if entry.get('verified') else tr('Импортированное или добавленное вручную значение; карта его ещё не подтвердила.'))
                    elif col == 4:
                        item.setToolTip(tr('Первое добавление ключа. Исходная дата UTC / ISO: ') + str(entry.get('created_at') or tr('неизвестна')))
                        item.setData(Qt.ItemDataRole.UserRole, timestamp_order(entry.get('created_at')))
                    else:
                        item.setToolTip(str(value))
                    self.key_table.setItem(index, col, item)
        finally:
            self.key_table.setUpdatesEnabled(True)
        self._key_fill_index = end
        if end < len(rows):
            self.key_count.setText(self._key_count_text + tr(' · загружено {0}/{1}', f'{end}', f'{len(rows)}'))
            self._key_fill_timer.start(0)
        else:
            self._key_fill_timer.stop()
            self.key_table.setSortingEnabled(True)
            self.key_count.setText(self._key_count_text)

    def show_card_keys(self):
        if not self.card_id:
            return
        uid = self.library.get_card(self.card_id)['uid']
        entries = self.key_library.list_card_keys(uid) if uid else []
        dialog = QDialog(self)
        dialog.setWindowTitle(tr('Ключи A/B · {0}', f"{uid or tr('UID не указан')}"))
        screen = self.screen().availableGeometry()
        dialog.resize(min(940, screen.width()-40), min(520, screen.height()-60))
        layout = QVBoxLayout(dialog)
        hint = label(tr('Ключи по секторам'))
        hint.setToolTip(tr('«Добавлен» — первое появление этой связи UID / сектор / A-B / ключ. «Проверен» — последнее подтверждение клиентом. Читаемый Key B может хранить данные и не подходить для аутентификации.'))
        layout.addWidget(hint)
        table = QTableWidget(len(entries), 7)
        table.setHorizontalHeaderLabels([tr('Сектор'), tr('Тип'), tr('Ключ'), tr('Статус'), tr('Добавлен'), tr('Проверен'), tr('Источник')])
        configure_table(table, [70, 60, 150, 100, 195, 195, 200])
        install_table_copy(table)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        header = table.horizontalHeader()
        header.setMinimumSectionSize(36)
        table.horizontalHeaderItem(4).setToolTip(tr('Первая запись этой связи с UID и сектором; дата не меняется при повторном импорте.'))
        table.horizontalHeaderItem(5).setToolTip(tr('Последнее подтверждение значения клиентом. Импорт сам по себе не является проверкой.'))
        for index, entry in enumerate(entries):
            values = [f"{entry['sector']:02d}", entry.get('type', entry.get('key_type', '?')), entry['key_hex'],
                      tr('С карты') if entry.get('verified') else tr('Кандидат'), format_key_timestamp(entry.get('created_at')),
                      format_key_timestamp(entry.get('verified_at')), display_key_source(entry['source'])]
            for column, value in enumerate(values):
                item = SortableItem(str(value))
                if column == 2:
                    item.setFont(mono_font())
                if column in (4, 5):
                    field = 'created_at' if column == 4 else 'verified_at'
                    item.setData(Qt.ItemDataRole.UserRole, timestamp_order(entry.get(field)))
                    item.setToolTip(str(table.horizontalHeaderItem(column).toolTip()) + tr('\nИсходная дата UTC / ISO: ') + str(entry.get(field) or tr('неизвестна')))
                else:
                    item.setToolTip(str(table.horizontalHeaderItem(column).text()) + ': ' + str(value))
                table.setItem(index, column, item)
        table.setSortingEnabled(True)
        if not entries:
            layout.addWidget(label(tr('Для этого UID сохранённых ключей пока нет.')))
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def add_key(self):
        value, accepted = QInputDialog.getText(self, tr('Ключ MIFARE Classic'), tr('Ключ из 12 HEX-символов (6 байт):'))
        if accepted and value.strip():
            try:
                result = self.key_library.add_keys([value], label=tr('Добавлен вручную'), source="manual")
                self.refresh_keys()
                self.statusBar().showMessage(tr('Добавлено: {0}, уже было: {1}', f"{result['added']}", f"{result['duplicates']}"), 6000)
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.error(exc)

    def import_keys(self):
        filenames, _ = QFileDialog.getOpenFileNames(self, tr('Импорт ключей Flipper / MIFARE'), str(ROOT), tr('Ключи (*.nfc *.shd *.keys *.dic *.dict *.txt);;Все файлы (*)'))
        if not filenames:
            return
        added = duplicates = 0
        errors = []
        for filename in filenames:
            try:
                report = self.key_library.import_file(Path(filename))
                added += report['added']
                duplicates += report['duplicates']
            except (ValueError, OSError, sqlite3.Error) as exc:
                errors.append(f"{Path(filename).name}: {exc}")
        self.refresh_keys()
        self.statusBar().showMessage(tr('Импорт ключей: добавлено {0}, повторов {1}.', f'{added}', f'{duplicates}'), 10000)
        if errors:
            self.error("\n".join(errors))

    def export_keys(self):
        filename, _ = QFileDialog.getSaveFileName(self, tr('Экспорт словаря для Flipper Zero / Proxmark'), str(self.data_dir / "mf_classic_dict_user.nfc"), tr('Словарь Flipper (*.nfc);;Словарь Proxmark (*.dic)'))
        if filename:
            try:
                self.key_library.export_dictionary(Path(filename), flipper=Path(filename).suffix.lower() == '.nfc')
                self.statusBar().showMessage(tr('Словарь экспортирован: {0}', f'{filename}'), 6000)
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.error(exc)

    def import_flipper_usb(self):
        if self.flipper_worker is not None:
            return
        try:
            devices = available_flippers()
            if not devices:
                raise ValueError(tr('Flipper не найден по USB. Подключите его и разблокируйте экран устройства.'))
            if len(devices) == 1:
                port = devices[0]['device']
            else:
                port, accepted = QInputDialog.getItem(self, tr('Выберите Flipper'), tr('USB-порт:'), [device['device'] for device in devices], 0, False)
                if not accepted:
                    return
            destination = self.data_dir / 'flipper-imports' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8])
            self.flipper_info.show()
            self.flipper_worker = FlipperFetchWorker(port, destination, self)
            self.flipper_worker.progress.connect(self.flipper_info.setText)
            self.flipper_worker.completed.connect(self._flipper_downloaded)
            self.flipper_worker.failed.connect(self._flipper_failed)
            self.flipper_worker.finished.connect(self._flipper_thread_finished)
            self.flipper_button.setEnabled(False)
            self.flipper_cancel.setEnabled(True)
            self.flipper_info.setText(tr('Загрузка с Flipper на {0}…', f'{port}'))
            self.flipper_worker.start()
        except (ValueError, OSError) as exc:
            self.error(exc)

    def cancel_flipper_usb(self):
        if self.flipper_worker:
            self.flipper_worker.requestInterruption()
            self.flipper_info.setText(tr('Останавливаю загрузку…'))

    def _flipper_downloaded(self, report: dict):
        added = duplicates = imported = skipped = 0
        errors = list(report.get('errors', []))
        for item in report.get('files', []):
            path = Path(item['local'])
            try:
                text = path.read_text(encoding='utf-8-sig')
                if 'Filetype: Flipper NFC device' in text and not any(line.strip().lower() == 'device type: mifare classic' for line in text.splitlines()):
                    skipped += 1
                    continue
                result = self.key_library.import_file(path)
                added += result['added']
                duplicates += result['duplicates']
                imported += 1
            except (ValueError, OSError, sqlite3.Error) as exc:
                errors.append({'remote': item.get('remote', path.name), 'error': str(exc)})
        summary = {'files_imported': imported, 'keys_added': added, 'duplicate_keys': duplicates,
                   'unsupported_card_files': skipped, 'errors': errors, 'download': report}
        if self.flipper_worker:
            (self.flipper_worker.destination / 'import-report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        self.refresh_keys()
        message = tr('Flipper: загружено файлов {0}, импортировано {1}; новых ключей {2}, повторов {3}, других типов карт {4}.', f"{len(report.get('files', []))}", f'{imported}', f'{added}', f'{duplicates}', f'{skipped}')
        if errors:
            message += tr(' Ошибок: {0}; подробности в import-report.json.', f'{len(errors)}')
        self.flipper_info.setText(message)
        self.statusBar().showMessage(message, 20000)

    def _flipper_failed(self, message: str):
        self.flipper_info.setText(tr('Загрузка с Flipper не завершена: {0}', f'{message}'))

    def _flipper_thread_finished(self):
        worker = self.flipper_worker
        self.flipper_worker = None
        if worker:
            worker.deleteLater()
        self.flipper_button.setEnabled(True)
        self.flipper_cancel.setEnabled(False)

    def error(self, exc: Exception | str):
        QMessageBox.warning(self, tr('Не удалось выполнить действие'), str(exc))

    def _mark_dirty(self, *_):
        if not self.loading and self.card_id:
            self.dirty = True
            self.save_button.setEnabled(True)
            if hasattr(self, 'card_workspace'):self.card_workspace.schedule()

    def _discard_allowed(self) -> bool:
        if not self.dirty:
            return True
        result = QMessageBox.question(self, tr('Есть несохранённые изменения'), tr('Сохранить изменения перед переходом?'), QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)
        if result == QMessageBox.StandardButton.Cancel:
            return False
        if result == QMessageBox.StandardButton.Save:
            return self.save_card()
        if self.card_id:
            self.load_card(self.card_id)
            self.refresh_library()
        return True

    def _refresh_library_organization(self):
        options = self.library.organization_options()
        for combo, entries in ((self.library_collection_filter,
                [(tr('Все коллекции'), None), (tr('Без коллекции'), '')] + [(value, value) for value in options['collections']]),
                (self.library_tag_filter, [(tr('Все метки'), None)] + [(value, value) for value in options['tags']])):
            value = combo.currentData()
            blocked = combo.blockSignals(True)
            combo.clear()
            for title, data in entries:combo.addItem(title, data)
            index = combo.findData(value)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(blocked)
        # Rebuilding the suggestions must not overwrite an unsaved collection.
        value = self.card_collection.currentText()
        blocked = self.card_collection.blockSignals(True)
        self.card_collection.clear()
        self.card_collection.addItem('')
        self.card_collection.addItems(options['collections'])
        self.card_collection.setEditText(value)
        self.card_collection.blockSignals(blocked)

    def refresh_library(self, *_):
        self._refresh_library_organization()
        cards = self.library.list_cards(self.search.text(),
            collection=self.library_collection_filter.currentData(), tag=self.library_tag_filter.currentData())
        all_cards = self.library.list_cards()
        band=self.library_bands.tabData(self.library_bands.currentIndex())
        cards=[card for card in cards if band=='all' or card['rf_band']==band]
        self.library_count.setText(tr('Карт: {0} · по фильтру: {1} · HF: {2} · LF: {3} · без типа: {4}', f'{len(all_cards)}', f'{len(cards)}', f"{sum((c['rf_band'] == 'hf' for c in all_cards))}", f"{sum((c['rf_band'] == 'lf' for c in all_cards))}", f"{sum((c['rf_band'] == 'unknown' for c in all_cards))}"))
        self.cards.blockSignals(True)
        self.cards.clear()
        selected = None
        for card in cards:
            item = QListWidgetItem(tr('{0}\n{1} · {2} Б\nUID: {3}', f"{card['name']}", f"{card['protocol'] or tr('Тип не указан')}", f"{card['size']}", f"{card['uid'] or '—'}"))
            if card.get('partial_version'):
                item.setText(tr('НЕПОЛНЫЙ ДАМП · ')+item.text())
            organization = []
            if card['collection']:organization.append(card['collection'])
            if card['tags']:organization.append(', '.join(card['tags']))
            if organization:item.setText(item.text() + '\n' + ' · '.join(organization))
            item.setToolTip(item.text())
            item.setData(Qt.ItemDataRole.UserRole, card["id"])
            self.cards.addItem(item)
            if card["id"] == self.card_id:
                selected = item
        self.cards.setCurrentItem(selected)
        self.cards.blockSignals(False)
        self.cards.fit_items()
        self.recent_cards.clear()
        workspace_band='hf' if self.workspace_bands.currentIndex()==0 else 'lf'
        recent=[card for card in all_cards if card['rf_band']==workspace_band]
        self.recent_heading.setText(tr('Последние {0}',workspace_band.upper()))
        for card in recent[:3]:
            item = QListWidgetItem(f"{card['name']} · {card['uid'] or tr('UID не указан')}")
            item.setData(Qt.ItemDataRole.UserRole, card['id'])
            item.setToolTip(card['name'] + '\n' + card['protocol'] + '\n' + card['uid'])
            self.recent_cards.addItem(item)
        self.recent_cards.setVisible(bool(recent))
        self.recent_empty.setText(tr('Сохранённых {0}-карт пока нет.',workspace_band.upper()))
        self.recent_empty.setVisible(not recent)

    def _workspace_band_changed(self,*_):
        if hasattr(self,'library_bands'):self.refresh_library()

    def open_band_library(self):
        band='hf' if self.workspace_bands.currentIndex()==0 else 'lf'
        self.library_bands.setCurrentIndex(next(i for i in range(self.library_bands.count()) if self.library_bands.tabData(i)==band))
        self.navigate(1)

    def open_selected_card_write(self):
        if self.loaded_dump and card_band({'protocol':self.protocol.text()},self.loaded_dump.metadata)=='lf':self.open_lf_write()
        else:self.open_write_dialog()

    def _card_selected(self, item, previous):
        if not item:
            return
        card_id = item.data(Qt.ItemDataRole.UserRole)
        if card_id == self.card_id:
            return
        if not self._discard_allowed():
            self.cards.blockSignals(True)
            self.cards.setCurrentItem(previous)
            self.cards.blockSignals(False)
            return
        self.load_card(card_id)
        self.refresh_library()

    def load_card(self, card_id: str, revision_id=None):
        self.loading = True
        try:
            card = self.library.get_card(card_id)
            self.card_id = card_id
            self.card_name.setText(card["name"])
            self.uid.setText(card["uid"])
            self.protocol.setText(card["protocol"])
            self.notes.setText(card["notes"])
            self.card_collection.setEditText(card['collection'])
            self.card_tags.setText(', '.join(card['tags']))
            self.card_summary.setText(card["name"])
            self.card_summary.setToolTip(card["name"])
            self.revisions.blockSignals(True)
            self.revisions.clear()
            for rev in self.library.list_revisions(card_id):
                time = format_key_timestamp(rev['created_at'])
                title = tr('{0} · {1} Б · {2}', f'{time}', f"{rev['size']}", display_revision_note(rev['note']))
                self.revisions.addItem(title, rev["id"])
                self.revisions.setItemData(self.revisions.count()-1, title, Qt.ItemDataRole.ToolTipRole)
            self.revisions.setCurrentIndex(max(0, self.revisions.findData(revision_id)) if revision_id else 0)
            self.revisions.blockSignals(False)
            self._load_revision(self.revisions.currentData())
            self.detail_stack.setCurrentIndex(1)
        finally:
            self.loading = False
        self.dirty = False
        self.save_button.setEnabled(False)
        for widget in (self.write_card_button,self.device_write_button):
            widget.setEnabled(not self.runner.busy and self.read_job is None and self.antenna_job is None and not self.firmware_busy)
        if hasattr(self, 'card_workspace'):self.card_workspace.schedule()

    def _load_revision(self, revision_id: str):
        self.loaded_revision = revision_id
        self.loaded_dump = self.library.get_dump(self.card_id, revision_id)
        band=card_band(self.library.get_card(self.card_id),self.loaded_dump.metadata)
        current=self.library_bands.tabData(self.library_bands.currentIndex())
        if current not in ('all',band):
            self.library_bands.setCurrentIndex(next(i for i in range(self.library_bands.count()) if self.library_bands.tabData(i)==band))
        self.write_card_button.setText(tr('Запись LF…') if band=='lf' else tr('Записать на карту…'))
        self.card_keys_button.setVisible(band!='lf')
        self.memory_button.setEnabled(band!='lf' and self.loaded_dump.block_size==16 and len(self.loaded_dump.data) in {v[1] for v in CLASSIC_GEOMETRIES.values()})
        self.resume_card_button.setVisible(band!='lf')
        self.hex_edit.setPlainText(format_hex(self.loaded_dump.data, self.loaded_dump.block_size))
        self.dump_info.setText(tr('{0} байт · {1} · блок {2} Б', f'{len(self.loaded_dump.data)}', f'{self.loaded_dump.format.upper()}', f'{self.loaded_dump.block_size}'))
        if self.loaded_dump.metadata.get('record_kind')=='identifier':self.dump_info.setText(self.dump_info.text()+tr(' · LF · только идентификатор'))
        partial = self.loaded_dump.metadata.get('partial_dump')
        if partial:
            self.dump_info.setText(self.dump_info.text() + tr(' · НЕПОЛНЫЙ: {0} блоков с пропусками', f"{len(partial['incomplete_blocks'])}"))
            self.dump_info.setToolTip(tr('Не считаны блоки: ') + ', '.join(map(str,partial['missing_blocks'])) + tr('\nНеизвестные байты заполнены нулями; это не считанные значения.'))
        self._update_decoded()

        identifier=self.loaded_dump.metadata.get('record_kind')=='identifier'
        self.card_editors.widget(1).setVisible(not identifier)
        self.encoding.setVisible(not identifier);self.decode_info.setVisible(not identifier)

    def _revision_selected(self, index: int):
        if self.loading or index < 0 or not self.card_id:
            return
        revision_id = self.revisions.itemData(index)
        if not self._discard_allowed():
            self.revisions.blockSignals(True)
            self.revisions.setCurrentIndex(self.revisions.findData(self.loaded_revision))
            self.revisions.blockSignals(False)
            return
        # Saving can repopulate the list. Restore the user's requested revision by ID.
        self.revisions.blockSignals(True)
        self.revisions.setCurrentIndex(self.revisions.findData(revision_id))
        self.revisions.blockSignals(False)
        self.loading = True
        self._load_revision(revision_id)
        self.loading = False
        self.dirty = False
        self.save_button.setEnabled(False)

        if hasattr(self, 'card_workspace'):self.card_workspace.schedule()

    def _hex_changed(self):
        self._mark_dirty()
        self._update_decoded()
        self._annotate_partial_bytes()

    def _update_decoded(self, *_):
        self.text_error = ""
        self.encoding.setEnabled(True)
        self.hex_edit.setReadOnly(False)
        self.decoded.blockSignals(True)
        try:
            data = parse_hex(self.hex_edit.toPlainText())
            text = decode_bytes(data, self.encoding.currentData())
            self.decoded.setPlainText(text)
            self.decoded.setReadOnly(False)
            # QTextDocument normalizes CR/newline characters; require a lossless round trip.
            lossless = encode_text(self.decoded.toPlainText(), self.encoding.currentData()) == data
            notice(self.decode_info, tr('{0} байт · строгое декодирование', f'{len(data)}') if lossless else tr('Есть управляющие символы: текст показан только для просмотра.'))
            self.decoded.setReadOnly(not lossless)
        except (ValueError, UnicodeError, LookupError) as exc:
            self.decoded.setPlainText("")
            self.decoded.setReadOnly(True)
            notice(self.decode_info, tr('Не удалось прочитать в этой кодировке: {0}', f'{exc}'), error=True)
        finally:
            self.decoded.blockSignals(False)

    def _text_changed(self):
        try:
            data = encode_text(self.decoded.toPlainText(), self.encoding.currentData())
            self.text_error = ""
            self.encoding.setEnabled(True)
            self.hex_edit.setReadOnly(False)
            self.hex_edit.blockSignals(True)
            self.hex_edit.setPlainText(format_hex(data, self.loaded_dump.block_size if self.loaded_dump else 16))
            self.hex_edit.blockSignals(False)
            self._annotate_partial_bytes()
            notice(self.decode_info, tr('{0} байт · текст обновил HEX. Проверьте размер перед экспортом.', f'{len(data)}'))
        except (ValueError, UnicodeError, LookupError) as exc:
            self.text_error = str(exc)
            self.encoding.setEnabled(False)
            self.hex_edit.setReadOnly(True)
            notice(self.decode_info, tr('Исправьте текст: {0}', f'{exc}'), error=True)
        self._mark_dirty()

    def copy_to_converter(self):
        try:dump=self._working_dump()
        except ValueError as exc:
            notice(self.converter_info,str(exc),error=True)
            return
        self.converter_hide_keys.setChecked(False)
        self.converter_hex.setPlainText(self.hex_edit.toPlainText())
        self.converter_encoding.setCurrentIndex(self.encoding.currentIndex())
        self.converter_origin={'data':dump.data,'block_size':dump.block_size,
                               'kind':dump.metadata.get('read_validation',{}).get('kind'),
                               'filetype':dump.metadata.get('source_json',{}).get('FileType')}
        self.converter_text.clear()
        self.convert_hex()
        self.navigate(4)

    def _converter_hex_changed(self):
        if self.converter_key_view is None:
            self.converter_origin=None
            if self.converter_text_mode.currentData()!='strict':
                self.converter_text.clear();self.convert_hex()

    def _toggle_converter_keys(self,enabled):
        if enabled:
            try:
                data=parse_hex(self.converter_hex.toPlainText())
                origin=self.converter_origin or {}
                hints={k:origin[k] for k in ('block_size','kind','filetype')} if origin.get('data')==data else {}
                filtered,sectors=without_classic_keys(data,**hints)
            except ValueError as exc:
                blocked=self.converter_hide_keys.blockSignals(True);self.converter_hide_keys.setChecked(False);self.converter_hide_keys.blockSignals(blocked)
                notice(self.converter_info,str(exc),error=True)
                return
            self.converter_key_view={'hex':self.converter_hex.toPlainText(),'text':self.converter_text.toPlainText(),
                                     'encoding':self.converter_encoding.currentIndex(),'info':self.converter_info.text(),
                                     'error':self.converter_info.property('tone')=='error','sectors':sectors,
                                     'data':data,'positions':classic_view_positions(len(data))[0],
                                     'mode':self.converter_text_mode.currentIndex()}
            self.converter_hex.setReadOnly(True);self.converter_text.setReadOnly(True);self.converter_encode_button.setEnabled(False)
            self.converter_hex.setPlainText(format_hex(filtered))
            if self.converter_text_mode.currentData()=='strict':self.converter_text_mode.setCurrentIndex(self.converter_text_mode.findData('blocks'))
            self.converter_text.clear();self.convert_hex()
            return
        state=self.converter_key_view
        if state is None:return
        # Restore both editing buffers exactly, including invalid/unsaved text.
        self.converter_hex.setPlainText(state['hex']);self.converter_text.setPlainText(state['text'])
        blocked=self.converter_encoding.blockSignals(True);self.converter_encoding.setCurrentIndex(state['encoding']);self.converter_encoding.blockSignals(blocked)
        blocked=self.converter_text_mode.blockSignals(True);self.converter_text_mode.setCurrentIndex(state['mode']);self.converter_text_mode.blockSignals(blocked)
        self.converter_key_view=None
        strict=self.converter_text_mode.currentData()=='strict'
        self.converter_hex.setReadOnly(False);self.converter_text.setReadOnly(not strict);self.converter_encode_button.setEnabled(strict)
        notice(self.converter_info,state['info'],error=state['error'])

    def _converter_codec_changed(self,*_):
        if self.converter_key_view is not None or self.converter_text_mode.currentData()!='strict':
            self.converter_text.clear();self.convert_hex()

    def _converter_mode_changed(self,*_):
        strict=self.converter_text_mode.currentData()=='strict'
        locked=self.converter_key_view is not None or not strict
        self.converter_text.setReadOnly(locked);self.converter_encode_button.setEnabled(not locked)
        if self.converter_key_view is None:
            if not strict and self.converter_text_snapshot is None:
                self.converter_text_snapshot={'hex':self.converter_hex.toPlainText(),'text':self.converter_text.toPlainText(),
                                              'encoding':self.converter_encoding.currentIndex(),'info':self.converter_info.text()}
            elif strict and self.converter_text_snapshot is not None:
                snapshot=self.converter_text_snapshot;self.converter_text_snapshot=None
                if snapshot['hex']==self.converter_hex.toPlainText() and snapshot['encoding']==self.converter_encoding.currentIndex():
                    self.converter_text.setPlainText(snapshot['text']);notice(self.converter_info,snapshot['info']);return
        self.converter_text.clear();self.convert_hex()

    def _working_dump(self) -> DumpData:
        if self.text_error:
            raise ValueError(tr('Текст не помещается в выбранную кодировку: {0}', f'{self.text_error}'))
        if self.loaded_dump is None:
            raise ValueError(tr('Сначала выберите запись.'))
        data = parse_hex(self.hex_edit.toPlainText())
        partial=self.loaded_dump.metadata.get('partial_dump')
        if partial and len(data)!=partial['expected_bytes']:
            raise ValueError(tr('Размер неполного дампа зафиксирован картой пропусков; меняйте байты без изменения длины.'))
        return DumpData(data=data, format=self.loaded_dump.format, metadata=self.loaded_dump.metadata, block_size=self.loaded_dump.block_size)

    def save_card(self) -> bool:
        if not self.card_id:
            return False
        try:
            name = self.card_name.text().strip()
            if not name:
                raise ValueError(tr('Укажите название карты.'))
            collection = self.library.normalize_collection(self.card_collection.currentText())
            tags = self.library.normalize_tags(self.card_tags.text())
            dump = self._working_dump()
            changed = dump.data != self.loaded_dump.data
            selected_revision = self.loaded_revision
            if changed:
                self.library.save_revision(self.card_id, dump, tr('Изменение в редакторе'))
            self.library.update_card(self.card_id, name=name, uid=self.uid.text().strip(), protocol=self.protocol.text().strip(), notes=self.notes.text(), collection=collection, tags=tags)
            self.load_card(self.card_id, None if changed else selected_revision)
            self.refresh_library()
            self.statusBar().showMessage(tr('Изменения сохранены. Предыдущие версии доступны в истории.'), 6000)
            return True
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            self.error(exc)
            return False

    def import_card(self):
        if not self._discard_allowed():
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr('Импортировать дамп карты'), str(ROOT), tr('Дампы (*.json *.bin *.mfd *.eml);;Все файлы (*)'))
        if not filename:
            return
        try:
            path = Path(filename)
            dump = parse_dump(path)
            metadata = dump.metadata
            source = metadata.get("source_json", metadata.get("json", metadata))
            card = source.get("Card", {}) if isinstance(source, dict) else {}
            uid = str(card.get("UID", card.get("CSN", ""))) if isinstance(card, dict) else ""
            if metadata.get('partial_dump'):
                uid=metadata['partial_dump']['uid']
            protocol = str(source.get("FileType", "")) if isinstance(source, dict) else ""
            card_id = self.library.create_card(path.stem, dump, uid=uid, protocol=protocol, notes=tr('Импорт: {0}', f'{path.name}'))
            self.load_card(card_id)
            self.refresh_library()
            self.navigate(1)
        except (OSError, ValueError, KeyError) as exc:
            self.error(exc)

    def new_card(self):
        if not self._discard_allowed():
            return
        name, ok = QInputDialog.getText(self, tr('Новая запись'), tr('Название карты:'))
        if ok and name.strip():
            try:
                card_id = self.library.create_card(name.strip(), DumpData(bytes(16), "bin", {}, 16), notes=tr('Новая запись: 16 нулевых байт, не дамп физической карты.'))
                self.load_card(card_id)
                self.refresh_library()
                self.navigate(1)
            except (OSError, ValueError) as exc:
                self.error(exc)

    def export_card(self):
        if not self.card_id:
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr('Экспорт редактируемой копии'), str(self.data_dir / ("dump." + (self.loaded_dump.format if self.loaded_dump.format in {"json", "bin", "eml", "mfd"} else "bin"))), tr('JSON Proxmark3 (*.json);;Поток байтов BIN (*.bin);;EML (*.eml);;MFD (*.mfd)'))
        if filename:
            try:
                export_dump(self._working_dump(), Path(filename))
                self.statusBar().showMessage(tr('Экспортировано: {0}', f'{filename}'), 6000)
            except (OSError, ValueError) as exc:
                self.error(exc)

    def convert_hex(self):
        try:
            data = parse_hex(self.converter_hex.toPlainText())
            mode=self.converter_text_mode.currentData()
            if mode!='strict':
                original=self.converter_key_view['data'] if self.converter_key_view else data
                positions=self.converter_key_view['positions'] if self.converter_key_view else None
                # Preview large pasted files without filling a Qt document with millions of rows.
                preview=original[:262144]
                output,count=readable_preview(preview,self.converter_encoding.currentData(),positions=positions,strings_only=mode=='strings')
                self.converter_text.setPlainText(output or tr('Печатных строк длиной от 4 символов не найдено.'))
                description=tr('Печатных строк: {0} · от 4 символов', f'{count}') if mode=='strings' else tr('По блокам · нулевые области свёрнуты')
                suffix=(tr(' · ключи A/B скрыты в {0} секторах', f"{self.converter_key_view['sectors']}") if self.converter_key_view else '')
                if len(original)>len(preview):suffix+=tr(' · показаны первые 256 КиБ')
                notice(self.converter_info,description+tr(' · адреса исходного дампа')+suffix)
                return
            self.converter_text.setPlainText(decode_bytes(data, self.converter_encoding.currentData()))
            suffix=(tr(' · ключи A/B скрыты в {0} секторах · только просмотр', f"{self.converter_key_view['sectors']}") if self.converter_key_view else tr('. Преобразование строгое, без подстановки символов.'))
            notice(self.converter_info, tr('Прочитано {0} байт', f'{len(data)}')+suffix)
        except (ValueError, UnicodeError, LookupError) as exc:
            notice(self.converter_info, tr('Не удалось преобразовать HEX: {0}', f'{exc}'), error=True)

    def convert_text(self):
        if self.converter_key_view is not None or self.converter_text_mode.currentData()!='strict':
            notice(self.converter_info,tr('Для редактирования выберите «Строго» и выключите «Без ключей MF».'),error=True)
            return
        try:
            data = encode_text(self.converter_text.toPlainText(), self.converter_encoding.currentData())
            self.converter_hex.setPlainText(format_hex(data))
            notice(self.converter_info, tr('Закодировано {0} байт.', f'{len(data)}'))
        except (ValueError, UnicodeError, LookupError) as exc:
            notice(self.converter_info, tr('Не удалось закодировать текст: {0}', f'{exc}'), error=True)

    def browse_client(self):
        filename, _ = QFileDialog.getOpenFileName(self, tr('Выберите официальный клиент'), self.client_path.text() or str(ROOT), tr('Клиент Proxmark3 (proxmark3.exe);;Все файлы (*)'))
        if filename:
            self.client_path.setText(filename)

    def refresh_ports(self):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            return
        previous = self._port()
        current = previous or self.port_profiles[self._active_transport]
        if self._active_transport == 'tcp':
            from .studio_data import say
            placeholder = 'tcp:192.168.1.10:18888'
            help_text = say('Введите адрес совместимого сетевого моста Proxmark вручную. Blue Shark использует Bluetooth COM.',
                            'Enter a compatible Proxmark network bridge address manually. Blue Shark uses a Bluetooth COM port.')
            blocked = self.ports.blockSignals(True)
            self.ports.clear();self.ports.setEditText(current);self.ports.setPlaceholderText(placeholder)
            self.ports.lineEdit().setPlaceholderText(placeholder)
            self.ports.setToolTip(help_text);self.ports.lineEdit().setToolTip(help_text)
            self.ports.blockSignals(blocked)
            return
        wanted = 'bluetooth' if self._active_transport == 'blueshark' else 'usb'
        blocked = self.ports.blockSignals(True)
        try:
            ports = sorted(available_ports(), key=lambda item: (
                item.get('kind', 'other') != wanted,
                int(item['device'][3:]) if re.fullmatch(r'COM\d+', item['device'], re.I) else 1000000,
                item['device']))
            self.ports.clear()
            for port in ports:
                kind = {'usb': 'USB PM3', 'bluetooth': 'Bluetooth', 'other': tr('Другой порт')}.get(port.get('kind'), tr('Другой порт'))
                self.ports.addItem(f"{port['device']} · {kind}", port["device"])
                self.ports.setItemData(self.ports.count()-1, port['description'], Qt.ItemDataRole.ToolTipRole)
            matching = [port['device'] for port in ports if port.get('kind') == wanted]
            if not current and len(matching) == 1:
                current = matching[0]
            index = self.ports.findData(current)
            if index >= 0:
                self.ports.setCurrentIndex(index)
            else:
                self.ports.setCurrentIndex(-1)
                self.ports.setEditText(current)
            placeholder = tr('Выберите исходящий Bluetooth COM-порт') if wanted == 'bluetooth' else tr('Выберите USB COM-порт')
            self.ports.setPlaceholderText(placeholder)
            self.ports.lineEdit().setPlaceholderText(placeholder)
            self.ports.setToolTip('');self.ports.lineEdit().setToolTip('')
            self.port_profiles[self._active_transport] = current
        except (OSError, ValueError) as exc:
            self.device_info.setText(str(exc))
        finally:
            self.ports.blockSignals(blocked)
        if previous != self._port():
            self._reset_connection_status()

    def _transport(self) -> str:
        return self._active_transport

    def _device_profile(self) -> dict:
        return {'client_path': self.client_path.text().strip(), 'port': self._port(), 'transport': self._transport(),
                'keep_open': self.keep_connection.isChecked()}

    def _transport_changed(self):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            blocked = self.transport_combo.blockSignals(True)
            self.transport_combo.setCurrentIndex(self.transport_combo.findData(self._active_transport))
            self.transport_combo.blockSignals(blocked)
            return
        if hasattr(self,'reconnector'):self.reconnector.cancel()
        self._close_idle_session()
        self.port_profiles[self._active_transport] = self._port()
        self._active_transport = self.transport_combo.currentData()
        blocked = self.ports.blockSignals(True)
        self.ports.setCurrentIndex(-1)
        self.ports.setEditText('')
        self.ports.blockSignals(blocked)
        self._update_connection_help()
        self.refresh_ports()
        self._reset_connection_status()

    def _connection_input_changed(self):
        if hasattr(self,'reconnector'):self.reconnector.cancel()
        self._close_idle_session()
        self.port_profiles[self._active_transport] = self._port()
        self._reset_connection_status()

    def _close_idle_session(self):
        if not self.runner.busy and self.read_job is None and self.antenna_job is None and self.runner.session_open:
            self.runner.disconnect()

    def _keep_connection_changed(self, checked):
        if not checked:
            if hasattr(self,'reconnector'):self.reconnector.cancel()
            self._close_idle_session()

    def toggle_connection(self):
        if self.runner.session_open:
            self.runner.disconnect()
        else:
            self.keep_connection.setChecked(True)
            self.run_command('hw version', 45000)

    def _session_changed(self, connected, message):
        if connected and self._transport()=='usb':
            matches=[p for p in available_ports() if p['device'].upper()==self._port().upper()
                     and p.get('kind')=='usb' and p.get('serial_number')]
            self._connected_usb_identity=matches[0]['serial_number'] if len(matches)==1 else None
        self.session_button.setText(tr('Отключить') if connected else tr('Подключить'))
        role='' if connected else 'primary'
        if self.session_button.objectName()!=role:
            self.session_button.setObjectName(role)
            self.session_button.style().unpolish(self.session_button)
            self.session_button.style().polish(self.session_button)
        self.connection_status.setText(message)
        self._sync_connection_view()
        if not connected and self.read_job and self.read_job.get('job_kind')=='emulation' and self.read_job.get('active_notified') and self.read_job['stage'] in {'emulating','emu_start'}:
            self._lab_uncertain(tr('Связь потеряна. Состояние эмуляции неизвестно; восстановите связь и нажмите «Остановить».'))
        if not self.runner.busy and self.read_job is None and self.antenna_job is None:
            self.device_info.setText(message)

    def _resolve_reconnect_usb(self, profile, serial_number):
        matches=[p for p in available_ports() if p.get('kind')=='usb' and p.get('serial_number')==serial_number]
        if len(matches)!=1:return None
        profile=dict(profile);profile['port']=matches[0]['device']
        blocked=self.ports.blockSignals(True)
        self.ports.setEditText(profile['port']);self.ports.blockSignals(blocked)
        self.port_profiles['usb']=profile['port']
        return profile

    def _reset_connection_status(self):
        self.connection_status.setText(tr('Соединение ещё не проверено.'))
        self._sync_connection_view()
        if hasattr(self, 'firmware_current'):
            self.firmware_current.setText(tr('Версия не проверена'))
        if hasattr(self, 'device_info') and not (self.runner.busy or self.read_job is not None or self.antenna_job is not None):
            self.device_info.setText(tr('Готово к подключению'))

    def _update_connection_help(self):
        if self._active_transport == 'tcp':
            from .studio_data import say
            self.connection_help.setText(say('TCP-транспорт клиента: tcp:host:port. Нужен совместимый сетевой мост Proxmark.', 'Client TCP transport: tcp:host:port. Requires a compatible Proxmark network bridge.'))
            self.transport_combo.setToolTip(self.connection_help.text());self.battery_status.hide();return
        bluetooth = self._active_transport == 'blueshark'
        self.bluetooth_settings_button.setEnabled(bluetooth)
        self.connection_help.setText(
            tr('Нужна OS с BTADDON. Сопрягите PM3_RDV4.0 (заводской PIN 1234) и выберите исходящий COM службы SPP. Внутренний UART Blue Shark: 115200 8N1; скорость виртуального COM не настраивается.')
            if bluetooth else tr('Подключите Proxmark по USB и выберите его COM-порт. USB-порт сохраняется отдельно от Bluetooth.'))
        self.transport_combo.setToolTip(self.connection_help.text())
        self.battery_status.setVisible(bluetooth)

    def open_bluetooth_settings(self):
        if not QDesktopServices.openUrl(QUrl('ms-settings:bluetooth')):
            self.device_info.setText(tr('Не удалось открыть настройки. Откройте Bluetooth в параметрах Windows.'))

    def _port(self) -> str:
        if self.ports.currentIndex() >= 0 and self.ports.currentText() == self.ports.itemText(self.ports.currentIndex()):
            return self.ports.currentData() or ""
        return self.ports.currentText().strip()

    def save_settings(self):
        try:
            self.port_profiles[self._active_transport] = self._port()
            self.settings = dict(self.settings, client_path=self.client_path.text().strip(), port=self._port(),
                                 transport=self._active_transport, usb_port=self.port_profiles['usb'],
                                 bluetooth_port=self.port_profiles['blueshark'], keep_connection=self.keep_connection.isChecked())
            self.settings['tcp_endpoint'] = self.port_profiles['tcp']
            temp = self.settings_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.settings_path)
            self.statusBar().showMessage(tr('Настройки сохранены локально.'), 4000)
        except OSError as exc:
            self.error(exc)

    def run_command(self, command: str, timeout: int):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            self.error(tr('Сначала завершите или остановите считывание.'))
            return
        try:
            self.current_session = self._new_session()
            self._start_step(command, timeout)
            if command == 'hw version':
                self.connection_status.setText(tr('Проверяем соединение…'))
        except (OSError, ValueError) as exc:
            if command == 'hw version':
                self.connection_status.setText(tr('Проверка не запущена: {0}', f'{exc}'))
            self.error(exc)

    def _new_session(self) -> Path:
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8]
        directory = self.data_dir / 'sessions' / name
        directory.mkdir(parents=True)
        return directory

    def _start_step(self, operation: str | Operation, timeout: int = 90000, *, _reconnect=False):
        if self.backup_busy:
            raise ValueError(tr('Дождитесь завершения резервного копирования или восстановления.'))
        if hasattr(self,'reconnector') and not _reconnect:self.reconnector.cancel()
        if self.firmware_busy:
            raise ValueError(tr('Дождитесь завершения прошивки.'))
        snapshot=getattr(self,'client_catalog_snapshot',None)
        if snapshot is not None and isinstance(operation,Operation) and operation.kind in {'expert_tool','hf_tool','lf_tool'}:
            from .client_catalog import confirmed_command,split_command
            command,args=split_command(operation.render())
            confirmed_command(snapshot,self.client_path.text(),command,args)
        self.operation_output = ''
        self.current_command = operation if isinstance(operation, str) else operation.kind
        self.current_step_bulk = (operation in {'hf search','lf search'} if isinstance(operation, str)
                                  else operation_supports_bulk_cancel(operation))
        self.current_step_bulk_failed = False
        self.current_step_requires_resync = (operation in {'hf search','lf search'} if isinstance(operation, str)
                                             else operation_requires_resync(operation))
        active_job = self.read_job if self.read_job is not None else self.antenna_job
        self._step_token = object()
        self._step_job = active_job
        self.runner.completion_token = self._step_token
        if active_job is not None:
            active_job['step_number'] = active_job.get('step_number', 0) + 1
            self.current_log_name = f"{active_job['step_number']:03d}-{self.current_command.replace(' ', '-')}.log"
        else:
            self.current_log_name = self.current_command.replace(' ', '-') + '.log'
        profile = self._device_profile()
        if active_job is not None:
            profile = active_job.setdefault('device_profile', profile)
        self.current_step_profile = dict(profile)
        self.current_step_transport = profile['transport']
        if profile['transport'] == 'blueshark':
            if operation == 'hw version' or (isinstance(operation, Operation) and operation.kind == 'hf14a_select'):
                cleanup_recheck = (operation == 'hw version' and active_job is not None
                    and active_job.get('cleanup_version_recheck_used')
                    and active_job.get('stage') in {'cleanup_confirm','cleanup_version','auto_cleanup_version'})
                if not cleanup_recheck:timeout = max(timeout, 20000)
            elif isinstance(operation, Operation) and operation.kind == 'antenna_field_off':
                timeout = max(timeout, 15000)
        settings = DeviceSettings(profile['client_path'], profile['port'], str(self.current_session), profile['transport'],
                                  profile['keep_open'], bool(active_job and (active_job.get('job_kind') == 'studio' or active_job.get('auto_capture'))))
        self._log(f"\n—— {self.current_command} ——\n")
        if isinstance(operation, str):
            self.runner.run(settings, operation, timeout_ms=timeout)
        else:
            self.runner.run_operation(settings, operation, timeout_ms=timeout)
        description = {
            'hw version': tr('Проверяем соединение…'), 'hf search': tr('Ищем HF-карту…'), 'lf search': tr('Ищем LF-карту…'),
            'hf14a_select': tr('Проверяем карту…'), 'mfc_check_keys': tr('Проверяем известные ключи…'),
            'mfc_dump': tr('Считываем дамп…'), 'mfu_dump': tr('Считываем дамп…'), 'mfc_nonce_probe': tr('Определяем метод поиска…'),
        }.get(self.current_command, tr('Выполняется операция…'))
        self.device_info.setText(description)

    def read_card(self):
        self._begin_card_job(keys_only=False)

    def read_hf_card(self):self._begin_card_job(keys_only=False,band='hf')

    def read_lf_card(self):self._begin_card_job(keys_only=False,band='lf')

    def check_card_keys(self):
        self._begin_card_job(keys_only=True,band='hf')

    def _begin_card_job(self, *, keys_only: bool,band='auto'):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            self.error(tr('Дождитесь завершения текущей операции.'))
            return
        if not self._discard_allowed():
            return
        try:
            self.current_session = self._new_session()
            self.read_job = {'stage': 'identify_lf' if band=='lf' else 'identify', 'keys_only': keys_only, 'session': self.current_session,'band':band,
                             'recover': self.recovery_enabled.isChecked()}
            self._start_step('lf search' if band=='lf' else 'hf search',120000 if band=='lf' else 90000)
        except (OSError, ValueError) as exc:
            self._card_job_failed(exc)

    def cancel_operation(self):
        if hasattr(self,'reconnector'):self.reconnector.cancel()
        if self.read_job and self.read_job.get('stage') in {'cancel_wait','cancel_resync','cancel_resync_failed'}:
            if self.runner.busy:
                # Keep the same job/token while its cancellation is pending.
                self.runner.cancel()
                return
            # There can be no later completion when the client is already idle.
            self.resync_after_cancel = False
            self.read_job['stage'] = 'cancel_resync'
            try:self._start_step('hw version', 20000)
            except (OSError, ValueError) as exc:self._hold_cancel_resync(exc)
            return
        if self._auto_owned():
            self._stop_auto_capture();return
        if self.read_job and self.read_job.get('job_kind') == 'studio':
            self._cancel_studio();return
        if self.read_job and self.read_job.get('job_kind') in {'emulation','lf_write'}:
            self._cancel_lab_job();return
        if self.read_job and 'partial_reader' in self.read_job:
            self.read_job['partial_cancelled']=True
            if self.runner.busy:self.runner.cancel()
            else:
                self.read_job['partial_reader'].stop(tr('Остановлено пользователем; неподтверждённые блоки отброшены.'))
                try:self._next_partial_read()
                except (ValueError,OSError) as exc:self._card_job_failed(exc)
            return
        if self.read_job and self.read_job.get('job_kind')=='write':
            self.read_job['write_cancelled']=True
            try:
                self.read_job['write_plan'].fail(tr('Отменено пользователем; отправленные записи могли изменить карту.'))
            except OSError as exc:
                self._log(tr('Не удалось обновить отчёт отмены: ')+str(exc)+'\n')
            if self.runner.busy:
                self.runner.cancel()
            else:
                self._end_write(False,tr('Запись остановлена. Отчёт и исходные изменяемые блоки сохранены.'))
            return
        if self.antenna_job is not None:
            self.cancel_antenna_measurement()
            return
        operation = self.read_job.get('recovery_operation') if self.read_job else None
        self.resync_after_cancel = (getattr(self, 'resync_after_cancel', False)
                                   or bool(operation and operation_requires_resync(operation))
                                   or bool((self.runner.busy or self.read_job is not None) and
                                           getattr(self, 'current_step_requires_resync', False)))
        if self.read_job and 'recovery_plan' in self.read_job:
            try:
                self._write_recovery_report(self.read_job, 'cancelled')
            except OSError as exc:
                self._log(tr('[!] Не удалось обновить отчёт: {0}\n', f'{exc}'))
        self.read_job = ({'stage':'cancel_wait','session':self.current_session,
                          'device_profile':dict(self.current_step_profile)}
                         if self.resync_after_cancel else None)
        if self.runner.busy:
            # This is an intentional ownership transfer of this exact step,
            # not a new operation. Its matching completion must reach cleanup.
            self._step_job = self.read_job
            self.runner.cancel()
        elif self.resync_after_cancel:
            self.resync_after_cancel = False
            self.read_job['stage'] = 'cancel_resync'
            try:self._start_step('hw version', 20000)
            except (OSError, ValueError) as exc:self._hold_cancel_resync(exc)
        else:
            self._step_token = None
            self._step_job = None
        self._busy_changed(self.runner.busy)

    def _card_job_failed(self, exc):
        if self.read_job and self.read_job.get('stage') in {'cancel_wait','cancel_resync','cancel_resync_failed'}:
            self._hold_cancel_resync(exc);return
        if self._auto_owned():
            self._auto_read_failed(exc);return
        if self.read_job and self.read_job.get('job_kind') == 'studio':
            self._studio_failed(exc);return
        if self.read_job and self.read_job.get('job_kind') in {'emulation','lf_write'}:
            self._lab_failed(exc);return
        if self.read_job and self.read_job.get('job_kind')=='write':
            self._write_failed(exc)
            return
        if self.read_job and 'recovery_plan' in self.read_job:
            try:
                self._write_recovery_report(self.read_job, 'failed', str(exc))
            except OSError:
                pass
        if self.read_job and getattr(self,'current_step_bulk_failed',False):
            self.read_job = {'stage':'cancel_resync','session':self.current_session,
                             'device_profile':dict(self.current_step_profile),'operation_error':str(exc)}
            self._busy_changed(self.runner.busy)
            try:self._start_step('hw version',20000)
            except (OSError,ValueError) as check_error:self._hold_cancel_resync(check_error)
            self.error(exc)
            return
        self.read_job = None
        self._busy_changed(self.runner.busy)
        self.device_info.setText(tr('Считывание не завершено: {0}', f'{exc}'))
        self.error(exc)

    def _hold_cancel_resync(self, reason):
        self.resync_after_cancel = False
        self.read_job.update(stage='cancel_resync_failed', resync_error=str(reason))
        if not self.runner.busy:self.runner.disconnect()
        message = tr('Связь после остановки не подтверждена. Операции заблокированы. Нажмите «Остановить», чтобы повторить проверку связи.')
        self._busy_changed(self.runner.busy)
        self.device_info.setText(message)
        self.error(message+'\n'+str(reason))

    def _show_detection(self, detection):
        self.detected = detection
        if detection.uid:
            size = f" · {detection.classic_size.upper()}" if detection.classic_size else ''
            band='LF' if detection.family=='em410x' else 'HF'
            self.detection_label.setText(f"{band} · {detection.protocol}{size} · {detection.uid}")
            self.workspace_bands.setCurrentIndex(1 if band=='LF' else 0)
            self.detection_label.setToolTip(detection.reason)
            if detection.family == 'mfu':
                self.read_options.setExpanded(True)
        else:
            self.detection_label.setText(detection.reason)

    def _advance_read(self, output: str, code: int):
        if self._route_auto_reply(output, code):return
        if self.read_job.get('job_kind') == 'studio':
            self._advance_studio(output, code);return
        job = self.read_job
        if job.get('job_kind') in {'emulation','lf_write'}:
            self._advance_lab_job(output,code);return
        if job.get('job_kind')=='write':
            self._advance_write(output,code)
            return
        stage = job['stage']
        if stage=='resume_select':
            actual=parse_detection(output)
            if code or actual.uid!=job['detection'].uid or actual.ambiguous_identity:raise ValueError(tr('UID не совпал со снимком продолжения. Чтение не запущено.'))
            job['stage']='info';self._advance_read(output,0);return
        if stage=='partial_read':
            self._advance_partial_read(output,code)
            return
        if stage in {'recovery_resync', 'cancel_resync'}:
            connection = parse_connection_status(
                output, self.current_step_transport, code,
                verified_transport=self.runner.connection_transport)
            if not connection['connected']:
                if stage == 'cancel_resync':
                    self._hold_cancel_resync(connection['message']);return
                raise ValueError(tr('Клиент остановлен, но устройство не подтвердило выход из операции. Коротко нажмите кнопку Proxmark (или перезапустите питание), затем повторите проверку связи.'))
            if stage == 'cancel_resync':
                self.read_job = None
                self._busy_changed(False)
                self.device_info.setText((tr('Считывание не завершено: {0}',job['operation_error'])+' '+tr('Устройство отвечает.'))
                    if job.get('operation_error') else tr('Операция отменена. Устройство отвечает; найденные ранее ключи сохранены.'))
            else:
                job['stage'] = 'recovery_resync_select'
                self._start_step(Operation('hf14a_select'), 8000)
            return
        if stage == 'recovery_resync_select':
            if code or parse_detection(output).uid != job['detection'].uid:
                raise ValueError(tr('После прерывания тот же UID не подтверждён. Проверьте карту и положение на антенне.'))
            self._next_recovery_step()
            return
        if stage in {'identify', 'identify_lf'}:
            detected = parse_detection(output)
            self._show_detection(detected)
            # PM3_ESOFT (-10) is the native search result when no tag is found.
            # Accept only its explicit, band-specific absence line. A timeout,
            # empty response or mixed transport error must retain its real code.
            from .operations import _text
            frequency = r'13\.56\s+MHz' if stage == 'identify' else r'125/134\s+kHz'
            no_card = (code in {0, -10} and not detected.uid and not detected.ambiguous_identity
                       and not search_transport_failed(output, code) and re.search(
                           r'^\s*(?:\[[!+=-]+\]\s*)?No known(?:/supported)?\s+' + frequency +
                           r'\s+tags found[!.]?\s*$', _text(output), re.I | re.M))
            if stage == 'identify' and job.get('band','auto')=='auto' and no_card:
                job['stage'] = 'identify_lf'
                self._start_step('lf search', 120000)
                return
            if no_card:
                raise ValueError(detected.reason or tr('Карта не обнаружена. Проверьте положение на антенне.'))
            if code:
                raise ValueError(tr('Команда обнаружения карты прервана (код {0}). Проверьте связь с платой; причина обмена записана в журнале.', f'{code}'))
            if not detected.supported:
                raise ValueError(detected.reason or tr('Этот тип карты пока не поддерживается для считывания.'))
            if job.get('band')=='lf' and detected.family!='em410x' or job.get('band')=='hf' and detected.family=='em410x':
                raise ValueError(tr('Ответ не соответствует выбранному диапазону HF/LF. Используйте инструменты нужного семейства.'))
            job['detection'] = detected
            if detected.family == 'mfc':
                job['stage'] = 'info'
                self._start_step(Operation('hf14a_select'))
                return
            if job['keys_only']:
                raise ValueError(tr('Библиотека ключей A/B предназначена для MIFARE Classic.'))
            if detected.family == 'mfu':
                job['stage'] = 'dump'
                self._start_step(Operation('mfu_dump', output_prefix=job['session'] / 'card', auth_key=self.mfu_auth.text().strip()), 120000)
                return
            if detected.family == 'em410x':
                job['stage'] = 'lf_identifier'
                self._start_step(Operation('lf_em410x_read'), 45000)
                return
            raise ValueError(tr('Для этого семейства ещё нет профиля считывания.'))
        no_dictionary_key = stage == 'keys' and re.search(r'\bNo keys found\b', output, re.IGNORECASE)
        if stage == 'dump':
            job['dump_output'], job['dump_returncode'] = output, code
            if code and job['detection'].family=='mfc' and not (job['session']/'card.json').is_file():
                self._log(tr('[Чтение] Клиент не сохранил дамп. Переходим к адресному чтению с промежуточным сохранением.\n'))
                self._begin_partial_read()
                return
            job['stage'] = 'post_select'
            self._start_step(Operation('hf14a_select'))
            return
        if stage=='post_select':
            post_identity=parse_detection(output)
            if post_identity.ambiguous_identity or post_identity.uid and post_identity.uid!=job['detection'].uid:
                raise ValueError(tr('После чтения UID изменился или обнаружены несколько карт. Дамп не сохранён.'))
        if stage=='post_select' and (code or not post_identity.uid):
            if not job.get('post_select_retry'):
                job['post_select_retry']=True
                self.device_info.setText(tr('Финальная проверка карты не ответила. Повторяем только проверку UID…'))
                QTimer.singleShot(1000,lambda:self._retry_post_select(job))
                return
            raise ValueError(tr('Не удалось подтвердить UID после чтения. Проверьте соединение с платой и положение карты. Файлы сеанса сохранены: {0}. Дамп не приписан непроверенной карте.', f"{job['session']}"))
        if code and stage != 'recover' and not no_dictionary_key:
            raise ValueError(tr('Клиент завершил этап {0} с кодом {1}. Смотрите журнал.', f'{stage}', f'{code}'))
        detected = job['detection']
        if stage == 'info':
            info = parse_detection(output)
            if info.ambiguous_identity or info.uid != detected.uid:
                raise ValueError(tr('UID изменился или карта снята. Операция остановлена.'))
            keys = self.key_library.list_keys()
            if job.get('resume_dump'):
                from .operations import sector_trailer
                prior=job['resume_dump'];bitmap=set(prior.metadata['partial_dump']['known_bytes'])
                for sector in range(CLASSIC_GEOMETRIES[detected.classic_size][0]):
                    for off in (0,10):
                        start=sector_trailer(sector)*16+off
                        if all(i in bitmap for i in range(start,start+6)):keys.append({'key_hex':prior.data[start:start+6].hex().upper()})
            dictionary = None
            if keys:
                dictionary = job['session'] / 'known-keys.dic'
                dictionary.write_text('\n'.join(sorted({entry['key_hex'] for entry in keys}))+'\n',encoding='ascii')
            job['dictionary'] = dictionary
            job['checked_candidates'] = [entry['key_hex'] for entry in keys]
            job['stage'] = 'keys'
            self._start_step(Operation('mfc_check_keys', classic_size=detected.classic_size, dictionary_path=dictionary), 180000)
            return
        if stage == 'keys':
            keyfile = key_file_for_session(job['session'], detected.uid)
            parsed = parse_found_keys(output, detected.classic_size)
            if parsed:
                found = validate_key_file(keyfile, output, detected.classic_size)
            elif no_dictionary_key:
                found = []
            else:
                raise ValueError(tr('Нет подтверждённого результата проверки ключей. Подробности в журнале.'))
            if found:
                self.key_library.record_verified_keys(detected.uid, found)
            job['found_keys'] = found
            self.refresh_keys()
            expected = CLASSIC_GEOMETRIES[detected.classic_size][0] * 2
            if len(found) < expected and job.get('recover') and not job.get('recovery_attempted'):
                job['recovery_attempted'] = True
                job['recovery_plan'] = RecoveryPlan(detected.classic_size, found,
                                                   checked_candidates=job.get('checked_candidates', ()))
                job['recovery_events'] = 0
                self._next_recovery_step()
                return
            self._after_card_keys(found, keyfile)
            return
        if stage == 'recover':
            actual = parse_detection(output)
            if actual.ambiguous_identity or (actual.uid and actual.uid != detected.uid):
                raise ValueError(tr('UID изменился при поиске ключей. Результат не сохранён.'))
            # A missing device/serial port is not a failed card key. Stop here
            # instead of opening COM again for every remaining candidate.
            if re.search(r'\bUART\s+CreateFile\s+failed\b|\binvalid\s+serial\s+port\b|'
                         r'\bNo\s+response\s+from\s+Proxmark3\b', output, re.I):
                if self.runner.session_open:
                    self.runner.disconnect()
                raise ValueError(tr('Соединение с платой потеряно; поиск ключей остановлен. Проверьте подключение и повторите проверку связи. Найденные ранее ключи сохранены.'))
            transport_failed = bool(re.search(
                r'command execution time\s*out|received packet.*payload too short|resynchronising', output, re.I))
            if transport_failed:
                code = -5
                job['transport_failures'] = job.get('transport_failures', 0) + 1
            new = job['recovery_plan'].consume(job['recovery_operation'], output, code)
            if new:
                job['pending_keys'] = new
                job['stage'] = 'recovery_identity'
                self._start_step(Operation('hf14a_select'))
            elif transport_failed or (code in {-3, -4} and job['recovery_operation'].kind in RECOVERY_OPERATION_KINDS):
                if job.get('transport_failures', 0) >= 2:
                    raise ValueError(tr('Обмен с платой повторно прервался; новые команды остановлены. Перезапустите питание и проверьте связь. Найденные ключи сохранены.'))
                job['stage'] = 'recovery_resync'
                self._start_step('hw version', 8000)
                self.device_info.setText(tr('Операция прервана. Проверяем отклик платы перед продолжением…'))
            else:
                self._next_recovery_step()
            return
        if stage == 'recovery_identity':
            if parse_detection(output).uid != detected.uid:
                raise ValueError(tr('UID изменился после проверки нового ключа. Результат не сохранён.'))
            for entry in job.pop('pending_keys'):
                self.key_library.record_verified_keys(detected.uid, [entry],
                    source=tr('Proxmark3: адресный сценарий / ') + entry['method'])
            job['found_keys'] = job['recovery_plan'].found
            self.refresh_keys()
            self._next_recovery_step()
            return
        if stage == 'lf_identifier':
            actual = parse_detection(output)
            if actual.family != 'em410x' or actual.uid != detected.uid:
                raise ValueError(tr('Не удалось подтвердить тот же идентификатор EM410x повторным чтением.'))
            self._save_device_dump(identity_dump(actual), actual)
            return
        if stage == 'post_select':
            actual = parse_detection(output)
            if actual.uid != detected.uid:
                raise ValueError(tr('После чтения UID изменился или карта снята. Дамп не будет приписан другой карте.'))
            try:
                if job.get('partial_mode'):
                    raise ValueError(tr('Не все ключи получены; полнота секретных полей не подтверждена.'))
                dump = validate_read_dump(job['session'] / 'card.json', detected, job['dump_output'],
                                          returncode=job.get('dump_returncode',0),confirmed_uid=actual.uid)
            except (ValueError,OSError) as exc:
                if detected.family not in {'mfc','mfu'}:
                    raise
                dump = (partial_classic(job['session']/'card.json',detected,job['dump_output'],actual.uid,job.get('found_keys',[]))
                        if detected.family=='mfc' else partial_ultralight(job['session']/'card.json',detected,job['dump_output'],actual.uid))
                info = dump.metadata['partial_dump']
                answer = (QMessageBox.StandardButton.Save if self._auto_owned() else
                          QMessageBox.question(self,tr('Неполный дамп карты'),partial_prompt(info),
                            QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel))
                if answer != QMessageBox.StandardButton.Save:
                    raise ValueError(tr('Неполный дамп не сохранён. {0}', f'{exc}'))
            if detected.family == 'mfc':
                dump.metadata['read_validation']['known_key_slots'] = job['found_keys']
                dump.metadata['read_validation']['all_key_slots_known'] = len(job['found_keys'])==CLASSIC_GEOMETRIES[detected.classic_size][0]*2
            self._save_device_dump(dump, detected)

    def _next_recovery_step(self):
        job = self.read_job
        plan = job['recovery_plan']
        operation = plan.next_operation()
        for event in plan.events[job['recovery_events']:]:
            self._log(tr('[Сценарий] ') + event + '\n')
        job['recovery_events'] = len(plan.events)
        self._write_recovery_report(job, 'finished' if plan.done else 'running')
        if operation is None:
            found = plan.found
            job['found_keys'] = found
            expected = CLASSIC_GEOMETRIES[plan.classic_size][0] * 2
            keyfile = job['session'] / 'targeted-key.bin'
            if len(found) == expected:
                write_complete_key_file(keyfile, plan.classic_size, found)
            self._after_card_keys(found, keyfile)
            if job['keys_only']:
                self.device_info.setText(tr('Сохранено {0} из {1} ключей. {2}', f'{len(found)}', f'{expected}', f'{plan.stop_reason}'))
            return
        job['stage'] = 'recover'
        job['recovery_operation'] = operation
        self._start_step(operation, OPERATION_TIMEOUT_MS.get(operation.kind, 120000))
        expected = CLASSIC_GEOMETRIES[plan.classic_size][0] * 2
        self.operation_progress.setRange(0, expected)
        self.operation_progress.setValue(len(plan.found))
        self.device_info.setText(tr('{0} / {1} ключей · {2}', f'{len(plan.found)}', f'{expected}', f'{plan.events[-1]}'))

    @staticmethod
    def _write_recovery_report(job, status, error=''):
        plan = job['recovery_plan']
        report = {'uid': job['detection'].uid, 'profile': plan.classic_size,
                  'nonce_kind': plan.nonce_kind, 'done': plan.done, 'status': status,
                  'stop_reason': error or plan.stop_reason, 'events': plan.events,
                  'found_keys': job.get('found_keys', plan.found),
                  'pending_identity_keys': job.get('pending_keys', [])}
        (job['session'] / 'recovery-plan.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def _after_card_keys(self, found: list[dict], keyfile: Path):
        job = self.read_job
        detected = job['detection']
        expected = CLASSIC_GEOMETRIES[detected.classic_size][0] * 2
        if job.get('resume_dump'):
            if not found:raise ValueError(tr('Не подтверждены ключи для продолжения чтения.'))
            self._begin_partial_read();return
        if job['keys_only']:
            self.read_job = None
            self._busy_changed(False)
            self.device_info.setText(tr('Сохранено {0} из {1} ключей A/B по секторам. UID {2}.', f'{len(found)}', f'{expected}', f'{detected.uid}'))
            self.navigate(2)
            return
        slots = {(entry['sector'], entry['type']) for entry in found}
        if len(slots) != expected:
            if not found:
                raise ValueError(tr('Ключей для чтения нет. Найдите или импортируйте хотя бы один ключ; пустой образ не сохраняется.'))
            keyfile = partial_key_file(job['session']/'available-key.bin',detected.classic_size,found)
            job['partial_mode'] = True
            self._begin_partial_read()
            return
        job['stage'] = 'dump'
        self._start_step(Operation('mfc_dump', output_prefix=job['session'] / 'card', classic_size=detected.classic_size, key_file=keyfile), 180000)

    def _retry_post_select(self,job):
        if self.read_job is not job:return
        try:self._start_step(Operation('hf14a_select'))
        except (ValueError,OSError) as exc:self._card_job_failed(exc)

    def _save_device_dump(self, dump, detected):
        if self._auto_owned():
            self._auto_save_dump(dump, detected);return
        dump.metadata['rf_band']='lf' if detected.family=='em410x' else 'hf'
        profile = detected.protocol + (f" · {detected.classic_size.upper()}" if detected.classic_size else '')
        matches = []
        for card in self.library.list_cards():
            if card['uid'] != detected.uid:
                continue
            previous = self.library.get_dump(card['id'])
            validation = previous.metadata.get('read_validation', {})
            same_profile = validation.get('kind') == detected.family
            if detected.family == 'mfc':
                same_profile = same_profile and validation.get('expected_bytes') == CLASSIC_GEOMETRIES[detected.classic_size][1]
            if detected.family == 'em410x':
                same_profile = previous.metadata.get('record_kind') == 'identifier' and previous.metadata.get('protocol') == 'EM410x'
            if same_profile or card['protocol'] == profile:
                matches.append(card)
        note = tr('Считывание с Proxmark3; ') + str(self.current_session)
        if len(matches) == 1:
            card_id = matches[0]['id']
            self.library.save_revision(card_id, dump, tr('Неполное считывание с Proxmark3') if dump.metadata.get('partial_dump') else tr('Повторное считывание с Proxmark3'))
        else:
            card_id = self.library.create_card(f"{profile} · {detected.uid}", dump, uid=detected.uid, protocol=profile, notes=note)
        self.read_job = None
        self._busy_changed(False)
        self.load_card(card_id)
        self.refresh_library()
        self.navigate(1)
        message = tr('Карта сохранена в библиотеку: {0} байт.', f'{len(dump.data)}')
        if dump.metadata.get('partial_dump'):
            message = tr('НЕПОЛНЫЙ дамп сохранён: {0} байт, пропуски отмечены и заполнены нулями.', f'{len(dump.data)}')
        if detected.family == 'em410x':
            message = tr('Идентификатор EM410x сохранён в библиотеку (5 байт ID).')
        self.device_info.setText(message)
        self.statusBar().showMessage(message, 15000)

    def _log(self, text: str):
        self.operation_output += text
        self._lab_output(self.operation_output)
        cursor = self.log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()

    def _busy_changed(self, busy: bool):
        if hasattr(self, 'workspace_bridge'):self.workspace_bridge.result_changed.emit()
        active = busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy or self.backup_busy
        if hasattr(self, 'auto_capture_button'):
            self.auto_capture_button.setEnabled(not active or self._auto_owned() and not self.auto_intake['stopping'])
        if getattr(self, 'family_wizard', None) is not None:self.family_wizard.sync()
        if getattr(self, 'classic_attack_dialog', None) is not None:self.classic_attack_dialog.sync()
        if hasattr(self, 'studio'):self.studio.set_busy(active)
        self.operation_badge.setText(tr('Прошивка…') if self.firmware_busy else tr('Операция выполняется') if active else tr('Ожидание действия'))
        self._sync_connection_view()
        self.operation_progress.setVisible(active and not self.firmware_busy)
        if not self.read_job or self.read_job.get('stage') != 'recover':
            self.operation_progress.setRange(0, 0)
        for item in self.hardware_buttons + [self.browse_button, self.refresh_button, self.settings_button, self.client_path, self.ports, self.mfu_auth, self.recovery_enabled, self.transport_combo, self.keep_connection, self.session_button, self.firmware_variant, self.firmware_button, self.overview_read_button]:
            item.setEnabled(not active)
        self.bluetooth_settings_button.setEnabled(not active and self._transport() == 'blueshark')
        self.detail_stack.setEnabled(self.read_job is None)
        self.stop_button.setEnabled(active and not self.firmware_busy and not self.backup_busy)
        for widget in (self.antenna_sweep_button, self.antenna_measure_button, self.antenna_mode,
                       self.antenna_frequency, self.antenna_samples, self.antenna_repeat, self.antenna_note):
            widget.setEnabled(not active)
        self.antenna_stop_button.setEnabled(self.antenna_job is not None and self.antenna_job['stage'] in {'measure', 'wait'})
        self.antenna_baseline_button.setEnabled(not active and self.antenna_last_result is not None)
        self._sync_busy_presentation(active)
        for widget in (self.write_card_button,self.device_write_button):
            widget.setEnabled(not active and self.loaded_dump is not None)
        self.device_write_button.setEnabled(not active)
        if getattr(self,'hf_tools_dialog',None) is not None:
            self.hf_tools_dialog.set_busy(active)
        if getattr(self,'lf_tools_dialog',None) is not None:
            self.lf_tools_dialog.set_busy(active)

    def _step_completed(self, token, code: int, status: str):
        current = self.read_job if self.read_job is not None else self.antenna_job
        if token is None or token is not getattr(self, '_step_token', None) or current is not self._step_job:
            return
        self._step_token = None
        reconnect_probe=hasattr(self,'reconnector') and self.reconnector.probing and self.reconnector.token is token
        output=self.operation_output
        self._finished(code, status)
        if reconnect_probe:self.reconnector.completed(output,code)

    def _finished(self, code: int, status: str):
        if type(code) is int and code:
            self.diagnostic_error_codes=(getattr(self,'diagnostic_error_codes',[])+[code])[-30:]
        output = self.operation_output
        try:
            if getattr(self, 'current_step_profile', self._device_profile()) == self._device_profile():
                self.update_firmware_badge(output)
            if self.current_session:
                name = getattr(self, 'current_log_name', self.current_command.replace(' ', '-') + '.log')
                try:(self.current_session / name).write_text(output, encoding='utf-8')
                except OSError:
                    if self.read_job and self.read_job.get('job_kind') in {'emulation','lf_write'} and self.read_job['stage'].startswith('cleanup_'):
                        self.read_job['log_warning']=tr('Не удалось сохранить журнал остановки.')
                    else:raise
            absence_frequency = r'13\.56\s+MHz' if self.current_command == 'hf search' else r'125/134\s+kHz'
            explicit_search_absence = (self.current_command in {'hf search','lf search'} and code in {0,-10}
                and not search_transport_failed(output,code)
                and re.search(r'No known(?:/supported)?\s+'+absence_frequency+r'\s+tags found',output,re.I))
            open_failed = transport_never_opened(output)
            bulk_failure = ((code != 0 or search_transport_failed(output,code))
                            and getattr(self,'current_step_bulk',False) and not explicit_search_absence and not open_failed)
            self.current_step_bulk_failed = bulk_failure
            manual_rf_abort = ((code in {-3, -4} or bulk_failure) and self.read_job is None
                               and self.antenna_job is None and not self.firmware_busy
                               and not open_failed
                               and getattr(self, 'current_step_requires_resync', False))
            if getattr(self, 'resync_after_cancel', False) or manual_rf_abort:
                self.resync_after_cancel = False
                self.read_job = {'stage': 'cancel_resync', 'session': self.current_session,
                                 'device_profile': dict(self.current_step_profile)}
                self._busy_changed(self.runner.busy)
                self._start_step('hw version', 8000)
                return
            if self.antenna_job is not None:
                self._advance_antenna(output, code)
            elif self.read_job is not None:
                self._advance_read(output, code)
            else:
                if self.current_command == 'hw version':
                    connection = parse_connection_status(output, getattr(self, 'current_step_transport', self._transport()), code,
                                                         verified_transport=self.runner.connection_transport)
                    detail = connection['message']
                    if connection['connected']:
                        channel = 'TCP' if connection['transport'] == 'tcp' else 'FPC UART' if connection['transport'] == 'blueshark' else 'USB-CDC'
                        detail += f" {channel} · OS: {connection['firmware']}"
                        if self.keep_connection.isChecked() and not self.runner.session_open:
                            detail += tr(' Постоянный сеанс недоступен; клиент закрыт.')
                    profile_changed=getattr(self, 'current_step_profile', self._device_profile()) != self._device_profile()
                    if profile_changed:
                        detail = tr('Настройки подключения изменились во время проверки. Повторите проверку связи.')
                    self.connection_status.setToolTip(detail)
                    self.connection_status.setText((tr('Подключение подтверждено · ') + channel) if connection['connected'] and not profile_changed else detail)
                    self.device_info.setText(tr('Готово к чтению') if connection['connected'] and not profile_changed else detail)
                elif self.current_command in {'hf search', 'lf search'} and code == 0:
                    self._show_detection(parse_detection(output))
                    self.device_info.setText(tr('{0} · код процесса {1}.', f'{status}', f'{code}'))
                elif self.current_command in {'hf_tool','lf_tool'}:
                    band='LF' if self.current_command=='lf_tool' else 'HF'
                    message=tr('Команда {0} завершилась с кодом {1}. Результат и сообщения клиента — в журнале; запись автоматически не проверялась.', f'{band}', f'{code}')
                    self.device_info.setText(message)
                    dialog=getattr(self,band.lower()+'_tools_dialog',None)
                    if dialog is not None:dialog.status.setText(message)
                else:
                    self.device_info.setText(tr('{0} · код процесса {1}.', f'{status}', f'{code}'))
            self._sync_connection_view()
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            if self.antenna_job is not None:
                if self.antenna_job['stage'] == 'measure':
                    self._antenna_cleanup(str(exc))
                else:
                    self.antenna_job['error'] = str(exc)
                    self._end_antenna_job()
            else:
                self._card_job_failed(exc)

    def save_log(self):
        filename, _ = QFileDialog.getSaveFileName(self, tr('Сохранить журнал'), str(self.data_dir / "client-log.txt"), tr('Текст (*.txt)'))
        if filename:
            try:
                Path(filename).write_text(self.log.toPlainText(), encoding="utf-8")
            except OSError as exc:
                self.error(exc)

    def _close_unconfirmed_operation(self, jobs):
        """Exit an idle failed cleanup without claiming the hardware stopped."""
        record = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'reason': 'application-exit-with-unconfirmed-device-state',
                  'device_stop_confirmed': False, 'client_busy': False,
                  'jobs': [{'kind': job.get('job_kind', 'card'), 'stage': job.get('stage', '')} for job in jobs]}
        try:
            path = self.data_dir/'last-exit-status.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            temporary.replace(path)
        except OSError as exc:
            self._log(tr('Не удалось сохранить статус выхода: {0}\n', str(exc)))
        for job in jobs:stop_cleanup_version_checks(job)
        for name in ('antenna_timer', 'studio_timer', 'studio_deadline', 'emulation_timer',
                     'auto_poll_timer', 'auto_card_timer'):
            timer = getattr(self, name, None)
            if timer is not None:timer.stop()
        self._step_token = None
        self.read_job = self.antenna_job = None
        self.auto_intake = None

    def closeEvent(self, event: QCloseEvent):
        if self.backup_busy:
            self._backup_close_requested = True
            self._backup_worker.requestInterruption()
            event.ignore()
            return
        # Help exports and report preparation have their own cancellable host
        # workers. Finish them asynchronously before destroying their parent.
        from .catalog_ui import CatalogDialog
        from .diagnostics_ui import DiagnosticBundleDialog
        pending=[]
        for cls in (CatalogDialog,DiagnosticBundleDialog):
            for dialog in self.findChildren(cls):
                worker=getattr(dialog,'worker',None)
                if worker is not None:
                    pending.append((dialog,worker))
        if pending:
            for dialog,worker in pending:
                if not getattr(worker,'_owner_close_connected',False):
                    worker._owner_close_connected=True
                    worker.finished.connect(lambda:QTimer.singleShot(0,self.close))
                worker.requestInterruption()
                dialog.close()
            event.ignore();return
        if self.firmware_busy:
            QMessageBox.information(self, tr('Идёт прошивка'), tr('Дождитесь завершения записи и проверки прошивки.'))
            event.ignore()
            return
        if self.flipper_worker is not None:
            QMessageBox.information(self, tr('Идёт загрузка Flipper'), tr('Остановите загрузку ключей и дождитесь завершения USB-сеанса.'))
            event.ignore()
            return
        jobs = [job for job in (self.read_job, self.antenna_job) if job is not None]
        failed_cleanup = (not self.runner.busy and bool(jobs)
                          and all(job.get('stage') in {'cancel_resync_failed', 'uncertain'} for job in jobs))
        if failed_cleanup:
            if not self._discard_allowed():
                event.ignore();return
            self._close_unconfirmed_operation(jobs)
        if self._auto_owned():
            self.auto_intake['close_requested'] = True
            self._stop_auto_capture();event.ignore();return
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None:
            QMessageBox.information(self, tr('Идёт операция'), tr('Сначала остановите операцию на экране устройства и дождитесь завершения процесса.'))
            event.ignore()
            return
        if not self._discard_allowed():
            event.ignore()
            return
        self._key_fill_timer.stop()
        self._closing=True
        self.reconnector.cancel()
        self.card_workspace.timer.stop()
        self._save_presentation()
        self.runner.shutdown()
        self.library.close()
        self.key_library.close()
        event.accept()


def create_demo(data_dir: Path):
    library = Library(data_dir / "library.sqlite3")
    try:
        if not library.list_cards():
            data = "Пример данных".encode("cp1251").ljust(48, b" ")
            library.create_card(tr('Демо · текстовая запись'), DumpData(data, "bin", {}, 16), protocol=tr('Синтетический пример'), notes=tr('Только пример интерфейса. Не является дампом физической карты.'))
    finally:
        library.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Proxmark Studio desktop GUI")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--demo", action="store_true", help="Use a separate synthetic demo library")
    parser.add_argument('--language',choices=('ru','en'),help='Override the interface language for this launch')
    parser.add_argument('--version', action='version', version=f'{APP_NAME} {APP_VERSION}')
    parser.add_argument('--self-test', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test is not None:
        from .release_check import run_release_check
        return run_release_check(args.self_test, language=args.language or 'en')
    data_dir = args.data_dir or default_data_dir(demo=args.demo)
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("Proxmark Studio")
    apply_theme(application)
    window = MainWindow(data_dir,language=args.language)
    if args.demo:
        create_demo(data_dir)
        window.refresh_library()
    screen = application.primaryScreen().availableGeometry()
    if not window.geometry_restored:
        window.resize(min(1440, screen.width() - 60), min(930, screen.height() - 70))
    window.show()
    return application.exec()
