"""Laboratory data fidelity and protocol checks; no serial port is opened."""
from copy import deepcopy
import itertools
import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from pm3gui.core import DumpData, Library, parse_dump, export_dump
from pm3gui.keys import KeyLibrary
from pm3gui.writing import access_conditions, HFWritePlan, WriteOptions, source_family
from pm3gui.studio_data import *
from pm3gui.studio_flows import Request, HF_DUMP_PROFILES, LF_READERS
from pm3gui.client_catalog import COMMANDS, split_command
from test_writing import classic_image, Card, UID


class StudioDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_all_4096_access_combinations_roundtrip_with_redundancy(self):
        for values in itertools.product(range(8),repeat=4):
            acl=encode_access(values,0x69)
            self.assertEqual(access_conditions(bytes(6)+acl+bytes(6)),list(values))
            for i in range(3):
                broken=bytearray(acl);broken[i]^=1
                with self.assertRaises(ValueError):access_conditions(bytes(6)+broken+bytes(6))

    def test_readable_key_b_is_not_an_authentication_key(self):
        for trailer in (0,1,2):
            r=access_report([6,3,0,trailer]);self.assertFalse(r['key_b_authenticates'])
            self.assertEqual(r['data'][0],('A','-','-','A'));self.assertEqual(r['data'][1][0],'-')
        for trailer in range(3,8):self.assertTrue(access_report([0,0,0,trailer])['key_b_authenticates'])

    def test_selections_validate_without_silently_clamping(self):
        self.assertEqual(block_selection('1, 4 8-11',64),(1,4,8,9,10,11))
        for value in ('','1-0','64','-1','2-x','1 1-256','0x10'):
            with self.subTest(value=value),self.assertRaises(ValueError):block_selection(value,64)
        self.assertEqual(WriteOptions('mfc',blocks=[8,1]).blocks,(1,8))
        for blocks in ([1,1],[True],[-1],[256],'1,2'):
            with self.subTest(blocks=blocks),self.assertRaises(ValueError):WriteOptions('mfc',blocks=blocks)

    def test_selected_write_has_backup_and_only_writes_checked_blocks(self):
        data=bytearray(classic_image());data[16:32]=b'a'*16;data[64:80]=b'b'*16;data[128:144]=b'c'*16
        plan=HFWritePlan(DumpData(bytes(data),'bin',{},16),WriteOptions('mfc',blocks=(1,8)),self.root/'plan')
        card=Card(classic_image())
        for _ in range(200):
            op=plan.next_operation()
            if op is None:break
            plan.consume(*card.respond(op))
        self.assertTrue(plan.ready);self.assertEqual(card.writes,[])
        self.assertEqual({e['block'] for e in plan.entries},{1,8});plan.approve()
        for _ in range(200):
            op=plan.next_operation()
            if op is None:break
            plan.consume(*card.respond(op))
        self.assertTrue(plan.done);self.assertEqual(card.writes,[1,8]);self.assertEqual(card.data[64:80],classic_image()[64:80])

    def test_sector_bin_layout_and_candidate_provenance(self):
        entries=[{'sector':0,'type':'A','key_hex':'123456789ABC'},{'sector':15,'type':'B','key_hex':'AABBCCDDEEFF'}]
        raw,slots=binary_keys(entries,16);self.assertEqual(len(raw),192)
        self.assertEqual(raw[:6],bytes.fromhex('123456789ABC'));self.assertEqual(raw[-6:],bytes.fromhex('AABBCCDDEEFF'))
        self.assertEqual(parse_binary_keys(raw,16),entries)
        lib=KeyLibrary(self.root/'keys.sqlite3');self.addCleanup(lib.close)
        lib.record_candidate_keys(UID,entries,source='key.bin')
        self.assertTrue(all(not k['verified'] for k in lib.list_card_keys(UID)))
        lib.record_verified_keys(UID,entries[:1]);lib.record_candidate_keys(UID,entries,source='second')
        self.assertTrue(any(k['verified'] for k in lib.list_card_keys(UID)))
        self.assertEqual(len(slots),2)

    def test_bad_key_bin_and_conflicts_rejected(self):
        with self.assertRaises(ValueError):parse_binary_keys(bytes(191),16)
        with self.assertRaises(ValueError):binary_keys([{'sector':0,'type':'A','key_hex':'111111111111'},{'sector':0,'type':'A','key_hex':'222222222222'}],16)

    def test_extract_keys_respects_unknown_mask_and_merge_preserves_original(self):
        data=classic_image();partial=DumpData(data,'bin',{'partial_dump':{'known_bytes':list(range(54,60))}},16)
        self.assertEqual(key_entries(partial),[])
        source=DumpData(data,'bin',{},16);changed=merge_keys(source,[{'sector':0,'type':'A','key_hex':'123456789ABC'}])
        self.assertEqual(source.data,data);self.assertEqual(changed.data[54:58],data[54:58]);self.assertEqual(changed.data[48:54],bytes.fromhex('123456789ABC'))

    def test_compare_tracks_knownness_even_if_values_match(self):
        a=DumpData(bytes(16),'bin',{},16);b=DumpData(bytes(16),'bin',{'partial_dump':{'known_bytes':[0]}},16)
        self.assertEqual([r['offset'] for r in compare_dumps(a,b)],list(range(1,16)))

    def test_value_block_redundancy_and_signed_value(self):
        value=(-20).to_bytes(4,'little',signed=True);raw=value+bytes(x^255 for x in value)+value+bytes([7,248,7,248])
        self.assertEqual(decode_value_block(raw),(-20,7))
        for i in range(16):
            damaged=bytearray(raw);damaged[i]^=1
            with self.assertRaises(ValueError):decode_value_block(damaged)

    def test_ndef_multirecord_unicode_uri_id_and_tlv(self):
        records=[{'kind':'uri','value':'https://www.example.com/a'},{'kind':'text','language':'ru','value':'Привет 🌍','id':'aabb'},{'kind':'aar','value':'com.example.app'}]
        for tlv in (False,True):
            decoded=decode_ndef(encode_ndef(records,tlv));self.assertEqual([r['value'] for r in decoded],[r['value'] for r in records]);self.assertEqual(decoded[1]['id'],'aabb')
            self.assertEqual(decode_ndef(encode_ndef(decoded)),decoded)

    def test_ndef_long_payload_and_all_truncations(self):
        raw=encode_ndef([{'kind':'text','language':'en','value':'x'*1000}],True)
        self.assertEqual(len(decode_ndef(raw)[0]['value']),1000)
        for size in range(len(raw)-1):
            with self.assertRaises(ValueError):decode_ndef(raw[:size])

    def test_ndef_bad_flags_and_trailing_bytes_are_rejected(self):
        raw=encode_ndef([{'kind':'uri','value':'https://example.com'}])
        for bad in (bytes([raw[0]&0x7f])+raw[1:],bytes([raw[0]&0xbf])+raw[1:],raw+b'\x00',bytes([raw[0]|0x20])+raw[1:]):
            with self.assertRaises(ValueError):decode_ndef(bad)

    def test_signal_formats_and_spectrum_physical_frequency(self):
        samples=[int(100*math.sin(2*math.pi*64*i/1024)) for i in range(2048)]
        path=self.root/'a.pm3';path.write_text('\n'.join(map(str,samples)),encoding='ascii')
        values,rate=load_samples(path);self.assertIsNone(rate);self.assertEqual(values,samples)
        spectrum=fft_spectrum(values,1024);self.assertAlmostEqual(max(spectrum,key=lambda r:r[1])[0],64,delta=.5)
        wav=self.root/'a.wav'
        with wave.open(str(wav),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(1024);f.writeframes(struct.pack('<'+'h'*len(samples),*samples))
        self.assertEqual(load_samples(wav),(samples,1024))
        path.write_text('1\nnan\n',encoding='ascii')
        with self.assertRaises(ValueError):load_samples(path)

    def test_trace_annotation_and_crc_are_preserved(self):
        frames=parse_trace('[=] Start | End | Src | Data | CRC | Annotation\n 0 | 100 | Rdr | 30 04 26 EE | ok | READBLOCK(4)\n 102 | 250 | Tag | 00! 01 | !crc | RESPONSE\n')
        self.assertEqual(len(frames),2);self.assertEqual(frames[0].annotation,'READBLOCK(4)');self.assertEqual(frames[1].data,'00 01');self.assertEqual(frames[1].crc,'!crc')

    def test_real_client_lf_formats_and_ambiguous_ids(self):
        examples={'EM410x':'EM 410x ID: 0123456789','HID Prox':'TAG ID: 2000123456 (1) - Format Len: 26 bit - FC: 3 - Card: 4',
          'Indala':'Indala (len 64) Raw: 8000000012345678\nFmt 26 FC: 3 Card: 4',
          'AWID':'AWID - len: 26 FC: 3 Card: 4 - Wiegand: 1234, Raw: 011122223333444455556666',
          'ioProx':'IO Prox - XSF(01)03:00004, Raw: 1122334455667788 [CRC ok]',
          'FDX-B':'Animal ID........... 643-000000012345\nCountry Code...... 643 - Russia'}
        for profile,text in examples.items():
            fields=lf_identity(profile,text);self.assertEqual(fields['protocol'],profile);self.assertTrue(fields['identifier'])
        self.assertEqual(lf_identity('FDX-B',examples['FDX-B'])['national_code'],12345)
        self.assertEqual(lf_identity('ioProx',examples['ioProx'])['facility_code'],3)
        with self.assertRaises(ValueError):lf_identity('EM410x',examples['EM410x']+'\nEM 410x ID: 1123456789')
        with self.assertRaises(ValueError):lf_identity('ioProx',examples['ioProx']+'\nCRC failed')

    def test_structured_snapshot_roundtrip_and_raw_edit_rejection(self):
        document={'FileType':'desfire','Applications':[{'AID':'000001','Files':{'1':{'Data':'0102'}}}]}
        dump=structured_dump(document,'DESFire','01020304');path=self.root/'a.json';export_dump(dump,path)
        other=parse_dump(path);self.assertEqual(other.data,dump.data);self.assertEqual(other.metadata['structured_document'],document)
        with self.assertRaises(ValueError):export_dump(dump,self.root/'a.bin')
        with self.assertRaises(ValueError):source_family(dump)
        damaged=deepcopy(dump);damaged.data+=b'!'
        with self.assertRaises(ValueError):export_dump(damaged,self.root/'bad.json')
        lib=Library(self.root/'lib.sqlite3');self.addCleanup(lib.close);cid=lib.create_card('structured',dump,uid='01020304',protocol='DESFire')
        self.assertEqual(lib.get_dump(cid).metadata['structured_document'],document)

    def test_expert_boundary_disallows_chains_and_cross_operation_fields(self):
        from pm3gui.operations import Operation
        for c in ('data diff','trace load','mem spiffs tree','script run','hw status'):
            self.assertEqual(Request(c).operation().render(),c)
        for line in ('hw status; hf search','script run x | anything','data load -f "unclosed','unknown command','hw status\nlf search'):
            with self.assertRaises(ValueError):split_command(line)
        with self.assertRaises(ValueError):Operation('expert_tool',client_command='hw status',auth_key='FFFFFFFFFFFF').render()
        with self.assertRaises(ValueError):Operation('hf14a_select',client_command='hw status').render()

    def test_profile_commands_belong_to_the_actual_catalogue(self):
        for reader,dump in HF_DUMP_PROFILES.values():self.assertIn(reader,COMMANDS);self.assertIn(dump,COMMANDS)
        for reader in LF_READERS.values():self.assertIn(reader,COMMANDS)

    def test_current_felica_systems_dump_has_no_filetype_and_keeps_service_tree(self):
        doc={'specification_version':None,'product_information':None,'container_issue_information':None,
             'systems':[{'code':'0003','idm':'0102030405060708','pmm':'1122334455667788','nodes':[
                 {'type':'service','code':'090F','des_key_version':None,'aes_key_version':None,'data':{'0000':'AA'*16}}]}]}
        path=self.root/'felica.json';path.write_text(json.dumps(doc),encoding='utf-8')
        dump=parse_dump(path);self.assertEqual(dump.metadata['structured_document'],doc);self.assertEqual(dump.metadata['uid'],'0102030405060708')
        self.assertEqual(import_profile_dump(path,'FeliCa').metadata['protocol'],'FeliCa')
        from pm3gui.studio_flows import simulate_family
        it=simulate_family(dump,'FeliCa')(self.root);self.assertEqual(next(it).line,'hw break');it.send(('',0));request=it.send(('',0))
        self.assertTrue(request.line.startswith('hf felica sim'))
        it.close();doc['systems'][0]['nodes'][0]['data']['0000']='XX'
        self.assertFalse(is_felica_document(doc))
