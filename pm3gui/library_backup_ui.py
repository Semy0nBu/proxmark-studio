"""Whole-library backup UI. Workers never touch Qt widgets or device transports."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog,
                              QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                              QProgressBar, QTabWidget, QVBoxLayout, QWidget)

from .i18n import tr
from .library_backup import BackupCancelled, BackupError, create_backup, restore_backup, validate_profile
from .paths import default_data_dir
from .ui import SoftButton


class BackupWorker(QThread):
    progress = Signal(str, int, int)
    completed = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, mode, profile, archive, destination=None, password=None, parent=None):
        super().__init__(parent)
        self.mode, self.profile, self.archive = mode, profile, archive
        self.destination, self.password = destination, password

    def run(self):
        try:
            options = dict(password=self.password, cancelled=self.isInterruptionRequested,
                           progress=self.progress.emit)
            if self.mode == 'backup':
                result = create_backup(self.profile, self.archive, **options)
            else:
                result = restore_backup(self.archive, self.destination, active_profile=self.profile, **options)
            self.completed.emit(result)
        except BackupCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.password = None


def button(caption, callback, layout):
    item = SoftButton(caption)
    item.clicked.connect(callback)
    layout.addWidget(item)
    return item


class LibraryBackupDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.worker, self.restored = owner, None, None
        self._close_requested = False
        self.setWindowTitle(tr('Резервная копия библиотеки'))
        self.resize(680, 410)
        layout = QVBoxLayout(self)
        description = QLabel(tr('Карты, все версии, ключи, настройки и файлы профиля — в одном архиве. Сохраняются данные, уже записанные в библиотеку.'))
        description.setWordWrap(True); layout.addWidget(description)
        self.profile_label = QLabel(tr('Текущий профиль: {0}', str(owner.data_dir)))
        self.profile_label.setWordWrap(True); layout.addWidget(self.profile_label)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        backup, restore = QWidget(), QWidget()
        self.tabs.addTab(backup, tr('Создать копию'))
        self.tabs.addTab(restore, tr('Восстановить'))
        form = QFormLayout(backup)
        self.archive = QLineEdit()
        self.archive.setPlaceholderText(tr('Выберите файл .pm3backup'))
        row = QHBoxLayout(); row.addWidget(self.archive)
        button(tr('Обзор…'), self._choose_output, row); form.addRow(tr('Архив'), row)
        self.protect = QCheckBox(tr('Защитить паролем (AES-256)')); form.addRow(self.protect)
        self.password, self.confirm = QLineEdit(), QLineEdit()
        for field in (self.password, self.confirm):field.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(tr('Пароль'), self.password); form.addRow(tr('Повторите пароль'), self.confirm)
        self.protect.toggled.connect(self._password_fields); self._password_fields(False)
        hint = QLabel(tr('Без пароля архив содержит открытые дампы и ключи. Забытый пароль восстановить нельзя.'))
        hint.setWordWrap(True); form.addRow(hint)
        row = QHBoxLayout(); button(tr('Создать архив'), self._backup, row); form.addRow(row)
        form = QFormLayout(restore)
        self.input_archive, self.restore_password = QLineEdit(), QLineEdit()
        row = QHBoxLayout(); row.addWidget(self.input_archive)
        button(tr('Обзор…'), self._choose_input, row); form.addRow(tr('Архив'), row)
        self.restore_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(tr('Пароль, если задан'), self.restore_password)
        profiles = default_data_dir().parent / 'Proxmark Studio Profiles'
        self.destination = QLineEdit(str(profiles / ('Restored-' + datetime.now().strftime('%Y%m%d-%H%M%S'))))
        form.addRow(tr('Новая папка профиля'), self.destination)
        hint = QLabel(tr('Текущая библиотека не изменяется. Выберите новую папку: объединение и перезапись профилей не выполняются.'))
        hint.setWordWrap(True); form.addRow(hint)
        row = QHBoxLayout(); button(tr('Восстановить в новый профиль'), self._restore, row); form.addRow(row)
        self.status = QLabel(''); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.hide(); layout.addWidget(self.progress)
        row = QHBoxLayout(); layout.addLayout(row)
        self.open_restored = button(tr('Открыть восстановленный профиль'), self._open_restored, row)
        self.open_restored.hide()
        self.open_other = button(tr('Открыть профиль…'), self._open_other, row)
        self.cancel = button(tr('Закрыть'), self.reject, row)

    def _password_fields(self, checked):
        for field in (self.password, self.confirm):
            field.setEnabled(checked)
            if not checked:field.clear()

    def _choose_output(self):
        path, _ = QFileDialog.getSaveFileName(self, tr('Создать архив'),
            str(Path.home() / ('ProxmarkStudio-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.pm3backup')),
            'Proxmark Studio (*.pm3backup)')
        if path:self.archive.setText(path if Path(path).suffix else path + '.pm3backup')

    def _choose_input(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('Восстановить'), '', 'Proxmark Studio (*.pm3backup);;ZIP (*.zip)')
        if path:self.input_archive.setText(path)

    def _backup(self):
        password = self.password.text() if self.protect.isChecked() else None
        if password is not None and (not password or password != self.confirm.text()):
            self.status.setText(tr('Введите пароль и совпадающее подтверждение.')); return
        if not self.archive.text().strip():
            self.status.setText(tr('Выберите файл архива.')); return
        self._start('backup', Path(self.archive.text().strip()), password=password)

    def _restore(self):
        if not self.input_archive.text().strip() or not self.destination.text().strip():
            self.status.setText(tr('Укажите архив и новую папку профиля.')); return
        target = Path(self.destination.text().strip()).absolute()
        # Only the container is created here; the target itself must remain absent.
        try:
            if target.exists():raise BackupError(tr('Профиль уже существует. Выберите новую папку.'))
            active = self.owner.data_dir.resolve()
            if target.resolve().is_relative_to(active) or active.is_relative_to(target.resolve()):
                raise BackupError(tr('Новый профиль должен находиться отдельно от текущего.'))
            target.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc)); return
        self._start('restore', Path(self.input_archive.text().strip()), destination=target,
                    password=self.restore_password.text() or None)

    def _start(self, mode, archive, destination=None, password=None):
        if self.worker is not None:return
        if not self.owner._backup_available():
            self.status.setText(tr('Сначала завершите текущую операцию устройства.')); return
        self.owner.reconnector.cancel()
        worker = BackupWorker(mode, self.owner.data_dir, archive, destination, password, self)
        self.worker = self.owner._backup_worker = worker
        self.password.clear(); self.confirm.clear(); self.restore_password.clear()
        self.tabs.setEnabled(False); self.open_other.setEnabled(False); self.open_restored.hide()
        self.cancel.setText(tr('Отменить')); self.progress.setRange(0, 0); self.progress.show()
        self.status.setText(tr('Подготовка…'))
        worker.progress.connect(lambda *args: self._progress(worker, *args))
        worker.completed.connect(lambda report: self._result(worker, report))
        worker.failed.connect(lambda message: self._message(worker, message))
        worker.cancelled.connect(lambda: self._message(worker, tr('Операция отменена.')))
        worker.finished.connect(lambda: self._finished(worker))
        self.owner._busy_changed(self.owner.runner.busy)
        worker.start()

    def _progress(self, worker, message, current, total):
        if self.worker is not worker:return
        self.status.setText(message); self.progress.setRange(0, total); self.progress.setValue(current)

    def _message(self, worker, message):
        if self.worker is worker:self.status.setText(message)

    def _result(self, worker, report):
        if self.worker is not worker:return
        self.status.setText(tr('Готово: карт — {0}, версий — {1}, ключей — {2}.\n{3}',
                               report['cards'], report['revisions'], report['keys'], report['path']))
        if worker.mode == 'restore':
            self.restored = Path(report['path']); self.open_restored.show()

    def _finished(self, worker):
        if self.worker is not worker:return
        self.worker = None
        if self.owner._backup_worker is worker:self.owner._backup_worker = None
        self.tabs.setEnabled(True); self.open_other.setEnabled(True)
        self.cancel.setText(tr('Закрыть')); self.progress.hide()
        self.owner._busy_changed(self.owner.runner.busy)
        worker.deleteLater()
        if self._close_requested:super().reject()
        if getattr(self.owner, '_backup_close_requested', False):
            self.owner._backup_close_requested = False
            super().reject()
            QTimer.singleShot(0, self.owner.close)

    def reject(self):
        if self.worker is not None:
            self._close_requested = True; self.worker.requestInterruption()
            self.cancel.setEnabled(False); self.status.setText(tr('Отмена…')); return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:self.reject(); event.ignore()
        else:super().closeEvent(event)

    def _open_restored(self):
        if self.restored is not None:self._open_profile(self.restored)

    def _open_other(self):
        path = QFileDialog.getExistingDirectory(self, tr('Открыть профиль…'))
        if path:self._open_profile(Path(path))

    def _open_profile(self, path):
        if self.worker is not None:return
        try:
            if self.owner._switch_library_profile(path):self.accept()
        except (OSError, ValueError) as exc:self.status.setText(str(exc))


class LibraryBackupMixin:
    @property
    def backup_busy(self):
        return getattr(self, '_backup_worker', None) is not None

    def _backup_available(self):
        return (not self.backup_busy and not getattr(self, '_closing', False)
                and not self.runner.busy and self.read_job is None and self.antenna_job is None
                and not self.firmware_busy and self.flipper_worker is None
                and not self.runner.rf_lease and not self._auto_owned())

    def show_library_backup(self):
        dialog = LibraryBackupDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _switch_library_profile(self, path):
        if not self._backup_available():raise BackupError(tr('Сначала завершите текущую операцию устройства.'))
        path = Path(path).resolve(strict=True)
        if path == self.data_dir.resolve():raise BackupError(tr('Этот профиль уже открыт.'))
        validate_profile(path)
        if not self._discard_allowed():return False
        # Construct before closing: a failed load leaves the current window available.
        window = type(self)(path, language=self.ui_language)
        app = QApplication.instance()
        windows = getattr(app, '_profile_windows', [])
        app._profile_windows = windows
        window.show()
        # Discard was already approved above; an unsaved new card may have no
        # card_id for _discard_allowed() to reload and clear its dirty flag.
        dirty = self.dirty
        self.dirty = False
        if not self.close():
            self.dirty = dirty
            window.close(); window.deleteLater(); return False
        windows.append(window)
        if self in windows:windows.remove(self)
        self.deleteLater()
        return True
