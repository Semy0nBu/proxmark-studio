"""Cancellable real helper children and fake native CLI; no Bluetooth I/O."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QProcess, QTimer
from PySide6.QtWidgets import QApplication
from pm3gui.device import ClientRunner, DeviceSettings
from pm3gui.persistent import PersistentSession
from test_persistent import FAKE


NATIVE_TRACE = r'''
import json,pathlib,sys,time
root=pathlib.Path(__file__).parent
event='native-help' if '--help' in sys.argv else 'native-open'
with (root/'lifecycle.jsonl').open('a') as f:f.write(json.dumps({'event':event,'time':time.monotonic()})+'\n')
'''

HELPER = r'''
import json,os,pathlib,sys,time
root=pathlib.Path(__file__).parent
args=sys.argv[1:];port=args[args.index('--port')+1];target=pathlib.Path(args[args.index('--result')+1])
config=json.loads((root/'helper-config.json').read_text())
def trace(event,**values):
 with (root/'lifecycle.jsonl').open('a') as f:f.write(json.dumps({'event':event,'time':time.monotonic(),'pid':os.getpid(),**values})+'\n')
trace('helper-start',port=port,result=str(target),env={k:os.environ.get(k) for k in ('PYTHONHOME','PYTHONPATH','VIRTUAL_ENV','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','PYINSTALLER_RESET_ENVIRONMENT','__PYVENV_LAUNCHER__','PYTHONEXECUTABLE')})
time.sleep(config.get('delay',0))
mode=config.get('mode','ready')
if mode=='missing':sys.exit(0)
if mode=='stdout-only':
 print(json.dumps({'proto':1,'port':port,'status':'ready','queries':1,'elapsed_ms':1,'last_error':None,'channel':1}),flush=True);sys.exit(0)
if mode=='oversized-output':
 print('PRIVATE-MARKER-'+'x'*9000,flush=True);time.sleep(1);sys.exit(0)
result={'proto':1,'port':port,'status':'ready','queries':1,'elapsed_ms':1,'last_error':None,'channel':1}
if mode in ('unavailable','not_bluetooth','error'):
 result.update(status=mode,channel=None,last_error=None if mode=='not_bluetooth' else 121,queries=0 if mode=='not_bluetooth' else 1)
if mode=='wrong-port':result['port']='COM77'
if mode=='extra-field':result['address']='PRIVATE-MARKER'
payload=(b'not JSON PRIVATE-MARKER' if mode=='invalid-json' else b'x'*8193 if mode=='oversized-file' else json.dumps(result).encode())
target.write_bytes(payload)
if config.get('noise'):print('PRIVATE-MARKER',flush=True)
trace('helper-end',port=port)
sys.exit(7 if mode=='nonzero-ready' else 0)
'''


class BluetoothPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.native=self.root/'native.py';self.native.write_text(NATIVE_TRACE+FAKE,encoding='utf8')
        self.helper=self.root/'helper.py';self.helper.write_text(HELPER,encoding='utf8')
        self.configure();self.helper_config()
        self.runner=ClientRunner(argument_prefix=[str(self.native)])
        self.settings=DeviceSettings(getattr(sys,'_base_executable',sys.executable),'COM5',str(self.root),'blueshark',True)
        self.results=[];self.outputs=[];self.completed=[];self.invocations=[];self.result_paths=[]
        self.runner.finished.connect(lambda code,message:self.results.append((code,message)))
        self.runner.completed.connect(lambda token,code,message:self.completed.append((token,code)))
        self.runner.output.connect(self.outputs.append)
        self.default_gate=self.runner._session._should_prepare_bluetooth
        def needed(run):
            return self.runner._session.transport=='blueshark' and not getattr(run,'bluetooth_prepared',False)
        self.enterContext(patch.object(self.runner._session,'_should_prepare_bluetooth',side_effect=needed))
        def invocation(port,budget_ms=7000,result_path=None):
            self.invocations.append((port,budget_ms));self.result_paths.append(Path(result_path))
            return self.settings.client_path,[str(self.helper),'--port',port,'--result',str(result_path)],str(self.root)
        self.factory=self.enterContext(patch('pm3gui.bluetooth_ready.preparation_invocation',side_effect=invocation))

    def tearDown(self):
        self.runner.shutdown()
        self.runner.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents();self.temp.cleanup()

    def configure(self,**values):(self.root/'config.json').write_text(json.dumps(values),encoding='utf8')
    def helper_config(self,**values):(self.root/'helper-config.json').write_text(json.dumps(values),encoding='utf8')

    def wait(self,predicate,timeout=5000):
        if predicate():return
        loop=QEventLoop();poll=QTimer();limit=QTimer()
        poll.setInterval(5);poll.timeout.connect(lambda:loop.quit() if predicate() else None)
        limit.setSingleShot(True);limit.timeout.connect(loop.quit)
        poll.start();limit.start(timeout);loop.exec();poll.stop();limit.stop()
        self.assertTrue(predicate(),'Child process or completion deadline')

    def settle(self,ms=150):
        loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()

    def rows(self,name):
        p=self.root/name
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def run_command(self,command='hw version',**kwargs):
        before=len(self.results);self.runner.run(self.settings,command,**kwargs)
        self.wait(lambda:len(self.results)>before)
        return self.results[-1][0]

    def start_slow_preparation(self,**kwargs):
        self.helper_config(delay=3)
        self.runner.run(self.settings,'hf search',**kwargs)
        self.wait(lambda:any(x['event']=='helper-start' for x in self.rows('lifecycle.jsonl')))
        self.assertEqual(self.runner._session.phase,'preparing')

    def assert_no_native_open(self):
        self.assertFalse(any(x['event']=='native-open' for x in self.rows('lifecycle.jsonl')))
        self.assertEqual(self.rows('trace.jsonl'),[])

    def assert_temp_clean(self):
        self.assertTrue(self.result_paths)
        self.assertTrue(all(not path.parent.exists() for path in self.result_paths))

    def test_fresh_service_preparation_precedes_first_native_open_and_requires_fpc_bootstrap(self):
        self.assertEqual(self.run_command('hf search'),0)
        events=[x['event'] for x in self.rows('lifecycle.jsonl')]
        self.assertEqual(events,['native-help','helper-start','helper-end','native-open'])
        self.assertEqual([x[1] for x in self.rows('trace.jsonl')],['hw version','hf search'])
        self.assertEqual(len(self.results),1);self.assertEqual(len(self.completed),1)
        self.assertTrue(self.runner.session_open);self.assert_temp_clean()

    def test_helper_ready_is_not_a_device_connection_without_correct_fresh_channel(self):
        self.configure(channel='USB-CDC')
        self.assertEqual(self.run_command('hf search'),-5)
        self.assertFalse(self.runner.session_open)
        self.assertEqual([x[1] for x in self.rows('trace.jsonl')],['hw version'])

    def test_connected_session_skips_preparation_and_cached_help_still_prepares_after_close(self):
        self.assertEqual(self.run_command(),0);pid=self.runner._session.process.processId()
        self.assertEqual(self.run_command(),0)
        self.assertEqual(self.runner._session.process.processId(),pid)
        self.assertEqual(self.factory.call_count,1)
        self.runner.disconnect();self.wait(lambda:not self.runner._session.has_process)
        self.assertEqual(self.run_command(),0)
        self.assertEqual(self.factory.call_count,2)
        self.assertEqual(sum(x['event']=='native-help' for x in self.rows('lifecycle.jsonl')),1)
        self.assert_temp_clean()

    def test_failed_or_unrelated_service_never_opens_com_and_does_not_leak_worker_data(self):
        for mode in ('unavailable','not_bluetooth','error','invalid-json','wrong-port','extra-field',
                     'oversized-file','missing','stdout-only','nonzero-ready','oversized-output'):
            with self.subTest(mode=mode):
                self.helper_config(mode=mode)
                self.assertEqual(self.run_command('hf search'),-5)
                self.assertFalse(self.runner.session_open);self.assertFalse(self.runner.busy)
                self.assert_no_native_open();self.assert_temp_clean()
        text=''.join(self.outputs)+' '.join(message for _,message in self.results)
        self.assertNotIn('PRIVATE-MARKER',text);self.assertNotIn('"proto"',text)
        self.assertEqual(len(self.results),11);self.assertEqual(len(self.completed),11)

    def test_helper_has_clean_environment_and_file_ipc_without_stdout(self):
        dirty={name:'PRIVATE-PATH' for name in ('PYTHONHOME','PYTHONPATH','VIRTUAL_ENV','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH')}
        self.helper_config(noise=True)
        # The native fixture is Python too; only it ignores the deliberately
        # poisoned parent interpreter variables. The helper receives no prefix.
        self.runner._argument_prefix=['-E',str(self.native)]
        with patch.dict(os.environ,dirty):self.assertEqual(self.run_command(),0)
        env=next(x['env'] for x in self.rows('lifecycle.jsonl') if x['event']=='helper-start')
        self.assertTrue(all(env[name] is None for name in dirty))
        self.assertEqual(env['PYINSTALLER_RESET_ENVIRONMENT'],'1')
        self.assertNotIn('PRIVATE-MARKER',''.join(self.outputs));self.assert_temp_clean()

    def test_source_helper_actual_process_is_owned_and_frozen_environment_has_no_source_launcher(self):
        self.start_slow_preparation()
        actual=next(row['pid'] for row in self.rows('lifecycle.jsonl') if row['event']=='helper-start')
        self.assertEqual(actual,self.runner._session.preparation_process.processId())
        self.runner.cancel();self.wait(lambda:bool(self.results))
        with patch.dict(os.environ,{'__PYVENV_LAUNCHER__':'UNTRUSTED','PYTHONEXECUTABLE':'UNTRUSTED'}), \
             patch('pm3gui.persistent.sys.frozen',True,create=True):
            env=PersistentSession._preparation_environment()
            self.assertFalse(env.contains('__PYVENV_LAUNCHER__'))
            self.assertFalse(env.contains('PYTHONEXECUTABLE'))
        self.assert_temp_clean()

    def test_cancel_from_preparation_status_sends_no_helper_or_native_open(self):
        def cancel(connected,message):
            if self.runner._session.phase=='preparing':
                self.runner.session_changed.disconnect(cancel)
                self.runner.cancel()
        self.runner.session_changed.connect(cancel)
        self.assertEqual(self.run_command('hf search'),-2)
        self.settle();self.assert_no_native_open();self.assert_temp_clean()
        self.assertFalse(any(x['event']=='helper-start' for x in self.rows('lifecycle.jsonl')))
        self.assertIsNone(self.runner._session.pending)

    def test_cancel_status_listener_can_start_new_profile_without_old_helper_cleanup_losing_it(self):
        def replace(connected,message):
            if self.runner._session.phase=='preparing':
                self.runner.session_changed.disconnect(replace)
                self.runner.cancel()
                self.settings.port='COM6'
                self.runner.run(self.settings,'hf search')
        self.runner.session_changed.connect(replace)
        self.runner.run(self.settings,'hf search')
        self.wait(lambda:len(self.results)==2)
        self.assertEqual([x[0] for x in self.results],[-2,0])
        self.assertEqual([x[0] for x in self.invocations],['COM5','COM6'])
        self.assertEqual([x['port'] for x in self.rows('lifecycle.jsonl') if x['event']=='helper-start'],['COM6'])
        self.assertEqual([x[1] for x in self.rows('trace.jsonl')],['hw version','hf search'])
        self.assert_temp_clean()

    def test_stop_during_preparation_releases_helper_and_cannot_later_send_rf(self):
        self.start_slow_preparation();self.runner.cancel()
        self.wait(lambda:bool(self.results) and not self.runner._session.has_process)
        self.settle()
        self.assertEqual([x[0] for x in self.results],[-2])
        self.assert_no_native_open();self.assert_temp_clean()

    def test_operation_timeout_includes_preparation_and_is_not_restarted(self):
        started=time.monotonic();self.start_slow_preparation(timeout_ms=450)
        self.wait(lambda:bool(self.results) and not self.runner._session.has_process)
        self.assertEqual([x[0] for x in self.results],[-3])
        self.assertLess(time.monotonic()-started,1.5)
        self.assert_no_native_open();self.assert_temp_clean()

    def test_helper_has_its_own_hard_deadline_within_larger_command_budget(self):
        with patch('pm3gui.persistent.BLUESHARK_PREPARATION_HARD_TIMEOUT_MS',150):
            self.start_slow_preparation(timeout_ms=4000)
            self.wait(lambda:bool(self.results) and not self.runner._session.has_process)
        self.assertEqual([x[0] for x in self.results],[-5])
        self.assert_no_native_open();self.assert_temp_clean()

    def test_temp_cleanup_failure_cannot_strand_completion_or_worker_ownership(self):
        self.helper_config(delay=.15)
        self.runner.run(self.settings,'hw version')
        self.wait(lambda:self.runner._session.preparation_process is not None)
        directory=self.runner._session.preparation_directory
        with patch.object(directory,'cleanup',side_effect=PermissionError('busy scanner')):
            self.wait(lambda:bool(self.results))
        self.assertEqual([x[0] for x in self.results],[0])
        self.assertFalse(self.runner.busy);self.assertTrue(self.runner.session_open)
        self.assertIsNone(self.runner._session.preparation_process)
        self.assertIsNone(self.runner._session.preparation_directory)
        directory.cleanup();self.assert_temp_clean()

    def test_shutdown_during_preparation_produces_one_result_and_no_late_native_client(self):
        self.start_slow_preparation();self.runner.shutdown();self.settle()
        self.assertEqual([x[0] for x in self.results],[-2])
        self.assertFalse(self.runner.busy);self.assertFalse(self.runner._session.has_process)
        self.assert_no_native_open();self.assert_temp_clean()

    def test_old_timeout_and_old_child_completion_cannot_affect_new_profile_after_stop(self):
        self.start_slow_preparation();old_process=self.runner._session.process;old_run=self.runner._active
        self.runner.cancel();self.wait(lambda:bool(self.results))
        self.settings.port='COM6';self.helper_config()
        self.runner.run(self.settings,'hf search')
        self.runner._session._preparation_expired(old_run,old_process)
        self.runner._session._ended(old_process,0,QProcess.ExitStatus.NormalExit)
        self.wait(lambda:len(self.results)==2)
        self.assertEqual([x[0] for x in self.results],[-2,0])
        self.assertEqual([x[0] for x in self.invocations],['COM5','COM6'])
        self.assertEqual([x[1] for x in self.rows('trace.jsonl')],['hw version','hf search'])
        self.assert_temp_clean()

    def test_native_open_retry_repeats_service_preparation_without_replaying_rf(self):
        self.configure(open_failures=1)
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS',20):
            self.assertEqual(self.run_command('hf search'),0)
        self.assertEqual(self.factory.call_count,2)
        self.assertEqual(len(self.rows('open-attempts.jsonl')),2)
        self.assertEqual([x[1] for x in self.rows('trace.jsonl')],['hw version','hf search'])
        self.assertEqual(len(self.results),1);self.assert_temp_clean()

    def test_legacy_capability_usb_and_offline_paths_do_not_prepare(self):
        self.configure(legacy=True);self.settings.require_session=True
        self.assertEqual(self.run_command('hf search'),-5);self.assertEqual(self.factory.call_count,0)
        self.configure();self.settings.require_session=False
        # Use a different executable fingerprint to invalidate cached legacy help.
        self.native.write_text(NATIVE_TRACE+FAKE+'\n#new fingerprint\n',encoding='utf8')
        for transport,channel in (('usb','USB-CDC'),('tcp','FPC UART over TCP'),('offline','FPC UART')):
            self.settings.transport=transport;self.settings.port='tcp:localhost:1234' if transport=='tcp' else 'COM5'
            self.configure(channel=channel)
            self.assertEqual(self.run_command(),0)
            self.runner.disconnect();self.wait(lambda:not self.runner._session.has_process)
        self.assertEqual(self.factory.call_count,0)

    def test_default_preparation_gate_is_only_windows_blueshark_without_test_wrappers(self):
        run=SimpleNamespace(bluetooth_prepared=False);session=self.runner._session
        session.transport='blueshark'
        with patch('pm3gui.persistent.sys.platform','win32'):
            self.assertFalse(self.default_gate(run))
            self.runner._argument_prefix=[]
            self.assertTrue(self.default_gate(run))
            run.bluetooth_prepared=True;self.assertFalse(self.default_gate(run));run.bluetooth_prepared=False
            for transport in ('usb','tcp','offline'):
                session.transport=transport;self.assertFalse(self.default_gate(run))
        session.transport='blueshark'
        with patch('pm3gui.persistent.sys.platform','linux'):self.assertFalse(self.default_gate(run))


if __name__=='__main__':unittest.main()
