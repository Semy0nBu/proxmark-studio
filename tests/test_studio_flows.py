"""Exercise native transactions against file-producing card/board doubles."""
from pathlib import Path
import json
import re
import tempfile
import unittest
from pm3gui.core import DumpData,export_dump,parse_dump
from pm3gui.operations import Operation
from pm3gui.studio_data import encode_ndef,structured_dump
from pm3gui.studio_flows import *
from test_writing import Card,classic_image,UID
from test_lab_features import T55Card


def quoted(line):
    return re.findall(r'"([^"]+)"',line)


class StudioFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def pump(self,flow,responder):
        it=flow(self.root);request=next(it);seen=[]
        for _ in range(500):
            line=request.line;seen.append(line);answer=responder(request)
            try:request=it.send(answer)
            except StopIteration as done:return done.value,seen
        self.fail('Flow did not terminate')

    def test_classic_selected_reads_only_requested_blocks_and_saves_mask(self):
        card=Card(classic_image());keys=[{'sector':s,'type':'A','key_hex':'FFFFFFFFFFFF'} for s in (0,1,2)]
        result,seen=self.pump(classic_selected('1k',UID,(1,4,8),keys),lambda r:card.respond(r.operation()))
        self.assertEqual(result['dump'].metadata['partial_dump']['read_blocks'],[1,4,8])
        reads=[int(re.search(r'--blk (\d+)',line)[1]) for line in seen if line.startswith('hf mf rdbl')]
        self.assertEqual(reads,[1,4,8]);self.assertEqual(card.writes,[])
        self.assertTrue((self.root/'partial-draft.json').is_file())

    def test_classic_changed_uid_cannot_publish_new_sector(self):
        keys=[{'sector':0,'type':'A','key_hex':'FFFFFFFFFFFF'}];it=classic_selected('1k',UID,(1,),keys)(self.root)
        next(it)
        with self.assertRaises(ValueError):it.send(('UID: 11223344',0))

    def test_gen1a_uid_write_requires_backup_and_uid_guard(self):
        uid=[UID];writes=[]
        def reply(request):
            line=request.line
            if line.startswith('hf 14a info'):return f'UID: {uid[0]}\nATQA: 00 04\nSAK: 08\nMagic capabilities: Gen1a',0
            if line.startswith('hf 14a reader'):return 'UID: '+uid[0],0
            if line.startswith('hf mf csave'):
                path=Path(quoted(line)[0]).with_suffix('.bin');path.write_bytes(classic_image());return 'Saved',0
            if line.startswith('hf mf csetuid'):
                self.assertTrue((self.root/'magic-before.bin').is_file());writes.append(line);uid[0]='11223344';return 'Done',0
            self.fail(line)
        result,seen=self.pump(magic_uid('11223344','0004','08'),reply)
        self.assertEqual(result['verification'],'uid-atqa-sak-readback');self.assertEqual(len(writes),1)

    def test_magic_unknown_generation_stops_before_any_write(self):
        it=magic_uid('11223344','0004','08')(self.root);next(it)
        with self.assertRaises(ValueError):it.send((f'UID: {UID}\nMagic: unknown',0))

    def test_magic_backup_failure_never_reaches_uid_command(self):
        it=magic_uid('11223344','0004','08')(self.root);next(it)
        req=it.send((f'UID: {UID}\nMagic Gen1a',0));self.assertIn('csave',req.line)
        with self.assertRaises((ValueError,FileNotFoundError)):it.send(('Saved',0))

    def test_imported_trace_uses_client_buffer_not_unrelated_device_capture(self):
        path=self.root/'input.trace';path.write_bytes(b'test')
        result,seen=self.pump(traces('14a',path),lambda r:(' 0 | 1 | Rdr | 26 | | REQA',0))
        self.assertIn('-1',seen[1]);self.assertIn('--buffer',seen[2]);self.assertFalse(any('sniff' in s for s in seen))

    def test_trace_capture_stop_retains_buffer_before_decoder(self):
        it=traces('14a',capture=True)(self.root);r=next(it);self.assertTrue(r.allow_capture_stop)
        r=it.send(('',-2));self.assertEqual(r.line,'hw break');r=it.send(('',0));self.assertEqual(r.line,'hw fpgaoff')
        r=it.send(('',0));self.assertTrue(r.line.startswith('trace save'))

    def test_ndef_write_has_unique_backup_and_readback_files(self):
        before=encode_ndef([{'kind':'text','language':'en','value':'before'}]);after=encode_ndef([{'kind':'text','language':'en','value':'after'}]);current=[before];writes=[]
        def reply(r):
            line=r.line
            if line.startswith('hf 14a reader'):return f'UID: {UID}',0
            if line.startswith('hf mfu info'):return 'TYPE: NTAG 213',0
            if line.startswith('hf mfu ndefread'):
                p=Path(quoted(line)[0]).with_suffix('.bin');p.write_bytes(current[0]);return 'Saved',0
            if line.startswith('hf mfu ndefwrite'):
                self.assertEqual((self.root/'ndef-backup.bin').read_bytes(),before);current[0]=Path(quoted(line)[0]).read_bytes();writes.append(line);return 'Done',0
            self.fail(line)
        result,seen=self.pump(ndef_write(after),reply)
        self.assertEqual(result['verification'],'ndef-readback-and-uid');self.assertEqual(len(writes),1)
        readfiles=[quoted(s)[0] for s in seen if s.startswith('hf mfu ndefread')];self.assertNotEqual(readfiles[0],readfiles[1])

    def test_ndef_changed_uid_stops_before_write(self):
        raw=encode_ndef([{'kind':'text','value':'test'}]);it=ndef_write(raw)(self.root);next(it)
        req=it.send((f'UID: {UID}',0));self.assertEqual(req.operation().client_command,'hf mfu info')
        req=it.send(('TYPE: NTAG 213',0));Path(quoted(req.line)[0]).with_suffix('.bin').write_bytes(raw)
        req=it.send(('Saved',0));self.assertIn('reader',req.line)
        with self.assertRaises(ValueError):it.send(('UID: 11223344',0))

    def test_structured_hf_dump_keeps_original_application_tree(self):
        doc={'FileType':'desfire','Applications':[{'AID':'000001','Files':{'1':'AABB'}}]}
        def reply(r):
            if r.line.startswith('hf mfdes dump'):Path(quoted(r.line)[0]).with_suffix('.json').write_text(json.dumps(doc),encoding='utf-8');return 'Saved',0
            return f'UID: {UID}',0
        result,seen=self.pump(read_hf_profile('DESFire'),reply)
        self.assertEqual(result['dump'].metadata['structured_document'],doc)

    def test_mismatched_hf_identity_is_not_added_as_a_library_result(self):
        it=read_hf_profile('ISO15693 / NFC-V')(self.root);next(it)
        r=it.send(('UID: E0 04 01 02 03 04 05 06',0));Path(quoted(r.line)[0]).with_suffix('.json').write_text('{"FileType":"15693 v4","blocks":{"0":"01020304"}}')
        it.send(('Saved',0))
        with self.assertRaises(ValueError):it.send(('UID: E0 04 01 02 03 04 05 07',0))

    def test_emulator_write_is_verified_and_backup_kept(self):
        data=[bytes(16)]
        def reply(r):
            if 'esetblk' in r.line:data[0]=bytes.fromhex(re.search(r'-d (\w+)',r.line)[1]);return 'Done',0
            return ' 1 | '+data[0].hex(' ').upper()+' | ...',0
        result,seen=self.pump(emulator_write(1,'AA'*16),reply)
        self.assertEqual(result['blocks'][1],'AA'*16);self.assertTrue((self.root/'emulator-before.txt').is_file())

    def test_mfu_simulation_requires_matching_ram_before_rf_start(self):
        data=DumpData(bytes(64),'json',{'source_json':{'FileType':'mfu','blocks':{str(i):'00000000' for i in range(16)}}},4)
        seen=[]
        def reply(r):
            seen.append(r.line)
            if 'esave' in r.line:
                path=Path(quoted(r.line)[0]).with_suffix('.json');export_dump(data,path)
            return 'MIFARE Ultralight',0
        result,_=self.pump(simulate_family(data,'Ultralight / NTAG',2),reply)
        self.assertIn('hf mfu sim -t 2',seen);self.assertIn('ram-readback',result['verification'])

    def test_lf_clone_uses_original_blank_backup_and_no_em410x_write(self):
        fields={'protocol':'HID Prox','identifier':'2000123456'};card=T55Card();writes=[]
        def reply(r):
            line=r.line
            if line.startswith('lf hid clone'):writes.append(line);return '[+] Done!',0
            if line.startswith('lf hid reader'):return 'TAG ID: 2000123456 (0) - Format Len: 26 bit - FC: 3 - Card: 4',0
            return card.respond(r.operation()) if r.operation().kind=='lf_tool' else card.respond(Operation('lf_tool',lf_command=r.operation().client_command,lf_args=r.operation().client_args))
        result,seen=self.pump(clone_lf_profile(fields),reply)
        self.assertEqual(len(writes),1);self.assertFalse(any(s.startswith('lf em 410x clone') for s in seen))
        self.assertTrue(Path(result['backup']).is_file());self.assertIn('identifier',result['verification'])

    def test_lf_clone_source_fields_require_actual_protocol_parameters(self):
        with self.assertRaises(ValueError):lf_profile_arguments({'protocol':'AWID','identifier':'AA'},'clone')
        self.assertEqual(lf_profile_arguments({'protocol':'ioProx','identifier':'AA','version':1,'facility_code':3,'card_number':4},'clone'),('lf io clone','--vn 1 --fc 3 --cn 4'))

    def test_spiffs_paths_cannot_escape_the_flat_filesystem(self):
        for name in ('../a','a/b','a\\b','a; wipe','.','x'*32):
            with self.assertRaises(ValueError):spiffs_file(name)
        self.assertEqual(spiffs_file('snapshot.bin'),'snapshot.bin')

    def test_current_identity_printers_use_dots_and_parenthesised_idm(self):
        self.assertEqual(identity('    CSN... 01 02 03 04 05 06 07 08 uid'),'0102030405060708')
        self.assertEqual(identity('Manufacture ID(IDm): 01 02 03 04 05 06 07 08'),'0102030405060708')

    def test_legic_restore_backs_up_and_preserves_target_uid_mcc_dcf(self):
        memory=bytearray(bytes(range(256)));source_raw=bytes([0xAA]*256)
        def image(raw):return {'FileType':'legic v2','blocks':{str(i):raw[i*16:(i+1)*16].hex() for i in range(16)}}
        source_path=self.root/'source.json';source_path.write_text(json.dumps(image(source_raw)));source=import_profile_dump(source_path,'LEGIC');writes=[]
        def reply(r):
            line=r.line
            if line.startswith('hf legic info'):return 'UID: 01 02 03 04',0
            if line.startswith('hf legic dump'):
                Path(quoted(line)[0]).with_suffix('.json').write_text(json.dumps(image(memory)));return 'Saved',0
            if line.startswith('hf legic restore'):
                self.assertTrue((self.root/'before/card.json').is_file());writes.append(line)
                memory[7:]=parse_dump(Path(quoted(line)[0])).data[7:];return 'Done!',0
            self.fail(line)
        result,_=self.pump(restore_hf_profile(source,'LEGIC','','-f __studio_source__.json'),reply)
        self.assertEqual(memory[:7],bytes(range(7)));self.assertEqual(memory[7:],source_raw[7:]);self.assertEqual(len(writes),1)
        self.assertIn('memory-and-identity-readback',result['verification'])

    def memory_responder(self,files,mutations):
        def reply(r):
            line=r.line
            if line=='mem spiffs tree; hw version':
                listing='\n'.join(f'[#] [{i:04d}] {len(data):5d} B |-- {name}' for i,(name,data) in enumerate(files.items())) or '[#] <empty>'
                return '[=] --- Flash Memory tree (SPIFFS) -----------------\n'+listing+'\n[ ARM ]\n OS......... Iceman/test\n',0
            if line.startswith('mem spiffs dump'):
                name=re.search(r'-s (\S+)',line)[1];Path(quoted(line)[0]).write_bytes(files[name]);return 'Saved',0
            if line.startswith('mem spiffs upload'):
                name=re.search(r'-d (\S+)',line)[1];files[name]=Path(quoted(line)[0]).read_bytes();mutations.append(line);return 'Done',0
            if line.startswith('mem spiffs remove'):
                del files[re.search(r'-f (\S+)',line)[1]];mutations.append(line);return 'Done',0
            if line.startswith(('mem spiffs rename','mem spiffs copy')):
                a=re.search(r'-s (\S+)',line)[1];b=re.search(r'-d (\S+)',line)[1];files[b]=files[a]
                if ' rename ' in line:del files[a]
                mutations.append(line);return 'Done',0
            self.fail(line)
        return reply

    def test_spiffs_upload_existing_file_has_byte_exact_backup_and_readback(self):
        files={'keys':b'old bytes'};mutations=[]
        result,_=self.pump(upload_spiffs('keys',b'new bytes'),self.memory_responder(files,mutations))
        self.assertEqual((self.root/'before/download.bin').read_bytes(),b'old bytes');self.assertEqual(files['keys'],b'new bytes');self.assertEqual(len(mutations),1)
        self.assertIn('byte-readback',result['verification'])

    def test_spiffs_new_file_never_overwrites_existing_name(self):
        it=upload_spiffs('keys',b'new',True)(self.root);next(it)
        with self.assertRaisesRegex(ValueError,'already exists'):it.send(('[=] Flash Memory tree (SPIFFS)\n[#] [0001] 3 B |-- keys\n[ ARM ]\n OS......... Iceman/test\n',0))
        files={};mutations=[];result,_=self.pump(upload_spiffs('newfile',b'new',True),self.memory_responder(files,mutations))
        self.assertEqual(files,{'newfile':b'new'});self.assertEqual(len(mutations),1)

    def test_spiffs_rename_and_remove_keep_backups_and_check_listing(self):
        files={'one':b'original'};mutations=[]
        result,_=self.pump(change_spiffs('rename','one','two'),self.memory_responder(files,mutations))
        self.assertEqual(files,{'two':b'original'});self.assertEqual(Path(result['backup']).read_bytes(),b'original')
        # A separate task directory is used just as the GUI does in production.
        nested=self.root/'remove';nested.mkdir();previous=self.root;self.root=nested
        try:result,_=self.pump(change_spiffs('remove','two'),self.memory_responder(files,mutations))
        finally:self.root=previous
        self.assertEqual(files,{});self.assertEqual(Path(result['backup']).read_bytes(),b'original')
