"""Offscreen tests of real windows, worker lifecycle and profile isolation."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QAbstractButton
from shiboken6 import isValid
from pm3gui.app import MainWindow
from pm3gui.core import DumpData
from pm3gui.i18n import set_language
from pm3gui.library_backup import BackupCancelled, BackupError, create_backup, restore_backup
from pm3gui.library_backup_ui import LibraryBackupDialog


class LibraryBackupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.enterContext(patch('pm3gui.app.available_ports',return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client',return_value=''))
        self.raw=self.enterContext(patch('pm3gui.app.ClientRunner.run',side_effect=AssertionError('device I/O')))
        self.typed=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation',side_effect=AssertionError('device I/O')))
        self.question=self.enterContext(patch('pm3gui.app.QMessageBox.question',return_value=QMessageBox.StandardButton.Cancel))
        self.window=MainWindow(self.root/'original',language='en');self.windows=[self.window];self.dialogs=[]
        self.window.library.create_card('Synthetic',DumpData(bytes(32),'bin',{},16))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                if dialog.worker is not None:
                    dialog.worker.requestInterruption();dialog.worker.wait(5000);self.app.processEvents()
                dialog.reject();dialog.deleteLater()
        for window in (*self.windows,*getattr(self.app,'_profile_windows',[])):
            if isValid(window):window.dirty=False;window.close();window.deleteLater()
        self.app._profile_windows=[]
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents()
        set_language('ru');self.temp.cleanup()

    def dialog(self):
        dialog=LibraryBackupDialog(self.window);self.dialogs.append(dialog);return dialog

    def until(self, predicate, timeout=8000):
        loop=QEventLoop();poll=QTimer();poll.setInterval(5)
        poll.timeout.connect(lambda:loop.quit() if predicate() else None)
        poll.start();QTimer.singleShot(timeout,loop.quit)
        if not predicate():loop.exec()
        poll.stop();self.assertTrue(predicate(),'Worker did not settle within deadline')

    def test_real_encrypted_worker_roundtrip_and_no_password_in_settings(self):
        dialog=self.dialog();path=self.root/'library.pm3backup';target=self.root/'restored'
        dialog.archive.setText(str(path));dialog.protect.setChecked(True)
        dialog.password.setText('private password');dialog.confirm.setText('private password')
        original_progress=dialog._progress
        observed=[]
        def progress(*args):observed.append(QThread.currentThread() is self.app.thread());original_progress(*args)
        dialog._progress=progress
        dialog._backup();worker=dialog.worker
        self.assertTrue(self.window.backup_busy);self.assertEqual(dialog.password.text(),'')
        self.assertFalse(self.window.read_button.isEnabled());self.assertFalse(self.window.stop_button.isEnabled())
        self.until(lambda:dialog.worker is None)
        self.assertTrue(path.is_file());self.assertTrue(all(observed));self.assertTrue(observed)
        self.assertIsNone(worker.password)
        dialog.input_archive.setText(str(path));dialog.destination.setText(str(target));dialog.restore_password.setText('private password')
        dialog._restore();self.until(lambda:dialog.worker is None)
        self.assertEqual(dialog.restored,target);self.assertEqual(dialog.restore_password.text(),'')
        self.window._save_presentation()
        self.assertNotIn('private password',(self.window.data_dir/'settings.json').read_text())
        self.assertNotIn('private password',self.window.log.toPlainText())
        self.raw.assert_not_called();self.typed.assert_not_called()

    def test_password_validation_never_starts_worker(self):
        dialog=self.dialog();dialog.archive.setText(str(self.root/'backup.pm3backup'));dialog.protect.setChecked(True)
        dialog.password.setText('one');dialog.confirm.setText('two');dialog._backup()
        self.assertIsNone(dialog.worker);self.assertIn('matching',dialog.status.text())
        dialog.protect.setChecked(False);self.assertEqual(dialog.password.text(),'')

    def test_operation_guard_and_stale_callback(self):
        dialog=self.dialog();self.window.read_job={'stage':'test'}
        dialog._start('backup',self.root/'b.pm3backup');self.assertIsNone(dialog.worker)
        self.window.read_job=None
        owned=object();self.window._backup_worker=owned
        try:
            with self.assertRaises(ValueError):self.window._start_step('hf search')
            self.assertFalse(self.window.reconnector._available())
            dialog.status.setText('current');dialog._message(object(),'stale')
            dialog._result(object(),{});dialog._finished(object());self.assertEqual(dialog.status.text(),'current')
            self.assertIs(self.window._backup_worker,owned)
        finally:self.window._backup_worker=None

    def test_cancelled_background_job_allows_dialog_and_main_window_to_close(self):
        ready=threading.Event()
        def wait_for_cancel(*args,cancelled,**kwargs):
            ready.set()
            # Poll only inside the background worker, never the GUI thread.
            for _ in range(400):
                if cancelled():raise BackupCancelled('cancelled')
                threading.Event().wait(.005)
            raise AssertionError('Cancellation was never requested')
        dialog=self.dialog();dialog.show();self.window.show()
        with patch('pm3gui.library_backup_ui.create_backup',side_effect=wait_for_cancel):
            dialog._start('backup',self.root/'b.pm3backup')
            self.until(ready.is_set)
            self.assertFalse(self.window.close());self.assertTrue(self.window.backup_busy)
            self.until(lambda:not self.window.isVisible())
        self.assertIsNone(dialog.worker);self.assertFalse(dialog.isVisible())
        self.assertFalse((self.root/'b.pm3backup').exists())

    def test_escape_cancels_worker_without_destroying_live_qthread(self):
        ready=threading.Event()
        def wait_for_cancel(*args,cancelled,**kwargs):
            ready.set()
            while not cancelled():threading.Event().wait(.005)
            raise BackupCancelled('cancelled')
        dialog=self.dialog();dialog.show()
        with patch('pm3gui.library_backup_ui.create_backup',side_effect=wait_for_cancel):
            dialog._start('backup',self.root/'b.pm3backup');self.until(ready.is_set)
            dialog.reject();self.assertIsNotNone(dialog.worker)
            self.until(lambda:dialog.worker is None)
        self.assertFalse(dialog.isVisible());self.assertFalse(self.window.backup_busy)

    def test_restore_existing_target_refused_and_wrong_password_shown(self):
        archive=self.root/'b.pm3backup';create_backup(self.window.data_dir,archive,password='secret')
        dialog=self.dialog();dialog.input_archive.setText(str(archive));dialog.destination.setText(str(self.window.data_dir))
        dialog._restore();self.assertIsNone(dialog.worker)
        target=self.root/'restored';dialog.destination.setText(str(target));dialog.restore_password.setText('wrong')
        dialog._restore();self.until(lambda:dialog.worker is None)
        self.assertIn('Incorrect password',dialog.status.text());self.assertFalse(target.exists())

    def test_unsaved_changes_can_cancel_switch_then_open_separate_profile(self):
        archive=self.root/'b.pm3backup';target=self.root/'restored'
        create_backup(self.window.data_dir,archive);restore_backup(archive,target)
        self.window.show();self.window.dirty=True
        self.assertFalse(self.window._switch_library_profile(target));self.assertTrue(self.window.isVisible())
        self.window.dirty=False
        self.assertTrue(self.window._switch_library_profile(target))
        new=self.app._profile_windows[-1];self.windows.append(new)
        # Windows CI may supply TEMP through an 8.3 alias (RUNNER~1).
        # Profile switching canonicalizes it; verify filesystem identity.
        self.assertTrue(new.data_dir.samefile(target));self.assertEqual(len(new.library.list_cards()),1)
        new.library.create_card('Only in restored',DumpData(bytes(16),'bin',{},16))
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.root/'original/library.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cards').fetchone()[0],1)
        self.raw.assert_not_called();self.typed.assert_not_called()

    def test_english_backup_dialog_has_no_untranslated_product_text(self):
        dialog=self.dialog()
        texts=[dialog.windowTitle(),*(dialog.tabs.tabText(i) for i in range(dialog.tabs.count()))]
        texts += [w.text() for kind in (QLabel,QAbstractButton) for w in dialog.findChildren(kind)]
        import re
        self.assertEqual([s for s in texts if re.search('[А-Яа-яЁё]',s)],[])

    def test_profile_switch_from_dialog_discards_new_unsaved_card_only_once(self):
        archive=self.root/'b.pm3backup';target=self.root/'restored'
        create_backup(self.window.data_dir,archive);restore_backup(archive,target)
        dialog=self.dialog();dialog.show();self.window.dirty=True;self.window.card_id=None
        self.question.return_value=QMessageBox.StandardButton.Discard
        dialog._open_profile(target)
        self.assertEqual(self.question.call_count,1)
        self.assertEqual(dialog.result(),dialog.DialogCode.Accepted)
        self.assertTrue(self.app._profile_windows[-1].data_dir.samefile(target))
