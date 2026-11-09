"""SPIFFS completion fencing, including a real framed child with late output."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import tempfile
import unittest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from pm3gui.device import ClientRunner, DeviceSettings, build_operation_invocation
from pm3gui.operations import Operation
from pm3gui.studio_flows import (Request, one_command, spiffs_listing, spiffs_info, spiffs_tree,
                                upload_spiffs, download_spiffs, change_spiffs)

HEADER = '[=] --- Flash Memory tree (SPIFFS) -----------------\n'
ROW = '[#] [0001] 3 B |-- original.bin\n'
OS = '[ ARM ]\n OS......... Iceman/test\n'
INFO_HEADER = '[=] --- Flash Memory info (SPIFFS) -----------------\n'
INFO_BODY = ('[#] Logical block size... 4096 bytes\n[#] Logical page size.... 256 bytes\n'
             '[#] Max open files....... 4 file descriptors\n[#] Max path length...... 32 chars\n'
             '[#] Filesystem size used available use% mounted\n'
             '[#] spiffs 1024000 B 24000 B 1000000 B 2% /\n')


class SpiffsFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.root = Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)

    def result(self, output, code=0):
        flow = spiffs_listing(self.root);request = next(flow)
        self.assertEqual(request.line, 'mem spiffs tree; hw version')
        with self.assertRaises(StopIteration) as end:flow.send((output, code))
        return end.exception.value

    def test_fixed_typed_operation_rejects_all_user_command_chaining(self):
        self.assertEqual(Operation('spiffs_tree').render(), 'mem spiffs tree; hw version')
        for fields in ({'client_args': '; mem spiffs wipe'}, {'client_command': 'mem spiffs wipe'},
                       {'auth_key':'FFFFFFFFFFFF'}, {'output_prefix':self.root/'x; wipe'},
                       {'block':1}, {'classic_size':'4k'}, {'hf_command':'hf search'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):Operation('spiffs_tree', **fields).render()
        with self.assertRaises(ValueError):Request(Operation('spiffs_tree'), '--anything').operation()
        with self.assertRaises(ValueError):one_command('mem spiffs tree', '; mem spiffs wipe')
        with self.assertRaises(ValueError):Operation('expert_tool', client_command='mem spiffs tree', client_args='; mem spiffs wipe').render()
        settings = DeviceSettings(sys.executable, 'COM123', str(self.root))
        self.assertEqual(build_operation_invocation(settings, Operation('spiffs_tree'))[1][-2:], ['-c','mem spiffs tree; hw version'])

    def test_ui_one_command_shortcut_uses_the_typed_fence_with_valid_timeout(self):
        request = next(one_command('mem spiffs tree')(self.root))
        self.assertEqual(request.operation().kind, 'spiffs_tree');self.assertFalse(request.modifying)
        self.assertLessEqual(request.timeout, request.operation().recommended_timeout_ms)

    def test_delayed_listing_before_fresh_os_and_prior_bootstrap_are_accepted(self):
        result = self.result(OS+HEADER+'[ Client ]\nClient build\n'+ROW+OS)
        self.assertEqual(result['entries'], {'original.bin':3})
        self.assertIn('fresh-device-os', result['verification'])

    def test_header_alone_is_not_empty_even_when_os_fence_succeeds(self):
        for output in (HEADER, HEADER+OS, OS+HEADER, HEADER+'[ Client ]\nOS: Windows\n'):
            with self.subTest(output=output), self.assertRaises(ValueError):self.result(output)
        with self.assertRaises(ValueError):spiffs_tree(HEADER)
        self.assertEqual(self.result(HEADER+'[#] <empty>\n'+OS)['entries'], {})

    def test_no_listing_after_the_fence_is_mistaken_for_completed_tree(self):
        with self.assertRaises(ValueError):self.result(HEADER+OS+ROW)

    def test_stale_rows_or_empty_markers_before_current_heading_are_ignored(self):
        with self.assertRaises(ValueError):self.result('[#] <empty>\n'+HEADER+OS)
        self.assertEqual(self.result(ROW+HEADER+'[#] <empty>\n'+OS)['entries'], {})

    def test_timeout_transport_and_offline_outputs_fail_closed(self):
        for output, code in ((HEADER+ROW+OS,-3), (HEADER+ROW+OS,-5),
                            (HEADER+'[=] OFFLINE mode\n'+ROW+OS,0),
                            (HEADER+'[!] No response from Proxmark3\n'+ROW+OS,0),
                            (HEADER+ROW+'[ Client ]\nOS: Windows\n',0)):
            with self.subTest(code=code, output=output), self.assertRaises(ValueError):self.result(output, code)

    def test_unfenced_listing_never_allows_file_changes_or_even_download(self):
        factories = [upload_spiffs('new.bin', b'test', True), upload_spiffs('original.bin', b'test'),
                     download_spiffs('original.bin'), change_spiffs('remove','original.bin'),
                     change_spiffs('copy','original.bin','copy.bin'), change_spiffs('rename','original.bin','renamed.bin')]
        for factory in factories:
            with self.subTest(factory=factory):
                flow=factory(self.root);first=next(flow)
                self.assertEqual(first.operation().kind,'spiffs_tree');self.assertFalse(first.modifying)
                with self.assertRaises(ValueError):flow.send((HEADER+ROW,0))
                self.assertFalse((self.root/'download.bin').exists())

    def test_download_requires_fenced_presence_exact_size_and_a_fresh_file(self):
        flow=download_spiffs('missing.bin')(self.root);next(flow)
        with self.assertRaisesRegex(ValueError,'not listed'):flow.send((HEADER+ROW+OS,0))
        flow=download_spiffs('original.bin')(self.root);next(flow)
        request=flow.send((HEADER+ROW+OS,0));self.assertTrue(request.line.startswith('mem spiffs dump'))
        (self.root/'download.bin').write_bytes(b'wrong length')
        with self.assertRaisesRegex(ValueError,'size'):flow.send(('Saved',0))
        flow=download_spiffs('original.bin')(self.root);next(flow)
        with self.assertRaisesRegex(ValueError,'fresh directory'):flow.send((HEADER+ROW+OS,0))


class SpiffsInfoFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)

    def result(self, output, code=0):
        flow=spiffs_info(self.root);request=next(flow)
        self.assertEqual(request.line,'mem spiffs info; hw version');self.assertFalse(request.modifying)
        with self.assertRaises(StopIteration) as end:flow.send((output,code))
        return end.exception.value

    def test_real_geometry_and_usage_require_a_fresh_post_info_os(self):
        result=self.result(OS+INFO_HEADER+INFO_BODY+OS)
        self.assertEqual(result['info'],{'block_size':4096,'page_size':256,'max_open_files':4,'max_path_length':32,
            'total_bytes':1024000,'used_bytes':24000,'available_bytes':1000000,'used_percent':2})

    def test_info_is_parameterless_while_mem_info_and_ack_mutations_are_unchanged(self):
        request=next(one_command('mem spiffs info')(self.root))
        self.assertEqual(request.operation().kind,'spiffs_info');self.assertLessEqual(request.timeout,45000)
        for command in ('mem info','mem spiffs remove','mem spiffs copy','mem spiffs rename'):
            self.assertEqual(next(one_command(command)(self.root)).line,command)
        for fields in ({'client_args':'; mem spiffs wipe'},{'auth_key':'FFFFFFFFFFFF'},{'output_prefix':self.root/'x'},{'block':4}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):Operation('spiffs_info',**fields).render()
        with self.assertRaises(ValueError):one_command('mem spiffs info','; mem spiffs wipe')

    def test_headers_partial_stats_banners_and_old_or_late_stats_are_not_completion(self):
        for output in (INFO_HEADER+OS,INFO_HEADER+'[#] <empty>\n'+OS,
                       INFO_HEADER+INFO_BODY+'[ Client ]\nOS: Windows\n',
                       INFO_HEADER+INFO_BODY.replace('Max open files....... 4 file descriptors\n','')+OS,
                       INFO_BODY+INFO_HEADER+OS,INFO_HEADER+OS+INFO_BODY):
            with self.subTest(output=output),self.assertRaises(ValueError):self.result(output)

    def test_timeout_and_inconsistent_counters_are_not_successful_info(self):
        with self.assertRaises(ValueError):self.result(INFO_HEADER+INFO_BODY+OS,-3)
        with self.assertRaises(ValueError):self.result(INFO_HEADER+INFO_BODY.replace('1000000 B','999999 B')+OS)


FAKE = r'''
import json,os,pathlib,sys,time
root=pathlib.Path(__file__).parent
args=sys.argv[1:]
if '--help' in args:
 print('--incognito --gui-pipe TOKEN',flush=True);sys.exit(0)
print('[+] Communicating with PM3 over USB-CDC',flush=True)
token=args[args.index('--gui-pipe')+1]
print('[PM3GUI '+token+' READY]',flush=True)
pending=''
for raw in sys.stdin:
 ident,cwd,wire=raw.strip().split(' ')
 command=bytes.fromhex(wire).decode()
 with (root/'trace.jsonl').open('a') as f:f.write(json.dumps([os.getpid(),command])+'\n')
 print(f'[PM3GUI {token} {ident} BEGIN]',flush=True)
 if command=='mem spiffs tree':
  print('[=] --- Flash Memory tree (SPIFFS) -----------------',flush=True);pending='tree'
 elif command=='mem spiffs info':
  print('[=] --- Flash Memory info (SPIFFS) -----------------',flush=True);pending='info'
 elif command=='hw version':
  if pending:
   time.sleep(.08)
   if pending=='tree':print('[#] [0001] 3 B |-- late.bin',flush=True)
   else:print('[#] Logical block size... 4096 bytes\n[#] Logical page size.... 256 bytes\n[#] Max open files....... 4 file descriptors\n[#] Max path length...... 32 chars\n[#] spiffs 1024000 B 24000 B 1000000 B 2% /',flush=True)
  if pending and (root/'bad-fence').exists():print('[ Client ]\nOS: Windows',flush=True)
  else:print('[ ARM ]\n OS......... Iceman/fake-device',flush=True)
  pending=''
 print(f'[PM3GUI {token} {ident} END 0]',flush=True)
'''


class FramedSpiffsFenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        script=self.root/'fake.py';script.write_text(FAKE,encoding='utf-8')
        self.runner=ClientRunner(argument_prefix=[str(script)]);self.output=[];self.results=[]
        self.runner.output.connect(self.output.append);self.runner.finished.connect(lambda *args:self.results.append(args))
        self.settings=DeviceSettings(getattr(sys,'_base_executable',sys.executable),'COM123',str(self.root),'usb',True)

    def tearDown(self):
        self.runner.shutdown();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents();self.tmp.cleanup()

    def run_fence(self,kind='spiffs_tree'):
        self.runner.run_operation(self.settings,Operation(kind))
        loop=QEventLoop();timer=QTimer();timer.setSingleShot(True);timer.timeout.connect(loop.quit)
        self.runner.finished.connect(loop.quit);timer.start(4000);loop.exec();timer.stop()
        self.assertEqual(len(self.results),1)
        flow=(spiffs_listing if kind=='spiffs_tree' else spiffs_info)(self.root);next(flow)
        return flow,''.join(self.output),self.results[0][0]

    def test_one_run_captures_post_tree_end_debug_rows_before_version_fence(self):
        flow,output,code=self.run_fence()
        with self.assertRaises(StopIteration) as end:flow.send((output,code))
        self.assertEqual(end.exception.value['entries'],{'late.bin':3})
        trace=[json.loads(s) for s in (self.root/'trace.jsonl').read_text().splitlines()]
        self.assertEqual([r[1] for r in trace],['hw version','mem spiffs tree','hw version'])
        self.assertEqual(len({r[0] for r in trace}),1)
        self.assertLess(output.index('Flash Memory tree'),output.index('late.bin'))
        self.assertLess(output.index('late.bin'),output.rindex('OS.........'))

    def test_bootstrap_os_does_not_replace_missing_fresh_post_tree_os(self):
        (self.root/'bad-fence').touch();flow,output,code=self.run_fence()
        self.assertIn('OS.........',output)
        with self.assertRaisesRegex(ValueError,'fresh device OS'):flow.send((output,code))

    def test_real_framed_info_retains_late_stats_in_same_run(self):
        flow,output,code=self.run_fence('spiffs_info')
        with self.assertRaises(StopIteration) as end:flow.send((output,code))
        self.assertEqual(end.exception.value['info']['available_bytes'],1000000)

    def test_info_cannot_reuse_bootstrap_os_when_final_frame_only_has_client_banner(self):
        (self.root/'bad-fence').touch();flow,output,code=self.run_fence('spiffs_info')
        with self.assertRaisesRegex(ValueError,'fresh device OS'):flow.send((output,code))


if __name__=='__main__':unittest.main()
