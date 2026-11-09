"""Safety/state integration for library emulation, LF writes, resume and map."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json,re,tempfile
from pathlib import Path
import unittest
from unittest.mock import patch,PropertyMock
from PySide6.QtCore import QCoreApplication,QEvent,QObject,Signal
from PySide6.QtWidgets import QApplication
from pm3gui.core import DumpData,parse_dump
from pm3gui.operations import Detection,Operation,identity_dump
from pm3gui.reading import ClassicPartialRead
from pm3gui.lf_writing import LFWritePlan,em410x_blocks
from pm3gui.memory_map import sector_info
from pm3gui.device import ClientRunner
from test_writing import Card,classic_image,UID


class T55Card:
    def __init__(self):
        self.blocks={b:0x11220000+b for b in range(8)};self.blocks[0]=0x00148040
        self.trace={1:0xa1b2c3d4,2:0x55667788};self.writes=0
    def respond(self,op):
        if isinstance(op,str):return '[ ARM ]\n OS......... Iceman/test\n',0
        cmd=op.render()
        if cmd.startswith('lf t55xx detect'):return f'Chip type......... T55x7\nBlock0............ {self.blocks[0]:08X}\n',0
        if cmd.startswith('lf t55xx read'):
            b=int(re.search(r'-b (\d)',cmd)[1]);value=(self.trace if '--pg1' in cmd else self.blocks)[b]
            return f'[+] {b:02d} | {value:08X} | 010101 | ....',0
        if cmd.startswith('lf em 410x clone'):
            identifier=re.search(r'--id (\w+)',cmd)[1];clock=int(re.search(r'--clk (\d+)',cmd)[1])
            self.blocks.update(em410x_blocks(identifier,clock));self.writes+=1;return '[+] Done!\nHint: verify',0
        if cmd.startswith('lf em 410x reader'):return '[+] EM 410x ID: 0123456789',0
        return '',0


class LabModelTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
    def pump_lf(self,plan,card):
        for _ in range(100):
            op=plan.next_operation()
            if op is None:return
            plan.consume(*card.respond(op))
        self.fail('LF did not terminate')
    def test_em410x_frame_has_row_column_parity_and_expected_rate(self):
        for clock,config in ((32,0x00088040),(64,0x00148040)):
            blocks=em410x_blocks('0123456789',clock);bits=f'{blocks[1]:032b}{blocks[2]:032b}'
            self.assertEqual(blocks[0],config);self.assertEqual(bits[:9],'1'*9);self.assertEqual(bits[-1],'0')
            rows=[bits[9+i*5:14+i*5] for i in range(10)]
            self.assertEqual(''.join(f'{int(r[:4],2):X}' for r in rows),'0123456789')
            self.assertTrue(all(sum(map(int,row))%2==0 for row in rows))
            self.assertEqual(bits[59:63],''.join(str(sum(int(r[i]) for r in rows)%2) for i in range(4)))
    def test_lf_prepare_is_readonly_and_write_checks_unrelated_blocks_and_factory_id(self):
        card=T55Card();plan=LFWritePlan('0123456789',64,self.root)
        self.pump_lf(plan,card);self.assertTrue(plan.ready);self.assertEqual(card.writes,0)
        self.assertTrue((self.root/'backup-page0.json').is_file());plan.approve();self.pump_lf(plan,card)
        self.assertTrue(plan.verified);self.assertEqual(card.writes,1)
    def test_lf_swap_after_review_stops_before_any_write(self):
        card=T55Card();plan=LFWritePlan('0123456789',64,self.root);self.pump_lf(plan,card)
        card.trace[1]^=1;plan.approve()
        with self.assertRaisesRegex(ValueError,'Заготовка'):self.pump_lf(plan,card)
        self.assertEqual(card.writes,0)
    def test_lf_unrelated_block_change_is_not_reported_success_and_no_replay(self):
        card=T55Card();plan=LFWritePlan('0123456789',64,self.root);self.pump_lf(plan,card);plan.approve()
        while (op:=plan.next_operation()) is not None:
            response=card.respond(op)
            if op.lf_command=='lf em 410x clone':card.blocks[7]^=1
            try:plan.consume(*response)
            except ValueError as exc:plan.fail(exc);break
        self.assertFalse(plan.verified);self.assertEqual(card.writes,1);self.assertIsNone(plan.next_operation())
        self.assertTrue(json.loads((self.root/'lf-write-report.json').read_text(encoding='utf-8'))['may_have_written'])
    def test_resume_skips_known_data_but_rechecks_uid_and_preserves_old_checkpoint(self):
        card=Card(classic_image());det=Detection('mfc',UID,'MIFARE Classic','1k',True)
        keys=[{'sector':s,'type':t,'key_hex':'FFFFFFFFFFFF'} for s in range(2) for t in ('A','B')]
        first=ClassicPartialRead(det,keys[:2],self.root/'first')
        while (op:=first.next_operation()) is not None:first.consume(*card.respond(op))
        old=first.draft.read_bytes();snapshot=first.dump();resume=ClassicPartialRead.resume(snapshot,keys,self.root/'second','first')
        reads=[]
        while (op:=resume.next_operation()) is not None:
            if op.kind=='mfc_read_block':reads.append(op.block)
            resume.consume(*card.respond(op))
        self.assertEqual(reads,[4,5,6,7]);self.assertEqual(resume.dump().metadata['partial_dump']['read_blocks'],list(range(8)))
        self.assertEqual(first.draft.read_bytes(),old)
        bad=ClassicPartialRead.resume(snapshot,keys,self.root/'bad');bad.next_operation()
        with self.assertRaisesRegex(ValueError,'UID'):bad.consume('[+] UID: 11223344',0)
    def test_colour_states_distinguish_unknown_partial_and_invalid_acl(self):
        data=classic_image();dump=DumpData(data,'bin',{},16)
        self.assertEqual(sector_info(dump,0)['state'],'complete')
        partial=DumpData(data,'bin',{'partial_dump':{'known_bytes':list(range(16))}},16)
        self.assertEqual(sector_info(partial,0)['state'],'partial');self.assertEqual(sector_info(partial,1)['state'],'missing')
        broken=bytearray(data);broken[54]=0
        self.assertEqual(sector_info(DumpData(bytes(broken),'bin',{},16),0)['state'],'invalid')
    def test_rf_lease_blocks_idle_pings_and_typed_stop_has_no_parameters(self):
        from PySide6.QtWidgets import QApplication
        app=QApplication.instance() or QApplication([]);runner=ClientRunner();runner.rf_lease=True
        with patch.object(runner._session,'_send') as send:runner._session._ping();send.assert_not_called()
        self.assertEqual(Operation('emulation_break').render(),'hw break')
        with self.assertRaises(ValueError):Operation('emulation_break',auth_key='FFFFFFFFFFFF').render()

    def test_command_executor_obeys_short_stop_timeout(self):
        from pm3gui.autowrite import CommandExecutor
        from pm3gui.device import DeviceSettings
        app=QApplication.instance() or QApplication([])
        class Fake(QObject):
            output=Signal(str);finished=Signal(int,str)
            def run_operation(self,settings,op,**kwargs):self.timeout=kwargs['timeout_ms'];self.output.emit('OK');self.finished.emit(0,'Done')
        runner=Fake();executor=CommandExecutor(DeviceSettings(),self.root,runner)
        self.assertEqual(executor(Operation('emulation_break')),('OK',0));self.assertEqual(runner.timeout,15000)


class LabAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.enterContext(patch('pm3gui.connection_checks.CLEANUP_VERSION_RECHECK_DELAY_MS',0))
        for name,value in (('available_ports',[]),('discover_client','')):self.enterContext(patch('pm3gui.app.'+name,return_value=value))
        self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.run=self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.operations=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        from pm3gui.app import MainWindow
        self.window=MainWindow(Path(self.temp.name).resolve());self.addCleanup(self.cleanup)
    def cleanup(self):
        w=self.window
        if hasattr(w,'emulation_timer'):w.emulation_timer.stop()
        w.read_job=None;w.dirty=False
        for name in ('emulation_dialog','lf_write_dialog','resume_dialog','memory_dialog'):
            if getattr(w,name,None) is not None:getattr(w,name).close()
        w.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents();self.temp.cleanup()
    def source(self,lf=False):
        dump=identity_dump(Detection('em410x','0123456789','EM410x',supported=True)) if lf else DumpData(classic_image(),'bin',{},16)
        cid=self.window.library.create_card('Fixture',dump,uid='0123456789' if lf else UID,protocol='EM410x' if lf else 'MIFARE Classic 1K')
        self.window.load_card(cid);return cid
    def finish(self,output='',code=0):self.window.operation_output=output;self.window._finished(code,'Fake response');self.app.processEvents()
    def cleanup_pump(self):
        for _ in range(5):
            if self.window.read_job is None:return
            self.finish('[+] Communicating with PM3 over USB-CDC\nOS......... Test-build' if self.window.read_job['stage']=='cleanup_confirm' else '')
        self.fail('Cleanup did not end')
    def prepare_emulator(self,dialog):
        dialog.start.click()
        for _ in range(3):
            self.finish('OS......... Test-build' if self.window.read_job['stage']=='emu_prepare_confirm' else '')
    def test_classic_emulation_loads_verifies_runs_and_stops_without_mutating_library(self):
        w=self.window;cid=self.source();original=w.library.get_dump(cid).data;w.open_emulation();d=w.emulation_dialog
        self.run.assert_not_called();self.operations.assert_not_called();self.prepare_emulator(d)
        self.assertEqual(w.read_job['stage'],'emu_load');self.finish('Loaded memory')
        (w.read_job['plan'].directory/'emulator-readback.bin').write_bytes(original);self.finish('Saved')
        self.assertEqual(self.operations.call_args.args[1].hf_command,'hf mf sim');self.finish('MIFARE Classic 1K\nOptions [ flags: 0 ]')
        self.assertEqual(w.read_job['stage'],'emulating');self.assertTrue(w.runner.rf_lease)
        w.cancel_operation();count=self.operations.call_count;w.cancel_operation();self.assertEqual(self.operations.call_count,count)
        self.cleanup_pump();self.assertFalse(w.runner.rf_lease)
        self.assertEqual(w.library.get_dump(cid).data,original);self.assertEqual(len(w.library.list_revisions(cid)),1)
    def test_failed_emulator_memory_comparison_never_starts_rf_and_cleanup_runs(self):
        w=self.window;self.source();w.open_emulation();self.prepare_emulator(w.emulation_dialog);self.finish('Loaded')
        (w.read_job['plan'].directory/'emulator-readback.bin').write_bytes(bytes(1024));self.finish('Saved')
        self.assertTrue(w.read_job['stage'].startswith('cleanup'));self.cleanup_pump()
        self.assertNotIn('hf mf sim',[call.args[1].hf_command for call in self.operations.call_args_list])
    def test_lf_wizard_controller_requires_review_and_finishes_only_after_cleanup(self):
        w=self.window;self.source(True);w.open_lf_write();d=w.lf_write_dialog;card=T55Card();d.prepare.click()
        for _ in range(30):
            if w.read_job['stage']=='review':break
            self.finish(*card.respond(w.read_job['plan'].pending[1]))
        self.assertEqual(card.writes,0);self.assertTrue(d.write.isEnabled());d.write.click()
        for _ in range(40):
            if w.read_job['stage'].startswith('cleanup'):break
            self.finish(*card.respond(w.read_job['plan'].pending[1]))
        self.assertEqual(card.writes,1);self.cleanup_pump();self.assertIn('проверена',d.status.text())
    def test_memory_and_resume_source_selection_do_not_issue_hardware_commands(self):
        w=self.window;self.source();w.open_memory_map();m=w.memory_dialog
        m.show_sector(4);self.assertEqual(m.sector,4);self.assertEqual(m.table.rowCount(),4)
        w.open_resume_read();self.assertFalse(w.resume_dialog.start.isEnabled())
        self.run.assert_not_called();self.operations.assert_not_called();self.assertFalse(w.dirty)
    def test_lost_link_keeps_rf_state_unknown_until_explicit_manual_reset(self):
        w=self.window;self.source();w.open_emulation();self.prepare_emulator(w.emulation_dialog);self.finish('Loaded')
        (w.read_job['plan'].directory/'emulator-readback.bin').write_bytes(classic_image());self.finish('Saved');self.finish('MIFARE Classic 1k')
        w._session_changed(False,'Link lost');self.assertEqual(w.read_job['stage'],'uncertain')
        self.assertFalse(w.emulation_dialog.reset_button.isHidden());w.acknowledge_lab_reset(w.emulation_dialog)
        self.assertIsNone(w.read_job);self.assertFalse(w.runner.rf_lease)

    def test_lf_emulation_native_wait_is_cancelled_then_rf_cleanup_is_required(self):
        w=self.window;self.source(True);w.open_emulation();d=w.emulation_dialog;self.prepare_emulator(d)
        self.assertEqual(w.read_job['plan'].family,'em410x');self.assertEqual(w.read_job['stage'],'emu_start')
        w._log('Starting simulating EM Tag ID\nPress pm3 button or <Enter> to abort simulation\n')
        self.assertTrue(w.read_job['active_notified']);self.assertTrue(w.emulation_timer.isActive())
        with patch('pm3gui.device.ClientRunner.busy',new_callable=PropertyMock,return_value=True),patch.object(w.runner,'cancel') as cancel:
            w.cancel_operation();cancel.assert_called_once()
        self.finish('Done!',-3);self.assertEqual(w.read_job['stage'],'cleanup_break');self.cleanup_pump()
        self.assertIsNone(w.read_job);self.assertFalse(w.emulation_timer.isActive())

    def test_resume_controller_checks_uid_and_keys_before_only_missing_sectors(self):
        from pm3gui.operations import key_file_for_session,sector_trailer
        w=self.window;card=Card(classic_image());keys=[{'sector':0,'type':t,'key_hex':'FFFFFFFFFFFF'} for t in ('A','B')]
        first=ClassicPartialRead(Detection('mfc',UID,'MIFARE Classic','1k',True),keys,Path(self.temp.name).resolve()/'prior')
        while (op:=first.next_operation()) is not None:first.consume(*card.respond(op))
        cid=w.library.create_card('Partial',first.dump(),uid=UID,protocol='MIFARE Classic 1K');w.load_card(cid)
        w.open_resume_read();w.resume_dialog.start.click();self.assertEqual(w.read_job['stage'],'resume_select')
        self.finish('[+] UID: '+UID+'\n[+] SAK: 08\n');self.assertEqual(w.read_job['stage'],'keys')
        key_file_for_session(w.current_session,UID).write_bytes(bytes.fromhex('FFFFFFFFFFFF')*32)
        log='\n'.join(f'[+] {s:03d} | {sector_trailer(s):03d} | FFFFFFFFFFFF | 1 | FFFFFFFFFFFF | 1 |' for s in range(16))
        self.finish(log);self.assertEqual(w.read_job['stage'],'partial_read')
        reads=[]
        for _ in range(150):
            if w.read_job is None:break
            op=w.read_job['partial_reader'].pending
            if op.kind=='mfc_read_block':reads.append(op.block)
            self.finish(*card.respond(op))
        self.assertIsNone(w.read_job);self.assertNotIn(0,reads);self.assertEqual(reads,list(range(4,64)))
        saved=w.library.get_dump(cid);self.assertNotIn('partial_dump',saved.metadata)
        self.assertEqual(saved.metadata['read_validation']['snapshot_scope'],'multiple_reads')

    def test_map_navigation_uses_byte_positions_and_refuses_an_unrelated_editor(self):
        w=self.window;self.source();w.hex_edit.setPlainText(classic_image().hex(' '));w.dirty=False
        w.open_memory_map();m=w.memory_dialog;m.show_sector(1);m.jump_to_block(0)
        self.assertEqual(len(w.hex_edit.textCursor().selectedText().split()),16)
        w.hex_edit.setPlainText('00 '*1024);m.jump_to_block(1)
        self.assertIn('изменился',m.heading.text())

    def test_report_write_failure_cannot_block_rf_stop(self):
        w=self.window;self.source();w.open_emulation();self.prepare_emulator(w.emulation_dialog);self.finish('Loaded')
        (w.read_job['plan'].directory/'emulator-readback.bin').write_bytes(classic_image());self.finish('Saved');self.finish('MIFARE Classic 1k')
        plan=w.read_job['plan']
        with patch.object(plan,'report',side_effect=OSError('disk unavailable')):
            w.cancel_operation();self.cleanup_pump()
        self.assertIsNone(w.read_job);self.assertFalse(w.runner.rf_lease);self.assertIn('отчёт',w.emulation_dialog.status.text())

    def emulation_cleanup(self,transport='blueshark',lf=False):
        w=self.window;w.transport_combo.setCurrentIndex(w.transport_combo.findData(transport))
        self.enterContext(patch.object(type(w.runner),'connection_transport',new_callable=PropertyMock,return_value=transport))
        self.source(lf);w.open_emulation();d=w.emulation_dialog;self.prepare_emulator(d)
        if not lf:
            self.finish('Loaded');(w.read_job['plan'].directory/'emulator-readback.bin').write_bytes(classic_image())
            self.finish('Saved');self.finish('MIFARE Classic 1K')
            w.cancel_operation()
        else:
            w._log('Press pm3 button to abort simulation\n')
            with patch.object(type(w.runner),'busy',new_callable=PropertyMock,return_value=True),patch.object(w.runner,'cancel'):
                w.cancel_operation()
            self.finish('',-2)
        self.assertEqual(w.read_job['stage'],'cleanup_break');self.finish();self.finish()
        self.assertEqual(w.read_job['stage'],'cleanup_confirm')
        return d,w.read_job['session']

    def test_hf_emulation_cleanup_rechecks_only_version_then_releases_lock(self):
        w=self.window;d,directory=self.emulation_cleanup();count=self.operations.call_count
        w.read_job['cleanup_version_deadline']-=1
        for _ in range(3):self.finish('Client banner only')
        self.assertEqual(w.read_job['stage'],'cleanup_confirm');self.assertTrue(w.runner.rf_lease)
        self.assertFalse(w.read_button.isEnabled());self.assertEqual(self.operations.call_count,count)
        self.assertEqual(self.run.call_args.args[1],'hw version');self.assertLess(self.run.call_args.kwargs['timeout_ms'],20000)
        self.finish('OS......... Fresh-build')
        self.assertIsNone(w.read_job);self.assertFalse(w.runner.rf_lease)
        self.assertEqual(sum(c.args[1].hf_command=='hf mf sim' for c in self.operations.call_args_list),1)
        checks=json.loads((directory/'cleanup-version-checks.json').read_text(encoding='utf-8'))['checks']
        self.assertEqual([c['connected'] for c in checks],[False,False,False,True]);self.assertEqual([c['probe_number'] for c in checks],[1,2,3,4])

    def test_lf_emulation_cancel_recheck_does_not_restart_simulation(self):
        w=self.window;self.emulation_cleanup(lf=True);before=self.operations.call_count
        self.finish('Client banner only');self.assertEqual(self.operations.call_count,before)
        self.finish('OS......... Fresh-build');self.assertIsNone(w.read_job)
        self.assertEqual(sum(c.args[1].lf_command=='lf em 410x sim' for c in self.operations.call_args_list),1)

    def test_lab_eighth_missing_os_retains_uncertain_lock(self):
        w=self.window;self.emulation_cleanup();before=self.run.call_count
        for _ in range(8):self.finish('Client banner only')
        self.assertEqual(w.read_job['stage'],'uncertain');self.assertTrue(w.runner.rf_lease)
        self.assertEqual(self.run.call_count,before+7)
        self.assertFalse(w.read_button.isEnabled());self.assertFalse(w.emulation_dialog.reset_button.isHidden())
        self.assertEqual(sum(c.args[1].hf_command=='hf mf sim' for c in self.operations.call_args_list),1)

    def test_lab_cleanup_does_not_retry_usb_errors_wrong_transport_or_expired_budget(self):
        cases=[('usb','Client banner only',0,False),('blueshark','Communicating with PM3 over USB-CDC',0,False),
               ('blueshark','No response from Proxmark3',0,False),('blueshark','OFFLINE mode',0,False),
               ('blueshark','Client banner only',-5,False),('blueshark','Client banner only',0,True)]
        for transport,output,code,expired in cases:
            with self.subTest(transport=transport,output=output,code=code,expired=expired):
                d,_=self.emulation_cleanup(transport);before=self.run.call_count
                if expired:w=self.window;w.read_job['cleanup_version_deadline']=0
                self.finish(output,code)
                self.assertEqual(self.window.read_job['stage'],'uncertain');self.assertEqual(self.run.call_count,before)
                self.window.acknowledge_lab_reset(d);d.close()


if __name__=='__main__':unittest.main()
