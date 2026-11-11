"""Named actions persist locally and use the existing reviewed operation lifecycle."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pm3gui.quick_actions import QuickAction, action_flow, actions_revision, load_actions, save_actions


class QuickActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'quick-actions.json'

    def test_unicode_roundtrip_and_isolation(self):
        action = QuickAction('Проверка Ω', ('hw version', 'hw status'), 30)
        save_actions(self.path, [action])
        self.assertEqual(load_actions(self.path), [action])
        self.assertEqual(load_actions(self.path.parent / 'another-profile.json'), [])

    def test_saved_unsupported_commands_survive_editing_another_action(self):
        old = QuickAction('Old client', ('hf future command',))
        save_actions(self.path, [old, QuickAction('Status', ('hw status',))])
        self.assertEqual(load_actions(self.path)[0], old)
        with self.assertRaises(ValueError):action_flow(old)

    def test_invalid_steps_and_names_fail_before_any_client(self):
        actions = [QuickAction('', ('hw status',)), QuickAction('a\nb', ('hw status',)),
                   QuickAction('x', ()), QuickAction('x', ('hw status',)*21),
                   QuickAction('x', ('hw status; hf search',)),
                   QuickAction('x', ('hw status\nhf search',)),
                   QuickAction('x', ('hw status',), True),
                   QuickAction('x', ('hw status',), 120, 1),
                   QuickAction('x', ('hf search',), 120, True)]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(ValueError):action_flow(action)

    def test_corrupt_or_duplicate_file_is_not_partly_loaded(self):
        self.path.write_text('{', encoding='utf-8')
        with self.assertRaises(ValueError):load_actions(self.path)
        with self.assertRaises(ValueError):save_actions(self.path, [QuickAction('A', ('hw status',)), QuickAction('a', ('hw version',))])
        self.assertEqual(self.path.read_text(), '{')

    def test_failed_atomic_publish_keeps_previous_file(self):
        save_actions(self.path, [QuickAction('Before', ('hw status',))]); before = self.path.read_bytes()
        with patch('pm3gui.quick_actions.os.replace', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):save_actions(self.path, [QuickAction('After', ('hw version',))])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_concurrent_editor_revision_cannot_overwrite_a_new_save(self):
        revision = actions_revision(self.path)
        new_revision = save_actions(self.path, [QuickAction('First', ('hw status',))], expected_revision=revision)
        self.assertEqual(new_revision, actions_revision(self.path))
        with self.assertRaises(ValueError):
            save_actions(self.path, [QuickAction('Second', ('hw version',))], expected_revision=revision)
        self.assertEqual([a.name for a in load_actions(self.path)], ['First'])

    def test_sequence_progress_timeout_and_result(self):
        flow = action_flow(QuickAction('Check', ('hw version', 'hw status'), 30))(self.path.parent)
        self.assertEqual(next(flow).line, 'hw version')
        request = flow.send(('version', 0)); self.assertEqual(request.line, 'hw status'); self.assertEqual(request.timeout, 30000)
        with self.assertRaises(StopIteration) as done:flow.send(('status', 0))
        self.assertEqual(done.exception.value['completed_steps'], 2)
        self.assertEqual(done.exception.value['verification'], 'client-output-only')

    def test_first_error_never_yields_next_step(self):
        flow = action_flow(QuickAction('Check', ('hw version', 'hw status')))(self.path.parent)
        next(flow)
        with self.assertRaises(ValueError):flow.send(('error', -5))
        with self.assertRaises(StopIteration):next(flow)

    def test_closing_generator_does_not_run_pending_steps(self):
        flow = action_flow(QuickAction('Check', ('hw version', 'hw status')))(self.path.parent)
        next(flow); flow.close()
        with self.assertRaises(StopIteration):next(flow)

    def test_spiffs_query_keeps_its_native_completion_fence(self):
        flow = action_flow(QuickAction('Files', ('mem spiffs tree', 'hw status')))(self.path.parent)
        self.assertEqual(next(flow).line, 'mem spiffs tree; hw version')
        with self.assertRaises(ValueError):flow.send(('Flash Memory tree (SPIFFS)\n<empty>', 0))
        with self.assertRaises(StopIteration):next(flow)
        flow = action_flow(QuickAction('Files', ('mem spiffs tree', 'hw status')))(self.path.parent)
        next(flow)
        request = flow.send(('Flash Memory tree (SPIFFS)\n<empty>\n[ ARM ]\nOS......... fixture', 0))
        self.assertEqual(request.line, 'hw status'); flow.close()

    def test_archive_includes_quick_actions_and_restores_them(self):
        from pm3gui.core import Library
        from pm3gui.keys import KeyLibrary
        from pm3gui.library_backup import create_backup, restore_backup
        profile = self.path.parent / 'profile'; profile.mkdir()
        library = Library(profile / 'library.sqlite3'); keys = KeyLibrary(profile / 'library.sqlite3')
        library.close(); keys.close()
        action = QuickAction('Check', ('hw version',))
        save_actions(profile / 'quick-actions.json', [action])
        archive = self.path.parent / 'backup.zip'
        create_backup(profile, archive)
        restored = self.path.parent / 'restored'
        restore_backup(archive, restored)
        self.assertEqual(load_actions(restored / 'quick-actions.json'), [action])


class QuickActionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from pm3gui.app import MainWindow
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.calls = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.raw = self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.w = MainWindow(self.root / 'profile', language='en'); self.addCleanup(self.cleanup)

    def cleanup(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        if self.w.read_job and self.w.read_job.get('flow'):self.w.read_job['flow'].close()
        self.w.read_job = None; self.w.dirty = False
        self.w.close(); self.w.deleteLater()
        from pm3gui.i18n import set_language
        set_language('ru')
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); self.app.processEvents(); self.tmp.cleanup()

    def finish(self, code=0):
        self.w.operation_output = 'Fixture'; self.w._finished(code, 'Fixture')

    def test_editor_open_save_select_never_opens_a_client(self):
        self.w.studio.open_quick_actions(); dialog = self.w.studio.quick_actions_dialog
        dialog.name.setText('Check'); dialog.commands.setPlainText('hw version\nhw status'); dialog.save()
        dialog.new(); dialog.choices.setCurrentRow(0)
        self.assertEqual(dialog.commands.toPlainText(), 'hw version\nhw status')
        self.assertFalse((self.w.data_dir / 'sessions').exists()); self.calls.assert_not_called(); self.raw.assert_not_called()
        dialog.close()

    def test_review_cancel_does_not_start_transaction(self):
        with patch.object(self.w.studio, 'review', return_value=False):
            self.assertFalse(self.w.studio.run_quick_action(QuickAction('Check', ('hw status',))))
        self.assertIsNone(self.w.read_job); self.calls.assert_not_called()

    def test_opening_actions_twice_reuses_the_editor(self):
        self.w.studio.open_quick_actions(); dialog = self.w.studio.quick_actions_dialog
        self.w.studio.open_quick_actions(); self.assertIs(self.w.studio.quick_actions_dialog, dialog)
        dialog.close()

    def test_two_editor_instances_reject_stale_save_instead_of_losing_actions(self):
        from pm3gui.quick_actions_ui import QuickActionsDialog
        first, second = QuickActionsDialog(self.w.studio), QuickActionsDialog(self.w.studio)
        first.name.setText('First'); first.commands.setPlainText('hw status'); first.save()
        second.name.setText('Second'); second.commands.setPlainText('hw version')
        with self.assertRaises(ValueError):second.save()
        self.assertEqual([a.name for a in load_actions(first.path)], ['First'])
        first.close(); second.close()

    def test_backup_blocks_action_before_review_session_or_rf_lease(self):
        from unittest.mock import PropertyMock
        with patch.object(type(self.w), 'backup_busy', new_callable=PropertyMock, return_value=True), \
                patch.object(self.w.studio, 'review') as review:
            with self.assertRaises(ValueError):self.w.studio.run_quick_action(QuickAction('Search', ('hf search',)))
        review.assert_not_called(); self.calls.assert_not_called()
        self.assertIsNone(self.w.read_job); self.assertFalse(self.w.runner.rf_lease)
        self.assertFalse((self.w.data_dir / 'sessions').exists())

    def test_busy_action_cannot_replace_current_transaction(self):
        with patch.object(self.w.studio, 'review', return_value=True):
            self.w.studio.run_quick_action(QuickAction('First', ('hw status',)))
            job = self.w.read_job
            with self.assertRaises(ValueError):self.w.studio.run_quick_action(QuickAction('Second', ('hw version',)))
        self.assertIs(self.w.read_job, job); self.assertEqual(self.calls.call_count, 1)
        self.finish()

    def test_sequence_runs_one_step_at_a_time_and_stop_never_replays(self):
        with patch.object(self.w.studio, 'review', return_value=True):
            self.w.studio.run_quick_action(QuickAction('Check', ('hw version', 'hw status')))
        self.assertEqual(self.calls.call_count, 1)
        self.finish(); self.assertEqual(self.calls.call_count, 2)
        self.assertEqual(self.calls.call_args.args[1].render(), 'hw status')
        self.w.cancel_operation(); self.finish(-10)
        self.assertIsNone(self.w.read_job); self.assertEqual(self.calls.call_count, 2)

    def test_offline_action_uses_no_device_transport(self):
        with patch.object(self.w.studio, 'review', return_value=True):
            self.w.studio.run_quick_action(QuickAction('Formats', ('wiegand list',), offline=True))
        self.assertEqual(self.w.read_job['device_profile']['transport'], 'offline')
        self.assertFalse(self.w.read_job['rf']); self.finish()


if __name__ == '__main__':unittest.main()
