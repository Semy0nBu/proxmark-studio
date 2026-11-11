"""Real offscreen dialog/worker lifecycle without a Proxmark or personal profile."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget
from shiboken6 import isValid

from pm3gui.catalog_snapshot import CatalogCancelled, CatalogSnapshot, executable_sha256, save_catalog
from pm3gui.catalog_ui import CatalogDialog
from pm3gui.client_catalog import COMMANDS
from pm3gui.i18n import set_language


class CatalogUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.client = self.root/'client.exe';self.client.write_bytes(b'synthetic client')
        self.owner = QWidget();self.owner.data_dir = self.root/'profile'
        self.owner.client_path = QLineEdit(str(self.client), self.owner)
        self.snapshot = CatalogSnapshot(str(self.client.resolve()), executable_sha256(self.client), 'Test version',
            '2026-11-11T00:00:00+00:00', {name:dict(COMMANDS[name]) for name in ('hf mf rdbl','hf mf wrbl','hf search')})
        self.dialogs = [];self.addCleanup(self.cleanup);set_language('en')

    def cleanup(self):
        for dialog in self.dialogs:
            if isValid(dialog):
                if dialog.worker is not None:
                    dialog.worker.requestInterruption();dialog.worker.wait(5000);self.app.processEvents()
                dialog.close();dialog.deleteLater()
        self.owner.close();self.owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete);self.app.processEvents()
        set_language('ru');self.temp.cleanup()

    def dialog(self, **kwargs):
        result = CatalogDialog(self.owner, **kwargs);self.dialogs.append(result);return result

    def until(self, predicate, timeout=5000):
        loop = QEventLoop();timer = QTimer();timer.setInterval(5)
        timer.timeout.connect(lambda:loop.quit() if predicate() else None)
        timer.start();QTimer.singleShot(timeout, loop.quit)
        if not predicate():loop.exec()
        timer.stop();self.assertTrue(predicate(), 'catalog worker did not settle')

    def select(self, dialog, name):
        dialog.search.setText(name)
        for index in range(dialog.commands.count()):
            if dialog.commands.item(index).text() == name:
                dialog.commands.setCurrentRow(index);return
        self.fail('command missing: '+name)

    def test_unchecked_profile_does_not_activate_forms_or_touch_hardware(self):
        dialog = self.dialog()
        self.assertIsNone(dialog.snapshot);self.assertFalse(dialog.open_button.isEnabled())
        self.assertEqual(dialog.commands.count(), 0);self.assertIn('not been checked', dialog.summary.text())

    def test_confirmed_existing_form_emits_only_its_command(self):
        opened = [];dialog = self.dialog(snapshot=self.snapshot, on_open=opened.append)
        self.select(dialog, 'hf mf rdbl')
        self.assertTrue(dialog.open_button.isEnabled())
        dialog.open_selected();self.assertEqual(opened, ['hf mf rdbl'])
        self.assertEqual(dialog.help.toPlainText(), COMMANDS['hf mf rdbl']['help'])

    def test_new_and_changed_commands_are_help_only(self):
        rows = {name:dict(row) for name,row in self.snapshot.commands.items()}
        rows['hf mf rdbl']['help'] = rows['hf mf rdbl']['help'].replace('--blk', '--block-new')
        rows['hf new command'] = {'command':'hf new command','offline':True,'description':'New',
                                 'help':'New help\nusage:\n    hf new command [-h]\n'}
        opened = [];dialog = self.dialog(snapshot=replace(self.snapshot, commands=rows), on_open=opened.append)
        for name in ('hf mf rdbl', 'hf new command'):
            self.select(dialog,name);self.assertFalse(dialog.open_button.isEnabled())
            dialog.open_selected()
        self.assertEqual(opened, [])
        dialog.state_filter.setCurrentIndex(dialog.state_filter.findData('added'))
        dialog.search.clear();self.assertEqual(dialog.commands.count(), 1)
        self.assertEqual(dialog.commands.item(0).text(), 'hf new command')

    def test_binary_replacement_invalidates_open_even_without_settings_change(self):
        opened = [];dialog = self.dialog(snapshot=self.snapshot, on_open=opened.append)
        self.select(dialog,'hf mf rdbl');self.assertTrue(dialog.open_button.isEnabled())
        self.client.write_bytes(b'changed client')
        dialog.open_selected();self.assertEqual(opened, [])
        dialog.selected(dialog.commands.currentItem());self.assertFalse(dialog.open_button.isEnabled())

    def test_changed_offline_availability_keeps_the_old_form_blocked(self):
        rows = {name:dict(row) for name,row in self.snapshot.commands.items()}
        rows['hf mf rdbl']['offline'] = not rows['hf mf rdbl']['offline']
        opened = [];dialog = self.dialog(snapshot=replace(self.snapshot,commands=rows),on_open=opened.append)
        self.select(dialog,'hf mf rdbl');self.assertFalse(dialog.open_button.isEnabled())
        dialog.open_selected();self.assertEqual(opened,[])

    def test_cache_is_read_but_never_accepted_for_a_different_selected_executable(self):
        cache = self.owner.data_dir/'client-catalog.json';save_catalog(self.snapshot,cache)
        other = self.root/'other.exe';other.write_bytes(b'another client');self.owner.client_path.setText(str(other))
        dialog = self.dialog();self.select(dialog,'hf mf rdbl')
        self.assertFalse(dialog.open_button.isEnabled());self.assertIn('stale',dialog.status.text())

    def test_async_success_publishes_snapshot_and_persists_only_after_capture(self):
        updates = [];dialog = self.dialog();dialog.snapshot_updated.connect(updates.append)
        with patch('pm3gui.catalog_ui.capture_catalog', return_value=self.snapshot) as capture:
            dialog.update_catalog();self.assertFalse(dialog.update_button.isEnabled())
            self.until(lambda:dialog.worker is None)
        self.assertEqual(updates, [self.snapshot]);self.assertTrue(dialog.cache_path.is_file())
        self.assertEqual(capture.call_args.args, (str(self.client),))
        self.assertTrue(dialog.update_button.isEnabled());self.assertFalse(dialog.cancel_button.isEnabled())

    def test_failed_refresh_keeps_prior_cache_and_active_snapshot(self):
        dialog = self.dialog(snapshot=self.snapshot);save_catalog(self.snapshot,dialog.cache_path)
        old = dialog.cache_path.read_bytes();updates = [];dialog.snapshot_updated.connect(updates.append)
        with patch('pm3gui.catalog_ui.capture_catalog', side_effect=ValueError('invalid export')):
            dialog.update_catalog();self.until(lambda:dialog.worker is None)
        self.assertEqual(dialog.cache_path.read_bytes(), old)
        self.assertEqual(dialog.snapshot, self.snapshot);self.assertEqual(updates, [])
        self.assertIn('invalid export',dialog.status.text())

    def test_close_requests_cancel_and_waits_for_worker_before_destroying_dialog(self):
        dialog = self.dialog();dialog.show();entered = threading.Event()
        def capture(*_args, cancelled, **_kwargs):
            entered.set()
            while not cancelled():threading.Event().wait(0.005)
            raise CatalogCancelled('cancelled')
        with patch('pm3gui.catalog_ui.capture_catalog', side_effect=capture):
            dialog.update_catalog();self.until(entered.is_set)
            dialog.close();self.assertTrue(dialog.isVisible());self.assertTrue(dialog._close_requested)
            self.until(lambda:dialog.worker is None);self.until(lambda:not dialog.isVisible())
        self.assertFalse(dialog.cache_path.exists())
        dialog.show();self.assertFalse(dialog._close_requested)

    def test_selection_change_during_export_cannot_publish_a_stale_result(self):
        dialog = self.dialog();updates = [];dialog.snapshot_updated.connect(updates.append)
        entered, release = threading.Event(), threading.Event()
        def capture(*_args, **_kwargs):
            entered.set();release.wait(3);return self.snapshot
        with patch('pm3gui.catalog_ui.capture_catalog', side_effect=capture):
            dialog.update_catalog();self.until(entered.is_set)
            other = self.root/'other.exe';other.write_bytes(b'other')
            self.owner.client_path.setText(str(other));release.set()
            self.until(lambda:dialog.worker is None)
        self.assertEqual(updates, []);self.assertIsNone(dialog.snapshot)


if __name__ == '__main__':unittest.main()
