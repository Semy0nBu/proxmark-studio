"""Allowlisted diagnostic exports using synthetic state and offline Qt windows."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from collections import deque
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from pm3gui import diagnostics as diag
from pm3gui.diagnostics_ui import DiagnosticBundleDialog
from pm3gui.i18n import set_language


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = 'PRIVATE-card-name-11223344556677-key-A0A1A2A3A4A5'

    def document(self, snapshot=None, client=None):
        prepared = diag.prepare_diagnostics(snapshot or diag.DiagnosticSnapshot(), client)
        return prepared, json.loads(prepared.document)

    def test_only_normalized_choices_leave_the_collector(self):
        snapshot = diag.DiagnosticSnapshot(
            transport=self.secret, connection=self.secret, operation=self.secret,
            job_kind=self.secret, stage=self.secret, reconnect_attempts=self.secret,
            rf_lease=self.secret, firmware_busy=1, error_codes=(self.secret, True, -5))
        prepared, body = self.document(snapshot)
        self.assertNotIn(self.secret.encode(), prepared.document)
        self.assertEqual(body['state'], {'transport': 'unknown', 'connection': 'unknown',
            'operation': 'unknown', 'job_kind': 'other', 'stage': 'other',
            'rf_lease': None, 'firmware_busy': None})
        self.assertIsNone(body['counters']['reconnect_attempts'])
        self.assertEqual(body['recent_completion_codes'], [-5])

    def test_malformed_unhashable_values_fail_closed(self):
        owner = SimpleNamespace(_active_transport=['usb'], read_job={'stage': ['read'], 'job_kind': {}},
                                runner=SimpleNamespace(busy=False, session_open=False, rf_lease='secret'))
        snapshot = diag.snapshot_from_owner(owner)
        self.assertEqual(snapshot.stage, 'other')
        self.assertEqual(snapshot.job_kind, 'other')
        self.assertEqual(snapshot.transport, 'unknown')
        self.assertIsNone(snapshot.rf_lease)

    def test_owner_without_state_does_not_invent_a_connection_or_operation(self):
        snapshot = diag.snapshot_from_owner(object())
        self.assertEqual((snapshot.connection, snapshot.operation, snapshot.stage),
                         ('unknown', 'unknown', 'other'))
        self.assertIsNone(snapshot.rf_lease)
        self.assertIsNone(snapshot.firmware_busy)

    def test_collector_does_not_read_logs_settings_library_commands_or_ports(self):
        class Owner:
            _active_transport = 'blueshark'
            runner = SimpleNamespace(busy=False, session_open=True, rf_lease=False)
            read_job = None
            firmware_busy = False
            reconnector = SimpleNamespace(attempts=2)
            diagnostic_error_codes = deque([0, -5, -10])

            def __getattr__(self, name):
                if name in {'operation_output', 'current_command', 'settings', 'library', 'key_library',
                            'ports', '_port', 'data_dir', 'client_path', 'detected'}:
                    raise AssertionError('Private or device data was accessed: ' + name)
                raise AttributeError(name)

        prepared, body = self.document(diag.snapshot_from_owner(Owner()))
        self.assertEqual(body['state']['transport'], 'blueshark')
        self.assertEqual(body['state']['connection'], 'connected')
        self.assertEqual(body['state']['operation'], 'idle')
        self.assertEqual(body['counters']['reconnect_attempts'], 2)
        self.assertEqual(body['recent_completion_codes'], [0, -5, -10])
        self.assertTrue(all(value is False for value in body['scope'].values()))

    def test_state_groups_explain_cleanup_and_uncertainty_without_copying_stage(self):
        for raw, expected in [('cleanup_version', 'cleanup'), ('uncertain', 'uncertain'), ('review', 'review')]:
            owner = SimpleNamespace(read_job={'stage': raw, 'job_kind': 'studio'},
                                    runner=SimpleNamespace(busy=False, session_open=True))
            state = diag.snapshot_from_owner(owner)
            self.assertEqual(state.operation, expected)
            self.assertEqual(state.stage, expected)
            if raw == 'uncertain':
                self.assertEqual(state.connection, 'uncertain')

    def test_firmware_and_antenna_states_are_generic(self):
        owner = SimpleNamespace(firmware_busy=True, read_job=None)
        self.assertEqual(diag.snapshot_from_owner(owner).job_kind, 'firmware')
        owner.firmware_busy = False
        owner.antenna_job = {'secret': self.secret}
        self.assertEqual(diag.snapshot_from_owner(owner).job_kind, 'antenna')

    def test_production_read_write_cancel_and_measurement_stages_are_normalized(self):
        owner = SimpleNamespace(runner=SimpleNamespace(busy=True, session_open=True), firmware_busy=False)
        for raw, expected in [('write_transaction', 'writing'), ('partial_read', 'reading'),
                              ('identify_lf', 'detecting'), ('cancel_resync', 'cleanup'),
                              ('recovery_resync', 'recovering')]:
            owner.read_job = {'stage': raw, 'job_kind': 'write'}
            state = diag.snapshot_from_owner(owner)
            self.assertEqual(state.stage, expected)
        owner.read_job = None
        owner.antenna_job = {'stage': 'measure', 'private': self.secret}
        state = diag.snapshot_from_owner(owner)
        self.assertEqual((state.job_kind, state.stage, state.operation), ('antenna', 'measuring', 'running'))
        owner.antenna_job['stage'] = 'off'
        self.assertEqual(diag.snapshot_from_owner(owner).operation, 'cleanup')

    def test_numeric_codes_are_bounded_without_coercion(self):
        _, body = self.document(diag.DiagnosticSnapshot(error_codes=tuple(range(60))))
        self.assertEqual(body['recent_completion_codes'], list(range(44, 60)))
        _, body = self.document(diag.DiagnosticSnapshot(error_codes=(True, 1.0, '5', -(2**31), 2**31-1, 2**31)))
        self.assertEqual(body['recent_completion_codes'], [-(2**31), 2**31-1])

    def test_client_hash_never_includes_path_filename_or_binary_contents(self):
        directory = self.root / self.secret
        directory.mkdir()
        path = directory / 'клиент-Ω.exe'
        payload = (self.secret + '\\Users\\private-person\\library.sqlite3').encode()
        path.write_bytes(payload)
        prepared, body = self.document(client=path)
        self.assertEqual(body['client'], {'status': 'hashed', 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)})
        self.assertNotIn(self.secret.encode(), prepared.document)
        self.assertNotIn(str(self.root).encode(), prepared.document)
        self.assertNotIn(b'private-person', prepared.document)

    def test_missing_and_unreadable_client_report_only_fixed_reasons(self):
        _, body = self.document(client=self.root / self.secret)
        self.assertEqual(body['client'], {'status': 'missing'})
        path = self.root / 'fake.exe'
        path.touch()
        with patch('pathlib.Path.open', side_effect=PermissionError(str(path))):
            prepared, body = self.document(client=path)
        self.assertEqual(body['client'], {'status': 'unreadable'})
        self.assertNotIn(str(path).encode(), prepared.document)

    def test_large_client_is_not_read_without_a_bound(self):
        path = self.root / 'fake.exe'
        path.write_bytes(bytes(400))
        with patch.object(diag, 'MAX_CLIENT_BYTES', 100), patch('pathlib.Path.open', side_effect=AssertionError('Should not open a large file')):
            _, body = self.document(client=path)
        self.assertEqual(body['client'], {'status': 'too_large'})

    def test_client_replaced_during_hashing_has_no_claimed_fingerprint(self):
        path = self.root / 'fake.exe'
        path.write_bytes(bytes(100))
        old_stat = path.stat()
        new_stat = SimpleNamespace(st_dev=old_stat.st_dev, st_ino=old_stat.st_ino,
                                   st_size=old_stat.st_size, st_mtime_ns=old_stat.st_mtime_ns + 1)
        with patch('pathlib.Path.stat', side_effect=[old_stat, old_stat, new_stat]):
            _, body = self.document(client=path)
        self.assertEqual(body['client'], {'status': 'changed_during_read'})

    def test_preparation_can_be_cancelled(self):
        with self.assertRaises(diag.DiagnosticsCancelled):
            diag.prepare_diagnostics(diag.DiagnosticSnapshot(), cancelled=lambda: True)
        path = self.root / 'fake.exe'
        path.write_bytes(bytes(100))
        calls = iter([False, True])
        with self.assertRaises(diag.DiagnosticsCancelled):
            diag.prepare_diagnostics(diag.DiagnosticSnapshot(), path, cancelled=lambda: next(calls))

    def test_zip_has_only_reviewed_document_static_readme_and_integrity_manifest(self):
        prepared, _ = self.document()
        target = self.root / 'report.zip'
        result = diag.export_diagnostics(prepared, target)
        self.assertEqual(result['files'], 3)
        self.assertLess(result['bytes'], 8192)
        self.assertEqual(result['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {'diagnostics.json', 'README.txt', 'manifest.json'})
            self.assertEqual(archive.read('diagnostics.json'), prepared.document)
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(set(manifest['files']), {'diagnostics.json', 'README.txt'})
            for name, facts in manifest['files'].items():
                content = archive.read(name)
                self.assertEqual(facts, {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})

    def test_failed_atomic_replace_preserves_existing_archive_and_removes_own_temp(self):
        target = self.root / 'report.zip'
        target.write_bytes(b'existing archive')
        prepared, _ = self.document()
        with patch.object(diag.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                diag.export_diagnostics(prepared, target)
        self.assertEqual(target.read_bytes(), b'existing archive')
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_unreviewed_or_changed_documents_are_rejected(self):
        target = self.root / 'report.zip'
        with self.assertRaises((ValueError, TypeError)):
            diag.export_diagnostics({'private': self.secret}, target)
        with self.assertRaises(ValueError):
            diag.export_diagnostics(diag.PreparedDiagnostics(b'{}'), target)
        original, _ = self.document()
        changed = replace(original, document=original.document.replace(b'"unknown"', b'"usb"', 1))
        with self.assertRaises(ValueError):
            diag.export_diagnostics(changed, target)
        self.assertFalse(target.exists())

    def test_prepare_requires_explicit_snapshot_not_an_arbitrary_mapping(self):
        with self.assertRaises(TypeError):
            diag.prepare_diagnostics({'settings': self.secret})

    def test_export_cannot_replace_profile_files_or_write_with_a_non_zip_extension(self):
        prepared, _ = self.document()
        profile = self.root / 'profile'
        profile.mkdir()
        originals = {'library.sqlite3': b'database', 'settings.json': b'settings', 'dump.zip': b'private dump'}
        for name, content in originals.items():
            path = profile / name
            path.write_bytes(content)
            with self.assertRaises(ValueError):
                diag.export_diagnostics(prepared, path, active_profile=profile)
            self.assertEqual(path.read_bytes(), content)
        with self.assertRaises(ValueError):
            diag.export_diagnostics(prepared, profile / 'new.zip', active_profile=profile)
        with self.assertRaises(ValueError):
            diag.export_diagnostics(prepared, self.root / 'application.exe')
        target = self.root / 'outside.zip'
        diag.export_diagnostics(prepared, target, active_profile=profile)
        self.assertTrue(target.is_file())

    def test_resolved_profile_alias_is_also_blocked(self):
        profile = self.root / 'profile'
        profile.mkdir()
        target = profile / 'nested' / '..' / 'report.zip'
        with self.assertRaises(ValueError):
            diag.validate_destination(target, active_profile=profile)


class DiagnosticDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = QWidget()
        self.addCleanup(self.owner.deleteLater)
        self.owner._active_transport = 'usb'
        self.owner.read_job = None
        self.owner.runner = SimpleNamespace(busy=False, session_open=False, rf_lease=False)
        self.owner.client_path = QLineEdit(str(self.root / 'missing-private-client.exe'))
        self.owner.diagnostic_error_codes = deque([-5])
        self.dialog = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.dialog is not None:
            self.dialog.reject()
            self.wait(lambda: self.dialog.worker is None)
            self.dialog.deleteLater()
        set_language('ru')

    def wait(self, predicate):
        end = time.monotonic() + 5
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def open_dialog(self):
        self.dialog = DiagnosticBundleDialog(self.owner)
        self.dialog.show()
        self.wait(lambda: self.dialog.worker is None)
        return self.dialog

    def test_review_and_save_are_offline_and_do_not_change_owner(self):
        self.owner.settings = {'secret': 'A0A1A2A3A4A5'}
        self.owner.operation_output = 'UID 11223344556677 PRIVATE KEY'
        self.owner.current_command = 'hf mf wrbl -k A0A1A2A3A4A5'
        dialog = self.open_dialog()
        self.assertTrue(dialog.save_button.isEnabled())
        before = dialog.preview.toPlainText()
        target = self.root / 'report.zip'
        self.assertIsNotNone(dialog.save_to(target))
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read('diagnostics.json').decode(), before)
            self.assertNotIn(b'A0A1A2A3A4A5', archive.read('diagnostics.json'))
            self.assertNotIn(b'11223344556677', archive.read('diagnostics.json'))
        self.assertEqual(self.owner.settings, {'secret': 'A0A1A2A3A4A5'})
        self.assertIsNone(self.owner.read_job)

    def test_both_languages_show_exclusions_and_a_save_action(self):
        for lang, title, save in [('en', 'Diagnostic package', 'Save ZIP'), ('ru', 'Отчёт для диагностики', 'Сохранить ZIP')]:
            set_language(lang)
            dialog = self.open_dialog()
            self.assertEqual(dialog.windowTitle(), title)
            self.assertIn(save, dialog.save_button.text())
            dialog.reject()
            dialog.deleteLater()
            self.dialog = None

    def test_refresh_captures_new_state_but_preserves_previous_export_as_a_snapshot(self):
        dialog = self.open_dialog()
        before = dialog.prepared
        self.owner.runner.busy = True
        self.owner.read_job = {'stage': 'uncertain', 'job_kind': 'studio', 'private': 'SECRET'}
        dialog.refresh_button.click()
        self.wait(lambda: dialog.worker is None)
        self.assertEqual(json.loads(before.document)['state']['operation'], 'idle')
        self.assertEqual(json.loads(dialog.prepared.document)['state']['operation'], 'uncertain')
        self.assertNotIn('SECRET', dialog.preview.toPlainText())

    def test_close_during_preparation_cancels_without_blocking_the_gui(self):
        started, release = threading.Event(), threading.Event()
        original = diag.prepare_diagnostics

        def wait_prepare(snapshot, client_path=None, **kwargs):
            started.set()
            release.wait(3)
            return original(snapshot, client_path, **kwargs)

        with patch('pm3gui.diagnostics_ui.prepare_diagnostics', side_effect=wait_prepare):
            self.dialog = DiagnosticBundleDialog(self.owner)
            self.dialog.show()
            self.wait(started.is_set)
            self.dialog.reject()
            self.assertIsNotNone(self.dialog.worker)
            self.assertTrue(self.dialog.worker.isInterruptionRequested())
            release.set()
            self.wait(lambda: self.dialog.worker is None)
            self.assertFalse(self.dialog.isVisible())
            self.assertIsNone(self.dialog.prepared)

    def test_save_button_uses_reviewed_payload_and_native_file_selection(self):
        dialog = self.open_dialog()
        target = self.root / 'report.zip'
        with patch('pm3gui.diagnostics_ui.QFileDialog.getSaveFileName', return_value=(str(target), 'ZIP (*.zip)')):
            dialog.save_button.click()
        self.assertTrue(target.is_file())

    def test_save_failure_is_localized_without_echoing_private_path(self):
        set_language('en')
        dialog = self.open_dialog()
        self.assertIsNone(dialog.save_to(self.root / 'private-person' / 'report.zip'))
        self.assertIn('Could not save', dialog.status.text())
        self.assertNotIn('private-person', dialog.status.text())

    def test_dialog_cannot_save_inside_current_profile(self):
        set_language('en')
        profile = self.root / 'profile'
        profile.mkdir()
        self.owner.data_dir = profile
        original = profile / 'private.zip'
        original.write_bytes(b'private library file')
        dialog = self.open_dialog()
        self.assertIsNone(dialog.save_to(original))
        self.assertEqual(original.read_bytes(), b'private library file')
        self.assertIn('outside the current profile', dialog.status.text())
        self.assertIsNotNone(dialog.save_to(self.root / 'public-report.zip'))


if __name__ == '__main__':
    unittest.main()
