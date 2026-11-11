"""Review an offline, allowlisted support package before saving it."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout

from .diagnostics import (DiagnosticsCancelled, export_diagnostics, prepare_diagnostics,
                          snapshot_from_owner, validate_destination)
from .i18n import language
from .theme import mono_font
from .ui import SoftButton


def say(ru, en):
    return en if language() == 'en' else ru


class DiagnosticsWorker(QThread):
    prepared = Signal(object)
    failed = Signal()

    def __init__(self, snapshot, client_path, parent=None):
        super().__init__(parent)
        self.snapshot, self.client_path = snapshot, client_path

    def run(self):
        try:
            result = prepare_diagnostics(self.snapshot, self.client_path,
                                         cancelled=self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.prepared.emit(result)
        except DiagnosticsCancelled:
            pass
        except Exception:
            # The underlying exception may include a private path.
            self.failed.emit()
        finally:
            self.client_path = None


class DiagnosticBundleDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.worker, self.prepared = owner, None, None
        self._close_requested = False
        self.setWindowTitle(say('Отчёт для диагностики', 'Diagnostic package'))
        self.resize(740, 570)
        layout = QVBoxLayout(self)
        description = QLabel(say(
            'В архив войдут версии программы и среды, SHA256 клиента, состояние связи и операции, '
            'числовые коды завершения. Ниже показаны все собранные сведения.',
            'The archive includes app and runtime versions, the client SHA256, connection and operation '
            'state, and numeric completion codes. All collected details are shown below.'))
        description.setWordWrap(True)
        description.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(description)
        privacy = QLabel(say(
            'Дампы, ключи, UID, команды, журнал, настройки и личные пути не включаются. '
            'Устройство не опрашивается. Отчёт никуда не отправляется автоматически.',
            'Dumps, keys, card IDs, commands, logs, settings and personal paths are excluded. '
            'The device is not queried. Nothing is uploaded automatically.'))
        privacy.setWordWrap(True)
        privacy.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(privacy)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font())
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.preview, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        self.refresh_button = SoftButton(say('Обновить снимок', 'Refresh snapshot'))
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch()
        self.save_button = SoftButton(say('Сохранить ZIP…', 'Save ZIP…'))
        self.save_button.setObjectName('primary')
        self.save_button.clicked.connect(self.choose_destination)
        self.save_button.setEnabled(False)
        buttons.addWidget(self.save_button)
        self.close_button = SoftButton(say('Закрыть', 'Close'))
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        self.refresh()

    def refresh(self):
        if self.worker is not None:
            return
        self.prepared = None
        self.save_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.preview.clear()
        self.status.setText(say('Готовим снимок состояния…', 'Preparing a state snapshot…'))
        snapshot = snapshot_from_owner(self.owner)
        field = getattr(self.owner, 'client_path', None)
        client_path = field.text().strip() if field is not None and hasattr(field, 'text') else None
        worker = DiagnosticsWorker(snapshot, client_path, self)
        self.worker = worker
        worker.prepared.connect(self._prepared)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        worker.start()

    def _prepared(self, prepared):
        if self._close_requested:
            return
        self.prepared = prepared
        self.preview.setPlainText(prepared.document.decode('utf-8'))
        self.save_button.setEnabled(True)
        self.status.setText(say('Снимок готов. Проверьте содержимое перед сохранением.',
                               'Snapshot ready. Review its contents before saving.'))

    def _failed(self):
        if not self._close_requested:
            self.status.setText(say('Не удалось подготовить отчёт. Попробуйте обновить снимок.',
                                   'Could not prepare the report. Try refreshing the snapshot.'))

    def _finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()
        self.refresh_button.setEnabled(True)
        if self._close_requested:
            super().reject()

    def choose_destination(self):
        if self.prepared is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, say('Сохранить отчёт', 'Save diagnostic package'),
            'ProxmarkStudio-diagnostics-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.zip', 'ZIP (*.zip)')
        if filename:
            target = Path(filename)
            self.save_to(target if target.suffix else target.with_suffix('.zip'))

    def save_to(self, destination):
        if self.prepared is None:
            return None
        active_profile = getattr(self.owner, 'data_dir', None)
        try:
            validate_destination(destination, active_profile=active_profile)
        except (OSError, TypeError, ValueError):
            self.status.setText(say('Выберите ZIP-файл вне папки текущего профиля.',
                                   'Choose a ZIP file outside the current profile folder.'))
            return None
        try:
            result = export_diagnostics(self.prepared, destination, active_profile=active_profile)
        except (OSError, TypeError, ValueError):
            self.status.setText(say('Не удалось сохранить архив. Проверьте папку и свободное место.',
                                   'Could not save the archive. Check the folder and available space.'))
            return None
        self.status.setText(say('Архив сохранён. Его можно отправить вместе с описанием проблемы.',
                               'Archive saved. You can share it with a description of the problem.'))
        return result

    def reject(self):
        if self.worker is not None:
            self._close_requested = True
            self.worker.requestInterruption()
            self.close_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.status.setText(say('Завершаем подготовку…', 'Finishing preparation…'))
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
