"""Exercise desktop library workflows with synthetic files and patched dialogs."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from pm3gui.app import MainWindow, format_hex
from pm3gui.core import DumpData, parse_hex
from pm3gui.keys import parse_dictionary
from pm3gui.operations import (
    CLASSIC_GEOMETRIES, key_file_for_session, recovery_key_file_for_session,
    sector_trailer,
)


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for target, options in (
            ("pm3gui.app.available_ports", {"return_value": []}),
            ("pm3gui.app.discover_client", {"return_value": ""}),
            ("pm3gui.app.QMessageBox.question", {"return_value": QMessageBox.StandardButton.Cancel}),
            ("pm3gui.app.QMessageBox.warning", {}),
            ("pm3gui.app.ClientRunner.run", {}),
            ("pm3gui.app.ClientRunner.run_operation", {}),
            ("pm3gui.app.ClientRunner.cancel", {}),
        ):
            mock = self.enterContext(patch(target, **options))
            if target.endswith("question"):
                self.question = mock
            elif target.endswith("warning"):
                self.warning = mock
            elif target.endswith(".run"):
                self.run_client = mock
            elif target.endswith(".run_operation"):
                self.run_operation = mock
                self.run_operation.side_effect = lambda settings, operation, **kwargs: operation.render()
            elif target.endswith(".cancel"):
                self.cancel_client = mock
        self.window = MainWindow(self.root / "library")

    def tearDown(self):
        self.window.dirty = False
        self.window.read_job = None
        self.window.antenna_job = None
        self.window.antenna_timer.stop()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.processEvents()
        self.temp.cleanup()

    def create_card(self, name="Синтетическая карта", data=b"Original content", **metadata):
        return self.window.library.create_card(name, DumpData(data, "bin", {}, 16), **metadata)

    def select_card(self, card_id):
        for index in range(self.window.cards.count()):
            item = self.window.cards.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == card_id:
                self.window.cards.setCurrentItem(item)
                return
        self.fail("Synthetic card is missing from visible list")

    def test_initial_pages_and_navigation_do_not_contact_hardware(self):
        self.assertEqual(self.window.stack.count(), 6)
        self.assertEqual(self.window.cards.count(), 0)
        self.assertEqual(self.window.detail_stack.currentIndex(), 0)
        self.assertFalse(self.window.save_button.isEnabled())
        for index in range(5):
            self.window.nav_buttons[index].click()
            self.assertEqual(self.window.stack.currentIndex(), index)
            self.assertTrue(self.window.nav_buttons[index].isChecked())
            self.assertEqual(sum(button.isChecked() for button in self.window.nav_buttons), 1)
        self.warning.assert_not_called()
        self.run_client.assert_not_called()
        self.run_operation.assert_not_called()

    def test_serial_open_failure_is_reported_as_connection_failure_before_card_detection(self):
        self.window.read_card()
        self.finish_step('[!!] UART CreateFile failed for COM5 (Win32 error 1168)\n'
                         '[!] ERROR: invalid serial port COM5\n', code=-5)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.assertIn('связь с платой', self.warning.call_args.args[2])
        self.assertNotIn('тип карты', self.warning.call_args.args[2])
        self.assertEqual([c.args[1] for c in self.run_client.call_args_list],['hf search','hw version'])
        self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
        self.assertIsNone(self.window.read_job)
        self.run_operation.assert_not_called()

    def test_explicit_hf_absence_is_not_reported_as_a_connection_failure(self):
        for code, output in ((-10, '[!] No known/supported 13.56 MHz tags found\n'),
                             (0, '[!] No known/supported 13.56 MHz tags found\n'),
                             (-10, '\x1b[31m[!] No known/supported 13.56 MHz tags found\x1b[0m\r\n')):
            with self.subTest(code=code, output=output):
                self.run_client.reset_mock();self.warning.reset_mock()
                self.window.read_hf_card();self.finish_step(output, code=code)
                self.assertIsNone(self.window.read_job)
                self.assertIn('Карта не обнаружена', self.warning.call_args.args[2])
                self.assertNotIn('связь с платой', self.warning.call_args.args[2])
                self.assertEqual(self.run_client.call_count, 1)
                self.run_operation.assert_not_called()

    def test_explicit_lf_absence_is_not_reported_as_a_connection_failure(self):
        for code in (0, -10):
            with self.subTest(code=code):
                self.run_client.reset_mock();self.warning.reset_mock()
                self.window.read_lf_card();self.finish_step('[-] No known 125/134 kHz tags found!\n', code=code)
                self.assertIsNone(self.window.read_job)
                self.assertIn('Карта не обнаружена', self.warning.call_args.args[2])
                self.assertNotIn('связь с платой', self.warning.call_args.args[2])
                self.assertEqual(self.run_client.call_count, 1)
                self.run_operation.assert_not_called()

    def test_auto_search_falls_back_to_lf_only_after_explicit_hf_absence(self):
        for code in (0, -10):
            with self.subTest(code=code):
                self.run_client.reset_mock();self.warning.reset_mock()
                self.window.read_card();self.finish_step('[!] No known/supported 13.56 MHz tags found', code=code)
                self.assertEqual(self.window.read_job['stage'], 'identify_lf')
                self.assertEqual(self.run_client.call_args.args[1], 'lf search')
                self.warning.assert_not_called()
                self.finish_step('[-] No known 125/134 kHz tags found!', code=-10)
                self.assertIsNone(self.window.read_job)
                self.assertIn('Карта не обнаружена', self.warning.call_args.args[2])
                self.assertEqual(self.run_client.call_count, 2)

    def test_transport_codes_and_mixed_errors_are_never_hidden_by_no_tags_text(self):
        empty = '[!] No known/supported 13.56 MHz tags found\n'
        cases = [(empty, code) for code in (-5, -2, -3, -4, -8, -1, 1)]
        cases += [('', -4), ('No card response', -10), ('Unrecognized failure', -10),
                  ('No response from Proxmark3\n'+empty, -10),
                  ('UART CreateFile failed for COM5\n'+empty, -10),
                  ('[-] No known 125/134 kHz tags found!', -10)]
        for output, code in cases:
            with self.subTest(code=code, output=output):
                self.run_client.reset_mock();self.warning.reset_mock()
                self.window.read_card();self.finish_step(output, code=code)
                self.assertEqual(self.window.read_job['stage'],'cancel_resync')
                self.assertIn('связь с платой', self.warning.call_args.args[2])
                self.assertIn(f'код {code}', self.warning.call_args.args[2])
                self.assertEqual([c.args[1] for c in self.run_client.call_args_list],['hf search','hw version'])
                self.run_operation.assert_not_called()
                self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
                self.assertIsNone(self.window.read_job)

    def test_classic_info_requires_one_matching_uid_before_checking_keys(self):
        for output in ('[+] UID: A1B2C3D4\n[+] UID: 11223344\n[+] SAK: 08\n',
                       '[+] UID: A1B2C3D4\n[!] Multiple cards found\n',
                       '[!] No card response\n', '[+] UID: 11223344\n'):
            with self.subTest(output=output):
                self.window.read_card()
                self.finish_step('[+] UID: A1B2C3D4\n[+] MIFARE Classic 1K\n')
                count = self.run_operation.call_count
                self.finish_step(output)
                self.assertIsNone(self.window.read_job)
                self.assertEqual(self.run_operation.call_count, count)
                self.assertEqual(self.window.key_library.list_keys(), [])
                self.assertIn('UID', self.warning.call_args.args[2])

    def test_cancel_manual_recovery_and_lf_sim_requires_device_response(self):
        from pm3gui.operations import Operation
        for operation in (Operation('hf_tool', hf_command='hf mf hardnested'),
                          Operation('hf_tool', hf_command='hf mf autopwn'),
                          Operation('lf_tool', lf_command='lf em 410x sim', lf_args='--id AABBCCDDEE')):
            with self.subTest(operation=operation.render()):
                self.window.current_session = self.window._new_session()
                self.window._start_step(operation, 300000)
                with patch.object(type(self.window.runner), 'busy', new_callable=PropertyMock, return_value=True):
                    self.window.cancel_operation()
                self.finish_step('', code=-2)
                self.assertEqual(self.window.read_job['stage'], 'cancel_resync')
                self.assertEqual(self.run_client.call_args.args[1], 'hw version')
                self.assertFalse(self.window.transport_combo.isEnabled())
                self.finish_step('[+] Communicating with PM3 over USB-CDC\n[ ARM ]\n  OS......... Iceman/test-build\n')
                self.assertIsNone(self.window.read_job)
                self.assertIn('Устройство отвечает', self.window.device_info.text())
        self.warning.assert_not_called()

    def test_cancel_after_completed_manual_recovery_does_not_leave_pending_resync(self):
        from pm3gui.operations import Operation
        self.window.current_session = self.window._new_session()
        self.window._start_step(Operation('hf_tool', hf_command='hf mf hardnested'), 300000)
        self.finish_step('[-] No valid key found\n')
        self.assertFalse(self.window.runner.busy)
        self.window.cancel_operation()
        self.assertFalse(self.window.resync_after_cancel)
        self.window.run_command('hw version', 45000)
        self.finish_step('[ ARM ]\n  OS......... Iceman/test-build\n')
        self.assertIsNone(self.window.read_job)

    def test_manual_rf_timeout_and_auth2_guard_check_board_without_replaying_rf(self):
        from pm3gui.operations import Operation
        for operation in (Operation('hf_tool', hf_command='hf mf hardnested'),
                          Operation('hf_tool', hf_command='hf mf autopwn'),
                          Operation('lf_tool', lf_command='lf em 410x sim', lf_args='--id AABBCCDDEE')):
            for code in (-3, -4):
                with self.subTest(operation=operation.render(), code=code):
                    self.window.current_session=self.window._new_session()
                    self.window._start_step(operation, 300000)
                    operations=self.run_operation.call_count
                    self.finish_step('[!] RF acquisition interrupted\n', code=code)
                    self.assertEqual(self.window.read_job['stage'], 'cancel_resync')
                    self.assertEqual(self.run_client.call_args.args[1], 'hw version')
                    self.assertFalse(self.window.transport_combo.isEnabled())
                    self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
                    self.assertIsNone(self.window.read_job)
                    self.assertEqual(self.run_operation.call_count, operations)
        self.warning.assert_not_called()

    def test_ordinary_tool_timeout_does_not_schedule_recovery_resync(self):
        from pm3gui.operations import Operation
        self.window.current_session=self.window._new_session()
        self.window._start_step(Operation('hf_tool', hf_command='hf mfu info'), 300000)
        self.finish_step('[!] Acquisition interrupted\n', code=-3)
        self.assertIsNone(self.window.read_job)
        self.run_client.assert_not_called()
        self.assertEqual(self.run_operation.call_count, 1)

    def test_cancel_resync_rejects_client_os_offline_missing_channel_and_wrong_channel(self):
        from pm3gui.operations import Operation
        for output in ('[Client] OS: Windows\n',
                       '[+] Communicating with PM3 over USB-CDC\n[Client] OS: Windows\n',
                       '[=] OFFLINE mode\n[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n',
                       'OS......... Iceman/test-build\n',
                       '[+] Communicating with PM3 over FPC UART\nOS......... Iceman/test-build\n'):
            with self.subTest(output=output):
                self.window.current_session=self.window._new_session()
                self.window._start_step(Operation('hf_tool', hf_command='hf mf hardnested'), 300000)
                self.finish_step('', code=-4)
                operations=self.run_operation.call_count
                self.finish_step(output)
                self.assertEqual(self.window.read_job['stage'], 'cancel_resync_failed')
                self.assertFalse(self.window.transport_combo.isEnabled())
                self.assertEqual(self.run_operation.call_count, operations)
                self.assertIn('Операции заблокированы', self.warning.call_args.args[2])
                self.assertNotIn('Устройство отвечает', self.window.device_info.text())
                self.window.cancel_operation()
                self.assertEqual(self.run_client.call_args.args[1], 'hw version')
                self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
                self.assertIsNone(self.window.read_job)

    def test_resync_accepts_known_persistent_channel_only_with_fresh_device_os(self):
        from pm3gui.operations import Operation
        self.window.runner._session.connected=True
        self.window.runner._session.transport='usb'
        self.window.current_session=self.window._new_session()
        self.window._start_step(Operation('hf_tool', hf_command='hf mf hardnested'), 300000)
        self.finish_step('', code=-3)
        self.finish_step('OS......... Iceman/test-build\n')
        self.assertIsNone(self.window.read_job)
        self.assertIn('Устройство отвечает', self.window.device_info.text())
        self.warning.assert_not_called()
        self.window._start_step(Operation('hf_tool', hf_command='hf mf hardnested'), 300000)
        self.finish_step('', code=-3)
        self.finish_step('[Client] OS: Windows\n')
        self.assertEqual(self.window.read_job['stage'], 'cancel_resync_failed')
        self.assertIn('Операции заблокированы', self.warning.call_args.args[2])

    def test_bulk_crc_failure_fences_future_operations_until_fresh_os(self):
        from pm3gui.operations import Operation
        self.window.current_session=self.window._new_session()
        self.window._start_step(Operation('expert_tool',client_command='data hexsamples',client_args='-n 30000'))
        self.finish_step('[!] invalid CRC; partial data discarded\n',code=-10)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.assertEqual(self.run_client.call_args.args[1],'hw version')
        self.finish_step('[Client] OS: Windows\n')
        self.assertEqual(self.window.read_job['stage'],'cancel_resync_failed')
        calls=self.run_client.call_count
        self.window.run_command('hf search',90000)
        self.window.read_lf_card()
        self.assertEqual(self.run_client.call_count,calls)
        self.assertEqual(self.run_operation.call_count,1)
        self.window.cancel_operation()
        self.assertEqual(self.run_client.call_args.args[1],'hw version')
        self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
        self.assertIsNone(self.window.read_job)
        self.assertTrue(self.window.transport_combo.isEnabled())
        self.assertEqual(self.run_operation.call_count,1)

    def test_plain_search_stop_requires_fresh_os_without_repeating_search(self):
        for command in ('hf search','lf search'):
            with self.subTest(command=command):
                self.run_client.reset_mock()
                self.window.run_command(command,120000)
                with patch.object(type(self.window.runner),'busy',new_callable=PropertyMock,return_value=True):
                    self.window.cancel_operation()
                self.finish_step('[!] cancelled\n',code=-2)
                self.assertEqual(self.window.read_job['stage'],'cancel_resync')
                self.assertEqual([c.args[1] for c in self.run_client.call_args_list],[command,'hw version'])
                self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
                self.assertIsNone(self.window.read_job)

    def test_explicit_empty_search_does_not_create_uncertain_bulk_lock(self):
        for command,frequency in (('hf search','13.56 MHz'),('lf search','125/134 kHz')):
            with self.subTest(command=command):
                self.run_client.reset_mock();self.window.run_command(command,120000)
                self.finish_step(f'[-] No known/supported {frequency} tags found!\n',code=-10)
                self.assertIsNone(self.window.read_job)
                self.assertEqual(self.run_client.call_count,1)

    def test_success_code_with_corrupt_search_output_still_requires_fence(self):
        self.window.run_command('hf search',90000)
        self.finish_step('[!] Received packet frame with invalid CRC 1234 <> 5678\n'
                         '[-] No known/supported 13.56 MHz tags found!\n',code=0)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.assertEqual(self.run_client.call_args.args[1],'hw version')
        self.run_operation.assert_not_called()

    def test_resync_timeout_and_report_failure_retain_lock(self):
        from pm3gui.operations import Operation
        self.window.current_session=self.window._new_session()
        self.window._start_step(Operation('lf_capture_search_buffer'))
        self.finish_step('[!] disconnected\n',code=-5)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.finish_step('',code=-4)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync_failed')
        self.window._card_job_failed(OSError('test log write failed'))
        self.assertEqual(self.window.read_job['stage'],'cancel_resync_failed')
        self.assertEqual(self.run_operation.call_count,1)
        self.assertFalse(self.window.transport_combo.isEnabled())

    def test_stop_keeps_ui_reserved_while_native_cancel_is_pending(self):
        self.window.run_command('hf search',90000)
        with patch.object(type(self.window.runner),'busy',new_callable=PropertyMock,return_value=True):
            self.window.cancel_operation()
        self.assertEqual(self.window.read_job['stage'],'cancel_wait')
        self.window._busy_changed(False)
        self.assertFalse(self.window.transport_combo.isEnabled())
        calls=self.run_client.call_count
        self.window.run_command('lf search',120000)
        self.assertEqual(self.run_client.call_count,calls)
        self.window._card_job_failed(OSError('cancel report unavailable'))
        self.assertEqual(self.window.read_job['stage'],'cancel_resync_failed')
        self.assertFalse(self.window.resync_after_cancel)

    def test_recovery_resync_rejects_client_os_before_another_rf_operation(self):
        uid=self.prepare_classic_keys(classic_size='4k',missing_key_slots={(39,'A'),(39,'B')})
        keys=len(self.window.key_library.list_card_keys(uid))
        self.finish_step('[!] command execution time out\n')
        self.assertEqual(self.window.read_job['stage'], 'recovery_resync')
        operations=self.run_operation.call_count
        self.finish_step('[Client] OS: Windows\n')
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.run_operation.call_count, operations)
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), keys)
        self.assertIn('устройство не подтвердило', self.warning.call_args.args[2])

    def reopen_with_settings(self, settings):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        target = self.root / 'library' / 'settings.json'
        target.write_text(json.dumps(settings), encoding='utf-8')
        original = target.read_bytes()
        self.window = MainWindow(self.root / 'library')
        return target, original

    def select_transport(self, value):
        self.window.transport_combo.setCurrentIndex(self.window.transport_combo.findData(value))

    def test_old_usb_settings_migrate_without_copying_port_into_bluetooth(self):
        target, original = self.reopen_with_settings({'client_path': 'test-client.exe', 'port': 'COM3', 'extra': 'keep'})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.window._transport(), 'usb')
        self.assertEqual(self.window._port(), 'COM3')
        with patch('pm3gui.app.available_ports', return_value=[
            {'device': 'COM3', 'description': 'Proxmark', 'kind': 'usb'}]):
            self.select_transport('blueshark')
        self.assertEqual(self.window._port(), '')
        self.window.ports.setEditText('COM42')
        self.window.save_settings()
        saved = json.loads(target.read_text())
        self.assertEqual((saved['usb_port'], saved['bluetooth_port'], saved['transport']), ('COM3', 'COM42', 'blueshark'))
        self.assertEqual(saved['extra'], 'keep')
        self.select_transport('usb')
        self.assertEqual(self.window._port(), 'COM3')
        self.select_transport('blueshark')
        self.assertEqual(self.window._port(), 'COM42')

    def test_saved_profiles_restore_the_selected_transport_and_separate_ports(self):
        target, original = self.reopen_with_settings({'transport': 'blueshark', 'port': 'COM42',
                                                      'usb_port': 'COM3', 'bluetooth_port': 'COM42'})
        self.assertEqual(self.window._transport(), 'blueshark')
        self.assertEqual(self.window._port(), 'COM42')
        self.assertEqual(target.read_bytes(), original)
        self.select_transport('usb')
        self.window.ports.setEditText('COM7')
        self.select_transport('blueshark')
        self.assertEqual(self.window._port(), 'COM42')
        self.window.save_settings()
        saved = json.loads(target.read_text())
        self.assertEqual((saved['usb_port'], saved['bluetooth_port']), ('COM7', 'COM42'))

    def test_legacy_bluetooth_transport_migrates_only_its_active_port(self):
        target, original = self.reopen_with_settings({'transport': 'blueshark', 'port': 'COM42'})
        self.assertEqual(self.window._transport(), 'blueshark')
        self.assertEqual(self.window._port(), 'COM42')
        self.assertEqual(self.window.port_profiles, {'usb': '', 'blueshark': 'COM42', 'tcp': ''})
        self.assertEqual(target.read_bytes(), original)
        self.select_transport('usb')
        self.assertEqual(self.window._port(), '')

    def test_port_selection_requires_a_unique_matching_kind_and_preserves_explicit_choice(self):
        ports = [{'device': 'COM12', 'description': 'SPP', 'kind': 'bluetooth'},
                 {'device': 'COM3', 'description': 'Proxmark', 'kind': 'usb'},
                 {'device': 'COM2', 'description': 'Other USB', 'kind': 'other'},
                 {'device': 'COM11', 'description': 'Proxmark2', 'kind': 'usb'}]
        with patch('pm3gui.app.available_ports', return_value=ports):
            self.window.refresh_ports()
            self.assertEqual(self.window._port(), '')
            self.assertEqual([self.window.ports.itemData(i) for i in range(4)], ['COM3', 'COM11', 'COM2', 'COM12'])
            self.select_transport('blueshark')
            self.assertEqual(self.window._port(), 'COM12')
            self.window.ports.setEditText('COM99')
            self.window.refresh_ports()
            self.assertEqual(self.window._port(), 'COM99')
        self.window.ports.setEditText('')
        with patch('pm3gui.app.available_ports', return_value=ports + [
            {'device': 'COM13', 'description': 'SPP2', 'kind': 'bluetooth'}]):
            self.window.refresh_ports()
            self.assertEqual(self.window._port(), '')
        self.run_client.assert_not_called()

    def test_empty_tcp_profile_shows_real_editor_placeholder_without_discovery_or_connection(self):
        with patch('pm3gui.app.available_ports', side_effect=AssertionError('TCP must not discover ports')), \
                patch('socket.create_connection', side_effect=AssertionError('TCP must not connect')), \
                patch('socket.getaddrinfo', side_effect=AssertionError('TCP must not resolve hosts')):
            self.select_transport('tcp')
            self.window.refresh_ports()
        editor = self.window.ports.lineEdit()
        self.assertEqual(editor.placeholderText(), 'tcp:192.168.1.10:18888')
        self.assertEqual(self.window.ports.currentText(), '')
        self.assertEqual(self.window._port(), '')
        self.assertEqual(self.window.ports.count(), 0)
        self.assertIn('вручную', editor.toolTip())
        self.assertIn('Blue Shark', editor.toolTip())
        self.assertIn('Bluetooth COM', editor.toolTip())
        self.assertEqual(editor.toolTip(), self.window.ports.toolTip())
        self.run_client.assert_not_called();self.run_operation.assert_not_called()

    def test_tcp_editor_preserves_manual_endpoint_and_separate_pinned_com_profiles(self):
        target, original = self.reopen_with_settings({'transport': 'tcp',
            'tcp_endpoint': 'tcp:[::1]:18888', 'usb_port': 'COM17', 'bluetooth_port': 'COM42'})
        self.assertEqual(self.window._port(), 'tcp:[::1]:18888')
        self.window.refresh_ports()
        self.assertEqual(self.window._port(), 'tcp:[::1]:18888')
        self.window.ports.setEditText('tcp:pm3.local:18888')
        self.window.refresh_ports()
        self.assertEqual(self.window._port(), 'tcp:pm3.local:18888')
        ports = [{'device': 'COM17', 'description': 'Proxmark', 'kind': 'usb'},
                 {'device': 'COM42', 'description': 'Blue Shark', 'kind': 'bluetooth'}]
        with patch('pm3gui.app.available_ports', return_value=ports):
            self.select_transport('usb')
            self.assertEqual(self.window.ports.currentData(), 'COM17')
            self.assertEqual(self.window.ports.lineEdit().placeholderText(), 'Выберите USB COM-порт')
            self.assertEqual(self.window.ports.toolTip(), '')
            self.assertEqual(self.window.ports.lineEdit().toolTip(), '')
            self.select_transport('blueshark')
            self.assertEqual(self.window.ports.currentData(), 'COM42')
            self.assertIn('Bluetooth COM', self.window.ports.lineEdit().placeholderText())
            self.assertEqual(self.window.ports.lineEdit().toolTip(), '')
            self.select_transport('tcp')
        self.assertEqual(self.window._port(), 'tcp:pm3.local:18888')
        self.assertEqual(self.window.port_profiles, {'usb': 'COM17', 'blueshark': 'COM42',
                                                    'tcp': 'tcp:pm3.local:18888'})
        self.assertEqual(target.read_bytes(), original)
        self.run_client.assert_not_called();self.run_operation.assert_not_called()

    def test_tcp_endpoint_help_has_explicit_english_manual_bridge_guidance(self):
        from pm3gui.i18n import language, set_language
        previous = language()
        try:
            set_language('en')
            self.select_transport('tcp')
            tooltip = self.window.ports.lineEdit().toolTip()
            self.assertEqual(self.window.ports.lineEdit().placeholderText(), 'tcp:192.168.1.10:18888')
            self.assertIn('Proxmark network bridge', tooltip)
            self.assertIn('manually', tooltip)
            self.assertIn('Blue Shark uses a Bluetooth COM port', tooltip)
        finally:
            set_language(previous)
        self.run_client.assert_not_called();self.run_operation.assert_not_called()

    def test_connection_check_requires_current_os_and_matching_channel(self):
        self.window.ports.setEditText('COM3')
        self.window.run_command('hw version', 45000)
        self.assertEqual(self.run_client.call_args.args[0].transport, 'usb')
        self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
        self.assertIn('USB-CDC', self.window.connection_status.text())
        self.assertIn('подтверждено', self.window.connection_status.text())
        self.select_transport('blueshark')
        self.assertIn('не проверено', self.window.connection_status.text())
        self.assertNotIn('подтверждено', self.window.device_info.text())
        self.window.ports.setEditText('COM42')
        self.window.run_command('hw version', 45000)
        self.assertEqual(self.run_client.call_args.args[0].transport, 'blueshark')
        self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
        self.assertIn('но устройство ответило через USB', self.window.connection_status.text())
        self.window.run_command('hw version', 45000)
        self.finish_step('[+] Communicating with PM3 over FPC UART\nOS......... Iceman/test-build\n')
        self.assertIn('FPC UART', self.window.connection_status.text())
        self.assertIn('подтверждено', self.window.connection_status.text())
        # The journal contains both successful checks, but this run is offline.
        self.window.run_command('hw version', 45000)
        self.finish_step('Client: Iceman/test-build OS:Windows\n[=] OFFLINE mode\n')
        self.assertNotIn('подтверждено', self.window.connection_status.text())
        self.assertIn('не подтвердил', self.window.connection_status.text())
        self.window.run_command('hw version', 45000)
        self.finish_step('[+] Communicating with PM3 over FPC UART\nOS......... Iceman/test-build\n', code=-2)
        self.assertIn('кодом -2', self.window.connection_status.text())

    def test_edited_profile_during_connection_check_cannot_display_stale_success(self):
        self.window.ports.setEditText('COM3')
        self.window.run_command('hw version', 45000)
        self.assertIn('Проверяем', self.window.connection_status.text())
        self.window.ports.setEditText('COM7')
        self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
        self.assertIn('изменились', self.window.connection_status.text())
        self.assertNotIn('подтверждено', self.window.connection_status.text())

    def test_windows_bluetooth_button_opens_only_fixed_settings_url(self):
        self.select_transport('blueshark')
        with patch('pm3gui.app.QDesktopServices.openUrl', return_value=True) as opened:
            self.window.bluetooth_settings_button.click()
        self.assertEqual(opened.call_args.args[0].toString(), 'ms-settings:bluetooth')
        self.run_client.assert_not_called()

    def test_keep_connection_defaults_on_and_setting_is_saved(self):
        self.assertTrue(self.window.keep_connection.isChecked())
        self.window.keep_connection.setChecked(False)
        self.window.save_settings()
        saved = json.loads(self.window.settings_path.read_text(encoding='utf-8'))
        self.assertIs(saved['keep_connection'], False)
        self.window.run_command('hw version', 45000)
        self.assertFalse(self.run_client.call_args.args[0].keep_open)

    def test_connection_button_disconnects_an_idle_session_and_profile_change_closes_it(self):
        self.window.runner._session.connected = True
        self.window._session_changed(True, 'Подключено')
        self.assertEqual(self.window.session_button.text(), 'Отключить')
        with patch.object(self.window.runner, 'disconnect') as disconnect:
            self.window.session_button.click()
            disconnect.assert_called_once()
        with patch.object(self.window.runner, 'disconnect') as disconnect:
            self.window.ports.setEditText('COM9')
            disconnect.assert_called_once()
        self.window.runner._session.connected = False
        self.window._session_changed(False, 'Потеряна связь')
        self.assertEqual(self.window.session_button.text(), 'Подключить')
        self.assertIn('Потеряна связь', self.window.connection_status.text())

    def test_persistent_version_uses_verified_session_channel_and_fresh_os(self):
        self.select_transport('blueshark')
        self.window.ports.setEditText('COM42')
        self.window.runner._session.connected = True
        self.window.runner._session.transport = 'blueshark'
        self.window.run_command('hw version', 45000)
        self.finish_step('OS......... Iceman/test-build\n')
        self.assertIn('подтверждено', self.window.connection_status.text())
        self.window.run_command('hw version', 45000)
        self.finish_step('client banner without device reply\n')
        self.assertNotIn('подтверждено', self.window.connection_status.text())

    def test_read_job_freezes_profile_and_locks_controls_between_steps(self):
        self.select_transport('blueshark')
        self.window.ports.setEditText('COM42')
        self.window.client_path.setText('original-client.exe')
        self.window.read_card()
        self.window._busy_changed(False)
        for widget in (self.window.transport_combo, self.window.ports, self.window.refresh_button,
                       self.window.settings_button, self.window.bluetooth_settings_button):
            self.assertFalse(widget.isEnabled())
        self.select_transport('usb')
        self.assertEqual(self.window._transport(), 'blueshark')
        self.assertEqual(self.window.transport_combo.currentData(), 'blueshark')
        # Disabled widgets can still be changed by code; subsequent stages use
        # the immutable job snapshot rather than those edited controls.
        self.window.ports.setEditText('COM3')
        self.window.client_path.setText('changed-client.exe')
        self.window.keep_connection.setChecked(False)
        self.finish_step('[+] UID: A1B2C3D4\n[+] MIFARE Classic 1K\n')
        settings = self.run_operation.call_args.args[0]
        self.assertEqual((settings.port, settings.transport, settings.client_path),
                         ('COM42', 'blueshark', 'original-client.exe'))
        self.assertTrue(settings.keep_open)
        self.assertEqual(self.window.read_job['stage'], 'info')

    def test_antenna_cleanup_hw_version_keeps_its_job_and_transport(self):
        self.select_transport('blueshark')
        self.window.ports.setEditText('COM42')
        self.window.start_antenna_measurement()
        self.window.cancel_antenna_measurement()
        self.finish_step('', code=-2)
        self.assertEqual(self.window.antenna_job['stage'], 'off')
        self.assertEqual(self.run_operation.call_args.kwargs['timeout_ms'], 15000)
        self.finish_step('[+] FPGA switched off\n')
        self.assertEqual(self.window.antenna_job['stage'], 'verify')
        settings = self.run_client.call_args.args[0]
        self.assertEqual((settings.port, settings.transport), ('COM42', 'blueshark'))
        self.assertEqual(self.run_client.call_args.kwargs['timeout_ms'], 20000)
        self.assertFalse(self.window.transport_combo.isEnabled())
        self.finish_step('OS......... Iceman/test-build\n')
        self.assertIsNone(self.window.antenna_job)
        self.assertIn('Наблюдение остановлено', self.window.device_info.text())
        self.assertIn('не проверено', self.window.connection_status.text())
        self.assertTrue(self.window.transport_combo.isEnabled())

    def test_short_usb_verification_timeout_stays_unchanged(self):
        self.window.current_session = self.window._new_session()
        self.window._start_step('hw version', 8000)
        self.assertEqual(self.run_client.call_args.kwargs['timeout_ms'], 8000)

    def test_edit_hex_save_retains_original_and_updates_search_metadata(self):
        original = b"Original content"
        edited = b"Edited content!!"
        card_id = self.create_card(data=original, uid="11223344", protocol="Synthetic")
        initial_revision = self.window.library.list_revisions(card_id)[0]["id"]
        self.window.refresh_library()
        self.select_card(card_id)
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), original)
        self.assertFalse(self.window.dirty)
        self.window.hex_edit.setPlainText(format_hex(edited))
        self.window.card_name.setText("Исправленный пример")
        self.window.notes.setText("Метка для поиска: стенд")
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.save_button.isEnabled())
        self.assertTrue(self.window.save_card())
        revisions = self.window.library.list_revisions(card_id)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(self.window.library.get_dump(card_id).data, edited)
        self.assertEqual(self.window.library.get_dump(card_id, initial_revision).data, original)
        self.assertFalse(self.window.dirty)
        self.assertEqual(self.window.revisions.count(), 2)
        self.window.search.setText("СТЕНД")
        self.assertEqual(self.window.cards.count(), 1)
        self.window.search.setText("11223344")
        self.assertEqual(self.window.cards.count(), 1)
        self.window.search.setText("неизвестная метка")
        self.assertEqual(self.window.cards.count(), 0)
        self.warning.assert_not_called()

    def test_revision_selection_loads_actual_immutable_states(self):
        card_id = self.create_card(data=b"first revision")
        first_id = self.window.library.list_revisions(card_id)[0]["id"]
        second_data = b"second revision"
        second_id = self.window.library.save_revision(
            card_id, DumpData(second_data, "bin", {}, 16), "Synthetic edit")
        self.window.load_card(card_id)
        self.assertEqual(self.window.loaded_revision, second_id)
        self.window.revisions.setCurrentIndex(self.window.revisions.findData(first_id))
        self.assertEqual(self.window.loaded_revision, first_id)
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), b"first revision")
        self.assertFalse(self.window.dirty)
        self.window.revisions.setCurrentIndex(self.window.revisions.findData(second_id))
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), second_data)
        self.assertEqual(len(self.window.library.list_revisions(card_id)), 2)
        self.question.assert_not_called()

    def test_import_pm3_json_infers_uid_and_keeps_dump(self):
        data = bytes(range(32))
        source = {
            "Created": "synthetic-test", "FileType": "mfc v2",
            "Card": {"UID": "AABBCCDD", "ATQA": "0400", "SAK": "08"},
            "blocks": {"0": data[:16].hex(), "1": data[16:].hex()},
        }
        filename = self.root / "synthetic.json"
        filename.write_text(json.dumps(source), encoding="utf-8")
        with patch("pm3gui.app.QFileDialog.getOpenFileName", return_value=(str(filename), "")):
            self.window.import_card()
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.assertEqual(self.window.cards.count(), 1)
        self.assertEqual(self.window.uid.text(), "AABBCCDD")
        self.assertEqual(self.window.protocol.text(), "mfc v2")
        self.assertEqual(self.window.card_name.text(), "synthetic")
        self.assertEqual(self.window.library.get_dump(self.window.card_id).data, data)
        self.assertEqual(self.window.loaded_dump.metadata["source_json"], source)
        self.assertFalse(self.window.dirty)
        self.warning.assert_not_called()

    def test_export_current_working_bytes_without_saving_revision(self):
        card_id = self.create_card(data=b"saved bytes")
        self.window.load_card(card_id)
        edited = bytes(range(256))
        self.window.hex_edit.setPlainText(format_hex(edited))
        target = self.root / "export.bin"
        with patch("pm3gui.app.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            self.window.export_card()
        self.assertEqual(target.read_bytes(), edited)
        self.assertEqual(self.window.library.get_dump(card_id).data, b"saved bytes")
        self.assertEqual(len(self.window.library.list_revisions(card_id)), 1)
        self.assertTrue(self.window.dirty)
        self.warning.assert_not_called()

    def test_encoding_converter_is_strict_and_does_not_silently_replace(self):
        self.window.converter_text.setPlainText("Привет")
        cp1251_index = self.window.converter_encoding.findData("cp1251")
        self.window.converter_encoding.setCurrentIndex(cp1251_index)
        self.window.convert_text()
        self.assertEqual(parse_hex(self.window.converter_hex.toPlainText()), "Привет".encode("cp1251"))
        self.window.converter_text.clear()
        self.window.convert_hex()
        self.assertEqual(self.window.converter_text.toPlainText(), "Привет")
        self.window.converter_encoding.setCurrentIndex(self.window.converter_encoding.findData("ascii"))
        previous_hex = self.window.converter_hex.toPlainText()
        self.window.convert_text()
        self.assertEqual(self.window.converter_hex.toPlainText(), previous_hex)
        self.assertIn("ascii", self.window.converter_info.text().lower())
        self.window.converter_encoding.setCurrentIndex(self.window.converter_encoding.findData("utf-8"))
        self.window.converter_hex.setPlainText("FF")
        previous_text = self.window.converter_text.toPlainText()
        self.window.convert_hex()
        self.assertEqual(self.window.converter_text.toPlainText(), previous_text)
        self.assertIn("utf-8", self.window.converter_info.text().lower())
        self.assertNotIn("\ufffd", self.window.converter_text.toPlainText())

    def test_cancel_unsaved_card_selection_keeps_editor_and_selection(self):
        first = self.create_card("first")
        second = self.create_card("second")
        self.window.refresh_library()
        self.select_card(first)
        self.window.hex_edit.setPlainText("AA BB CC DD")
        self.question.return_value = QMessageBox.StandardButton.Cancel
        self.select_card(second)
        self.question.assert_called_once()
        self.assertEqual(self.window.card_id, first)
        self.assertEqual(self.window.cards.currentItem().data(Qt.ItemDataRole.UserRole), first)
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), b"\xaa\xbb\xcc\xdd")
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.window.library.get_dump(first).data, b"Original content")

    def test_cancel_unsaved_revision_switch_preserves_working_bytes(self):
        card_id = self.create_card(data=b"old bytes")
        first_revision = self.window.library.list_revisions(card_id)[0]["id"]
        latest_revision = self.window.library.save_revision(card_id, DumpData(b"new bytes", "bin", {}, 16))
        self.window.load_card(card_id)
        self.window.hex_edit.setPlainText("AA BB")
        self.window.revisions.setCurrentIndex(self.window.revisions.findData(first_revision))
        self.question.assert_called_once()
        self.assertEqual(self.window.loaded_revision, latest_revision)
        self.assertEqual(self.window.revisions.currentData(), latest_revision)
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), b"\xaa\xbb")
        self.assertTrue(self.window.dirty)

    def test_save_before_selecting_another_card_keeps_list_in_sync(self):
        first = self.create_card("first")
        second = self.create_card("second")
        self.window.refresh_library()
        self.select_card(first)
        self.window.hex_edit.setPlainText("AA BB CC DD")
        self.question.return_value = QMessageBox.StandardButton.Save
        self.select_card(second)
        self.assertEqual(self.window.library.get_dump(first).data, b"\xaa\xbb\xcc\xdd")
        self.assertEqual(self.window.card_id, second)
        self.assertEqual(self.window.cards.currentItem().data(Qt.ItemDataRole.UserRole), second)
        self.assertFalse(self.window.dirty)

    def test_metadata_edit_on_historic_revision_does_not_replace_latest_bytes(self):
        card_id = self.create_card(data=b"BBBB")
        historic_id = self.window.library.list_revisions(card_id)[0]["id"]
        latest_id = self.window.library.save_revision(card_id, DumpData(b"CCCC", "bin", {}, 16))
        self.window.load_card(card_id)
        self.window.revisions.setCurrentIndex(self.window.revisions.findData(historic_id))
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), b"BBBB")
        self.window.notes.setText("Only metadata changed while viewing history")
        self.assertTrue(self.window.save_card())
        self.assertEqual(self.window.library.get_dump(card_id).data, b"CCCC")
        self.assertEqual(self.window.library.list_revisions(card_id)[0]["id"], latest_id)
        self.assertEqual(len(self.window.library.list_revisions(card_id)), 2)
        self.assertEqual(self.window.library.get_dump(card_id, historic_id).data, b"BBBB")
        self.assertEqual(self.window.library.get_card(card_id)["notes"],
                         "Only metadata changed while viewing history")
        self.assertFalse(self.window.dirty)
        self.warning.assert_not_called()

    def test_discard_metadata_when_switching_revision_restores_saved_values(self):
        card_id = self.create_card("Saved name", data=b"BBBB", notes="Saved note")
        historic_id = self.window.library.list_revisions(card_id)[0]["id"]
        self.window.library.save_revision(card_id, DumpData(b"CCCC", "bin", {}, 16))
        self.window.load_card(card_id)
        self.window.card_name.setText("Unsaved name")
        self.window.notes.setText("Unsaved note")
        self.assertTrue(self.window.dirty)
        self.question.return_value = QMessageBox.StandardButton.Discard
        self.window.revisions.setCurrentIndex(self.window.revisions.findData(historic_id))
        self.question.assert_called_once()
        self.assertEqual(self.window.loaded_revision, historic_id)
        self.assertEqual(self.window.revisions.currentData(), historic_id)
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), b"BBBB")
        self.assertEqual(self.window.card_name.text(), "Saved name")
        self.assertEqual(self.window.notes.text(), "Saved note")
        self.assertEqual(self.window.library.get_card(card_id)["name"], "Saved name")
        self.assertFalse(self.window.dirty)
        self.assertFalse(self.window.save_button.isEnabled())
        self.warning.assert_not_called()

    def test_decoded_cp1251_edits_sync_bytes_and_invalid_text_blocks_save(self):
        original = "Привет".encode("cp1251")
        card_id = self.create_card(data=original)
        self.window.load_card(card_id)
        self.window.encoding.setCurrentIndex(self.window.encoding.findData("cp1251"))
        self.assertEqual(self.window.decoded.toPlainText(), "Привет")
        self.window.decoded.setPlainText("Новый текст")
        edited = "Новый текст".encode("cp1251")
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), edited)
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.save_card())
        self.assertEqual(self.window.library.get_dump(card_id).data, edited)
        self.assertEqual(len(self.window.library.list_revisions(card_id)), 2)
        self.window.decoded.setPlainText("Новый текст \U0001f680")
        self.assertTrue(self.window.dirty)
        self.assertTrue(self.window.text_error)
        self.assertFalse(self.window.encoding.isEnabled())
        self.assertTrue(self.window.hex_edit.isReadOnly())
        self.assertFalse(self.window.save_card())
        self.warning.assert_called_once()
        self.assertIn("кодировку", self.warning.call_args.args[2])
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.window.library.get_dump(card_id).data, edited)
        self.assertEqual(len(self.window.library.list_revisions(card_id)), 2)
        self.assertEqual(parse_hex(self.window.hex_edit.toPlainText()), edited)
        self.question.return_value = QMessageBox.StandardButton.Cancel
        self.assertFalse(self.window._discard_allowed())
        self.assertIn("\U0001f680", self.window.decoded.toPlainText())

    def finish_step(self, text, code=0):
        self.window._log(text)
        self.window._finished(code, "Synthetic client result")

    def prepare_classic_keys(self, *, keys_only=False, both_keys=True, named_type=True,
                             classic_size='1k', missing_key_slots=None):
        """Drive the real UI state machine with synthetic, positively found keys."""
        uid = "A1B2C3D4"
        if keys_only:
            self.window.check_card_keys()
        else:
            self.window.read_card()
        self.assertEqual(self.window.read_job['stage'], 'identify')
        sectors = CLASSIC_GEOMETRIES[classic_size][0]
        sak = {'mini': '09', '1k': '08', '2k': '08', '4k': '18'}[classic_size]
        identity = f"[+] UID: A1 B2 C3 D4\n[+] SAK: {sak} [2]\n"
        if named_type:
            identity += f"[+] MIFARE Classic {classic_size.upper()}\n"
        self.finish_step(identity)
        self.assertEqual(self.window.read_job['stage'], 'info')
        self.finish_step(f"[+] UID: A1 B2 C3 D4\n[+] MIFARE Classic {classic_size.upper()}\n")
        self.assertEqual(self.window.read_job['stage'], 'keys')
        keyfile = key_file_for_session(self.window.current_session, uid)
        missing = set(missing_key_slots or ())
        if not both_keys:
            missing.update((sector, 'B') for sector in range(sectors))
        arrays = {'A': bytearray(), 'B': bytearray()}
        rows = []
        for sector in range(sectors):
            evidence = {}
            for key_type, key in [('A', '112233445566'), ('B', 'A0A1A2A3A4A5')]:
                known = (sector, key_type) not in missing
                arrays[key_type].extend(bytes.fromhex(key if known else 'FFFFFFFFFFFF'))
                evidence[key_type] = f'{key} | 1' if known else '------------ | 0'
            rows.append(f"[+] {sector:03d} | {sector_trailer(sector):03d} | {evidence['A']} | {evidence['B']} |")
        keyfile.write_bytes(bytes(arrays['A'] + arrays['B']))
        output = '\n'.join(rows) + '\n'
        self.finish_step(output)
        return uid

    def write_classic_dump(self, uid, *, changed_byte=None, classic_size='1k'):
        sectors, size = CLASSIC_GEOMETRIES[classic_size]
        data = bytearray(size)
        data[:4] = bytes.fromhex(uid)
        for sector in range(sectors):
            start = sector_trailer(sector) * 16
            data[start:start + 16] = bytes.fromhex("112233445566FF078069A0A1A2A3A4A5")
        if changed_byte is not None:
            data[16] = changed_byte
        source = {'Created': 'synthetic-test', 'FileType': 'mfc v2', 'Card': {'UID': uid},
                  'blocks': {str(index): bytes(data[index * 16:(index + 1) * 16]).hex()
                             for index in range(size // 16)}}
        (self.window.current_session / 'card.json').write_text(json.dumps(source), encoding='utf-8')
        return bytes(data)

    def successful_classic_dump_log(self, classic_size='1k'):
        sectors = CLASSIC_GEOMETRIES[classic_size][0]
        return '\n'.join(f'[+] Sector... {sector} block... {block} ( ok )'
                         for sector in range(sectors) for block in range(4 if sector < 32 else 16)) + '\n[+] Saved to json file card.json\n'

    def confirm_post_read_uid(self, uid="A1B2C3D4", *, classic_size='1k'):
        self.assertEqual(self.window.read_job['stage'], 'post_select')
        sak = {'mini': '09', '1k': '08', '2k': '08', '4k': '18'}[classic_size]
        self.finish_step(f"[+] UID: {' '.join(uid[i:i + 2] for i in range(0, len(uid), 2))}\n[+] SAK: {sak} [2]\n")

    def recovered_key_artifact(self, uid, *, classic_size='4k', missing_slots=None):
        sectors = CLASSIC_GEOMETRIES[classic_size][0]
        missing = set(missing_slots or ())
        arrays = {'A': bytearray(), 'B': bytearray()}
        rows = []
        for sector in range(sectors):
            evidence = {}
            for key_type, key in [('A', '112233445566'), ('B', 'A0A1A2A3A4A5')]:
                known = (sector, key_type) not in missing
                arrays[key_type].extend(bytes.fromhex(key if known else 'FFFFFFFFFFFF'))
                evidence[key_type] = f'{key} | N' if known else '------------ | 0'
            rows.append(f"[+] {sector:03d} | {sector_trailer(sector):03d} | {evidence['A']} | {evidence['B']} |")
        path = recovery_key_file_for_session(self.window.current_session, uid, 'gui-recovery')
        path.write_bytes(bytes(arrays['A'] + arrays['B']))
        header = '[+] Sec | Blk | key A        |res| key B        |res\n'
        return path, header + '\n'.join(rows) + '\n'

    def test_import_flipper_dictionary_and_export_compatible_file(self):
        source = self.root / 'mf_classic_dict_user.nfc'
        source.write_text('# Synthetic Flipper user dictionary\n112233445566\nA1A2A3A4A5A6\n112233445566\n', encoding='ascii')
        with patch('pm3gui.app.QFileDialog.getOpenFileNames', return_value=([str(source)], '')):
            self.window.import_keys()
        rows = self.window.key_library.list_keys()
        self.assertEqual({row['key_hex'] for row in rows}, {'112233445566', 'A1A2A3A4A5A6'})
        self.assertTrue(all(not row['verified'] for row in rows))
        self.assertEqual(self.window.key_table.rowCount(), 2)
        self.window.key_search.setText('a1a2')
        self.assertEqual(self.window.key_table.rowCount(), 1)
        target = self.root / 'export-user.nfc'
        with patch('pm3gui.app.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            self.window.export_keys()
        raw = target.read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        self.assertNotIn(b'\r', raw)
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(set(parse_dictionary(raw.decode('ascii'))), {'112233445566', 'A1A2A3A4A5A6'})
        self.warning.assert_not_called()
        self.run_client.assert_not_called()
        self.run_operation.assert_not_called()

    def start_mock_flipper(self):
        class FakeSignal:
            def connect(self, callback):
                self.callback = callback

            def emit(self, *args):
                self.callback(*args)

        def worker_factory(port, destination, parent):
            destination.mkdir(parents=True)
            return SimpleNamespace(port=port, destination=destination,
                                   progress=FakeSignal(), completed=FakeSignal(), failed=FakeSignal(),
                                   finished=FakeSignal(), start=Mock(), requestInterruption=Mock(), deleteLater=Mock())

        with patch('pm3gui.app.available_flippers', return_value=[{'device': 'COM123', 'description': 'Synthetic Flipper'}]), \
             patch('pm3gui.app.FlipperFetchWorker', side_effect=worker_factory):
            self.window.import_flipper_usb()
        worker = self.window.flipper_worker
        self.assertIsNotNone(worker)
        worker.start.assert_called_once()
        self.assertFalse(self.window.flipper_button.isEnabled())
        self.assertTrue(self.window.flipper_cancel.isEnabled())
        return worker

    def test_flipper_usb_download_callback_imports_dictionary_cache_and_reports_other_cards(self):
        worker = self.start_mock_flipper()
        dictionary = worker.destination / 'mf_classic_dict_user.nfc'
        dictionary.write_text('# Synthetic dictionary\n112233445566\n112233445566\n', encoding='ascii')
        key_cache = worker.destination / 'A1B2C3D4.keys'
        key_cache.write_text('Filetype: Flipper NFC keys\nVersion: 1\nMifare Classic type: 1K\n'
                             'Key A map: 0000000000000001\nKey B map: 0000000000000000\n'
                             'Key A sector 0: A1 A2 A3 A4 A5 A6\n', encoding='ascii')
        other_card = worker.destination / 'ntag.nfc'
        other_card.write_text('Filetype: Flipper NFC device\nVersion: 4\nDevice type: NTAG\n', encoding='ascii')
        errors = [{'remote': '/ext/nfc/.cache/unavailable.keys', 'error': 'Synthetic download error'}]
        report = {'files': [{'local': str(path), 'remote': '/ext/nfc/' + path.name, 'size': path.stat().st_size}
                            for path in [dictionary, key_cache, other_card]], 'errors': errors}
        worker.completed.emit(report)
        saved = json.loads((worker.destination / 'import-report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['files_imported'], 2)
        self.assertEqual(saved['keys_added'], 2)
        self.assertEqual(saved['duplicate_keys'], 1)
        self.assertEqual(saved['unsupported_card_files'], 1)
        self.assertEqual(saved['errors'], errors)
        self.assertEqual({key['key_hex'] for key in self.window.key_library.list_keys()},
                         {'112233445566', 'A1A2A3A4A5A6'})
        self.assertTrue(all(not key['verified'] for key in self.window.key_library.list_keys()))
        associations = self.window.key_library.list_card_keys('A1B2C3D4')
        self.assertEqual(len(associations), 1)
        self.assertFalse(associations[0]['verified'])
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertIn('Ошибок: 1', self.window.flipper_info.text())
        worker.finished.emit()
        self.assertIsNone(self.window.flipper_worker)
        worker.deleteLater.assert_called_once()
        self.assertTrue(self.window.flipper_button.isEnabled())
        self.assertFalse(self.window.flipper_cancel.isEnabled())
        self.warning.assert_not_called()
        self.run_client.assert_not_called()
        self.run_operation.assert_not_called()

    def test_flipper_usb_connection_failure_does_not_change_keys(self):
        worker = self.start_mock_flipper()
        worker.failed.emit('Synthetic port is busy')
        self.assertIn('Synthetic port is busy', self.window.flipper_info.text())
        worker.finished.emit()
        self.assertIsNone(self.window.flipper_worker)
        self.assertTrue(self.window.flipper_button.isEnabled())
        self.assertFalse(self.window.flipper_cancel.isEnabled())
        self.assertEqual(self.window.key_library.list_keys(), [])
        self.assertEqual(self.window.library.list_cards(), [])
        self.warning.assert_not_called()

    def test_flipper_usb_missing_device_does_not_start_worker(self):
        with patch('pm3gui.app.available_flippers', return_value=[]), patch('pm3gui.app.FlipperFetchWorker') as worker:
            self.window.import_flipper_usb()
        worker.assert_not_called()
        self.assertIsNone(self.window.flipper_worker)
        self.assertEqual(self.window.key_library.list_keys(), [])
        self.warning.assert_called_once()
        self.assertIn('Flipper не найден', self.warning.call_args.args[2])

    def test_add_invalid_key_keeps_library_unchanged(self):
        with patch('pm3gui.app.QInputDialog.getText', return_value=('not-a-key', True)):
            self.window.add_key()
        self.assertEqual(self.window.key_library.list_keys(), [])
        self.warning.assert_called_once()

    def test_complete_classic_read_saves_dump_and_verified_key_context(self):
        uid = self.prepare_classic_keys()
        self.assertEqual(self.window.read_job['stage'], 'dump')
        data = self.write_classic_dump(uid)
        self.finish_step(self.successful_classic_dump_log())
        self.assertEqual(self.window.library.list_cards(), [])
        self.confirm_post_read_uid(uid)
        self.assertIsNone(self.window.read_job)
        cards = self.window.library.list_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['uid'], uid)
        self.assertEqual(self.window.library.get_dump(cards[0]['id']).data, data)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        associations = self.window.key_library.list_card_keys(uid)
        self.assertEqual(len(associations), 32)
        self.assertEqual({entry['type'] for entry in associations}, {'A', 'B'})
        self.assertTrue(all(entry['verified'] for entry in associations))
        self.assertEqual(len(self.window.key_library.list_keys()), 2)
        validation = self.window.library.get_dump(cards[0]['id']).metadata['read_validation']
        self.assertTrue(validation['all_key_slots_known'])
        self.assertEqual(len(validation['known_key_slots']), 32)
        self.warning.assert_not_called()
        self.assertEqual([call.args[1].kind for call in self.run_operation.call_args_list],
                         ['hf14a_select', 'mfc_check_keys', 'mfc_dump', 'hf14a_select'])

    def test_keys_only_checks_do_not_read_or_import_a_memory_dump(self):
        self.window.recovery_enabled.setChecked(False)
        uid = self.prepare_classic_keys(keys_only=True, both_keys=False)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 16)
        self.assertEqual(self.window.stack.currentIndex(), 2)
        self.assertEqual([call.args[1].kind for call in self.run_operation.call_args_list],
                         ['hf14a_select', 'mfc_check_keys'])
        self.warning.assert_not_called()

    def test_partial_classic_dump_is_not_imported_as_zero_filled_memory(self):
        uid = self.prepare_classic_keys()
        self.write_classic_dump(uid)
        evidence = '\n'.join(f'[+] Sector... {sector} block... {block} ( ok )'
                             for sector in range(16) for block in range(4)
                             if (sector, block) != (7, 2))
        self.finish_step(evidence + '\n[!] Sector... 7 block... 2 ( fail )\n')
        if self.window.read_job is not None and self.window.read_job['stage'] == 'post_select':
            self.confirm_post_read_uid(uid)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 32)
        self.assertTrue((self.window.current_session / 'card.json').is_file())
        self.warning.assert_called_once()

    def test_partial_classic_save_is_explicit_and_keeps_a_map_of_zero_placeholders(self):
        uid=self.prepare_classic_keys()
        original=self.write_classic_dump(uid,changed_byte=77)
        log=self.successful_classic_dump_log().replace('[+] Sector... 0 block... 1 ( ok )','[!] Sector... 0 block... 1 ( fail )')
        self.question.return_value=QMessageBox.StandardButton.Save
        self.finish_step(log)
        self.confirm_post_read_uid(uid)
        self.question.assert_called_once()
        cards=self.window.library.list_cards()
        self.assertEqual(len(cards),1)
        saved=self.window.library.get_dump(cards[0]['id'])
        self.assertEqual(saved.data[16:32],bytes(16))
        self.assertEqual(saved.data[:16],original[:16])
        self.assertIn(1,saved.metadata['partial_dump']['missing_blocks'])
        self.assertFalse(saved.metadata['read_validation']['complete'])
        self.assertIn('НЕПОЛНЫЙ',self.window.dump_info.text())
        self.assertIn('НЕПОЛНЫЙ',self.window.cards.item(0).text())
        spans=[(e.cursor.selectionStart(),e.cursor.selectionEnd()) for e in self.window.hex_edit.extraSelections()]
        self.assertTrue(any(a<=48<b for a,b in spans))
        self.assertFalse(any(a<=12<b for a,b in spans))  # A real zero in read block0.
        self.warning.assert_not_called()

    def test_wrong_post_read_uid_never_offers_to_save_a_partial_dump(self):
        uid=self.prepare_classic_keys()
        self.write_classic_dump(uid)
        self.finish_step('[!] Sector... 0 block... 1 ( fail )\n')
        self.confirm_post_read_uid('11223344')
        self.question.assert_not_called()
        self.assertEqual(self.window.library.list_cards(),[])
        self.warning.assert_called_once()

    def test_missing_b_keys_preserves_known_keys_without_importing_a_fake_full_dump(self):
        self.window.recovery_enabled.setChecked(False)
        uid = self.prepare_classic_keys(both_keys=False)
        self.assertEqual(self.window.read_job['stage'],'partial_read')
        self.assertTrue(self.window.read_job['partial_mode'])
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 16)
        self.assertEqual({entry['type'] for entry in self.window.key_library.list_card_keys(uid)}, {'A'})
        self.assertEqual([call.args[1].kind for call in self.run_operation.call_args_list],
                         ['hf14a_select', 'mfc_check_keys','hf14a_select'])
        from test_writing import Card,classic_image
        card=Card(self.write_classic_dump(uid))
        for _ in range(120):
            if self.window.read_job is None:break
            operation=self.run_operation.call_args.args[1]
            self.finish_step(*card.respond(operation))
        self.assertIsNone(self.window.read_job)
        self.question.assert_called_once()
        self.assertIn('неполный',self.question.call_args.args[2].lower())
        self.assertEqual(self.window.library.list_cards(),[])

    def test_partial_read_disconnect_offers_checkpointed_data_without_a_final_live_select(self):
        from test_writing import Card
        self.window.recovery_enabled.setChecked(False);uid=self.prepare_classic_keys(both_keys=False)
        card=Card(self.write_classic_dump(uid));reader=self.window.read_job['partial_reader']
        while not reader.guards:self.finish_step(*card.respond(self.run_operation.call_args.args[1]))
        self.finish_step(*card.respond(self.run_operation.call_args.args[1])) # sector 1 initial UID.
        count=self.run_operation.call_count;self.question.return_value=QMessageBox.StandardButton.Save
        self.finish_step('UART CreateFile failed for COM5 (Win32 error 1168)',-5)
        self.assertEqual(self.run_operation.call_count,count);self.assertIsNone(self.window.read_job)
        self.question.assert_called_once();self.warning.assert_not_called()
        saved=self.window.library.get_dump(self.window.library.list_cards()[0]['id'])
        self.assertEqual(saved.metadata['partial_dump']['read_blocks'],[0,1,2,3])
        self.assertEqual(saved.metadata['read_validation']['identity_scope'],'per_sector')
        self.assertEqual(saved.data[64:],bytes(960))

    def test_partial_read_changed_uid_does_not_offer_to_import_previous_sectors(self):
        from test_writing import Card
        self.window.recovery_enabled.setChecked(False);uid=self.prepare_classic_keys(both_keys=False)
        card=Card(self.write_classic_dump(uid));reader=self.window.read_job['partial_reader']
        while not reader.guards:self.finish_step(*card.respond(self.run_operation.call_args.args[1]))
        self.finish_step('[+] UID: 11223344')
        self.assertIsNone(self.window.read_job);self.question.assert_not_called()
        self.assertEqual(self.window.library.list_cards(),[]);self.assertTrue(reader.path.is_file())

    def test_partial_read_stop_can_save_only_previously_confirmed_sectors(self):
        from test_writing import Card
        self.window.recovery_enabled.setChecked(False);uid=self.prepare_classic_keys(both_keys=False)
        card=Card(self.write_classic_dump(uid));reader=self.window.read_job['partial_reader']
        while not reader.guards:self.finish_step(*card.respond(self.run_operation.call_args.args[1]))
        self.question.return_value=QMessageBox.StandardButton.Save;count=self.run_operation.call_count
        self.window.cancel_operation()
        self.assertIsNone(self.window.read_job);self.assertEqual(self.run_operation.call_count,count)
        saved=self.window.library.get_dump(self.window.library.list_cards()[0]['id'])
        self.assertEqual(saved.metadata['partial_dump']['read_blocks'],[0,1,2,3])

    def test_post_select_connection_error_retries_only_readonly_uid_check_once(self):
        uid=self.prepare_classic_keys();self.write_classic_dump(uid)
        self.finish_step(self.successful_classic_dump_log());count=self.run_operation.call_count
        with patch('pm3gui.app.QTimer.singleShot') as timer:
            self.finish_step('UART CreateFile failed for COM5 (Win32 error 1168)',-5)
        self.assertEqual(self.window.read_job['stage'],'post_select');self.assertEqual(self.run_operation.call_count,count)
        timer.assert_called_once();timer.call_args.args[1]()
        self.assertEqual(self.run_operation.call_count,count+1);self.confirm_post_read_uid(uid)
        self.assertEqual(len(self.window.library.list_cards()),1);self.warning.assert_not_called()

    def test_second_post_select_failure_preserves_files_and_never_offers_unconfirmed_dump(self):
        uid=self.prepare_classic_keys();self.write_classic_dump(uid)
        self.finish_step(self.successful_classic_dump_log())
        with patch('pm3gui.app.QTimer.singleShot') as timer:self.finish_step('invalid serial port COM5',-5)
        timer.call_args.args[1]();self.finish_step('invalid serial port COM5',-5)
        self.assertIsNone(self.window.read_job);self.assertEqual(self.window.library.list_cards(),[])
        self.question.assert_not_called();self.assertTrue((self.window.current_session/'card.json').is_file())
        self.assertIn('UID',self.warning.call_args.args[2])

    def test_post_select_wrong_uid_is_not_retried_even_with_nonzero_exit(self):
        uid=self.prepare_classic_keys();self.write_classic_dump(uid)
        self.finish_step(self.successful_classic_dump_log())
        with patch('pm3gui.app.QTimer.singleShot') as timer:self.finish_step('[+] UID: 11223344',-5)
        timer.assert_not_called();self.assertIsNone(self.window.read_job);self.question.assert_not_called()

    def test_targeted_readable_b_recovery_then_full_dump(self):
        self.assertTrue(self.window.recovery_enabled.isChecked())
        missing = {(sector, 'B') for sector in range(32, 40)}
        uid = self.prepare_classic_keys(classic_size='4k', missing_key_slots=missing)
        self.assertEqual(self.window.read_job['stage'], 'recover')
        self.assertTrue(self.window.read_job['recovery_attempted'])
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 72)
        for sector in range(32, 40):
            operation = self.run_operation.call_args.args[1]
            self.assertEqual(operation.kind, 'mfc_read_trailer')
            self.assertEqual(operation.known_sector, sector)
            self.finish_step(f'[+] {sector_trailer(sector)} | 00 00 00 00 00 00 FF 07 80 69 A0 A1 A2 A3 A4 A5 | ................\n')
            self.assertEqual(self.window.read_job['stage'], 'recovery_identity')
            self.finish_step(f'[+] UID: {uid}\n[+] SAK: 18\n')
        self.assertEqual(self.window.read_job['stage'], 'dump')
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 80)
        self.assertEqual(self.run_operation.call_args.args[1].kind, 'mfc_dump')
        self.assertEqual(self.run_operation.call_args.args[1].key_file.name, 'targeted-key.bin')
        self.assertEqual(self.run_operation.call_args.args[1].key_file.stat().st_size, 480)
        self.assertEqual(self.window.library.list_cards(), [])
        data = self.write_classic_dump(uid, classic_size='4k')
        self.finish_step(self.successful_classic_dump_log('4k'))
        self.confirm_post_read_uid(uid, classic_size='4k')
        cards = self.window.library.list_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(self.window.library.get_dump(cards[0]['id']).data, data)
        validation = self.window.library.get_dump(cards[0]['id']).metadata['read_validation']
        self.assertEqual(len(validation['known_key_slots']), 80)
        self.assertTrue(validation['all_key_slots_known'])
        self.assertNotIn('mfc_recover', [call.args[1].kind for call in self.run_operation.call_args_list])
        self.warning.assert_not_called()

    def test_incomplete_recovery_preserves_known_keys_without_import_or_retry_loop(self):
        missing = {(sector, kind) for sector in range(36, 40) for kind in ('A', 'B')}
        uid = self.prepare_classic_keys(classic_size='4k', missing_key_slots=missing)
        self.assertEqual(self.window.read_job['stage'], 'recover')
        for _ in range(80):
            if self.window.read_job is None:
                break
            operation = self.run_operation.call_args.args[1]
            if self.window.read_job['stage'] == 'recovery_resync':
                self.finish_step('[+] Communicating with PM3 over USB-CDC\n[ ARM ]\n  Bootrom.... Iceman/master/v4.23346\n  OS......... Iceman/master/v4.23346\n')
            elif self.window.read_job['stage'] == 'recovery_resync_select':
                self.finish_step(f'[+] UID: {uid}\n[+] SAK: 18\n')
            elif operation.kind == 'mfc_nonce_probe':
                self.finish_step('[+] UID: A1B2C3D4\n[+] Prng detection..... hard\n')
            elif operation.kind == 'mfc_hardnested_target':
                self.finish_step('[#] AcquireEncryptedNonces: Auth2 error len=1\n' * 12, code=-4)
            else:
                self.finish_step('[-] No valid key found\n')
        else:
            self.fail('Recovery failed to stop')
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 72)
        self.assertTrue((self.window.current_session / 'recovery-plan.json').is_file())
        kinds = [call.args[1].kind for call in self.run_operation.call_args_list]
        self.assertNotIn('mfc_recover', kinds)
        self.assertLessEqual(kinds.count('mfc_hardnested_target'), 6)
        self.assertEqual(kinds.count('mfc_dump'),0)
        self.warning.assert_called_once()

    def test_unresponsive_device_after_guard_stops_before_next_attack(self):
        missing = {(sector, kind) for sector in range(36, 40) for kind in ('A', 'B')}
        uid = self.prepare_classic_keys(classic_size='4k', missing_key_slots=missing)
        for _ in range(60):
            operation = self.run_operation.call_args.args[1]
            if operation.kind == 'mfc_hardnested_target':
                break
            self.finish_step('[+] Prng detection..... hard\n' if operation.kind == 'mfc_nonce_probe' else '[-] No valid key found\n')
        else:
            self.fail('No targeted attack scheduled')
        self.finish_step('[#] AcquireEncryptedNonces: Auth2 error len=1\n' * 12, code=-4)
        self.assertEqual(self.window.read_job['stage'], 'recovery_resync')
        self.assertEqual(self.run_client.call_args.args[1], 'hw version')
        self.finish_step('[!] Connection timed out\n', code=-3)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 72)
        self.assertEqual([c.args[1].kind for c in self.run_operation.call_args_list].count('mfc_hardnested_target'), 1)
        self.assertIn('кнопку Proxmark', self.warning.call_args.args[2])

    def test_internal_transport_timeout_requires_resync_even_with_zero_exit(self):
        self.prepare_classic_keys(classic_size='4k', missing_key_slots={(39, 'A'), (39, 'B')})
        self.assertEqual(self.run_operation.call_args.args[1].kind, 'mfc_check_candidate')
        self.finish_step('[!] command execution time out\n', code=0)
        self.assertEqual(self.window.read_job['stage'], 'recovery_resync')
        self.assertEqual(self.run_client.call_args.args[1], 'hw version')
        self.finish_step('[+] Communicating with PM3 over USB-CDC\n[ ARM ]\n  OS......... Iceman/master/v4.23346\n')
        self.assertEqual(self.window.read_job['stage'], 'recovery_resync_select')
        self.finish_step('[+] UID: A1B2C3D4\n[+] SAK: 18\n')
        self.assertEqual(self.window.read_job['stage'], 'recover')
        self.finish_step('[!] Resynchronising, dropped 5 byte(s)\n', code=0)
        self.assertIsNone(self.window.read_job)
        self.assertIn('повторно прервался', self.warning.call_args.args[2])

    def assert_recovery_transport_loss_stops(self, output, code):
        uid=self.prepare_classic_keys(classic_size='4k',missing_key_slots={(39,'A'),(39,'B')})
        self.assertEqual(self.run_operation.call_args.args[1].kind, 'mfc_check_candidate')
        operations=self.run_operation.call_count
        commands=self.run_client.call_count
        keys=len(self.window.key_library.list_card_keys(uid))
        self.finish_step(output, code=code)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.run_operation.call_count, operations)
        self.assertEqual(self.run_client.call_count, commands)
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), keys)
        self.assertEqual(self.window.library.list_cards(), [])
        report=json.loads((self.window.current_session/'recovery-plan.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'failed')
        self.assertIn('Соединение с платой потеряно', report['stop_reason'])

    def test_serial_open_failure_stops_key_search_without_queuing_more_commands(self):
        self.assert_recovery_transport_loss_stops('[!!] UART CreateFile failed for COM5 (Win32 error 1168)\n', -5)

    def test_invalid_port_stops_key_search_without_queuing_more_commands(self):
        self.assert_recovery_transport_loss_stops('[!] ERROR: invalid serial port COM5\n', -5)

    def test_no_device_response_stops_key_search_even_with_zero_exit(self):
        self.assert_recovery_transport_loss_stops('[!] No response from Proxmark3\n', 0)

    def test_reported_transport_loss_disconnects_idle_persistent_session(self):
        with patch.object(type(self.window.runner), 'session_open', new_callable=PropertyMock, return_value=True), \
             patch.object(self.window.runner, 'disconnect') as disconnect:
            self.assert_recovery_transport_loss_stops('[!] No response from Proxmark3\n', 0)
        disconnect.assert_called_once()

    def test_card_auth_failure_is_not_mistaken_for_a_lost_device(self):
        self.prepare_classic_keys(classic_size='4k',missing_key_slots={(39,'A'),(39,'B')})
        operations=self.run_operation.call_count
        with patch.object(self.window.runner, 'disconnect') as disconnect:
            self.finish_step('[!] Authentication failed; no response from card\n', code=-5)
        disconnect.assert_not_called()
        self.assertIsNotNone(self.window.read_job)
        self.assertEqual(self.window.read_job['stage'], 'recover')
        self.assertEqual(self.run_operation.call_count, operations+1)
        self.warning.assert_not_called()

    def test_cancel_attack_checks_device_response_before_releasing_gui(self):
        self.prepare_classic_keys(classic_size='4k', missing_key_slots={(39, 'A'), (39, 'B')})
        for _ in range(40):
            operation = self.run_operation.call_args.args[1]
            if operation.kind == 'mfc_hardnested_target':
                break
            self.finish_step('[+] Prng detection..... hard\n' if operation.kind == 'mfc_nonce_probe' else '[-] No valid key found\n')
        self.window.cancel_operation()
        self.finish_step('', code=-2)
        self.assertEqual(self.window.read_job['stage'], 'cancel_resync')
        self.finish_step('[+] Communicating with PM3 over USB-CDC\n[ ARM ]\n  OS......... Iceman/master/v4.23346\n')
        self.assertIsNone(self.window.read_job)
        self.assertIn('Устройство отвечает', self.window.device_info.text())
        self.warning.assert_not_called()

    def test_cancel_during_recovery_never_imports_late_results_or_starts_dump(self):
        missing = {(sector, 'B') for sector in range(32, 40)}
        uid = self.prepare_classic_keys(classic_size='4k', missing_key_slots=missing)
        self.assertEqual(self.window.read_job['stage'], 'recover')
        self.window.cancel_operation()
        self.cancel_client.assert_called_once()
        _, output = self.recovered_key_artifact(uid)
        self.finish_step(output, code=-2)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 72)
        kinds = [call.args[1].kind for call in self.run_operation.call_args_list]
        self.assertNotIn('mfc_recover', kinds)
        self.assertNotIn('mfc_dump', kinds)
        self.warning.assert_not_called()

    def test_no_dictionary_keys_starts_unseeded_recovery_without_inventing_key_file(self):
        self.window.read_card()
        self.finish_step('[+] UID: A1 B2 C3 D4\n[+] MIFARE Classic 4K\n')
        self.finish_step('[+] UID: A1 B2 C3 D4\n[+] SAK: 18 [2]\n')
        self.assertEqual(self.window.read_job['stage'], 'keys')
        self.finish_step('[!] No keys found\n')
        self.assertEqual(self.window.read_job['stage'], 'recover')
        operation = self.run_operation.call_args.args[1]
        self.assertEqual(operation.kind, 'mfc_nonce_probe')
        self.assertEqual(operation.auth_key, '')
        self.assertIsNone(operation.known_sector)
        self.finish_step('[+] Prng detection..... weak\n')
        self.assertEqual(self.run_operation.call_args.args[1].kind, 'mfc_darkside_seed')
        self.assertEqual(self.window.key_library.list_keys(), [])
        self.assertEqual(self.window.library.list_cards(), [])
        self.warning.assert_not_called()

    def test_recovered_key_is_not_saved_if_post_check_uid_changes(self):
        uid = self.prepare_classic_keys(classic_size='4k', missing_key_slots={(39, 'B')})
        self.finish_step('[+] 255 | 00 00 00 00 00 00 FF 07 80 69 A0 A1 A2 A3 A4 A5 | ................\n')
        self.assertEqual(self.window.read_job['stage'], 'recovery_identity')
        self.finish_step('[+] UID: 11223344\n[+] SAK: 18\n')
        self.assertIsNone(self.window.read_job)
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 79)
        self.assertEqual(self.window.key_library.list_card_keys('11223344'), [])
        self.warning.assert_called_once()

    def test_recovery_output_with_two_uids_rejects_positive_result(self):
        uid = self.prepare_classic_keys(classic_size='4k', missing_key_slots={(39, 'B')})
        self.finish_step('[+] UID: A1B2C3D4\n[+] UID: 11223344\n'
                         '[+] 255 | 00 00 00 00 00 00 FF 07 80 69 A0 A1 A2 A3 A4 A5 | ................\n')
        self.assertIsNone(self.window.read_job)
        self.assertEqual(len(self.window.key_library.list_card_keys(uid)), 79)
        self.assertEqual(self.window.key_library.list_card_keys('11223344'), [])
        self.warning.assert_called_once()

    def test_post_read_uid_change_rejects_import_and_retains_session_files(self):
        uid = self.prepare_classic_keys()
        self.write_classic_dump(uid)
        self.finish_step(self.successful_classic_dump_log())
        self.assertEqual(self.window.library.list_cards(), [])
        self.confirm_post_read_uid('11223344')
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertTrue((self.window.current_session / 'card.json').is_file())
        self.assertEqual(self.window.key_library.list_card_keys('11223344'), [])
        self.warning.assert_called_once()
        self.assertIn('UID', self.warning.call_args.args[2])

    def test_semantic_reread_adds_revision_and_preserves_user_name_and_protocol(self):
        uid = self.prepare_classic_keys()
        original = self.write_classic_dump(uid)
        self.finish_step(self.successful_classic_dump_log())
        self.confirm_post_read_uid(uid)
        first = self.window.library.list_cards()[0]
        card_id = first['id']
        original_revision = self.window.library.list_revisions(card_id)[0]['id']
        self.window.library.update_card(card_id, name='Моя карта', uid=uid,
                                        protocol='Название пользователя', notes='Моя заметка')
        self.prepare_classic_keys(named_type=False)
        latest = self.write_classic_dump(uid, changed_byte=77)
        self.finish_step(self.successful_classic_dump_log())
        self.confirm_post_read_uid(uid)
        cards = self.window.library.list_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['id'], card_id)
        self.assertEqual(cards[0]['name'], 'Моя карта')
        self.assertEqual(cards[0]['protocol'], 'Название пользователя')
        self.assertEqual(cards[0]['notes'], 'Моя заметка')
        self.assertEqual(cards[0]['revision_count'], 2)
        self.assertEqual(self.window.library.get_dump(card_id).data, latest)
        self.assertEqual(self.window.library.get_dump(card_id, original_revision).data, original)
        self.warning.assert_not_called()

    def test_cancel_read_schedules_only_a_fresh_version_fence(self):
        self.window.read_card()
        self.window.cancel_operation()
        self.cancel_client.assert_called_once()
        self.finish_step('[+] UID: A1 B2 C3 D4\n[+] MIFARE Classic 1K\n', code=-2)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.assertEqual(self.window.library.list_cards(), [])
        self.assertEqual(self.window.key_library.list_keys(), [])
        self.run_operation.assert_not_called()
        self.assertEqual([c.args[1] for c in self.run_client.call_args_list],['hf search','hw version'])
        self.finish_step('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n')
        self.assertIsNone(self.window.read_job)

    def test_invalid_mfu_auth_stops_before_any_read_command(self):
        self.window.mfu_auth.setText('bad-password')
        self.window.read_card()
        self.finish_step('[+] UID: 04 11 22 33 44 55 66\n[+] NTAG 213\n')
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.window.library.list_cards(), [])
        self.warning.assert_called_once()
        self.assertIn('8 HEX', self.warning.call_args.args[2])

    def test_lf_read_confirms_and_saves_identifier_without_claiming_full_dump(self):
        self.window.read_card()
        self.finish_step('[-] No known/supported 13.56 MHz tags found.\n')
        self.assertEqual(self.window.read_job['stage'], 'identify_lf')
        self.finish_step('[+] EM 410x ID: 0123456789\n')
        self.assertEqual(self.window.read_job['stage'], 'lf_identifier')
        self.finish_step('[+] EM 410x ID: 0123456789\n')
        cards = self.window.library.list_cards()
        self.assertEqual(len(cards), 1)
        dump = self.window.library.get_dump(cards[0]['id'])
        self.assertEqual(dump.data, bytes.fromhex('0123456789'))
        self.assertEqual(dump.metadata['record_kind'], 'identifier')
        self.assertEqual(cards[0]['uid'], '0123456789')
        self.assertIn('идентификатор', self.window.device_info.text().lower())
        self.assertIsNone(self.window.read_job)
        self.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
