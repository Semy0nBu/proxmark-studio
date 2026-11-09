import json
from pathlib import Path
import tempfile
import unittest
from pm3gui.core import export_dump,parse_dump
from pm3gui.operations import Detection
from pm3gui.partial import partial_classic,partial_ultralight,partial_key_file


class PartialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/'card.json'
        self.detected=Detection('mfc','A1B2C3D4','MIFARE Classic 1K','1k',True)

    def fixture(self,blocks=None,uid='A1B2C3D4'):
        blocks=blocks or {'0':'A1B2C3D4'+'00'*12,'1':'00'*16,'3':'112233445566FF078069A0A1A2A3A4A5'}
        self.path.write_text(json.dumps({'FileType':'mfc v2','Card':{'UID':uid},'blocks':blocks}),encoding='utf-8')
        return '[+] Sector... 0 block... 0 ( ok )\n[+] Sector... 0 block... 1 ( ok )\n[+] Sector... 0 block... 3 ( ok )\n'

    def partial(self):
        return partial_classic(self.path,self.detected,self.fixture(),'A1B2C3D4',[{'sector':0,'type':'A','key_hex':'112233445566'}])

    def test_gapped_blocks_keep_positions_and_real_zero_blocks_have_evidence(self):
        dump=self.partial();info=dump.metadata['partial_dump']
        self.assertEqual(len(dump.data),1024)
        self.assertEqual(dump.data[32:48],bytes(16))
        self.assertIn(1,info['read_blocks'])
        self.assertNotIn(1,info['missing_blocks'])
        self.assertIn(2,info['missing_blocks'])
        self.assertNotIn(58,info['known_bytes'])
        self.assertEqual(dump.data[58:64],bytes(6))
        self.assertEqual(dump.data[48:54],bytes.fromhex('112233445566'))
        self.assertFalse(dump.metadata['read_validation']['complete'])

    def test_zero_exit_size_and_nonzero_bytes_do_not_make_failed_blocks_known(self):
        log=self.fixture({'0':'A1B2C3D4'+'00'*12,'1':'AB'*16})
        dump=partial_classic(self.path,self.detected,log.replace('block... 1 ( ok )','block... 1 ( fail )'),'A1B2C3D4')
        self.assertEqual(dump.data[16:32],bytes(16))
        self.assertIn(1,dump.metadata['partial_dump']['missing_blocks'])

    def test_wrong_identity_no_successful_blocks_and_out_of_range_are_rejected(self):
        log=self.fixture()
        with self.assertRaises(ValueError):partial_classic(self.path,self.detected,log,'11223344')
        with self.assertRaises(ValueError):partial_classic(self.path,self.detected,'Write ok','A1B2C3D4')
        log=self.fixture(uid='11223344')
        with self.assertRaises(ValueError):partial_classic(self.path,self.detected,log,'A1B2C3D4')
        log=self.fixture({'64':'00'*16})
        with self.assertRaises(ValueError):partial_classic(self.path,self.detected,log,'A1B2C3D4')

    def test_json_and_flat_exports_round_trip_missing_map_without_sidecar_confusion(self):
        dump=self.partial()
        for suffix in ('json','bin','eml'):
            path=self.root/f'partial.{suffix}'
            export_dump(dump,path)
            parsed=parse_dump(path)
            self.assertEqual(parsed.data,dump.data)
            self.assertEqual(parsed.metadata['partial_dump'],dump.metadata['partial_dump'])
            if suffix!='json':self.assertTrue(path.with_name(path.name+'.pm3meta.json').is_file())

    def test_sidecar_cannot_mark_other_bytes_as_known(self):
        dump=self.partial();path=self.root/'partial.bin';export_dump(dump,path)
        path.write_bytes(bytes(1024))
        with self.assertRaisesRegex(ValueError,'другим байтам'):parse_dump(path)

    def test_partial_key_file_fills_only_transient_auth_slots_not_library(self):
        path=self.root/'available.bin'
        partial_key_file(path,'1k',[{'sector':0,'type':'A','key_hex':'112233445566'}])
        self.assertEqual(len(path.read_bytes()),192)
        self.assertEqual(path.read_bytes()[:6],bytes.fromhex('112233445566'))
        self.assertEqual(path.read_bytes()[6:],bytes(186))

    def test_mfu_requires_matching_prefix_artifacts_and_retains_page_addresses(self):
        detected=Detection('mfu','04112233445566','NTAG 213',supported=True)
        root={'FileType':'mfu','Card':{'UID':detected.uid},'blocks':{str(i):bytes([i]*4).hex() for i in range(10)}}
        self.path.write_text(json.dumps(root),encoding='utf-8')
        prefix=bytearray(56);prefix[11]=9
        self.path.with_suffix('.bin').write_bytes(prefix+b''.join(bytes([i]*4) for i in range(10)))
        output='[+] Reading tag memory...\n[!] Partial dump created. (10 of 45 blocks)\n'
        dump=partial_ultralight(self.path,detected,output,detected.uid)
        self.assertEqual(len(dump.data),180)
        self.assertEqual(dump.data[40:],bytes(140))
        self.assertEqual(dump.metadata['partial_dump']['missing_blocks'],list(range(10,45)))
        self.path.with_suffix('.bin').write_bytes(bytes(96))
        with self.assertRaises(ValueError):partial_ultralight(self.path,detected,output,detected.uid)


if __name__=='__main__':unittest.main()
