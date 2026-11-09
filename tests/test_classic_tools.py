"""Manual recovery, key provenance and native controls; no physical hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from pm3gui.app import MainWindow
from pm3gui.classic_tools import manual_recovery, probe_classic
from pm3gui.classic_ui import key_slots
from pm3gui.core import DumpData
from pm3gui.operations import Operation
from test_recovery import checked_key, recovered_candidate
from test_writing import UID, classic_image
from test_studio_app import VERSION

KEY = 'A0A1A2A3A4A5'
SELECT = 'UID: ' + UID + '\nSAK: 08\n'


def response(request):
    op = request.operation()
    if op.kind == 'hf14a_select':return SELECT, 0
    if op.kind == 'mfc_check_candidate':return checked_key(op), 0
    return recovered_candidate(op, KEY), 0


class ManualRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.root = Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)

    def flow(self, method='Nested', **changes):
        fields = dict(source_sector=0, source_type='A', key='FFFFFFFFFFFF', target_sector=3, target_type='B')
        fields.update(changes)
        return manual_recovery(method, '1k', UID, **fields)(self.root)

    def drive(self, flow, replace=None):
        requests = [];request = next(flow)
        while True:
            requests.append(request);answer = replace(request, len(requests)) if replace else response(request)
            try:request = flow.send(answer)
            except StopIteration as done:return done.value, requests

    def test_all_three_methods_authenticate_candidate_and_bind_uid(self):
        for method in ('Nested', 'Hardnested', 'Darkside'):
            with self.subTest(method=method):
                result, requests = self.drive(self.flow(method))
                self.assertEqual(result['uid'], UID)
                self.assertEqual(result['keys'], [{'sector': 0 if method == 'Darkside' else 3, 'type': 'A' if method == 'Darkside' else 'B', 'key_hex': KEY}])
                self.assertEqual(requests[-2].operation().kind, 'mfc_check_candidate')
                self.assertEqual(requests[-1].operation().kind, 'hf14a_select')
                self.assertEqual(json.loads((self.root/'classic-recovery.json').read_text())['uid'], UID)
                self.assertTrue(all(not r.modifying for r in requests))

    def test_source_authentication_failure_prevents_attack(self):
        flow = self.flow();flow.send(None)
        check = flow.send((SELECT, 0));self.assertEqual(check.operation().kind, 'mfc_check_candidate')
        with self.assertRaisesRegex(ValueError, 'аутентификацию|authenticate'):flow.send(('No keys found', 0))
        self.assertFalse((self.root/'classic-recovery.json').exists())

    def test_wrong_uid_or_geometry_prevents_attack(self):
        for select in (SELECT.replace(UID, '11223344'), SELECT.replace('08', '18'), 'UID: '+UID+'\nNTAG 213'):
            with self.subTest(select=select):
                flow = self.flow();next(flow)
                with self.assertRaises(ValueError):flow.send((select, 0))

    def test_source_card_swap_stops_before_attack(self):
        flow = self.flow();next(flow);check = flow.send((SELECT, 0));guard = flow.send(response(check))
        self.assertEqual(guard.operation().kind, 'hf14a_select')
        with self.assertRaisesRegex(ValueError, 'UID'):flow.send((SELECT.replace(UID, '11223344'), 0))

    def test_recovered_candidate_without_authentication_is_not_accepted(self):
        def replace(request, index):
            op = request.operation()
            return ('No keys found', 0) if op.kind == 'mfc_check_candidate' and op.auth_key == KEY else response(request)
        with self.assertRaisesRegex(ValueError, 'кандидат|candidate'):self.drive(self.flow(), replace)
        self.assertFalse((self.root/'classic-recovery.json').exists())

    def test_card_swap_after_candidate_authentication_is_not_saved(self):
        def replace(request, index):return (SELECT.replace(UID, '11223344'), 0) if index == 7 else response(request)
        with self.assertRaisesRegex(ValueError, 'UID'):self.drive(self.flow(), replace)
        self.assertFalse((self.root/'classic-recovery.json').exists())

    def test_nonzero_attack_exit_does_not_accept_printed_key(self):
        def replace(request, index):
            output, code = response(request)
            return (output, 1) if request.operation().kind == 'mfc_nested_target' else (output, code)
        with self.assertRaisesRegex(ValueError, 'не найден|did not find'):self.drive(self.flow(), replace)

    def test_auth2_failure_is_not_retried(self):
        issued = []
        def replace(request, index):
            issued.append(request.operation().kind)
            return ('[#] AcquireEncryptedNonces: Auth2 error len=1\n'*3, 0) if request.operation().kind == 'mfc_hardnested_target' else response(request)
        with self.assertRaises(ValueError):self.drive(self.flow('Hardnested'), replace)
        self.assertEqual(issued.count('mfc_hardnested_target'), 1)

    def test_invalid_parameters_fail_before_any_session(self):
        for changes in ({'key': ''}, {'source_sector': 16}, {'target_sector': 0, 'target_type': 'A'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):self.flow(**changes)
        with self.assertRaises(ValueError):manual_recovery('autopwn', '1k')

    def test_probe_only_identifies_one_classic(self):
        result, requests = self.drive(probe_classic(self.root))
        self.assertEqual(len(requests), 1);self.assertEqual(result['classic_size'], '1k')

    def test_darkside_is_fixed_to_zero_a_and_has_no_seed(self):
        result, requests = self.drive(self.flow('Darkside'))
        op = requests[1].operation()
        self.assertEqual(op.render(), 'hf mf darkside --blk 0')
        self.assertIsNone(op.known_sector)

    def test_unknown_partial_slots_and_conflicting_values_are_preserved(self):
        dump = DumpData(classic_image(), 'bin', {'partial_dump': {'known_bytes': list(range(58, 64))}}, 16)
        rows = [{'sector': 0, 'type': 'B', 'key_hex': KEY, 'verified': True},
                {'sector': 39, 'type': 'A', 'key_hex': KEY, 'verified': True}]
        slots = key_slots(dump, rows)
        self.assertNotIn((0, 'A'), slots);self.assertNotIn((39, 'A'), slots)
        self.assertEqual(len(slots[0, 'B']), 2)
        self.assertTrue(slots[0, 'B'][KEY]['verified'])


class ClassicControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.root = Path(self.tmp.name)
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.runs = self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.ops = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.w = MainWindow(self.root/'profile');self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.w.read_job and self.w.read_job.get('job_kind') == 'studio':self.w.read_job['flow'].close()
        self.w.read_job = None;self.w.dirty = False
        if getattr(self.w, 'classic_attack_dialog', None):self.w.classic_attack_dialog.close()
        self.w.close();self.w.deleteLater();QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents();self.tmp.cleanup()

    def finish(self, output='', code=0):self.w.operation_output = output;self.w._finished(code, 'Fixture')

    def cleanup_rf(self):self.finish();self.finish();self.finish(VERSION)

    def source(self):
        cid = self.w.library.create_card('Fixture', DumpData(classic_image(), 'bin', {}, 16), uid=UID, protocol='MIFARE Classic 1K')
        self.w.load_card(cid);self.w.studio.from_editor()

    def start(self, method='Nested'):
        self.source();dialog = self.w.open_classic_attack(method, True)
        dialog.key.setText('FFFFFFFFFFFF');dialog.source_type.setCurrentText('A');dialog.target_type.setCurrentText('B')
        dialog.source_sector.setValue(0);dialog.target_sector.setValue(3);dialog.start.click()
        return dialog

    def finish_run(self):
        while self.w.read_job['stage'] == 'running':
            answer = response(self.w.read_job['request']);self.finish(*answer)

    def test_workspace_and_classic_buttons_open_named_method_without_rf(self):
        for buttons in (self.w.classic_method_buttons, self.w.studio.attack_buttons):
            for name, button in buttons.items():
                button.click();self.assertEqual(self.w.classic_attack_dialog.method(), name)
        self.runs.assert_not_called();self.ops.assert_not_called()
        self.assertIsNone(self.w.read_job)

    def test_open_owned_dialog_reuses_same_operation(self):
        dialog = self.start('Hardnested');calls = self.ops.call_count
        self.assertIs(self.w.open_classic_attack('Darkside'), dialog)
        self.assertEqual(dialog.method(), 'Hardnested');self.assertEqual(self.ops.call_count, calls)

    def test_only_verified_keys_are_saved_after_rf_cleanup(self):
        dialog = self.start();self.finish_run()
        self.assertEqual(self.w.read_job['stage'], 'cleanup_break')
        self.assertFalse(self.w.key_library.list_card_keys(UID));self.assertFalse(dialog.start.isEnabled())
        self.cleanup_rf();entries = self.w.key_library.list_card_keys(UID)
        self.assertEqual(len(entries), 1);self.assertEqual(entries[0]['key_hex'], KEY);self.assertTrue(entries[0]['verified'])
        self.assertIsNone(self.w.read_job);self.assertTrue(dialog.start.isEnabled());self.assertIn(KEY, dialog.status.text())
        self.assertEqual(self.w.studio.key_overview.item(3, 2).text(), 'Варианты: 2')
        existing = self.w.studio.key_overview.item(0, 1).text()
        self.w.key_library.record_verified_keys(UID, [{'sector': 0, 'type': 'A', 'key_hex': existing}])
        self.w.refresh_keys();self.assertFalse(self.w.studio.key_overview.item(0, 1).icon().isNull())

    def test_stop_ends_without_attack_or_new_keys(self):
        dialog = self.start();dialog.stop.click();self.cleanup_rf()
        self.assertFalse(self.w.key_library.list_card_keys(UID));self.assertIsNone(self.w.read_job)
        self.assertTrue(all(c.args[1].kind != 'mfc_nested_target' for c in self.ops.call_args_list))

    def test_transport_failure_is_not_replayed_or_saved(self):
        dialog = self.start();self.finish('Connection lost', -5);self.cleanup_rf()
        self.assertIsNone(self.w.read_job);self.assertFalse(self.w.key_library.list_card_keys(UID))
        self.assertTrue(dialog.start.isEnabled())

    def test_unconfirmed_stop_keeps_other_hardware_actions_locked(self):
        dialog = self.start();dialog.stop.click();self.finish('', -5)
        self.assertEqual(self.w.read_job['stage'], 'uncertain');self.assertFalse(self.w.read_button.isEnabled())
        self.assertFalse(dialog.start.isEnabled());self.assertFalse(dialog.reset.isHidden())
        dialog.stop.click();self.cleanup_rf();self.assertIsNone(self.w.read_job)

    def test_seed_snapshot_is_not_reused_for_another_uid(self):
        self.source();dialog = self.w.open_classic_attack('Nested', True)
        self.assertGreater(dialog.seeds.count(), 1)
        dialog.uid.setText('11223344');dialog.load_seeds();self.assertEqual(dialog.seeds.count(), 1)

    def test_darkside_hides_irrelevant_key_inputs(self):
        dialog = self.w.open_classic_attack('Darkside');self.assertTrue(dialog.source_frame.isHidden());self.assertTrue(dialog.target_frame.isHidden())
        dialog.methods['Nested'].click();self.assertFalse(dialog.source_frame.isHidden())

    def test_classic_tables_remain_read_only_and_selection_reaches_write_wizard(self):
        self.source();ui = self.w.studio
        self.assertEqual(ui.key_overview.rowCount(), 16);self.assertEqual(ui.key_overview.item(0, 1).text(), 'FFFFFFFFFFFF')
        self.assertEqual(ui.block_table.item(3, 2).data(Qt.ItemDataRole.UserRole), 'trailer')
        ui.select_blocks('data');self.assertEqual(ui.block_count.text(), 'Выбрано: 47')
        ui.select_blocks('none');ui.block_table.item(4, 0).setCheckState(Qt.CheckState.Checked);ui.write_blocks()
        self.assertEqual(self.w.write_dialog.options().blocks, (4,))
        self.runs.assert_not_called();self.ops.assert_not_called();self.w.write_dialog.close()

    def test_card_identification_updates_dialog_without_starting_attack(self):
        dialog = self.w.open_classic_attack('Hardnested');dialog.probe.click();self.finish(SELECT);self.cleanup_rf()
        self.assertEqual(dialog.uid.text(), UID);self.assertEqual(dialog.geometry.currentData(), '1k')
        self.assertFalse(self.w.key_library.list_card_keys(UID))


if __name__ == '__main__':unittest.main()
