import json
from pathlib import Path
import tempfile
import unittest
from pm3gui.core import DumpData
from pm3gui.operations import Operation, sector_trailer
from pm3gui.writing import HFWritePlan, WriteOptions, parse_block, write_ack, access_conditions,block_sector,mfu_profile


UID='A1B2C3D4'
TRAILER=bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')


def classic_image():
    image=bytearray(1024)
    image[:4]=bytes.fromhex(UID)
    for s in range(16):image[(s*4+3)*16:(s*4+4)*16]=TRAILER
    return bytes(image)


class Card:
    """Independent in-memory RF responder, including masked trailer fields."""
    def __init__(self,data,family='mfc'):
        self.data=bytearray(data)
        self.uid=UID
        self.family=family
        self.writes=[]

    def respond(self,op):
        if isinstance(op,str):
            size={320:'MINI',1024:'1K',2048:'2K',4096:'4K'}.get(len(self.data),'1K')
            return (f'[+] UID: {self.uid}\nMIFARE Classic {size}\n' if self.family=='mfc' else f'[+] UID: {self.uid}\nNTAG 213\n'),0
        op.render()
        if op.kind=='hf14a_select': return f'[+] UID: {self.uid}\n[+] SAK: 08\n',0
        if op.kind=='mfu_info':return '[+] TYPE: NTAG 213\n',0
        if op.kind.startswith('mfu_'):
            if op.kind=='mfu_write_page':
                assert 4<=op.block<=39
                self.data[op.block*4:op.block*4+4]=bytes.fromhex(op.data_hex)
                self.writes.append(op.block)
                return '[+] Write ( ok )',0
            return f'[+] {op.block:02d}/0x{op.block:02X} | '+self.data[op.block*4:op.block*4+4].hex(' ')+' | ....',0
        sector=block_sector(op.block)
        trailer=bytes(self.data[sector_trailer(sector)*16:(sector_trailer(sector)+1)*16])
        actual=trailer[:6] if op.known_type=='A' else trailer[10:]
        cond=access_conditions(trailer)[3]
        if bytes.fromhex(op.auth_key)!=actual or op.known_type=='B' and cond in {0,1,2}:
            return '[!] Authentication failed',-1
        if op.kind=='mfc_write_block':
            assert op.block!=0
            self.data[op.block*16:(op.block+1)*16]=bytes.fromhex(op.data_hex)
            self.writes.append(op.block)
            return '[+] Write ( ok )\n[?] Hint: verify',0
        raw=bytes(self.data[op.block*16:(op.block+1)*16])
        if op.block==sector_trailer(sector):
            raw=bytes(6)+raw[6:10]+(raw[10:] if cond in {0,1,2} else bytes(6))
        return f'[+] {op.block:03d} | '+raw.hex(' ')+' | ................',0


class WritingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def prepare(self,source=None,options=None,metadata=None,card=None):
        source=source or classic_image()
        self.card=card or Card(classic_image())
        self.plan=HFWritePlan(DumpData(source,'bin',metadata or {},16),options or WriteOptions('mfc'),self.root)
        for _ in range(1000):
            op=self.plan.next_operation()
            if op is None:break
            self.plan.consume(*self.card.respond(op))
        else:self.fail('Preflight did not terminate')
        self.assertTrue(self.plan.ready)
        self.assertFalse(self.plan.approved)
        self.assertEqual(self.card.writes,[])
        return self.plan

    def execute(self):
        self.plan.approve()
        for _ in range(2000):
            op=self.plan.next_operation()
            if op is None:break
            self.plan.consume(*self.card.respond(op))
        else:self.fail('Write did not terminate')
        self.assertTrue(self.plan.done)

    def test_preflight_is_read_only_and_only_changed_data_is_written_and_verified(self):
        source=bytearray(classic_image());source[16:32]=bytes(range(16))
        self.prepare(bytes(source))
        self.assertEqual([e['block'] for e in self.plan.entries],[1])
        self.assertTrue((self.root/'write-report.json').exists())
        self.execute()
        self.assertEqual(self.card.writes,[1])
        self.assertEqual(bytes(self.card.data),bytes(source))
        report=json.loads((self.root/'write-report.json').read_text())
        self.assertEqual(report['status'],'verified')
        self.assertEqual(report['backup']['1'],'00'*16)
        self.assertEqual(report['verified_blocks'],[1])

    def test_uid_change_after_review_prevents_any_write(self):
        source=bytearray(classic_image());source[16]=1
        self.prepare(bytes(source));self.plan.approve()
        op=self.plan.next_operation()
        self.card.uid='11223344'
        with self.assertRaisesRegex(ValueError,'UID'):self.plan.consume(*self.card.respond(op))
        self.assertEqual(self.card.writes,[])

    def test_fail_text_with_zero_exit_is_not_success_and_never_retries(self):
        source=bytearray(classic_image());source[16]=1
        self.prepare(bytes(source));self.plan.approve()
        self.plan.consume(*self.card.respond(self.plan.next_operation()))
        op=self.plan.next_operation()
        self.assertEqual(op.kind,'mfc_write_block')
        with self.assertRaisesRegex(ValueError,'не подтвердил'):self.plan.consume('[!] Write ( fail )',0)
        self.plan.fail('ACK missing')
        self.assertIsNone(self.plan.next_operation())
        report=json.loads((self.root/'write-report.json').read_text())
        self.assertEqual(report['may_have_written'],[1])
        self.assertEqual(report['verified_blocks'],[])

    def test_readback_mismatch_stops_after_one_write(self):
        source=bytearray(classic_image());source[16]=1
        self.prepare(bytes(source));self.plan.approve()
        self.plan.consume(*self.card.respond(self.plan.next_operation()))
        self.plan.consume(*self.card.respond(self.plan.next_operation()))
        op=self.plan.next_operation();self.card.data[16]=2
        with self.assertRaisesRegex(ValueError,'не совпали'):self.plan.consume(*self.card.respond(op))
        self.assertEqual(self.card.writes,[1])

    def test_unknown_blocks_are_skipped_unless_explicitly_selected(self):
        source=bytearray(classic_image());source[16:32]=bytes(16);source[32]=7
        target=bytearray(classic_image());target[16:32]=b'X'*16
        known=[i for i in range(1024) if not 16<=i<32]
        self.prepare(bytes(source),metadata={'partial_dump':{'known_bytes':known}},card=Card(target))
        self.assertIn(1,self.plan.skipped)
        self.execute()
        self.assertEqual(self.card.data[16:32],b'X'*16)
        self.assertEqual(self.card.writes,[2])

    def test_keys_are_separate_and_preserve_target_access_conditions(self):
        source=bytearray(classic_image())
        source[48:54]=bytes.fromhex('112233445566')
        source[54:58]=bytes(4)  # Source ACL deliberately ignored.
        source[58:64]=bytes.fromhex('A0A1A2A3A4A5')
        self.prepare(bytes(source),WriteOptions('mfc',include_keys=True,first=3,last=3))
        self.execute()
        self.assertEqual(self.card.writes,[3])
        self.assertEqual(bytes(self.card.data[54:58]),TRAILER[6:10])
        self.assertEqual(len(self.plan.expected_keys),2)

    def test_unknown_keys_are_never_written_as_zero_placeholders(self):
        source=classic_image()
        options=WriteOptions('mfc',include_keys=True,include_unknown=True,first=3,last=3)
        plan=HFWritePlan(DumpData(source,'bin',{'partial_dump':{'known_bytes':list(range(48))}},16),options,self.root)
        with self.assertRaisesRegex(ValueError,'нет считанных'):plan.consume(*Card(source).respond(plan.next_operation()))

    def test_mfu_writes_only_user_pages_and_reads_them_back(self):
        target=bytearray(180);source=bytearray(180)
        source[0]=0xaa;source[16:20]=b'TEST';source[160:180]=bytes([9])*20
        self.prepare(bytes(source),WriteOptions('mfu'),card=Card(target,'mfu'))
        self.execute()
        self.assertEqual(self.card.writes,[4])
        self.assertEqual(self.card.data[:4],bytes(4))
        self.assertEqual(self.card.data[160:],bytes(20))

    def test_large_4k_sector_access_groups_use_five_data_blocks_and_choose_b(self):
        target=bytearray(4096);target[:4]=bytes.fromhex(UID)
        for sector in range(40):target[sector_trailer(sector)*16:(sector_trailer(sector)+1)*16]=TRAILER
        # Sector36: group0=000, group1=011, group2=000, trailer=011.
        a,b,c=0,10,10
        acl=bytes([((b^15)<<4)|(a^15),(a<<4)|(c^15),(c<<4)|b,0x69])
        base=sector_trailer(36)*16
        target[base+6:base+10]=acl
        source=bytearray(target);source[196*16]=1;source[197*16]=2
        self.prepare(bytes(source),WriteOptions('mfc',first=196,last=197),card=Card(target))
        self.assertEqual([(e['block'],e['type']) for e in self.plan.entries],[(196,'A'),(197,'B')])
        self.execute()
        self.assertEqual(self.card.writes,[196,197])

    def test_mini_and_2k_profiles_obey_actual_capacity(self):
        for sectors,size in [(5,320),(32,2048)]:
            with self.subTest(size=size):
                target=bytearray(size);target[:4]=bytes.fromhex(UID)
                for sector in range(sectors):target[sector_trailer(sector)*16:(sector_trailer(sector)+1)*16]=TRAILER
                source=bytearray(target);source[16]=1
                self.prepare(bytes(source),WriteOptions('mfc',first=1,last=1),card=Card(target))
                self.execute()
                self.assertEqual(self.card.writes,[1])

    def test_mfu_type_is_taken_only_from_fresh_unambiguous_type_line(self):
        self.assertEqual(mfu_profile('TYPE: MIFARE Ultralight EV1 48bytes (MF0UL1101)'),'UL EV1 48')
        self.assertEqual(mfu_profile('TYPE: MIFARE Ultralight C (MF0ULC)'),'UL-C')
        self.assertEqual(mfu_profile('TYPE: MIFARE Ultralight (MF0ICU1)'),'Ultralight')
        self.assertEqual(mfu_profile('TYPE: NTAG 213F 144bytes'),'NTAG 213')
        with self.assertRaises(ValueError):mfu_profile('TYPE: NTAG 213\nTYPE: NTAG 215')
        with self.assertRaises(ValueError):mfu_profile('TYPE: NTAG UNKNOWN\nHint: try NTAG 213')

    def test_typed_commands_reject_uid_overflow_and_injected_payload(self):
        for op in [Operation('mfc_write_block',block=0,auth_key='F'*12,data_hex='00'*16),
                   Operation('mfc_write_block',block=256,auth_key='F'*12,data_hex='00'*16),
                   Operation('mfc_write_block',block=3,auth_key='F'*12,data_hex='00'*16),
                   Operation('mfu_write_page',block=2,data_hex='00'*4),
                   Operation('mfc_write_block',block=1,auth_key='F'*12,data_hex=';hf mf wipe')]:
            with self.assertRaises(ValueError):op.render()
        self.assertEqual(Operation('mfc_write_block',block=1,auth_key='F'*12,data_hex='00'*16).render(),
                         'hf mf wrbl --blk 1 -a -k FFFFFFFFFFFF -d '+'00'*16)

    def test_parser_binds_address_and_ack_requires_positive_evidence(self):
        self.assertIsNone(parse_block('002 | '+'AA '*16+'|',1))
        self.assertFalse(write_ack('All done',0))
        self.assertFalse(write_ack('Write ( ok )\n[!] timeout',0))
        self.assertFalse(write_ack('Write ( ok )',-1))


if __name__=='__main__':unittest.main()
