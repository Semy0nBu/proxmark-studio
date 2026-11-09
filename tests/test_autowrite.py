from dataclasses import replace
import json
from pathlib import Path
import tempfile
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch
from pm3gui.core import DumpData
from pm3gui.write_scenario import save_scenario,load_scenario
from pm3gui.writing import HFWritePlan,WriteOptions
from pm3gui.autowrite import run_transaction,main,CommandExecutor
from pm3gui.device import ClientRunner
from test_writing import Card,classic_image,UID


class AutoWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        data=bytearray(classic_image());data[16:32]=bytes(range(16))
        self.dump=DumpData(bytes(data),'bin',{},16)
        self.options=WriteOptions('mfc',first=1,last=1,target_uid=UID)

    def test_portable_scenario_is_a_fixed_dump_snapshot_and_does_not_export_connection_secrets(self):
        path=self.root/'scenario.pm3write.json'
        options=replace(self.options,key_a='112233445566',key_b='A0A1A2A3A4A5')
        save_scenario(path,self.dump,options,'Тест')
        dump,loaded,name=load_scenario(path)
        self.assertEqual(dump.data,self.dump.data);self.assertEqual(loaded.target_uid,UID)
        self.assertEqual(loaded.key_a,'FFFFFFFFFFFF');self.assertEqual(loaded.key_b,'FFFFFFFFFFFF')
        self.assertEqual(name,'Тест')
        self.dump.data=bytes(1024)
        self.assertNotEqual(load_scenario(path)[0].data,self.dump.data)

    def test_corrupt_scenario_hash_or_options_never_reaches_runner(self):
        path=self.root/'scenario.pm3write.json';save_scenario(path,self.dump,self.options)
        value=json.loads(path.read_text(encoding='utf-8'));value['source']['sha256']='0'*64
        path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'сумма'):load_scenario(path)
        with patch('pm3gui.autowrite.CommandExecutor') as execute,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--scenario',str(path),'--execute']),1)
            execute.assert_not_called()

    def test_execute_requires_a_bound_target_before_any_command(self):
        card=Card(classic_image());plan=HFWritePlan(self.dump,replace(self.options,target_uid=''),self.root/'plan')
        calls=[]
        def perform(op):calls.append(op);return card.respond(op)
        with self.assertRaisesRegex(ValueError,'UID'):run_transaction(plan,perform,True)
        self.assertEqual(calls,[]);self.assertEqual(card.writes,[])

    def test_preapproved_or_completed_transactions_cannot_be_replayed(self):
        card=Card(classic_image());plan=HFWritePlan(self.dump,self.options,self.root/'plan')
        run_transaction(plan,card.respond);plan.approve()
        with self.assertRaisesRegex(ValueError,'нов'):run_transaction(plan,card.respond,False)
        self.assertEqual(card.writes,[])

    def test_default_preparation_never_writes_and_explicit_execute_runs_once(self):
        card=Card(classic_image())
        plan=HFWritePlan(self.dump,self.options,self.root/'prepare')
        self.assertFalse(run_transaction(plan,card.respond))
        self.assertEqual(card.writes,[])
        plan=HFWritePlan(self.dump,self.options,self.root/'execute')
        self.assertTrue(run_transaction(plan,card.respond,True))
        self.assertEqual(card.writes,[1]);self.assertEqual(plan.verified,[1])

    def test_wrong_card_stops_before_backup_or_write(self):
        card=Card(classic_image());card.uid='11223344'
        plan=HFWritePlan(self.dump,self.options,self.root/'plan')
        with self.assertRaisesRegex(ValueError,'UID'):run_transaction(plan,card.respond,True)
        self.assertEqual(card.writes,[]);self.assertEqual(plan.backup,{})

    def test_readback_failure_is_reported_without_retry_or_rollback(self):
        card=Card(classic_image());plan=HFWritePlan(self.dump,self.options,self.root/'plan')
        def perform(op):
            result=card.respond(op)
            if getattr(op,'kind','')=='mfc_write_block':card.data[16]=0xff
            return result
        with self.assertRaisesRegex(ValueError,'не совпали'):run_transaction(plan,perform,True)
        self.assertEqual(card.writes,[1]);self.assertEqual(plan.verified,[])
        report=json.loads((plan.directory/'write-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'failed');self.assertEqual(report['may_have_written'],[1])

    def test_verified_new_keys_are_published_only_after_readback_and_uid_guard(self):
        data=bytearray(classic_image());data[48:54]=bytes.fromhex('112233445566');data[58:64]=bytes.fromhex('A0A1A2A3A4A5')
        plan=HFWritePlan(DumpData(bytes(data),'bin',{},16),replace(self.options,include_keys=True,first=3,last=3),self.root/'plan')
        card=Card(classic_image());stored=[]
        self.assertTrue(run_transaction(plan,card.respond,True,verified_keys=lambda uid,keys:stored.append((uid,keys))))
        self.assertEqual(len(stored),1);self.assertEqual(stored[0][0],UID);self.assertEqual(len(stored[0][1]),2)

    def test_cli_runs_child_process_preflight_and_one_write_without_reusing_output(self):
        # The child only reads/writes this temporary file. No serial API exists
        # in the responder and COM123 is only an argv fixture.
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        app=QApplication.instance() or QApplication([])
        root=Path(__file__).resolve().parents[1]
        script=self.root/'fake card client.py'
        script.write_text('import sys\nsys.path[:0]='+repr([str(root),str(root/'tests')])+'\n'+r'''
import json,shlex
from pathlib import Path
from test_writing import Card
from pm3gui.operations import Operation
args=sys.argv[1:]
if '--help' in args:
    print('fake --incognito')
    sys.exit(0)
state_path=Path(__file__).with_suffix('.json')
state=json.loads(state_path.read_text())
card=Card(bytes.fromhex(state['data']))
command=args[args.index('-c')+1]
parts=shlex.split(command)
if parts[:3]==['hf','mf','rdbl'] or parts[:3]==['hf','mf','wrbl']:
    writing=parts[2]=='wrbl'
    op=Operation('mfc_write_block' if writing else 'mfc_read_block',
        block=int(parts[parts.index('--blk')+1]),known_type='B' if '-b' in parts else 'A',
        auth_key=parts[parts.index('-k')+1],data_hex=parts[parts.index('-d')+1] if writing else '')
elif parts[:3]==['hf','14a','reader']:op=Operation('hf14a_select')
else:op=command
output,code=card.respond(op)
state['data']=card.data.hex();state['writes']+=card.writes
state_path.write_text(json.dumps(state))
print(output)
sys.exit(code)
''',encoding='utf-8')
        state=script.with_suffix('.json');state.write_text(json.dumps({'data':classic_image().hex(),'writes':[]}))
        scenario=self.root/'scenario.pm3write.json';save_scenario(scenario,self.dump,self.options)
        args=['--scenario',str(scenario),'--client',sys.executable,'--port','COM123','--data-dir',str(self.root/'profile')]
        def executor(settings,directory):return CommandExecutor(settings,directory,ClientRunner(argument_prefix=[str(script)]))
        with patch('pm3gui.autowrite.CommandExecutor',side_effect=executor),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args),0)
            self.assertEqual(json.loads(state.read_text())['writes'],[])
            self.assertEqual(main(args+['--execute']),0)
        current=json.loads(state.read_text())
        self.assertEqual(current['writes'],[1]);self.assertEqual(bytes.fromhex(current['data']),self.dump.data)
        sessions=list((self.root/'profile/sessions').iterdir());self.assertEqual(len(sessions),2)
        reports=[json.loads((p/'write-report.json').read_text()) for p in sessions]
        self.assertEqual({r['status'] for r in reports},{'review','verified'})
        for directory in sessions:
            logs=list(directory.glob('*.log'));self.assertGreater(len(logs),3)
            for log in logs:self.assertLessEqual(log.read_text(encoding='utf-8').count('[+] UID:'),1)


if __name__=='__main__':unittest.main()
