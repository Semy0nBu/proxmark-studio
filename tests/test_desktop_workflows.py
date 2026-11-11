"""Integration boundaries for the remaining desktop roadmap, without devices."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from pm3gui.app import MainWindow
from pm3gui.catalog_snapshot import CatalogSnapshot, executable_sha256
from pm3gui.client_catalog import COMMANDS
from pm3gui.core import DumpData
from pm3gui.editor_ui import open_hex_editor
from pm3gui.operations import Operation
from pm3gui.quick_actions import QuickAction


class DesktopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.calls = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.raw = self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.w = MainWindow(self.root / 'profile', language='en'); self.addCleanup(self.cleanup)

    def cleanup(self):
        if not getattr(self.w, '_closing', False):
            if self.w.read_job and self.w.read_job.get('flow'):self.w.read_job['flow'].close()
            self.w.read_job = None; self.w.dirty = False; self.w.close()
        self.w.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        from pm3gui.i18n import set_language
        set_language('ru')
        self.app.processEvents(); self.tmp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.005)
        self.assertTrue(predicate())

    def snapshot(self):
        client = self.root / 'client.exe'; client.write_bytes(b'Synthetic client')
        self.w.client_path.setText(str(client))
        return CatalogSnapshot(str(client.resolve()), executable_sha256(client), 'fixture', '2026-11-11T00:00:00Z',
                               {'hw status': COMMANDS['hw status']})

    def test_expanded_hex_shares_working_document_without_saving_or_device_access(self):
        cid = self.w.library.create_card('Synthetic', DumpData(bytes(64), 'bin', {}, 16))
        self.w.load_card(cid); version = self.w.loaded_revision
        dialog = open_hex_editor(self.w)
        self.assertIs(dialog.editor.document(), self.w.hex_edit.document()); self.assertFalse(self.w.dirty)
        dialog.editor.setPlainText('AA ' + '00 '*63)
        self.assertEqual(self.w._working_dump().data[0], 0xAA); self.assertTrue(self.w.dirty)
        self.assertEqual(self.w.library.get_dump(cid, version).data, bytes(64))
        self.assertEqual(len(self.w.library.list_revisions(cid)), 1)
        self.calls.assert_not_called(); self.raw.assert_not_called(); dialog.close()

    def test_expanded_hex_reuses_visible_window_and_tracks_library_selection(self):
        a = self.w.library.create_card('A', DumpData(bytes(16), 'bin', {}, 16))
        b = self.w.library.create_card('B', DumpData(bytes([3])*16, 'bin', {}, 16))
        self.w.load_card(a); dialog = open_hex_editor(self.w)
        self.assertIs(open_hex_editor(self.w), dialog)
        self.w.load_card(b)
        self.assertEqual(dialog.editor.toPlainText(), self.w.hex_edit.toPlainText()); self.assertFalse(self.w.dirty)
        dialog.close()

    def test_expanded_hex_obeys_encoding_error_readonly_state(self):
        cid = self.w.library.create_card('Text', DumpData(b'A', 'bin', {}, 16)); self.w.load_card(cid)
        self.w.encoding.setCurrentIndex(self.w.encoding.findData('cp1251'))
        dialog = open_hex_editor(self.w)
        self.w.decoded.setPlainText('😀')
        self.assertTrue(self.w.hex_edit.isReadOnly()); self.assertTrue(dialog.editor.isReadOnly())
        self.assertTrue(self.w.text_error); self.assertEqual(self.w.decoded.toPlainText(), '😀')
        from PySide6.QtTest import QTest
        QTest.keyClicks(dialog.editor, 'AA')
        self.assertEqual(self.w.decoded.toPlainText(), '😀'); self.assertTrue(self.w.text_error)
        self.w.decoded.setPlainText('B'); self.assertFalse(dialog.editor.isReadOnly())
        dialog.close()

    def test_expanded_hex_keeps_unknown_byte_annotations_and_clears_on_new_snapshot(self):
        metadata = {'partial_dump': {'version': 1, 'kind': 'mfc', 'uid': 'A1B2C3D4',
            'block_size': 16, 'expected_bytes': 1024, 'complete': False,
            'known_bytes': list(range(16)), 'read_blocks': [0], 'missing_blocks': list(range(1,64)),
            'incomplete_blocks': list(range(1,64))}}
        a = self.w.library.create_card('Partial', DumpData(bytes(1024), 'bin', metadata, 16))
        b = self.w.library.create_card('Full', DumpData(bytes(16), 'bin', {}, 16))
        self.w.load_card(a); dialog = open_hex_editor(self.w)
        self.assertGreater(len(dialog.editor.extraSelections()), 0)
        self.assertEqual(dialog.editor.toolTip(), self.w.hex_edit.toolTip())
        self.w.load_card(b)
        self.assertEqual(dialog.editor.extraSelections(), []); self.assertEqual(dialog.editor.toolTip(), '')
        dialog.close()

    def test_stale_catalog_blocks_saved_action_before_review_or_client_start(self):
        snapshot = self.snapshot(); self.w.client_catalog_snapshot = snapshot
        Path(snapshot.client_path).write_bytes(b'Changed client')
        with patch.object(self.w.studio, 'review') as review:
            with self.assertRaises(ValueError):self.w.studio.run_quick_action(QuickAction('Status', ('hw status',)))
        review.assert_not_called(); self.calls.assert_not_called(); self.assertIsNone(self.w.read_job)

    def test_first_unknown_client_requires_catalog_before_expert_review(self):
        snapshot = self.snapshot(); self.w.client_catalog_snapshot = None
        with patch.object(self.w.studio, 'review') as review:
            with self.assertRaises(ValueError):self.w.studio.run_quick_action(QuickAction('Status', ('hw status',)))
        review.assert_not_called(); self.calls.assert_not_called(); self.assertIsNone(self.w.read_job)

    def test_bundled_fingerprint_allows_shipped_forms_without_manual_refresh(self):
        import json
        from pm3gui.client_catalog import bundled_client_matches
        snapshot = self.snapshot()
        (self.root/'release.json').write_text(json.dumps({'native_client_sha256':snapshot.client_sha256}))
        self.assertTrue(bundled_client_matches(snapshot.client_path, root=self.root))
        with patch('pm3gui.paths.application_root', return_value=self.root), patch.object(self.w.studio,'review',return_value=False) as review:
            self.w.client_catalog_snapshot=None
            self.assertFalse(self.w.studio.run_quick_action(QuickAction('Status',('hw status',))))
            review.assert_called_once()
        (self.root/'release.json').write_text('[]')
        self.assertFalse(bundled_client_matches(snapshot.client_path, root=self.root))

    def test_removed_catalog_command_blocks_generic_form_at_common_dispatch(self):
        self.w.client_catalog_snapshot = self.snapshot()
        with self.assertRaises(ValueError):self.w._start_step(Operation('hf_tool', hf_command='hf search'))
        self.calls.assert_not_called()
        self.w._start_step(Operation('expert_tool', client_command='hw status'))
        self.assertEqual(self.calls.call_count, 1)

    def test_known_catalog_does_not_interfere_with_native_cleanup_commands(self):
        self.w.client_catalog_snapshot = self.snapshot()
        self.w._start_step(Operation('antenna_field_off'))
        self.assertEqual(self.calls.call_args.args[1].render(), 'hw fpgaoff')

    def test_main_window_closes_after_offline_catalog_worker_cancellation(self):
        from pm3gui.catalog_snapshot import CatalogCancelled
        snapshot = self.snapshot()
        self.w.show()
        def capture(*args, cancelled, **kwargs):
            while not cancelled():time.sleep(.005)
            raise CatalogCancelled('cancelled')
        with patch('pm3gui.catalog_ui.capture_catalog', side_effect=capture):
            self.w.studio.open_client_catalog(); dialog = self.w.studio.catalog_dialog
            dialog.update_catalog(); self.wait_for(lambda:dialog.worker is not None and dialog.worker.isRunning())
            self.assertFalse(self.w.close())
            self.wait_for(lambda:getattr(self.w, '_closing', False))
            self.assertIsNone(dialog.worker)
        self.calls.assert_not_called(); self.raw.assert_not_called()

    def test_main_window_closes_after_diagnostics_worker_cancellation(self):
        from pm3gui.diagnostics import DiagnosticsCancelled
        self.w.show()
        def prepare(*args, cancelled, **kwargs):
            while not cancelled():time.sleep(.005)
            raise DiagnosticsCancelled()
        with patch('pm3gui.diagnostics_ui.prepare_diagnostics', side_effect=prepare):
            self.w.studio.open_diagnostics(); dialog = self.w.studio.diagnostics_dialog
            self.wait_for(lambda:dialog.worker is not None and dialog.worker.isRunning())
            self.assertFalse(self.w.close())
            self.wait_for(lambda:getattr(self.w, '_closing', False))
            self.assertIsNone(dialog.worker)
        self.calls.assert_not_called(); self.raw.assert_not_called()

    def test_report_error_history_contains_numbers_only_and_is_bounded(self):
        from pm3gui.diagnostics import snapshot_from_owner
        for code in range(-50, 0):self.w._finished(code, 'Fixture')
        self.assertEqual(self.w.diagnostic_error_codes, list(range(-30, 0)))
        self.assertEqual(snapshot_from_owner(self.w).error_codes, tuple(range(-16, 0)))


if __name__ == '__main__':unittest.main()
