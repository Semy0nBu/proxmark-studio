import json
from pathlib import Path
import tempfile
import unittest
from pm3gui.operations import Detection
from pm3gui.reading import ClassicPartialRead
from pm3gui.core import parse_dump
from test_writing import Card,classic_image,UID


class IncrementalReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.detected=Detection('mfc',UID,'MIFARE Classic 1K','1k',True)
        self.card=Card(classic_image())

    def plan(self,sectors=(0,)):
        return ClassicPartialRead(self.detected,[{'sector':s,'type':'A','key_hex':'FFFFFFFFFFFF'} for s in sectors],self.root)

    def pump(self,plan):
        for _ in range(1000):
            op=plan.next_operation()
            if op is None:return
            plan.consume(*self.card.respond(op))
        self.fail('Read did not stop')

    def test_missing_keys_skip_sectors_and_save_only_confirmed_real_bytes(self):
        plan=self.plan((0,2));self.pump(plan);dump=plan.dump();info=dump.metadata['partial_dump']
        self.assertEqual(info['read_blocks'],[0,1,2,3,8,9,10,11])
        self.assertIn(4,info['missing_blocks']);self.assertEqual(dump.data[64:128],bytes(64))
        self.assertEqual(dump.metadata['read_validation']['confirmed_sectors'],[0,2])
        self.assertEqual(dump.data[16:32],self.card.data[16:32]);self.assertEqual(self.card.writes,[])
        self.assertEqual(json.loads(plan.path.read_text(encoding='utf-8'))['ProxmarkStudioRead']['status'],'finished')

    def test_disconnect_keeps_previous_sector_and_discards_current_unconfirmed_bytes(self):
        plan=self.plan((0,1))
        while not plan.guards:plan.consume(*self.card.respond(plan.next_operation()))
        checkpoint=parse_dump(plan.draft)
        self.assertEqual(checkpoint.metadata['partial_dump']['read_blocks'],[0,1,2,3])
        plan.consume(*self.card.respond(plan.next_operation())) # sector 1 initial UID.
        plan.consume(*self.card.respond(plan.next_operation())) # block 4.
        self.assertIn(4,plan.buffer)
        plan.next_operation();plan.consume('UART CreateFile failed for COM5 (Win32 error 1168)',-5)
        dump=plan.dump()
        self.assertEqual(dump.metadata['partial_dump']['read_blocks'],[0,1,2,3])
        self.assertEqual(dump.data[64:80],bytes(16));self.assertIsNone(plan.next_operation())
        self.assertEqual(set(json.loads(plan.path.read_text(encoding='utf-8'))['blocks']),{'0','1','2','3'})

    def test_lost_final_sector_select_does_not_publish_that_sector(self):
        plan=self.plan((0,1))
        while not (plan.position==1 and plan.stage=='confirm'):plan.consume(*self.card.respond(plan.next_operation()))
        plan.next_operation();plan.consume('invalid serial port COM5',-5)
        self.assertEqual(plan.dump().metadata['partial_dump']['read_blocks'],[0,1,2,3])

    def test_wrong_or_ambiguous_uid_stops_and_never_returns_unconfirmed_sector(self):
        for output in ('[+] UID: 11223344','[+] UID: '+UID+'\n[+] UID: 11223344'):
            with self.subTest(output=output):
                plan=self.plan();plan.next_operation()
                with self.assertRaisesRegex(ValueError,'UID'):plan.consume(output,0)
                self.assertTrue(plan.done);self.assertEqual(plan.confirmed,{})
                with self.assertRaises(ValueError):plan.dump()

    def test_denied_blocks_are_not_filled_from_stale_output_and_attempts_are_bounded(self):
        plan=self.plan();calls=[]
        while (op:=plan.next_operation()) is not None:
            calls.append(op)
            plan.consume(*('Authentication failed',-10) if op.kind=='mfc_read_block' and op.block==1 else self.card.respond(op))
        self.assertEqual(len(calls),6)
        self.assertEqual(plan.dump().metadata['partial_dump']['read_blocks'],[0,2,3])

    def test_stop_before_first_uid_guard_refuses_empty_dump(self):
        plan=self.plan();plan.stop('cancelled')
        with self.assertRaisesRegex(ValueError,'ни одного'):plan.dump()
        self.assertEqual(json.loads(plan.path.read_text(encoding='utf-8'))['blocks'],{})

    def test_read_tries_the_other_known_key_only_once_when_authentication_fails(self):
        plan=ClassicPartialRead(self.detected,[{'sector':0,'type':t,'key_hex':k} for t,k in
                                [('A','112233445566'),('B','FFFFFFFFFFFF')]],self.root)
        operations=[]
        while (op:=plan.next_operation()) is not None:
            operations.append(op)
            if op.kind=='hf14a_select':reply=self.card.respond(op)
            elif op.known_type=='A':reply=('Authentication failed',-10)
            else:reply=(f'[+] {op.block:03d} | '+self.card.data[op.block*16:(op.block+1)*16].hex(' ')+' | ................',0)
            plan.consume(*reply)
        self.assertEqual(len(operations),10);self.assertEqual(plan.dump().metadata['partial_dump']['read_blocks'],[0,1,2,3])

    def test_full_fallback_with_all_bytes_and_uid_guards_is_marked_complete(self):
        keys=[{'sector':s,'type':t,'key_hex':'FFFFFFFFFFFF'} for s in range(16) for t in ('A','B')]
        plan=ClassicPartialRead(self.detected,keys,self.root);self.pump(plan);dump=plan.dump()
        self.assertNotIn('partial_dump',dump.metadata);self.assertTrue(dump.metadata['read_validation']['complete'])
        self.assertEqual(dump.data,classic_image())


if __name__=='__main__':unittest.main()
