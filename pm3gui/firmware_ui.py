"""A review-first, USB-only firmware dialog. Opening it never opens a port."""
from .i18n import tr
from pathlib import Path
import re
from time import monotonic

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
                              QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                              QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)
from .ui import Disclosure, SoftButton
from .theme import mono_font



class FirmwareDialog(QDialog):
    def __init__(self, owner, profile_key):
        super().__init__(owner)
        self.owner = owner
        self.prepared = None
        self.manifest = None
        self.setWindowTitle(tr('Прошивка Proxmark'))
        self.resize(660, 470)
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        heading = QLabel(tr('Основная прошивка · RDV4.01'))
        heading.setObjectName('section')
        layout.addWidget(heading)
        self.variant = QComboBox()
        self.variant.addItem(tr('С Blue Shark · Bluetooth'), 'blueshark')
        self.variant.addItem(tr('Без Blue Shark · USB'), 'usb')
        self.variant.setCurrentIndex(self.variant.findData(profile_key))
        self.port = QLineEdit(owner.port_profiles.get('usb', '') or 'COM3')
        self.port.setPlaceholderText(tr('USB COM-порт'))
        self.version = QLabel('—')
        self.version.setWordWrap(True)
        self.backup = QLabel('—')
        self.backup.setWordWrap(True)
        form = QFormLayout()
        form.setSpacing(10)
        form.addRow(tr('Сборка'), self.variant)
        form.addRow(tr('USB-порт'), self.port)
        form.addRow(tr('Версия'), self.version)
        form.addRow(tr('Резервная копия'), self.backup)
        layout.addLayout(form)
        note = QLabel(tr('Запись выполняется по USB. Загрузчик сохраняется; после записи проверяются прошивка и резервные копии.'))
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.details = Disclosure(tr('Файлы и журнал'))
        self.output = QPlainTextEdit()
        self.output.setFont(mono_font())
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(140)
        self.details.body.addWidget(self.output)
        self.browse = SoftButton(tr('Выбрать другую сборку…'))
        self.browse.clicked.connect(self.choose_manifest)
        self.details.body.addWidget(self.browse)
        layout.addWidget(self.details)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        self.close_button = SoftButton(tr('Закрыть'))
        self.close_button.clicked.connect(self.reject)
        self.flash_button = SoftButton(tr('Прошить по USB'))
        self.flash_button.setObjectName('primary')
        self.flash_button.clicked.connect(lambda: owner.start_firmware_install(self))
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.flash_button)
        layout.addLayout(buttons)
        self.variant.currentIndexChanged.connect(self._variant_changed)
        self.port.textChanged.connect(self.prepare)
        self.prepare()

    def _variant_changed(self):
        self.manifest = None
        self.prepare()

    def choose_manifest(self):
        filename, _ = QFileDialog.getOpenFileName(self, tr('Описание сборки RDV4'), '', tr('Описание сборки (*.json)'))
        if filename:
            self.manifest = Path(filename)
            self.prepare()

    def prepare(self):
        if self.owner.firmware_busy:
            return
        try:
            from .firmware import FirmwareCatalog
            self.prepared = FirmwareCatalog(self.owner.project_root).prepare(
                self.variant.currentData(), self.port.text().strip(),
                backup_root=self.owner.data_dir / 'firmware-backups', manifest_path=self.manifest)
            review = self.prepared.review()
            self.version.setText(str(review['version']))
            self.backup.setText(tr('Память платы + внешняя SPI'))
            self.backup.setToolTip(str(review['backup_directory']))
            self.output.setPlainText('\n'.join([
                tr('Клиент: ') + str(review['client_path']),
                tr('Образ: ') + str(review['image_path']),
                'SHA256: ' + str(review['image_sha256']),
                tr('Резервные копии: ') + str(review['backup_directory']),
            ]))
            self.status.setText(tr('Сборка проверена. Подключите плату по USB перед записью.'))
            self.flash_button.setEnabled(True)
        except (OSError, ValueError, RuntimeError, KeyError, ImportError) as exc:
            self.prepared = None
            self.version.setText(tr('Сборка недоступна'))
            self.backup.setText('—')
            self.status.setText(str(exc))
            self.flash_button.setEnabled(False)

    def set_running(self, running):
        for widget in (self.variant, self.port, self.browse, self.flash_button, self.close_button):
            widget.setEnabled(not running)
        self.progress.setVisible(running)

    def reject(self):
        if self.owner.firmware_busy:
            return
        super().reject()


class FirmwareControllerMixin:
    @property
    def firmware_busy(self):
        return getattr(self, 'firmware_worker', None) is not None or getattr(self, '_firmware_waiting', False)

    def build_firmware_tile(self):
        self.firmware_worker = None
        self._firmware_waiting = False
        self.firmware_dialog = None
        tile = QFrame()
        tile.setObjectName('panel')
        layout = QVBoxLayout(tile)
        layout.setContentsMargins(16, 12, 16, 12)
        heading = QHBoxLayout()
        title = QLabel(tr('Прошивка'))
        title.setObjectName('section')
        heading.addWidget(title)
        heading.addStretch(1)
        self.firmware_current = QLabel(tr('Версия не проверена'))
        self.firmware_current.setObjectName('muted')
        heading.addWidget(self.firmware_current)
        layout.addLayout(heading)
        row = QHBoxLayout()
        self.firmware_variant = QComboBox()
        self.firmware_variant.addItem(tr('С Blue Shark · Bluetooth'), 'blueshark')
        self.firmware_variant.addItem(tr('Без Blue Shark · USB'), 'usb')
        self.firmware_variant.setCurrentIndex(self.firmware_variant.findData(self.settings.get('firmware_profile', self._active_transport)))
        row.addWidget(self.firmware_variant)
        self.firmware_button = SoftButton(tr('Подготовить…'))
        self.firmware_button.clicked.connect(self.open_firmware_dialog)
        row.addWidget(self.firmware_button)
        row.addStretch(1)
        layout.addLayout(row)
        return tile

    def open_firmware_dialog(self):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            self.error(tr('Завершите текущую операцию перед подготовкой прошивки.'))
            return
        self.firmware_dialog = FirmwareDialog(self, self.firmware_variant.currentData())
        self.firmware_dialog.exec()

    def start_firmware_install(self, dialog):
        if dialog is not self.firmware_dialog or dialog.prepared is None or self.firmware_busy or self.runner.busy or self.read_job is not None or self.antenna_job is not None:
            return
        self.reconnector.cancel()
        self._firmware_waiting = True
        self._firmware_result = None
        self._firmware_error = ''
        self._firmware_prepared = dialog.prepared
        dialog.set_running(True)
        dialog.status.setText(tr('Освобождаем соединение с платой…'))
        self.runner.disconnect()
        self._busy_changed(False)
        self._firmware_release_started = monotonic()
        self._firmware_release_timer = QTimer(self)
        release_timer=self._firmware_release_timer
        self._firmware_release_timer.setInterval(100)
        self._firmware_release_timer.timeout.connect(lambda:self._firmware_wait_for_release()
            if self._firmware_release_timer is release_timer else None)
        self._firmware_release_timer.start()

    def _firmware_wait_for_release(self):
        if not self._firmware_waiting or self.firmware_worker is not None:return
        if self.runner._session.has_process:
            if monotonic() - self._firmware_release_started < 8:
                return
            self._firmware_release_timer.stop()
            self._firmware_waiting = False
            self.firmware_dialog.status.setText(tr('Клиент ещё занимает порт. Закройте соединение и повторите подготовку.'))
            self.firmware_dialog.set_running(False)
            self._busy_changed(False)
            return
        self._firmware_release_timer.stop()
        try:
            from .firmware import FirmwareWorker
            self.firmware_worker = FirmwareWorker(self._firmware_prepared, self)
            worker=self.firmware_worker
            self._firmware_waiting = False
            worker.progress.connect(lambda message:self._firmware_progress(message) if self.firmware_worker is worker else None)
            worker.completed.connect(lambda result: setattr(self, '_firmware_result', result) if self.firmware_worker is worker else None)
            worker.failed.connect(lambda message: setattr(self, '_firmware_error', message) if self.firmware_worker is worker else None)
            worker.finished.connect(lambda:self._firmware_finished() if self.firmware_worker is worker else None)
            self.firmware_worker.start()
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            if self.firmware_worker is not None:
                self.firmware_worker.deleteLater()
            self.firmware_worker = None
            self._firmware_waiting = False
            self.firmware_dialog.set_running(False)
            self.firmware_dialog.status.setText(tr('Подготовка не завершена: ') + str(exc))
        self._busy_changed(False)

    def _firmware_progress(self, message):
        self.firmware_dialog.output.appendPlainText(message)
        self.firmware_dialog.status.setText(message.split('\n')[0])

    def _firmware_finished(self):
        worker = self.firmware_worker
        self.firmware_worker = None
        if worker is not None:
            worker.deleteLater()
        dialog = self.firmware_dialog
        dialog.set_running(False)
        result = self._firmware_result
        if result and result.get('verified'):
            self.settings['firmware_profile'] = result['profile']
            self.client_path.setText(result['client_path'])
            self.port_profiles['usb'] = result['port']
            self.transport_combo.setCurrentIndex(self.transport_combo.findData('usb'))
            self.ports.setEditText(result['port'])
            self.save_settings()
            self.firmware_current.setText(result['label'])
            self.firmware_current.setToolTip(result['version'])
            self.firmware_variant.setCurrentIndex(self.firmware_variant.findData(result['profile']))
            dialog.status.setText(tr('Прошивка установлена и проверена. Подключитесь по USB для работы.'))
            dialog.flash_button.setEnabled(False)
        else:
            dialog.status.setText(self._firmware_error or tr('Прошивка не подтверждена. Откройте журнал операции.'))
            dialog.details.setExpanded(True)
            dialog.flash_button.setEnabled(False)
        self._busy_changed(False)

    def update_firmware_badge(self, output):
        version = re.search(r'^\s*OS\.{2,}\s+([^\n]+)', output, re.M)
        capability = re.search(r'FPC USART for BT add-on\.{2,}\s+(present|absent)', output)
        if version and capability:
            self.firmware_current.setText('Blue Shark' if capability.group(1) == 'present' else tr('USB · без Blue Shark'))
            self.firmware_current.setToolTip(version.group(1).strip())
