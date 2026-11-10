"""Synthetic RF responses exercise read-only recovery, including hidden A/B."""
import json
from pathlib import Path
import tempfile
import unittest
from test_writing import Card, classic_image
from pm3gui.core import DumpData
from pm3gui.writing import HFWritePlan, WriteOptions
from pm3gui.write_evidence import capture_plan
from pm3gui.write_readback import readback_flow

VERSION='[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n'


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def prepare(self,source,options,card=None):
        card=card or Card(classic_image())
        plan=HFWritePlan(DumpData(bytes(source),'bin',{},16 if options.family=='mfc' else 4),options,self.root/'plan')
        for _ in range(2000):
            op=plan.next_operation()
            if op is None:break
            plan.consume(*card.respond(op))
        self.assertTrue(plan.ready)
        self.capture=capture_plan(plan,operation_ref='test-operation',target_ref='test-target',selection_ref='test-source')
        return plan,card

    def run_check(self,plan,card,transform=None):
        directory=self.root/'check';directory.mkdir(exist_ok=True)
        flow=readback_flow(plan,self.capture.binding,'usb')(directory)
        request=next(flow);self.requests=[]
        for _ in range(3000):
            op=request.operation();self.requests.append(op)
            self.assertNotIn(op.kind,{'mfc_write_block','mfu_write_page'})
            if op.kind=='expert_tool':
                reply=(VERSION,0) if op.client_command=='hw version' else card.respond('hf search')
            else:reply=card.respond(op)
            if transform:reply=transform(op,reply)
            try:request=flow.send(reply)
            except StopIteration as done:return done.value
        self.fail('Readback did not terminate')

    def test_unwritten_data_differs_and_no_commands_are_replayed(self):
        source=bytearray(classic_image());source[16:20]=b'TEST'
        plan,card=self.prepare(source,WriteOptions('mfc',first=1,last=1))
        result=self.run_check(plan,card)
        self.assertEqual(result['compared_blocks'],[1]);self.assertEqual(result['matched_blocks'],[])
        self.assertEqual(card.writes,[]);self.assertEqual(plan.may_have_written,[])
        stored=json.loads(Path(result['report']).read_text())
        self.assertNotIn('entries',stored);self.assertNotIn('keys',stored)

    def test_readback_match_does_not_invent_dispatch(self):
        source=bytearray(classic_image());source[16:20]=b'TEST'
        plan,card=self.prepare(source,WriteOptions('mfc',first=1,last=1))
        card.data[16:20]=b'TEST'
        result=self.run_check(plan,card)
        self.assertEqual(result['matched_blocks'],[1]);self.assertEqual(plan.may_have_written,[])

    def test_trailer_uses_effective_preserved_acl_and_authenticates_masked_a(self):
        source=bytearray(classic_image());source[48:54]=bytes.fromhex('112233445566');source[54:58]=bytes(4)
        plan,card=self.prepare(source,WriteOptions('mfc',include_keys=True,first=3,last=3))
        card.data[48:54]=source[48:54]
        result=self.run_check(plan,card)
        self.assertEqual(result['matched_blocks'],[3])
        self.assertNotEqual(plan.entries[0]['new'][12:20],source[54:58].hex().upper())

    def test_authentication_with_old_key_confirms_trailer_difference(self):
        source=bytearray(classic_image());source[48:54]=bytes.fromhex('112233445566')
        plan,card=self.prepare(source,WriteOptions('mfc',include_keys=True,first=3,last=3))
        result=self.run_check(plan,card)
        self.assertEqual(result['compared_blocks'],[3]);self.assertEqual(result['matched_blocks'],[])

    def test_authentication_failures_leave_block_unknown(self):
        source=bytearray(classic_image());source[16]=1
        plan,card=self.prepare(source,WriteOptions('mfc',first=1,last=1))
        result=self.run_check(plan,card,lambda op,reply:('[!] Authentication failed',-1) if op.kind=='mfc_read_block' else reply)
        self.assertEqual(result['compared_blocks'],[])

    def test_link_loss_aborts_without_publishing_evidence(self):
        source=bytearray(classic_image());source[16]=1
        plan,card=self.prepare(source,WriteOptions('mfc',first=1,last=1))
        with self.assertRaises(ValueError):
            self.run_check(plan,card,lambda op,reply:('UART connection lost',-5) if op.kind=='mfc_read_block' else reply)
        self.assertFalse((self.root/'check'/'write-postcheck.json').exists())

    def test_changed_final_target_discards_all_results(self):
        source=bytearray(classic_image());source[16]=1
        plan,card=self.prepare(source,WriteOptions('mfc',first=1,last=1))
        searches=[]
        def change(op,reply):
            if op.kind=='expert_tool' and op.client_command=='hf search':
                searches.append(op)
                if len(searches)==2:return reply[0].replace(card.uid,'DEADBEEF'),0
            return reply
        with self.assertRaises(ValueError):self.run_check(plan,card,change)
        self.assertFalse((self.root/'check'/'write-postcheck.json').exists())

    def test_mfu_postcheck_reads_only_planned_user_pages(self):
        source=bytearray(180);source[16:20]=b'TEST'
        plan,card=self.prepare(source,WriteOptions('mfu',first=4,last=4),Card(bytes(180),'mfu'))
        card.data[16:20]=b'TEST'
        result=self.run_check(plan,card)
        self.assertEqual(result['matched_blocks'],[4]);self.assertEqual(card.writes,[])
