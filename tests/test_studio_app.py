"""Workbench lifecycle and real Qt controls, without Computer Use or hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import json
import re
import sys
import tempfile
import unittest
from unittest.mock import patch,PropertyMock
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QWidget,QLabel,QComboBox,QAbstractButton,QLineEdit,QPlainTextEdit,QTableWidget
from pm3gui.app import MainWindow
from pm3gui.core import DumpData
from pm3gui.device import DeviceSettings,build_invocation,parse_connection_status
from pm3gui.studio_flows import Request,one_command,traces
from pm3gui.studio_data import structured_dump
from test_writing import classic_image,UID

VERSION='[+] Communicating with PM3 over USB-CDC\n[ ARM ]\n OS......... Iceman/GUI-BlueShark/test\n'


class StudioAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.enterContext(patch('pm3gui.connection_checks.CLEANUP_VERSION_RECHECK_DELAY_MS',0))
        self.enterContext(patch('pm3gui.app.available_ports',return_value=[]));self.enterContext(patch('pm3gui.app.discover_client',return_value=''))
        self.runs=self.enterContext(patch('pm3gui.app.ClientRunner.run'));self.ops=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.w=MainWindow(self.root/'profile');self.ui=self.w.studio;self.addCleanup(self.cleanup)
    def cleanup(self):
        if hasattr(self.w,'studio_timer'):self.w.studio_timer.stop()
        if self.w.read_job and self.w.read_job.get('job_kind')=='studio':self.w.read_job['flow'].close()
        self.w.read_job=None;self.w.dirty=False
        if getattr(self.w,'write_dialog',None):self.w.write_dialog.close()
        self.w.close();self.w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents();self.tmp.cleanup()
    def finish(self,output='',code=0):self.w.operation_output=output;self.w._finished(code,'Fixture');self.app.processEvents()
    def cleanup_rf(self):
        self.finish();self.finish();self.finish(VERSION)
    def load_source(self):
        dump=DumpData(classic_image(),'bin',{},16);cid=self.w.library.create_card('Fixture',dump,uid=UID,protocol='MIFARE Classic 1K')
        self.w.load_card(cid);self.ui.from_editor();return cid

    def test_all_pages_and_1000_command_forms_are_available_without_hardware(self):
        self.assertEqual(self.ui.pages.count(),8)
        self.assertGreater(self.ui.command_choice.count(),1000)
        for page in range(8):self.ui.sections.setCurrentRow(page)
        self.runs.assert_not_called();self.ops.assert_not_called();self.assertFalse((self.root/'profile/sessions').exists())

    def test_native_noncontiguous_block_selection_reaches_existing_write_plan(self):
        self.load_source()
        for b in (1,4,8):self.ui.block_table.item(b,0).setCheckState(__import__('PySide6.QtCore',fromlist=['Qt']).Qt.CheckState.Checked)
        self.ui.write_blocks();dialog=self.w.write_dialog
        self.assertEqual(dialog.options().blocks,(1,4,8));self.runs.assert_not_called();self.ops.assert_not_called()

    def test_acl_edit_is_a_new_revision_not_a_write_or_source_overwrite(self):
        cid=self.load_source();before=self.w.library.get_dump(cid).data;before_count=len(self.w.library.list_revisions(cid))
        self.ui.conditions[0].setValue(3)
        with patch.object(self.ui,'review',return_value=True):self.ui.apply_acl()
        self.assertEqual(len(self.w.library.list_revisions(cid)),before_count+1)
        self.assertNotEqual(self.w.library.get_dump(cid).data,before);self.assertEqual(self.w.loaded_dump.data,before)
        self.runs.assert_not_called();self.ops.assert_not_called()

    def test_dump_keys_are_candidates_and_import_does_not_mutate_the_dump(self):
        self.load_source();original=self.ui.dump.data;self.ui.dump_keys_to_library()
        rows=self.w.key_library.list_card_keys(UID);self.assertTrue(rows);self.assertTrue(all(not r['verified'] for r in rows));self.assertEqual(self.ui.dump.data,original)

    def test_one_command_records_result_without_inventing_card_verification(self):
        self.w.start_studio(one_command('hw status'),'Status');directory=self.w.current_session
        self.assertIsNotNone(self.w.read_job);self.finish('[+] Actual status',0)
        self.assertIsNone(self.w.read_job);report=json.loads((directory/'studio-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'completed');self.assertEqual(report['physical_verification'],'client-output-only');self.assertFalse(report['may_have_written'])

    def test_rf_sequence_stops_field_and_requires_fresh_firmware_response(self):
        self.w.start_studio(one_command('hf 14a info'),'Read',rf=True);directory=self.w.current_session
        self.assertTrue(self.w.runner.rf_lease);self.finish('UID: '+UID)
        self.assertEqual(self.ops.call_args.args[1].render(),'hw break');self.finish()
        self.assertEqual(self.ops.call_args.args[1].render(),'hw fpgaoff');self.finish()
        self.assertEqual(self.runs.call_args.args[1],'hw version')
        self.finish('Client banner only')
        self.assertEqual(self.w.read_job['stage'],'uncertain');self.assertTrue(self.w.runner.rf_lease)
        self.w.cancel_operation();self.cleanup_rf();self.assertIsNone(self.w.read_job);self.assertFalse(self.w.runner.rf_lease)

    def test_transport_loss_does_not_replay_pending_write(self):
        def flow(directory):
            yield Request('hf 14a info')
            yield Request('hf mf csetblk','--blk 1 -d '+'AA'*16)
            return {'verification':'fixture'}
        self.w.start_studio(flow,'Write',rf=True);self.finish('UART CreateFile failed',-5)
        lines=[call.args[1].render() for call in self.ops.call_args_list]
        self.assertFalse(any('csetblk' in s for s in lines));self.cleanup_rf()
        self.assertIsNone(self.w.read_job)

    def test_second_operation_cannot_cancel_or_replace_first(self):
        self.w.start_studio(one_command('hw status'),'First');job=self.w.read_job;count=self.ops.call_count
        self.w.start_studio(one_command('hw status'),'Second')
        self.assertIs(self.w.read_job,job);self.assertFalse(job['cancelled']);self.assertEqual(self.ops.call_count,count)
        self.finish('Status')

    def test_hold_mode_keeps_rf_lease_and_stop_releases_only_after_cleanup(self):
        self.w.start_studio(one_command('hw standalone'),'Standalone',rf=True,hold=True,duration_ms=60000)
        self.finish('Started');self.assertEqual(self.w.read_job['stage'],'holding');self.assertTrue(self.w.runner.rf_lease)
        self.w.cancel_operation();self.assertEqual(self.w.read_job['stage'],'cleanup_break');self.cleanup_rf()
        self.assertIsNone(self.w.read_job);self.assertFalse(self.w.runner.rf_lease);self.assertFalse(self.w.studio_timer.isActive())

    def begin_cleanup_version(self,transport='blueshark',cancel=False):
        w=self.w;w.transport_combo.setCurrentIndex(w.transport_combo.findData(transport))
        self.enterContext(patch.object(type(w.runner),'connection_transport',new_callable=PropertyMock,return_value=transport))
        outcomes=[]
        w.start_studio(one_command('hf mf csetblk','--blk 1 -d '+'AA'*16),'Fixture write',rf=True,on_finished=outcomes.append)
        if cancel:w.cancel_operation()
        else:self.finish('Done')
        self.assertEqual(w.read_job['stage'],'cleanup_break');self.finish();self.finish()
        self.assertEqual(w.read_job['stage'],'cleanup_version')
        return outcomes,w.read_job['session']

    def test_studio_cleanup_rechecks_missing_os_without_replaying_write(self):
        w=self.w;outcomes,directory=self.begin_cleanup_version();before=self.ops.call_count
        w.read_job['cleanup_version_deadline']-=1
        for _ in range(3):self.finish('Client banner only')
        self.assertEqual(w.read_job['stage'],'cleanup_version');self.assertFalse(outcomes)
        self.assertTrue(w.runner.rf_lease);self.assertFalse(w.read_button.isEnabled());self.assertEqual(self.ops.call_count,before)
        self.assertEqual(self.runs.call_args.args[1],'hw version');self.assertLess(self.runs.call_args.kwargs['timeout_ms'],20000)
        self.finish('OS......... Fresh-build')
        self.assertIsNone(w.read_job);self.assertEqual(outcomes[0]['status'],'completed');self.assertFalse(w.runner.rf_lease)
        self.assertEqual(sum('csetblk' in c.args[1].render() for c in self.ops.call_args_list),1)
        report=json.loads((directory/'studio-report.json').read_text(encoding='utf-8'))
        self.assertEqual([c['connected'] for c in report['cleanup_version_checks']],[False,False,False,True])
        self.assertEqual(report['cleanup_version_checks'][0]['output'],'Client banner only')

    def test_cancelled_studio_operation_finishes_only_after_rechecked_os(self):
        w=self.w;outcomes,_=self.begin_cleanup_version(cancel=True);before=self.ops.call_count
        self.finish('Client banner only');self.assertFalse(outcomes)
        self.finish('OS......... Fresh-build');self.assertEqual(outcomes[0]['status'],'cancelled')
        self.assertEqual(self.ops.call_count,before);self.assertFalse(w.runner.rf_lease)

    def test_studio_eighth_missing_os_keeps_lock_and_does_not_call_finished(self):
        w=self.w;outcomes,_=self.begin_cleanup_version();before=self.ops.call_count;versions=self.runs.call_count
        for _ in range(8):self.finish('Client banner only')
        self.assertEqual(w.read_job['stage'],'uncertain');self.assertFalse(outcomes)
        self.assertEqual(self.runs.call_count,versions+7)
        self.assertTrue(w.runner.rf_lease);self.assertFalse(w.read_button.isEnabled());self.assertEqual(self.ops.call_count,before)

    def test_studio_cleanup_recheck_rejects_usb_errors_wrong_transport_or_expired_budget(self):
        cases=[('usb','Client banner only',0,False),('blueshark','Communicating with PM3 over USB-CDC',0,False),
               ('blueshark','No response from Proxmark3',0,False),('blueshark','OFFLINE mode',0,False),
               ('blueshark','Client banner only',-5,False),('blueshark','Client banner only',0,True)]
        for transport,output,code,expired in cases:
            with self.subTest(transport=transport,output=output,code=code,expired=expired):
                outcomes,_=self.begin_cleanup_version(transport);before=self.runs.call_count
                if expired:self.w.read_job['cleanup_version_deadline']=0
                self.finish(output,code)
                self.assertEqual(self.w.read_job['stage'],'uncertain');self.assertEqual(self.runs.call_count,before);self.assertFalse(outcomes)
                with patch('PySide6.QtWidgets.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):self.w.acknowledge_studio_reset()

    def nonrf_bulk_flow(self, *, command='data hexsamples', allow_failure=False):
        def flow(directory):
            yield Request(command, '-n 512', allow_failure=allow_failure)
            yield Request('data hexsamples', '-n 16 -o 0x200')
            return {'verification': 'fixture-only'}
        return flow

    def test_nonrf_bulk_stop_waits_for_readonly_fence_without_replaying_userdata(self):
        w=self.w;outcomes=[]
        w.start_studio(self.nonrf_bulk_flow(), 'Console bulk', on_finished=outcomes.append)
        job=w.read_job;self.assertTrue(job['bulk_cancel_operation'])
        with patch.object(type(w.runner),'busy',new_callable=PropertyMock,return_value=True),patch.object(w.runner,'cancel') as stop:
            w.cancel_operation();self.assertEqual(stop.call_count,1)
        self.runs.assert_not_called();self.assertFalse(outcomes)
        self.finish('BigBuf download cancelled; partial data discarded',-2)
        self.assertIs(w.read_job,job);self.assertEqual(job['stage'],'cleanup_version')
        self.assertEqual(self.runs.call_args.args[1],'hw version');self.assertEqual(self.ops.call_count,1)
        self.assertFalse(w.runner.rf_lease);self.assertFalse(w.read_button.isEnabled());self.assertFalse(outcomes)
        w.start_studio(one_command('data samples','-n 512'),'Replacement')
        self.assertIs(w.read_job,job);self.assertEqual(self.ops.call_count,1)
        self.finish(VERSION)
        self.assertIsNone(w.read_job);self.assertEqual(outcomes[0]['status'],'cancelled')
        self.assertEqual(self.ops.call_count,1)

    def test_nonrf_bulk_native_soft_error_requires_fence_even_when_failure_was_allowed(self):
        for command in ('data hexsamples','data samples'):
            with self.subTest(command=command):
                outcomes=[];before=self.ops.call_count
                self.w.start_studio(self.nonrf_bulk_flow(command=command,allow_failure=True),'Console bulk',on_finished=outcomes.append)
                self.finish('ERROR: invalid or incomplete BigBuf download; partial data discarded',-10)
                self.assertEqual(self.w.read_job['stage'],'cleanup_version');self.assertFalse(outcomes)
                self.assertEqual(self.ops.call_count,before+1);self.assertEqual(self.runs.call_args.args[1],'hw version')
                self.finish(VERSION)
                self.assertIsNone(self.w.read_job);self.assertEqual(outcomes[0]['status'],'failed')
                self.assertEqual(self.ops.call_count,before+1)

    def test_nonrf_bulk_failed_fence_keeps_lock_and_retry_sends_only_version(self):
        w=self.w;outcomes=[]
        w.start_studio(self.nonrf_bulk_flow(),'Console bulk',on_finished=outcomes.append)
        self.finish('BigBuf failed',-10);job=w.read_job;before=self.ops.call_count
        self.finish('No response from Proxmark3',-5)
        self.assertIs(w.read_job,job);self.assertEqual(job['stage'],'uncertain')
        self.assertFalse(w.read_button.isEnabled());self.assertFalse(outcomes);self.assertFalse(w.runner.rf_lease)
        w.start_studio(one_command('hw status'),'Replacement')
        self.assertIs(w.read_job,job);self.assertEqual(self.ops.call_count,before)
        versions=self.runs.call_count;w.cancel_operation()
        self.assertEqual(job['stage'],'cleanup_version');self.assertEqual(self.runs.call_count,versions+1)
        self.assertEqual(self.runs.call_args.args[1],'hw version');self.assertEqual(self.ops.call_count,before)
        self.finish(VERSION);self.assertIsNone(w.read_job);self.assertEqual(outcomes[0]['status'],'failed')

    def test_nonrf_bulk_silent_blueshark_fence_rechecks_without_sending_userdata(self):
        w=self.w;w.transport_combo.setCurrentIndex(w.transport_combo.findData('blueshark'))
        self.enterContext(patch.object(type(w.runner),'connection_transport',new_callable=PropertyMock,return_value='blueshark'))
        outcomes=[];w.start_studio(self.nonrf_bulk_flow(),'Console bulk',on_finished=outcomes.append)
        self.finish('BigBuf failed',-10);job=w.read_job
        self.assertEqual(job['cleanup_version_probe_count'],1)
        for _ in range(2):self.finish('Client banner only')
        self.assertIs(w.read_job,job);self.assertEqual(job['stage'],'cleanup_version');self.assertFalse(outcomes)
        self.assertEqual(job['cleanup_version_probe_count'],3);self.assertEqual(self.ops.call_count,1)
        self.assertFalse(w.read_button.isEnabled());self.assertFalse(w.runner.rf_lease)
        self.finish('OS......... Fresh-build')
        self.assertIsNone(w.read_job);self.assertEqual(outcomes[0]['status'],'failed')
        self.assertEqual(self.ops.call_count,1)

    def test_nonrf_bulk_fence_deadline_expiry_keeps_lock_without_new_probe(self):
        w=self.w;w.transport_combo.setCurrentIndex(w.transport_combo.findData('blueshark'))
        self.enterContext(patch.object(type(w.runner),'connection_transport',new_callable=PropertyMock,return_value='blueshark'))
        outcomes=[];w.start_studio(self.nonrf_bulk_flow(),'Console bulk',on_finished=outcomes.append)
        self.finish('BigBuf failed',-10);job=w.read_job;job['cleanup_version_deadline']=0
        versions=self.runs.call_count;self.finish('Client banner only')
        self.assertIs(w.read_job,job);self.assertEqual(job['stage'],'uncertain');self.assertFalse(outcomes)
        self.assertEqual(self.runs.call_count,versions);self.assertEqual(self.ops.call_count,1)
        self.assertFalse(w.read_button.isEnabled());self.assertFalse(w.runner.rf_lease)

    def test_successful_nonrf_bulk_needs_no_fence_and_current_operation_flag_is_exact(self):
        w=self.w;outcomes=[]
        def flow(directory):
            yield Request('data hexsamples','-n 512')
            yield Request('hw status')
        w.start_studio(flow,'Console sequence',on_finished=outcomes.append)
        self.finish('Complete 512-byte result')
        self.assertFalse(w.read_job['bulk_cancel_operation']);self.assertEqual(self.ops.call_count,2)
        self.finish('Ordinary command error',-10)
        self.assertIsNone(w.read_job);self.assertEqual(outcomes[0]['status'],'failed');self.runs.assert_not_called()
        w.start_studio(one_command('data samples','-n 512'),'Samples',on_finished=outcomes.append)
        self.finish('Complete sample result')
        self.assertIsNone(w.read_job);self.assertEqual(outcomes[-1]['status'],'completed');self.runs.assert_not_called()

    def test_ordinary_nonrf_cancel_and_offline_bulk_error_do_not_send_device_fence(self):
        w=self.w;outcomes=[]
        w.start_studio(one_command('hw status'),'Status',on_finished=outcomes.append)
        with patch.object(type(w.runner),'busy',new_callable=PropertyMock,return_value=True),patch.object(w.runner,'cancel'):
            w.cancel_operation()
        self.finish('',-2)
        self.assertIsNone(w.read_job);self.assertEqual(outcomes[-1]['status'],'cancelled');self.runs.assert_not_called()
        w.start_studio(one_command('data hexsamples','-n 512'),'Offline invalid',offline=True,on_finished=outcomes.append)
        self.finish('Device unavailable in offline mode',-10)
        self.assertIsNone(w.read_job);self.assertEqual(outcomes[-1]['status'],'failed');self.runs.assert_not_called()

    def test_capture_stop_continues_to_trace_save_and_never_restarts_sniff(self):
        self.w.start_studio(traces('14a',capture=True),'Trace',rf=True)
        with patch.object(type(self.w.runner),'busy',new_callable=PropertyMock,return_value=True),patch.object(self.w.runner,'cancel') as stop:
            self.w.cancel_operation();self.w.cancel_operation();self.assertEqual(stop.call_count,1)
        self.finish('',-2);self.assertEqual(self.ops.call_args.args[1].render(),'hw break');self.finish();self.finish()
        self.assertIn('trace save',self.ops.call_args.args[1].render());self.finish()
        self.assertIn('trace list',self.ops.call_args.args[1].render());self.finish('0 | 1 | Rdr | 26 | | REQA')
        self.cleanup_rf();lines=[c.args[1].render() for c in self.ops.call_args_list]
        self.assertEqual(sum('sniff' in s for s in lines),1);self.assertIsNone(self.w.read_job)

    def test_history_and_ndef_composition_are_offline_and_persisted(self):
        self.ui.ndef_records.setPlainText('URL https://example.com\nTEXT ru Привет');self.ui.build_ndef()
        self.assertIn('Привет',self.ui.ndef_tree.toPlainText());self.ui.remember_command('hw status')
        self.assertEqual(json.loads((self.root/'profile/studio-history.json').read_text()),['hw status'])
        self.ui.restore_history(0);self.assertTrue(self.ui.console_manual.isChecked());self.assertEqual(self.ui.console_line.text(),'hw status')
        self.ui.clear_history();self.assertEqual(self.ui.history.count(),0);self.runs.assert_not_called();self.ops.assert_not_called()

    def test_imported_family_uses_the_selected_family_not_the_simulator_picker(self):
        document={'FileType':'desfire','Applications':[{'AID':'000001','Files':{}}]}
        path=self.root/'desfire.json';path.write_text(json.dumps(document),encoding='utf-8')
        self.ui.profile.setCurrentIndex(next(i for i in range(self.ui.profile.count()) if self.ui.profile.itemData(i)[1]=='DESFire'))
        with patch('pm3gui.studio_ui.QFileDialog.getOpenFileName',return_value=(str(path),'')):
            self.ui.inspect_profile_file()
        self.assertEqual(self.ui.family_dump.metadata['protocol'],'DESFire')
        self.assertEqual(self.ui.family_dump.metadata['structured_document'],document)
        self.runs.assert_not_called();self.ops.assert_not_called()

    def test_structured_lf_record_is_classified_and_not_written_as_classic(self):
        dump=structured_dump({'protocol':'HID Prox','identifier':'2000123456'},'HID Prox','2000123456','lf-identifier')
        cid=self.ui.save_result({'dump':dump,'uid':'2000123456','protocol':'HID Prox','verification':'identifier-only'})
        self.assertEqual(next(c for c in self.w.library.list_cards() if c['id']==cid)['rf_band'],'lf')
        self.ui.family_dump=self.w.library.get_dump(cid);self.assertEqual(self.ui.lf_fields()['protocol'],'HID Prox')

    def test_tcp_profile_does_not_select_or_change_a_com_port(self):
        self.w.ports.setEditText('COM17');self.w.port_profiles['usb']='COM17'
        self.w.transport_combo.setCurrentIndex(self.w.transport_combo.findData('tcp'));self.w.ports.setEditText('tcp:127.0.0.1:18888');self.w.save_settings()
        settings=json.loads(self.w.settings_path.read_text(encoding='utf-8'))
        self.assertEqual(settings['tcp_endpoint'],'tcp:127.0.0.1:18888');self.assertEqual(settings['usb_port'],'COM17');self.runs.assert_not_called();self.ops.assert_not_called()

    def test_file_analysis_uses_offline_session_and_never_claims_a_device_connection(self):
        self.w.start_studio(one_command('nfc encode','--text test'),'NDEF',offline=True)
        settings=self.ops.call_args.args[0]
        self.assertEqual(settings.transport,'offline');self.assertEqual(settings.port,'');self.assertTrue(settings.require_session)
        self.finish('NDEF result');self.assertIsNone(self.w.read_job)

    def test_all_studio_transaction_steps_require_one_client_process(self):
        self.w.keep_connection.setChecked(False)
        def flow(directory):
            yield Request('hw status');yield Request('hw status')
        self.w.start_studio(flow,'Fixture');self.finish('one');self.finish('two')
        for call in self.ops.call_args_list:
            self.assertTrue(call.args[0].keep_open);self.assertTrue(call.args[0].require_session)


class TcpBoundaryTests(unittest.TestCase):
    def test_endpoints_are_validated_and_transport_is_confirmed_from_fresh_channel(self):
        with tempfile.TemporaryDirectory() as d:
            for endpoint in ('tcp:localhost:18888','tcp:127.0.0.1:18888','tcp:[::1]:18888'):
                _,args,_,_=build_invocation(DeviceSettings(sys.executable,endpoint,d,'tcp'),'hw version');self.assertEqual(args[1],endpoint)
            for endpoint in ('tcp:localhost:0','tcp:host:65536','tcp:host','tcp:host:1;quit','COM3','tcp:-flag:1'):
                with self.assertRaises(ValueError):build_invocation(DeviceSettings(sys.executable,endpoint,d,'tcp'),'hw version')
        text=VERSION.replace('USB-CDC','FPC UART over TCP')
        self.assertTrue(parse_connection_status(text,'tcp')['connected']);self.assertFalse(parse_connection_status(text,'blueshark')['connected'])
        self.assertFalse(parse_connection_status('OS......... Iceman/test','tcp')['connected'])
