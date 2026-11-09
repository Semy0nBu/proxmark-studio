"""Prepared family writes: review is a real barrier, with independent responders."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import json
import re
import tempfile
import unittest
from unittest.mock import patch,PropertyMock
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication
from pm3gui.app import MainWindow
from pm3gui.core import DumpData,parse_hex,parse_dump,export_dump
from pm3gui.studio_data import structured_dump, encode_ndef
from pm3gui.studio_flows import Request,restore_hf_profile,clone_lf_profile,magic_uid,ndef_write,read_hf_profile
from pm3gui.wizard_plans import (ReviewGate,review_changes,data_block_write,desfire_file_write,
    desfire_data,desfire_settings,desfire_source_files,magic_user_restore,one_block)
from pm3gui.family_wizard import FamilyWizard,OPERATIONS
from test_writing import UID,classic_image
from test_lab_features import T55Card

VERSION='[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/fixture\n'


class BlockCard:
    def __init__(self,profile='MIFARE Plus',address=1):
        self.profile=profile;self.address=address;self.uid=UID if profile!='FeliCa' else '0102030405060708'
        self.data=bytes(8 if profile=='Topaz' else 16);self.writes=[];self.bad_mac=False;self.corrupt=False
    def respond(self,r):
        line=r.line
        if any(line.startswith(c) for c in ('hf 14a reader','hf topaz reader','hf felica reader')):
            return ('IDm: ' if self.profile=='FeliCa' else 'UID: ')+self.uid,0
        if ' rdbl' in line:
            number=f'{1:04X}' if self.profile=='FeliCa' else str(self.address)
            return f'[+] {number} | {self.data.hex(" ")} | error failed timeout\n'+('[!] WARNING: mac not equal...' if self.bad_mac else ''),0
        if ' wrbl' in line:
            self.writes.append(line);self.data=bytes.fromhex(re.search(r'-d (\w+)',line)[1])
            if self.corrupt:self.data=bytes(len(self.data))
            return 'Write ( ok )',0
        raise AssertionError(line)


class DesfireCard:
    def __init__(self,size=48,backup=False):
        self.uid=UID;self.data=bytearray(range(size)) if size<256 else bytearray(size)
        self.settings=bytes([int(backup),0,0xEE,0xEE])+size.to_bytes(3,'little')
        self.writes=[];self.corrupt_outside=False;self.reject_chunk=0
    def respond(self,r):
        line=r.line
        if line.startswith('hf 14a reader'):return 'UID: '+self.uid,0
        if line.startswith('hf mfdes getfilesettings'):return f'[+] App file 01 settings[{len(self.settings)}]: '+self.settings.hex(' '),0
        if line.startswith('hf mfdes read'):
            text=f'[+] Read {len(self.data)} bytes from file 0x01 offset 0\n'
            text+='\n'.join(f'[+] {i}/0x{i:02X} | '+self.data[i:i+16].hex(' ')+' | error in ASCII' for i in range(0,len(self.data),16))
            return text,0
        if line.startswith('hf mfdes write'):
            self.writes.append(line)
            if self.reject_chunk==len(self.writes):return '[!] Authentication error',-10
            offset=int(re.search(r'--offset (\w+)',line)[1],16);payload=bytes.fromhex(re.search(r'--data (\w+)',line)[1])
            self.data[offset:offset+len(payload)]=payload
            if self.corrupt_outside:self.data[-1]^=1
            return 'Write success',0
        raise AssertionError(line)


class PreparedModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.gates=[];self.requests=[]
    def pump(self,flow,respond,approve=lambda g:True):
        it=flow(self.root);self.addCleanup(it.close);request=next(it)
        for _ in range(1000):
            if isinstance(request,ReviewGate):self.gates.append(request);response=approve(request)
            else:
                request.operation().render();self.requests.append(request)
                response=respond(request)
                if response[1] and not request.allow_failure:raise ValueError('Client step failed: '+str(response[1]))
            try:request=it.send(response)
            except StopIteration as done:return done.value
        self.fail('Workflow did not terminate')

    def test_prepare_is_read_only_until_the_review_barrier_is_approved(self):
        card=BlockCard();flow=data_block_write('MIFARE Plus',1,b'\xA5'*16,key='FF'*16)
        it=flow(self.root);request=next(it)
        while not isinstance(request,ReviewGate):request=it.send(card.respond(request))
        self.assertEqual(card.writes,[]);self.assertTrue((self.root/'before.bin').is_file())
        self.assertEqual(request.rows[0]['after'],'A5 '*15+'A5');it.close();self.assertEqual(card.writes,[])

    def test_user_blocks_are_verified_for_each_supported_family(self):
        for profile,address in [('MIFARE Plus',1),('Topaz',2),('FeliCa',7)]:
            folder=self.root/profile;folder.mkdir();card=BlockCard(profile,address);payload=b'\xA5'*len(card.data)
            original=self.root;self.root=folder
            try:result=self.pump(data_block_write(profile,address,payload,key='FF'*16),card.respond)
            finally:self.root=original
            self.assertEqual(card.data,payload);self.assertEqual(len(card.writes),1)
            self.assertEqual(result['verification'],'single-user-block-and-identity-readback')
            if profile=='FeliCa':self.assertIn('--ble 8007',card.writes[0]);self.assertIn('SC 0900',self.gates[-1].rows[0]['address'])

    def test_same_uid_modified_bytes_after_review_stop_before_writing(self):
        card=BlockCard()
        def swap(g):card.data=b'\x11'*16;return True
        with self.assertRaises(ValueError):self.pump(data_block_write('MIFARE Plus',1,b'\xA5'*16,key='FF'*16),card.respond,swap)
        self.assertEqual(card.writes,[])

    def test_changed_uid_after_review_stops_before_writing(self):
        card=BlockCard()
        def swap(g):card.uid='11223344';return True
        with self.assertRaises(ValueError):self.pump(data_block_write('MIFARE Plus',1,b'\xA5'*16,key='FF'*16),card.respond,swap)
        self.assertEqual(card.writes,[])

    def test_mac_warning_with_zero_return_code_is_not_a_valid_backup(self):
        card=BlockCard();card.bad_mac=True
        with self.assertRaises(ValueError):self.pump(data_block_write('MIFARE Plus',1,b'\xA5'*16,key='FF'*16),card.respond)
        self.assertEqual(card.writes,[]);self.assertEqual(self.gates,[])

    def test_failed_readback_is_not_reported_as_success_or_retried(self):
        card=BlockCard();card.corrupt=True
        with self.assertRaises(ValueError):self.pump(data_block_write('MIFARE Plus',1,b'\xA5'*16,key='FF'*16),card.respond)
        self.assertEqual(len(card.writes),1)

    def test_declining_or_tampering_with_a_plan_never_writes(self):
        for tamper in (False,True):
            card=BlockCard()
            def approval(g):
                if tamper:(self.root/'expected.bin').write_bytes(b'\x11'*16)
                return tamper
            with self.assertRaises(ValueError):self.pump(data_block_write('MIFARE Plus',1,b'\xA5'*16,key='FF'*16),card.respond,approval)
            self.assertEqual(card.writes,[])

    def test_equal_data_does_not_need_approval_or_write(self):
        card=BlockCard();result=self.pump(data_block_write('MIFARE Plus',1,card.data,key='FF'*16),card.respond)
        self.assertEqual(card.writes,[]);self.assertEqual(self.gates,[]);self.assertEqual(result['changed_units'],0)

    def test_service_and_system_blocks_cannot_be_selected(self):
        for profile,address,size in [('MIFARE Plus',0,16),('MIFARE Plus',3,16),('MIFARE Plus',255,16),('Topaz',0,8),('Topaz',1,8),('Topaz',13,8),('FeliCa',0x88,16)]:
            with self.assertRaises(ValueError):data_block_write(profile,address,bytes(size),key='FF'*16)

    def test_desfire_updates_a_region_and_compares_the_entire_file_and_settings(self):
        card=DesfireCard(48,backup=True);before=bytes(card.data)
        result=self.pump(desfire_file_write('000001','01',8,b'\xA5'*20,key='00'*16),card.respond)
        self.assertEqual(card.data,before[:8]+b'\xA5'*20+before[28:]);self.assertIn('--commit',card.writes[0])
        self.assertEqual(result['verification'],'entire-selected-file-settings-and-uid-readback')
        self.assertEqual((self.root/'before.bin').read_bytes(),before)

    def test_desfire_large_payload_is_chunked_with_explicit_offsets(self):
        card=DesfireCard(1300);payload=b'\xA5'*1025
        self.pump(desfire_file_write('000001','01',17,payload,key='00'*16),card.respond)
        self.assertEqual([int(re.search(r'--offset (\w+)',s)[1],16) for s in card.writes],[17,529,1041])
        self.assertTrue(all(len(re.search(r'--data (\w+)',s)[1])<=1024 for s in card.writes))
        self.assertEqual(card.data[17:1042],payload)

    def test_desfire_failed_chunk_does_not_dispatch_later_chunks(self):
        card=DesfireCard(1300);card.reject_chunk=2
        with self.assertRaises(ValueError):self.pump(desfire_file_write('000001','01',0,b'\xA5'*1200,key='00'*16),card.respond)
        self.assertEqual(len(card.writes),2);self.assertEqual(card.data[1024:],bytes(276))

    def test_desfire_catches_changes_outside_the_edited_region(self):
        card=DesfireCard();card.corrupt_outside=True
        with self.assertRaises(ValueError):self.pump(desfire_file_write('000001','01',3,b'\xA5'*8,key='00'*16),card.respond)
        self.assertEqual(len(card.writes),1)

    def test_desfire_changed_file_settings_or_unread_data_stop_before_write(self):
        for variant in ('settings','missing-row'):
            card=DesfireCard()
            def approval(g):
                if variant=='settings':card.settings=b'\x01'+card.settings[1:]
                else:card.data=card.data[:-1]
                return True
            with self.assertRaises(ValueError):self.pump(desfire_file_write('000001','01',0,b'\xA5'*8,key='00'*16),card.respond,approval)
            self.assertEqual(card.writes,[])

    def test_desfire_record_value_files_and_invalid_keys_are_rejected(self):
        for filetype in (2,3,4,5):
            card=DesfireCard();card.settings=bytes([filetype])+card.settings[1:]
            with self.assertRaises(ValueError):self.pump(desfire_file_write('000001','01',0,b'\xA5',key='00'*16),card.respond)
            self.assertEqual(card.writes,[])
        for algo,key in [('AES','00'*8),('DES','00'*16),('3TDEA','00'*16)]:
            with self.assertRaises(ValueError):desfire_file_write('000001','01',0,b'\xA5',key=key,algo=algo)

    def test_desfire_actual_json_schema_keeps_unread_files_out_of_source_list(self):
        readable={'TypeRaw':'00','Read':True,'FileSize':2,'Data':'AABB'}
        doc={'FileType':'desfire','Applications':{'000001':{'Files':{'01':readable,'02':{'TypeRaw':'00','Read':False,'FileSize':2},
              '03':{**readable,'TypeRaw':'03'},'04':{**readable,'FileSize':3}}}}}
        items=desfire_source_files(structured_dump(doc,'DESFire'))
        self.assertEqual(items,[{'aid':'000001','fid':'01','data':bytes.fromhex('AABB')}])

    def test_desfire_parser_requires_contiguous_complete_file_and_bound_fid(self):
        card=DesfireCard();text=card.respond(Request('hf mfdes read'))[0]
        self.assertEqual(desfire_data(text,'01',48),bytes(card.data))
        for bad in (text.replace('16/0x10','32/0x20'),text.replace('0x01 offset','0x02 offset'),text+'\n0/0x00 | AA | .'):
            with self.assertRaises(ValueError):desfire_data(bad,'01',48)

    def test_magic_user_restore_preserves_uid_keys_and_access_for_both_generations(self):
        for generation in ('Gen1a','Gen4 GTU'):
            folder=self.root/generation;folder.mkdir();target=bytearray(classic_image());source=bytearray(target)
            source[16:32]=b'\xA5'*16;source[48:64]=b'\xBB'*16;writes=[]
            def reply(r):
                line=r.line
                if line.startswith('hf 14a info'):return f'UID: {UID}\nMIFARE Classic 1K\nMagic capabilities: {generation}',0
                if line.startswith('hf 14a reader'):return 'UID: '+UID,0
                if line.startswith(('hf mf csave','hf mf gsave')):
                    path=Path(re.search(r'-f "([^"]+)"',line)[1]);path.with_suffix('.bin').write_bytes(target);return 'Saved',0
                if line.startswith(('hf mf csetblk','hf mf gsetblk')):
                    b=int(re.search(r'--blk (\d+)',line)[1]);target[b*16:(b+1)*16]=bytes.fromhex(re.search(r'-d (\w+)',line)[1]);writes.append(b);return 'Write ( ok )',0
                self.fail(line)
            original=self.root;self.root=folder
            try:result=self.pump(magic_user_restore(DumpData(bytes(source),'bin',{},16),generation),reply)
            finally:self.root=original
            self.assertEqual(writes,[1]);self.assertEqual(target[:16],classic_image()[:16]);self.assertEqual(target[48:64],classic_image()[48:64])
            self.assertIn('preserved-service',result['verification'])

    def test_lf_review_has_factory_backup_and_no_clone_before_approval(self):
        card=T55Card();fields={'protocol':'EM410x','identifier':'0123456789','clock':64}
        def approval(g):self.assertEqual(card.writes,0);self.assertTrue(Path(g.backup).is_file());return True
        result=self.pump(clone_lf_profile(fields,review=True),lambda r:card.respond(r.operation()),approval)
        self.assertEqual(card.writes,1);self.assertEqual(result['verification'],'lf-all-blocks-identifier-and-factory-readback')

    def test_lf_same_tag_changed_payload_after_review_never_clones(self):
        card=T55Card()
        def approval(g):card.blocks[5]^=1;return True
        with self.assertRaises(ValueError):self.pump(clone_lf_profile({'protocol':'EM410x','identifier':'0123456789','clock':64},review=True),lambda r:card.respond(r.operation()),approval)
        self.assertEqual(card.writes,0)

    def test_restore_legic_pauses_then_rechecks_and_preserves_service_bytes(self):
        current=bytearray(range(22));source=bytes([0xAA]*22);writes=[]
        def reply(r):
            line=r.line
            if line.startswith('hf legic info'):return 'UID: '+UID,0
            if line.startswith('hf legic dump'):
                path=Path(re.search(r'-f "([^"]+)"',line)[1]);path.with_suffix('.json').write_text(json.dumps({'FileType':'legic v2','blocks':{'0':current[:16].hex(),'1':current[16:].hex()}}),encoding='utf-8');return 'Saved',0
            if line.startswith('hf legic restore'):writes.append(line);current[7:]=source[7:];return 'Done',0
            self.fail(line)
        dump=DumpData(source,'json',{'source_json':{'FileType':'legic v2','blocks':{'0':source[:16].hex(),'1':source[16:].hex()}}},16)
        result=self.pump(restore_hf_profile(dump,'LEGIC','','-f __studio_source__.json',review=True),reply)
        self.assertEqual(len(self.gates),1);self.assertEqual(len(writes),1);self.assertEqual(current[:7],bytes(range(7)))
        self.assertEqual(result['dump'].data,bytes(range(7))+b'\xAA'*15)

    def test_legic_22_native_json_import_edit_export_keeps_its_short_last_block(self):
        doc={'FileType':'legic v2','Card':{'UID':UID},'blocks':{'0':'AA'*16,'1':'BB'*6}}
        path=self.root/'legic.json';path.write_text(json.dumps(doc),encoding='utf-8')
        dump=parse_dump(path);self.assertEqual(len(dump.data),22)
        dump.data=dump.data[:7]+b'\xCC'*15;out=self.root/'edited.json';export_dump(dump,out)
        self.assertEqual(parse_dump(out).data,dump.data)
        self.assertEqual(len(bytes.fromhex(json.loads(out.read_text())['blocks']['1'])),6)

    def test_reviewed_ndef_message_has_fresh_backup_and_model_guard(self):
        before=encode_ndef([{'kind':'text','language':'en','value':'before'}]);payload=encode_ndef([{'kind':'text','language':'en','value':'after'}])
        current=[before];writes=[]
        def reply(r):
            line=r.line
            if line.startswith('hf 14a reader'):return 'UID: '+UID,0
            if line.startswith('hf mfu info'):return 'TYPE: NTAG 213',0
            if line.startswith('hf mfu ndefread'):
                Path(re.search(r'-f "([^"]+)"',line)[1]).with_suffix('.bin').write_bytes(current[0]);return 'Saved',0
            if line.startswith('hf mfu ndefwrite'):
                writes.append(line);current[0]=Path(re.search(r'-f "([^"]+)"',line)[1]).read_bytes();return 'Done',0
            self.fail(line)
        result=self.pump(ndef_write(payload,review=True),reply)
        self.assertEqual(len(self.gates),1);self.assertEqual(len(writes),1);self.assertEqual(result['ndef'],payload)
        self.assertEqual((self.root/'ndef-backup.bin').read_bytes(),before)

    def test_ndef_target_change_before_review_never_offers_a_write_plan(self):
        before=encode_ndef([{'kind':'text','language':'en','value':'before'}]);payload=encode_ndef([{'kind':'text','language':'en','value':'after'}]);selections=[0]
        def reply(r):
            if r.line.startswith('hf 14a reader'):
                selections[0]+=1;return 'UID: '+(UID if selections[0]==1 else '11223344'),0
            if r.line.startswith('hf mfu info'):return 'TYPE: NTAG 213',0
            if r.line.startswith('hf mfu ndefread'):
                Path(re.search(r'-f "([^"]+)"',r.line)[1]).with_suffix('.bin').write_bytes(before);return 'Saved',0
            self.fail(r.line)
        with self.assertRaises(ValueError):self.pump(ndef_write(payload,review=True),reply)
        self.assertEqual(self.gates,[])

    def test_magic_uid_review_verifies_user_blocks_and_saves_a_full_after_snapshot(self):
        current=bytearray(classic_image());uid=[UID];writes=[]
        def reply(r):
            line=r.line
            if line.startswith('hf 14a info'):return 'UID: '+uid[0]+'\nATQA: 00 04\nSAK: 08\nMagic Gen1a',0
            if line.startswith('hf 14a reader'):return 'UID: '+uid[0],0
            if line.startswith('hf mf csave'):
                Path(re.search(r'-f "([^"]+)"',line)[1]).with_suffix('.bin').write_bytes(current);return 'Saved',0
            if line.startswith('hf mf csetuid'):
                writes.append(line);uid[0]='11223344';current[:4]=bytes.fromhex(uid[0]);return 'Done',0
            self.fail(line)
        result=self.pump(magic_uid('11223344','0004','08',review=True),reply)
        self.assertEqual(len(self.gates),1);self.assertEqual(len(writes),1)
        self.assertEqual(result['uid'],'11223344');self.assertEqual(result['dump'].data[16:],classic_image()[16:])

    def test_block_payload_is_frozen_before_preparation(self):
        card=BlockCard();payload=bytearray(b'\xA5'*16);flow=data_block_write('MIFARE Plus',1,payload,key='FF'*16)
        payload[:]=b'\xBB'*16;self.pump(flow,card.respond);self.assertEqual(card.data,b'\xA5'*16)

    def test_invalid_felica_status_rows_and_duplicate_block_rows_are_rejected(self):
        for reply in ('0001 | Status flag 1... 01; Status flag 2... A1', '\n'.join(['0001 | '+'AA '*16]*2)):
            with self.assertRaises(ValueError):one_block(reply,1,16,hexadecimal=True)

    def test_iso15693_zero_exit_padded_dump_is_not_accepted_as_a_backup(self):
        complete=[False]
        def reply(r):
            if r.line.startswith('hf 15 reader'):return 'UID: E004010203040506',0
            path=Path(re.search(r'-f "([^"]+)"',r.line)[1]);path.with_suffix('.json').write_text(json.dumps({'FileType':'15693 v4',
                'blocks':{'0':'00000000','1':'00000000','2':'00000000','3':'00000000'}}),encoding='utf-8')
            return 'blk   1\rblk   2\rblk   3\rblk   4\n' if complete[0] else 'blk   1\rblk   2\n',0
        with self.assertRaises(ValueError):self.pump(read_hf_profile('ISO15693 / NFC-V',require_complete=True),reply)
        self.assertEqual(self.gates,[])
        complete[0]=True;result=self.pump(read_hf_profile('ISO15693 / NFC-V',require_complete=True),reply)
        self.assertEqual(len(result['dump'].data),16)

    def test_iclass_backup_requires_the_actual_native_block_count(self):
        complete=[False]
        def reply(r):
            if r.line.startswith('hf iclass reader'):return 'CSN: 0102030405060708',0
            path=Path(re.search(r'-f "([^"]+)"',r.line)[1]);path.with_suffix('.json').write_text(json.dumps({'FileType':'iclass',
                'blocks':{str(b):'00'*8 for b in range(32)}}),encoding='utf-8')
            return 'saving dump file - 32 blocks read' if complete[0] else 'saving dump file - 31 blocks read',0
        with self.assertRaises(ValueError):self.pump(read_hf_profile('iCLASS / PicoPass',require_complete=True),reply)
        self.assertEqual(self.gates,[])
        complete[0]=True;result=self.pump(read_hf_profile('iCLASS / PicoPass',require_complete=True),reply)
        self.assertEqual(len(result['dump'].data),256)


class PreparedWizardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.enterContext(patch('pm3gui.app.available_ports',return_value=[]));self.enterContext(patch('pm3gui.app.discover_client',return_value=''))
        self.run=self.enterContext(patch('pm3gui.app.ClientRunner.run'));self.ops=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.w=MainWindow(self.root/'profile');self.addCleanup(self.cleanup)
    def cleanup(self):
        if hasattr(self.w,'studio_deadline'):self.w.studio_deadline.stop()
        if self.w.read_job and self.w.read_job.get('flow'):self.w.read_job['flow'].close()
        self.w.read_job=None;self.w.dirty=False
        if getattr(self.w,'family_wizard',None):self.w.family_wizard.closing=False;self.w.family_wizard.close()
        self.w.close();self.w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents();self.tmp.cleanup()
    def finish(self,out='',code=0):self.w.operation_output=out;self.w._finished(code,'Fixture')
    def pending(self):return self.w.read_job.get('request')
    def prepare_mfp(self):
        d=self.w.open_family_wizard(operation='block:MIFARE Plus');d.payload.setPlainText('A5'*16);d.prepare_operation();card=BlockCard()
        for _ in range(10):
            if self.w.read_job['stage']=='review':break
            self.finish(*card.respond(self.pending()))
        self.assertEqual(self.w.read_job['stage'],'review');return d,card
    def cleanup_rf(self):self.finish();self.finish();self.finish(VERSION)

    def test_all_17_operations_construct_without_hardware_and_no_writes_start_on_open(self):
        d=self.w.open_family_wizard();self.assertEqual(d.operation.count(),17)
        for i in range(d.operation.count()):d.operation.setCurrentIndex(i)
        self.run.assert_not_called();self.ops.assert_not_called();self.assertFalse((self.root/'profile/sessions').exists())

    def test_review_table_is_a_real_pause_with_hardware_lock_and_disabled_parameters(self):
        d,card=self.prepare_mfp();self.assertTrue(d.commit.isEnabled());self.assertFalse(d.configuration.isEnabled())
        self.assertFalse(d.config_disclosure.header.isChecked())
        self.assertTrue(self.w.runner.rf_lease);self.assertFalse(self.w.read_button.isEnabled());self.assertEqual(card.writes,[])
        self.assertEqual(d.plan.rowCount(),1);self.assertEqual(d.plan.item(0,2).text(),'A5 '*15+'A5')
        report=json.loads((self.w.current_session/'studio-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'awaiting-approval');self.assertFalse(report['may_have_written']);self.assertFalse(report['review_approved'])

    def test_commit_verifies_before_completion_and_saves_only_the_verified_region(self):
        d,card=self.prepare_mfp();d.approve()
        for _ in range(12):
            if self.w.read_job['stage'].startswith('cleanup_'):break
            self.finish(*card.respond(self.pending()))
        self.assertIsNone(d.outcome);self.assertEqual(len(card.writes),1);self.cleanup_rf()
        self.assertEqual(d.outcome['status'],'completed');self.assertFalse(self.w.runner.rf_lease)
        rows=self.w.library.list_cards();self.assertEqual(len(rows),1)
        self.assertEqual(self.w.library.get_dump(rows[0]['id']).metadata['snapshot_kind'],'selected-region')

    def test_cancel_during_review_never_writes_and_cleans_up_rf(self):
        d,card=self.prepare_mfp();self.w.cancel_operation();self.cleanup_rf()
        self.assertEqual(card.writes,[]);self.assertEqual(d.outcome['status'],'cancelled');self.assertEqual(self.w.library.list_cards(),[])

    def test_wrong_review_digest_and_duplicate_clicks_do_not_start_unapproved_writes(self):
        d,card=self.prepare_mfp();self.w.approve_studio_review('wrong digest');self.cleanup_rf()
        self.assertEqual(card.writes,[]);self.assertEqual(d.outcome['status'],'failed')

    def test_failed_readback_or_link_never_produces_a_successful_library_result(self):
        d,card=self.prepare_mfp();d.approve();self.finish('',-5);self.cleanup_rf()
        self.assertEqual(d.outcome['status'],'failed');self.assertEqual(self.w.library.list_cards(),[])

    def test_uncertain_stop_keeps_lock_and_exposes_reset_in_the_wizard(self):
        d,card=self.prepare_mfp();self.w.cancel_operation();self.finish('',-5)
        self.assertEqual(self.w.read_job['stage'],'uncertain');self.assertTrue(self.w.runner.rf_lease)
        self.assertFalse(d.reset.isHidden());self.assertIsNone(d.outcome);self.assertFalse(d.prepare.isEnabled())

    def test_reopening_during_review_reuses_the_same_transaction_and_dialog(self):
        d,card=self.prepare_mfp();count=self.run.call_count+self.ops.call_count
        self.assertIs(self.w.open_family_wizard(profile='Topaz'),d);self.assertEqual(count,self.run.call_count+self.ops.call_count)

    def test_desfire_source_file_choice_uses_actual_json_and_does_not_zero_unread_files(self):
        doc={'FileType':'desfire','Applications':{'000001':{'Files':{'01':{'TypeRaw':'00','Read':True,'FileSize':3,'Data':'AABBCC'},'02':{'TypeRaw':'00','Read':False,'FileSize':3}}}}}
        d=self.w.open_family_wizard('DESFire',structured_dump(doc,'DESFire'));self.assertEqual(d.source_files.count(),2)
        d.source_files.setCurrentIndex(1);self.assertEqual(parse_hex(d.payload.toPlainText()),bytes.fromhex('AABBCC'))
        self.assertEqual(d.aid.text(),'000001');self.assertEqual(d.fid.text(),'01');self.run.assert_not_called();self.ops.assert_not_called()
