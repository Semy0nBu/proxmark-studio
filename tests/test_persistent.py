"""Real framed child processes; no serial ports or RFID hardware."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from pm3gui.device import ClientRunner, DeviceSettings, parse_connection_status
from pm3gui.operations import Operation

FAKE = r'''
import json,os,pathlib,sys,time
root=pathlib.Path(__file__).parent
configpath=root/'config.json'
def config(): return json.loads(configpath.read_text())
version_sequence_count=0
args=sys.argv[1:]
if '--help' in args:
 with (root/'probes.jsonl').open('a') as f:f.write(json.dumps(os.getpid())+'\n')
 print('--incognito '+('--gui-pipe TOKEN' if not config().get('legacy') else '')+(' --gui-pipe-cancel' if config().get('soft_cancel') else '')+(' '+config().get('bulk_marker','--gui-pipe-bulk-cancel') if config().get('bulk_cancel') else ''),flush=True)
 sys.exit(0)
if '--gui-pipe' in args:
 attemptpath=root/'open-attempts.jsonl'
 with attemptpath.open('a') as f:f.write(json.dumps(os.getpid())+'\n')
 attempt=len(attemptpath.read_text().splitlines())
 errors=config().get('open_errors',[])
 if attempt <= config().get('open_failures',0) or attempt <= len(errors):
  print(errors[attempt-1] if attempt <= len(errors) else config().get('open_error','[!!] UART CreateFile failed for COM5 (Win32 error 1168)'),flush=True)
  sys.exit(-5)
def trace(cmd):
 with (root/'trace.jsonl').open('a') as f:f.write(json.dumps([os.getpid(),cmd,os.getcwd()])+'\n')
def execute(cmd):
 global version_sequence_count
 c=config()
 trace(cmd)
 if cmd.startswith(c.get('delay_command','NO_MATCH')):time.sleep(c.get('delay',0))
 if c.get('die_command') and cmd.startswith(c['die_command']):
  if c.get('die_output'):print(c['die_output'],flush=True)
  sys.exit(17)
 if cmd=='hw version':
  values=c.get('version_outputs')
  if values is not None:
   print(values[min(version_sequence_count,len(values)-1)],flush=True)
   version_sequence_count+=1
  else:print(c.get('version_output','  OS......... GUI-BlueShark-test'),flush=True)
 if cmd=='hf search': print('[+] UID: A1 B2 C3 D4\n[+] SAK: 08\n[+] Valid ISO 14443-A tag found',flush=True)
 if cmd.startswith('hw ping'):
  print('Ping timeout' if c.get('bad_ping') else 'Ping response received\ncontent ( ok )',flush=True)
 if c.get('bulk_wait') and cmd.startswith(c.get('bulk_command','hf search')):
  if c.get('bulk_auth2'):
   for _ in range(20): print('[#] AcquireEncryptedNonces: Auth2 error len=1',flush=True)
  print('BULK_WAITING_FOR_CONTROL',flush=True)
  control=sys.stdin.readline().strip()
  trace(control)
  if control != f'!CANCEL {token} {ident}':sys.exit(29)
  if c.get('bulk_ignore_cancel'):time.sleep(10)
  if c.get('cancel_wrong_end'):
   wrong='a'*32 if ident!='a'*32 else 'b'*32
   print(f'[PM3GUI {token} {wrong} END 0]\nCANCEL_ACK_PENDING',flush=True)
   time.sleep(.15)
  if c.get('cancel_tail'): print(c['cancel_tail'],flush=True)
  return c.get('cancel_rc',-10)
 if c.get('auth2') and cmd.startswith('hf mf hardnested'):
  for _ in range(20): print('[#] AcquireEncryptedNonces: Auth2 error len=1',flush=True)
  if c.get('soft_cancel') and not c.get('ignore_cancel'):
   control=sys.stdin.readline().strip()
   trace(control)
   if control != f'!CANCEL {token} {ident}':sys.exit(29)
   if c.get('late_key'): print('[+] Found valid key [A0A1A2A3A4A5]',flush=True)
   return c.get('cancel_rc',-5)
  time.sleep(1)
 if c.get('size') and cmd=='hf search':
  os.write(1,b'x'*c['size']+b'\n')
 return c.get('codes',{}).get(cmd.split(';')[0],0)
channel=config().get('channel','FPC UART')
print('[+] Communicating with PM3 over '+channel,flush=True)
if '--gui-pipe' not in args:
 sys.exit(execute(args[args.index('-c')+1]))
token=args[args.index('--gui-pipe')+1]
print('[PM3GUI '+token+' READY]',flush=True)
if config().get('die_after_ready'):
 print(config().get('die_output','[!!] UART CreateFile failed for COM5 (Win32 error 1168)'),flush=True)
 sys.exit(-5)
for raw in sys.stdin:
 ident,cwd,command=raw.strip().split(' ')
 command=bytes.fromhex(command).decode()
 os.chdir(bytes.fromhex(cwd).decode())
 print(f'[PM3GUI {token} {ident} BEGIN]',flush=True)
 if config().get('impostor'):
  print(f'[fpc|script] pm3 --> [PM3GUI {token} {ident} END 0]',flush=True)
  print(f'[PM3GUI {token} '+('a'*32)+' END 0]',flush=True)
 code=execute(command)
 marker=f'[PM3GUI {token} {ident} END {code}]\r\r\n'.encode()
 if config().get('split'):
  for i in range(0,len(marker),9):os.write(1,marker[i:i+9]);time.sleep(.002)
 else:os.write(1,marker)
 if config().get('idle_exit') and command=='hf search':time.sleep(.04);sys.exit(19)
'''


class PersistentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.script = self.root / 'fake.py'
        self.script.write_text(FAKE, encoding='utf-8')
        self.configure()
        self.runner = ClientRunner(argument_prefix=[str(self.script)])
        # Windows venv python.exe is a redirector that spawns another process;
        # use the actual interpreter so kill/EOF tests exercise one child.
        self.settings = DeviceSettings(getattr(sys, '_base_executable', sys.executable), 'COM5', str(self.root), 'blueshark', True)
        self.results, self.output, self.sessions = [], [], []
        self.runner.finished.connect(lambda code, message: self.results.append((code, message)))
        self.runner.output.connect(self.output.append)
        self.runner.session_changed.connect(lambda opened, message: self.sessions.append((opened, message)))

    def tearDown(self):
        if self.runner.busy:
            self.runner.cancel()
            self.wait(lambda: not self.runner.busy)
        self.runner.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def configure(self, **values):
        (self.root / 'config.json').write_text(json.dumps(values), encoding='utf-8')

    def wait(self, predicate, timeout=4000):
        if predicate():
            return
        loop = QEventLoop()
        poll, watchdog = QTimer(), QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        poll.start()
        watchdog.start(timeout)
        loop.exec()
        poll.stop()
        watchdog.stop()
        self.assertTrue(predicate(), 'Timed out waiting for framed child')

    def run_command(self, command='hw version', **kwargs):
        count = len(self.results)
        self.output.clear()
        self.runner.run(self.settings, command, **kwargs)
        self.wait(lambda: len(self.results) > count)
        return self.results[-1][0]

    def trace(self):
        path = self.root / 'trace.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def open_attempts(self):
        path = self.root / 'open-attempts.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def settle(milliseconds):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def test_offline_session_preserves_host_buffers_without_hardware_bootstrap_or_ping(self):
        self.settings.transport = 'offline';self.settings.port = '';self.settings.require_session = True
        for command in ('nfc encode','nfc decode'):
            count = len(self.results)
            self.runner.run_operation(self.settings,Operation('expert_tool',client_command=command))
            self.wait(lambda:len(self.results)>count)
            self.assertEqual(self.results[-1][0],0)
        self.assertEqual([row[1] for row in self.trace()],['nfc encode','nfc decode'])
        self.assertEqual(len({row[0] for row in self.trace()}),1)
        self.assertFalse(self.runner.session_open);self.assertIsNone(self.runner.connection_transport)
        self.runner._session._ping();self.assertEqual(len(self.trace()),2)
        self.assertFalse(any(opened for opened,_ in self.sessions))

    def test_required_session_cannot_fall_back_to_separate_processes(self):
        self.configure(legacy=True);self.settings.transport = 'offline';self.settings.port = '';self.settings.require_session = True
        self.runner.run_operation(self.settings,Operation('expert_tool',client_command='nfc encode'))
        self.wait(lambda:bool(self.results));self.assertEqual(self.results[-1][0],-5);self.assertFalse(self.trace())

    def test_same_process_and_port_survive_multiple_commands_and_cwd_changes(self):
        self.assertEqual(self.run_command(), 0)
        self.assertTrue(self.runner.session_open)
        second = self.root / 'новая папка'
        second.mkdir()
        self.settings.working_dir = str(second)
        self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual(len({row[0] for row in self.trace()}), 1)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hf search'])
        self.assertEqual(self.trace()[-1][2], str(second))
        self.assertIn('A1 B2 C3 D4', ''.join(self.output))
        self.assertEqual(self.runner.connection_transport, 'blueshark')
        self.runner.disconnect()
        self.wait(lambda: not self.runner._session.has_process)
        self.assertFalse(self.runner.session_open)

    def test_split_markers_and_echoes_cannot_finish_command_prematurely(self):
        self.configure(split=True, impostor=True, delay_command='hf search', delay=.12,
                       codes={'hf search': -8})
        self.assertEqual(self.run_command('hf search'), -8)
        self.assertTrue(self.runner.session_open)
        self.assertIn('A1 B2 C3 D4', ''.join(self.output))
        self.assertEqual(len(self.results), 1)

    def test_bootstrap_wrong_channel_prevents_rf_command(self):
        self.configure(channel='USB-CDC')
        self.assertEqual(self.run_command('hf search'), -5)
        self.assertFalse(self.runner.session_open)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'])

    def test_first_rf_command_bootstraps_and_subsequent_version_requires_new_os(self):
        self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual(self.run_command(), 0)
        output = ''.join(self.output)
        self.assertNotIn('Communicating with PM3', output)
        self.assertTrue(parse_connection_status(output, 'blueshark', verified_transport=self.runner.connection_transport)['connected'])
        self.assertFalse(parse_connection_status('client banner only', 'blueshark', verified_transport='blueshark')['connected'])

    def test_return_code_and_output_of_each_typed_sequence_part_are_preserved(self):
        operation = Operation('antenna_sweep', output_prefix=self.root / 'curve')
        self.configure(codes={'hw tune': -7})
        self.runner.run_operation(self.settings, operation)
        self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -7)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hw tune'])

    def test_heartbeat_requires_actual_content_success_even_when_rc_zero(self):
        self.settings.transport = 'usb';self.configure(channel='USB-CDC')
        self.run_command()
        self.configure(channel='USB-CDC',bad_ping=True)
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: not self.runner.session_open)
        self.wait(lambda: not self.runner._session.has_process)
        self.assertEqual(len(self.results), 1)
        self.assertTrue(any('контрольный' in message for opened, message in self.sessions if not opened))

    def test_idle_ping_keeps_client_and_defers_a_user_command_without_reopening(self):
        self.settings.transport = 'usb';self.configure(channel='USB-CDC')
        self.run_command()
        self.configure(channel='USB-CDC',delay_command='hw ping', delay=.1)
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: any(row[1].startswith('hw ping') for row in self.trace()))
        self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual(len({row[0] for row in self.trace()}), 1)
        self.assertTrue(self.runner.session_open)

    def test_blueshark_idle_check_uses_fresh_version_and_keeps_output_private(self):
        self.run_command();pid = self.trace()[0][0];self.output.clear()
        self.configure(bad_ping=True)
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: len(self.trace()) == 2)
        self.wait(lambda: self.runner._session.frame is None)
        self.assertTrue(self.runner.session_open)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hw version'])
        self.assertTrue(all(row[0] == pid for row in self.trace()))
        self.assertEqual(len(self.results), 1);self.assertEqual(self.output, [])

    def test_blueshark_idle_check_rejects_banner_timeout_wrong_channel_and_nonzero(self):
        cases = [('client banner only', 0, 2), ('[!] Version response timeout', 0, 1), ('', 0, 2),
                 ('[+] Communicating with PM3 over USB-CDC\n  OS......... GUI-test', 0, 1),
                 ('[+] Communicating with PM3 over USB-CDC', 0, 1),
                 ('OFFLINE mode\n  OS......... GUI-test', 0, 1), ('OFFLINE mode', 0, 1),
                 ('No response from Proxmark3', 0, 1), ('[!] Version response \x1b[31mtimeout\x1b[0m', 0, 1),
                 ('Client banner\rNo response from Proxmark3', 0, 1),
                 ('  OS......... GUI-test', -4, 1)]
        for output, code, probes in cases:
            with self.subTest(output=output, code=code):
                self.configure();self.assertEqual(self.run_command(), 0)
                before = len(self.trace())
                self.configure(version_output=output, codes={'hw version':code})
                self.runner._session.heartbeat.start(1)
                self.wait(lambda: not self.runner.session_open)
                self.wait(lambda: not self.runner._session.has_process)
                self.assertEqual(len(self.trace()) - before, probes)

    def test_blueshark_idle_version_defers_rf_command_until_fresh_reply(self):
        self.run_command();self.configure(delay_command='hw version', delay=.15)
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: len(self.trace()) == 2)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hw version'])
        self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hw version', 'hf search'])
        self.assertEqual(len({row[0] for row in self.trace()}), 1)

    def test_failed_blueshark_idle_reply_never_dispatches_pending_rf(self):
        self.run_command();self.configure(delay_command='hw version', delay=.15, version_output='Client banner only')
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: len(self.trace()) == 2)
        self.assertEqual(self.run_command('hf search'), -5)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'] * 3)
        self.assertFalse(self.runner.session_open)

    def test_blueshark_missing_os_rechecks_once_then_releases_one_pending_rf(self):
        self.run_command();self.output.clear()
        self.configure(delay_command='hw version', delay=.08,
                       version_outputs=['Client banner only', '  OS......... GUI-fresh'])
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: len(self.trace()) == 3)
        self.assertTrue(self.runner.session_open)
        self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'] * 3 + ['hf search'])
        self.assertEqual(len({row[0] for row in self.trace()}), 1)
        self.assertNotIn('Client banner only', ''.join(self.output))
        self.assertNotIn('GUI-fresh', ''.join(self.output))

    def test_blueshark_recheck_budget_resets_only_for_a_new_idle_cycle(self):
        self.run_command()
        self.configure(version_outputs=['Client banner only', '  OS......... GUI-fresh',
                                        'Client banner only', '  OS......... GUI-fresh'])
        for cycle in range(2):
            self.runner._session.heartbeat.start(1)
            self.wait(lambda: len(self.trace()) == 3 + cycle * 2)
            self.wait(lambda: self.runner._session.frame is None)
            self.assertTrue(self.runner.session_open)
        self.assertEqual(len({row[0] for row in self.trace()}), 1)

    def test_blueshark_recheck_does_not_extend_the_overall_heartbeat_deadline(self):
        self.run_command()
        self.configure(delay_command='hw version', delay=.2,
                       version_outputs=['Client banner only', '  OS......... GUI-too-late'])
        with patch('pm3gui.persistent.HEARTBEAT_TIMEOUT_MS', 320):
            self.runner._session.heartbeat.start(1)
            self.wait(lambda: not self.runner.session_open)
            self.wait(lambda: not self.runner._session.has_process)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'] * 3)
        self.assertTrue(any('не ответила' in message for opened, message in self.sessions if not opened))

    def test_cancelling_pending_rf_during_blueshark_recheck_sends_no_rf(self):
        self.run_command()
        self.configure(delay_command='hw version', delay=.15,
                       version_outputs=['Client banner only', '  OS......... GUI-fresh'])
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: len(self.trace()) == 3)
        count = len(self.results)
        self.runner.run(self.settings, 'hf search')
        self.runner.cancel();self.wait(lambda: len(self.results) > count)
        self.assertEqual(self.results[-1][0], -2)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'] * 3)
        self.wait(lambda: not self.runner._session.has_process)

    def test_tcp_heartbeat_retains_payload_ping_and_rejects_bad_content(self):
        self.settings.transport = 'tcp';self.settings.port = 'tcp:127.0.0.1:18888'
        self.configure(channel='FPC UART over TCP')
        self.assertEqual(self.run_command(), 0)
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: len(self.trace()) == 2)
        self.wait(lambda: self.runner._session.frame is None)
        self.assertTrue(self.runner.session_open)
        self.assertEqual(self.trace()[-1][1], 'hw ping --len 32')
        self.configure(channel='FPC UART over TCP',bad_ping=True)
        self.runner._session.heartbeat.start(1)
        self.wait(lambda: not self.runner.session_open)
        self.wait(lambda: not self.runner._session.has_process)

    def test_timeout_kills_session_and_next_manual_command_never_replays_rf(self):
        # Exercise an in-flight timeout rather than timing the cold interpreter.
        self.assertEqual(self.run_command(), 0)
        self.configure(delay_command='hf search', delay=2)
        self.assertEqual(self.run_command('hf search', timeout_ms=150), -3)
        self.assertFalse(self.runner.session_open)
        self.configure()
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(sum(row[1] == 'hf search' for row in self.trace()), 1)

    def test_mid_command_exit_is_not_success_even_after_positive_card_output(self):
        self.configure(die_command='hf search')
        self.assertEqual(self.run_command('hf search'), -5)
        self.assertFalse(self.runner.session_open)

    def test_auth2_guard_kills_session_and_does_not_queue_another_attack(self):
        self.configure(auth2=True)
        operation = Operation('mfc_hardnested_target', classic_size='4k', known_sector=0,
                              auth_key='FFFFFFFFFFFF', target_sector=36)
        self.runner.run_operation(self.settings, operation)
        self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -4)
        self.assertFalse(self.runner.session_open)
        self.assertEqual(sum(row[1].startswith('hf mf hardnested') for row in self.trace()), 1)

    def test_auth2_guard_cancels_only_operation_and_reuses_same_client_for_resync(self):
        self.configure(auth2=True, soft_cancel=True, late_key=True, cancel_rc=0)
        operation = Operation('mfc_hardnested_target', classic_size='4k', known_sector=0,
                              auth_key='FFFFFFFFFFFF', target_sector=36)
        self.runner.run_operation(self.settings, operation)
        self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -4)  # Even END0 / a late key is cancelled.
        self.assertTrue(self.runner.session_open)
        self.assertEqual(sum(row[1].startswith('!CANCEL ') for row in self.trace()), 1)
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(len({row[0] for row in self.trace()}), 1)
        self.assertEqual(sum(row[1].startswith('hf mf hardnested') for row in self.trace()), 1)

    def test_unacknowledged_soft_cancel_has_bounded_hard_stop_fallback(self):
        self.configure(auth2=True, soft_cancel=True, ignore_cancel=True)
        operation = Operation('mfc_hardnested_target', classic_size='4k', known_sector=0,
                              auth_key='FFFFFFFFFFFF', target_sector=36)
        with patch('pm3gui.persistent.CANCEL_TIMEOUT_MS', 100):
            self.runner.run_operation(self.settings, operation)
            self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -4)
        self.assertFalse(self.runner.session_open)
        self.assertIn('принудительно', ''.join(self.output))

    def test_bulk_cancel_requires_new_marker_and_matching_end_then_reuses_process(self):
        self.configure(bulk_cancel=True, bulk_wait=True, cancel_wrong_end=True, cancel_rc=0)
        self.runner.run(self.settings, 'hf search')
        self.wait(lambda: 'BULK_WAITING_FOR_CONTROL' in ''.join(self.output))
        run = self.runner._active
        self.assertTrue(run.bulk_cancel)
        self.assertFalse(run.preserve_tail)
        process = self.runner._session.process
        self.runner.cancel()
        self.wait(lambda: 'CANCEL_ACK_PENDING' in ''.join(self.output))
        self.assertTrue(self.runner.busy)  # Another frame's END is not our acknowledgement.
        self.assertIs(self.runner._session.process, process)
        with self.assertRaises(ValueError):
            self.runner.run(self.settings, 'lf search')
        self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -2)  # END0 cannot turn cancellation into success.
        self.assertTrue(self.runner.session_open)
        self.assertEqual(self.run_command(), 0)  # Fresh OS fence in the same client.
        self.assertEqual(len({row[0] for row in self.trace()}), 1)
        self.assertEqual(sum(row[1] == 'hf search' for row in self.trace()), 1)
        self.assertEqual(sum(row[1].startswith('!CANCEL ') for row in self.trace()), 1)
        self.assertFalse(any(row[1] == 'lf search' for row in self.trace()))

    def test_legacy_cancel_marker_alone_keeps_bulk_hard_stop(self):
        self.configure(soft_cancel=True, bulk_wait=True)
        self.runner.run(self.settings, 'hf search')
        self.wait(lambda: 'BULK_WAITING_FOR_CONTROL' in ''.join(self.output))
        first_pid = self.trace()[-1][0]
        self.runner.cancel()
        self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -2)
        self.assertFalse(self.runner.session_open)
        self.assertFalse(any(row[1].startswith('!CANCEL ') for row in self.trace()))
        self.configure()
        self.assertEqual(self.run_command(), 0)
        self.assertNotEqual(self.trace()[-1][0], first_pid)
        self.assertEqual(sum(row[1] == 'hf search' for row in self.trace()), 1)

    def test_bulk_marker_must_be_a_complete_help_option(self):
        self.configure(soft_cancel=True, bulk_cancel=True,
                       bulk_marker='--gui-pipe-bulk-cancel-unsupported', bulk_wait=True)
        self.runner.run(self.settings, 'hf search')
        self.wait(lambda: 'BULK_WAITING_FOR_CONTROL' in ''.join(self.output))
        self.assertFalse(self.runner._bulk_cancel_capabilities[self.runner._active.cache_key])
        self.runner.cancel()
        self.wait(lambda: bool(self.results))
        self.assertFalse(self.runner.session_open)
        self.assertFalse(any(row[1].startswith('!CANCEL ') for row in self.trace()))

    def test_bulk_marker_does_not_imply_framed_session_capability(self):
        self.configure(legacy=True, bulk_cancel=True)
        self.assertEqual(self.run_command(), 0)
        self.assertFalse(self.runner.session_open)
        self.assertEqual(self.open_attempts(), [])
        self.assertEqual(list(self.runner._pipe_capabilities.values()), [False])
        self.assertEqual(list(self.runner._bulk_cancel_capabilities.values()), [True])

    def test_bulk_auth2_text_never_enables_recovery_guard_or_tail(self):
        self.configure(bulk_cancel=True, bulk_wait=True, bulk_auth2=True,
                       bulk_command='lf read')
        count = len(self.results)
        self.runner.run_operation(self.settings, Operation('lf_capture_search_buffer'))
        self.wait(lambda: 'BULK_WAITING_FOR_CONTROL' in ''.join(self.output))
        self.assertTrue(self.runner._active.bulk_cancel)
        self.assertFalse(self.runner._active.preserve_tail)
        self.assertEqual(self.runner._active.auth2_errors, 0)
        self.assertEqual(len(self.results), count)
        self.runner.cancel()
        self.wait(lambda: len(self.results) > count)
        self.assertEqual(self.results[-1][0], -2)
        self.assertTrue(self.runner.session_open)

    def test_unacknowledged_bulk_cancel_has_bounded_hard_stop_without_replay(self):
        self.configure(bulk_cancel=True, bulk_wait=True, bulk_ignore_cancel=True)
        with patch('pm3gui.persistent.CANCEL_TIMEOUT_MS', 100):
            self.runner.run(self.settings, 'hf search')
            self.wait(lambda: 'BULK_WAITING_FOR_CONTROL' in ''.join(self.output))
            self.runner.cancel()
            self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -2)
        self.assertFalse(self.runner.session_open)
        self.assertEqual(sum(row[1] == 'hf search' for row in self.trace()), 1)
        self.assertEqual(sum(row[1].startswith('!CANCEL ') for row in self.trace()), 1)
        self.assertIn('принудительно', ''.join(self.output))

    def test_new_bulk_marker_does_not_soft_cancel_other_typed_commands(self):
        self.configure(soft_cancel=True, bulk_cancel=True, bulk_wait=True,
                       bulk_command='hf 14a reader')
        self.runner.run_operation(self.settings, Operation('hf14a_select'))
        self.wait(lambda: 'BULK_WAITING_FOR_CONTROL' in ''.join(self.output))
        self.assertFalse(self.runner._active.bulk_cancel)
        self.runner.cancel()
        self.wait(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -2)
        self.assertFalse(self.runner.session_open)
        self.assertFalse(any(row[1].startswith('!CANCEL ') for row in self.trace()))

    def test_bulk_stop_in_busy_callback_sends_no_user_or_cancel_frame(self):
        self.configure(bulk_cancel=True)
        self.assertEqual(self.run_command(), 0)
        self.runner.busy_changed.connect(lambda busy: self.runner.cancel() if busy else None)
        self.runner.run(self.settings, 'hf search')
        self.settle(80)
        self.assertEqual([code for code, _ in self.results], [0, -2])
        self.assertEqual([row[1] for row in self.trace()], ['hw version'])
        self.assertTrue(self.runner.session_open)

    def test_bulk_stop_before_begin_never_sends_cancel_control(self):
        self.configure(bulk_cancel=True)
        self.runner.session_changed.connect(lambda opened, _: self.runner.cancel() if opened else None)
        with patch.object(self.runner._session, '_send', wraps=self.runner._session._send) as send:
            self.runner.run(self.settings, 'hf search')
            self.wait(lambda: bool(self.results))
            self.wait(lambda: not self.runner._session.has_process)
        self.assertEqual([call.args[:2] for call in send.call_args_list], [('hw version', 'bootstrap')])
        self.assertEqual(self.results[-1][0], -2)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'])

    def test_bulk_capability_cache_changes_only_with_executable_fingerprint(self):
        self.configure(soft_cancel=True)
        self.assertEqual(self.run_command(), 0)
        old_key = self.runner._session.signature[:3]
        self.assertFalse(self.runner._bulk_cancel_capabilities[old_key])
        self.runner.disconnect()
        self.wait(lambda: not self.runner._session.has_process)
        self.configure(soft_cancel=True, bulk_cancel=True)
        self.assertEqual(self.run_command(), 0)
        self.assertFalse(self.runner._bulk_cancel_capabilities[old_key])
        self.assertEqual(len((self.root / 'probes.jsonl').read_text().splitlines()), 1)
        self.runner.disconnect()
        self.wait(lambda: not self.runner._session.has_process)
        original_stat = Path.stat
        executable = Path(self.settings.client_path)

        class ChangedFingerprint:
            def __init__(self, value):
                self.value = value
                self.st_mtime_ns = value.st_mtime_ns + 1
                self.st_size = value.st_size + 1

            def __getattr__(self, name):
                return getattr(self.value, name)

        def changed_stat(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            return ChangedFingerprint(value) if path == executable else value

        with patch('pm3gui.persistent.Path.stat', changed_stat):
            self.assertEqual(self.run_command(), 0)
        new_key = self.runner._session.signature[:3]
        self.assertNotEqual(old_key, new_key)
        self.assertTrue(self.runner._bulk_cancel_capabilities[new_key])
        self.assertFalse(self.runner._bulk_cancel_capabilities[old_key])
        self.assertEqual(len((self.root / 'probes.jsonl').read_text().splitlines()), 2)

    def test_large_output_is_bounded_without_losing_end_frame(self):
        self.configure(size=5 * 1024 * 1024)
        self.assertEqual(self.run_command('hf search'), 0)
        self.assertLess(len(''.join(self.output)), 4 * 1024 * 1024 + 4096)
        self.assertTrue(self.runner.session_open)

    def test_idle_client_exit_updates_connection_without_an_extra_command_result(self):
        self.configure(idle_exit=True)
        self.assertEqual(self.run_command('hf search'), 0)
        self.wait(lambda: not self.runner.session_open)
        self.assertEqual(len(self.results), 1)

    def test_profile_change_closes_old_port_before_starting_new_client(self):
        self.run_command()
        first = self.trace()[0][0]
        self.settings.port = 'COM9'
        self.assertEqual(self.run_command(), 0)
        self.assertNotEqual(self.trace()[-1][0], first)

    def test_legacy_client_falls_back_without_claiming_a_persistent_session(self):
        self.configure(legacy=True)
        self.assertEqual(self.run_command(), 0)
        self.assertFalse(self.runner.session_open)
        self.assertTrue(any('не поддерживает' in message for _, message in self.sessions))

    def test_invalid_wire_field_is_rejected_before_opening_any_client(self):
        self.settings.working_dir = str(self.root / ('x' * 8200))
        with self.assertRaises((ValueError, OSError)):
            self.runner.run(self.settings, 'hf search')
        self.assertFalse(self.runner.busy)
        self.assertEqual(self.trace(), [])

    def test_immediate_busy_listener_cancel_does_not_spawn_a_client(self):
        self.runner.busy_changed.connect(lambda busy: self.runner.cancel() if busy else None)
        self.runner.run(self.settings, 'hf search')
        self.assertFalse(self.runner.busy)
        self.assertFalse(self.runner._session.has_process)
        self.assertEqual([code for code, _ in self.results], [-2])
        self.assertEqual(self.trace(), [])

    def test_immediate_busy_listener_cancel_does_not_send_rf_on_open_session(self):
        self.assertEqual(self.run_command(), 0)
        self.runner.busy_changed.connect(lambda busy: self.runner.cancel() if busy else None)
        self.runner.run(self.settings, 'hf search')
        # Let the real child receive any incorrectly queued frame.
        loop = QEventLoop()
        QTimer.singleShot(100, loop.quit)
        loop.exec()
        self.assertFalse(self.runner.busy)
        self.assertTrue(self.runner.session_open)
        self.assertEqual([code for code, _ in self.results], [0, -2])
        self.assertEqual([row[1] for row in self.trace()], ['hw version'])

    def test_cancel_from_connected_callback_cannot_report_bootstrap_success(self):
        self.runner.session_changed.connect(lambda connected, _: self.runner.cancel() if connected else None)
        self.runner.run(self.settings, 'hw version')
        self.wait(lambda: bool(self.results))
        self.assertEqual([code for code, _ in self.results], [-2])
        self.wait(lambda: not self.runner._session.has_process)
        self.assertFalse(self.runner.session_open)

    def test_shutdown_waits_for_active_session_and_blocks_reentrant_restart(self):
        self.assertEqual(self.run_command(), 0)
        self.configure(delay_command='hf search', delay=10)
        self.runner.run(self.settings, 'hf search')
        self.wait(lambda: any(row[1] == 'hf search' for row in self.trace()))
        child_exited, refused = [], []
        self.runner._active.process.finished.connect(lambda *_: child_exited.append(True))

        def restart_on_finish(*_):
            try:
                self.runner.run(self.settings, 'lf search')
            except ValueError:
                refused.append(True)

        self.runner.finished.connect(restart_on_finish)
        self.runner.shutdown()
        self.runner.finished.disconnect(restart_on_finish)
        self.assertEqual(child_exited, [True])
        self.assertEqual(refused, [True])
        self.assertFalse(self.runner.busy)
        self.assertFalse(self.runner.session_open)
        self.assertFalse(self.runner._session.has_process)
        self.assertFalse(self.runner._session.close_timer.isActive())
        self.assertFalse(self.runner._session.heartbeat.isActive())
        self.assertEqual([code for code, _ in self.results], [0, -2])
        self.assertNotIn('lf search', [row[1] for row in self.trace()])
        self.configure()
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(len(self.results), 3)

    def test_blueshark_1168_before_ready_retries_open_then_sends_rf_only_once(self):
        self.configure(open_failures=1)
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 20):
            self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual(len(self.open_attempts()), 2)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hf search'])
        self.assertEqual(len({row[0] for row in self.trace()}), 1)
        self.assertEqual(len(self.results), 1)
        self.assertTrue(self.runner.session_open)

    def test_blueshark_121_before_ready_retries_open_then_sends_rf_only_once(self):
        self.configure(open_failures=1, open_error='[!!] UART CreateFile failed for COM5 (Win32 error 121)')
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 20):
            self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual(len(self.open_attempts()), 2)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hf search'])
        self.assertEqual(len(self.results), 1)
        self.assertIn('Win32 121', ''.join(self.output))
        self.assertTrue(self.runner.session_open)

    def test_blueshark_mixed_1168_and_121_share_one_bounded_retry_budget(self):
        self.configure(open_errors=['[!!] UART CreateFile failed for COM5 (Win32 error 1168)',
                                    '[!!] UART CreateFile failed for COM5 (Win32 error 121)'])
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 20):
            self.assertEqual(self.run_command('hf search'), 0)
        self.assertEqual(len(self.open_attempts()), 3)
        self.assertEqual([row[1] for row in self.trace()], ['hw version', 'hf search'])
        self.assertEqual(len(self.results), 1)
        self.assertTrue(self.runner.session_open)

    def test_blueshark_1168_open_retries_are_bounded_before_any_frame(self):
        self.configure(open_failures=99)
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 20):
            self.assertEqual(self.run_command('hf search'), -5)
        self.assertEqual(len(self.open_attempts()), 3)
        self.assertEqual(self.trace(), [])
        self.assertFalse(self.runner.session_open)
        self.assertFalse(self.runner._session.open_retry_timer.isActive())
        self.assertEqual(len(self.results), 1)

    def test_open_retry_does_not_apply_to_usb_or_other_native_errors(self):
        cases = [('usb', '[!!] UART CreateFile failed for COM5 (Win32 error 1168)'),
                 ('usb', '[!!] UART CreateFile failed for COM5 (Win32 error 121)'),
                 ('blueshark', '[!!] UART CreateFile failed for COM5 (Win32 error 5)'),
                 ('blueshark', '[!!] UART CreateFile failed for COM5 (Win32 error 2)'),
                 ('blueshark', '[!] ERROR: invalid serial port COM5'),
                 ('blueshark', 'Unrelated Win32 error 1168'),
                 ('blueshark', '[!!] UART CreateFile failed for COM5 (Win32 error 5)\nUnrelated Win32 error 1168')]
        for transport, output in cases:
            with self.subTest(transport=transport, output=output):
                self.settings.transport = transport
                self.configure(open_failures=99, open_error=output)
                before = len(self.open_attempts())
                with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 20):
                    self.assertEqual(self.run_command('hf search'), -5)
                self.assertEqual(len(self.open_attempts()) - before, 1)
                self.assertEqual(self.trace(), [])

    def test_blueshark_retry_still_requires_fresh_correct_device_channel(self):
        self.configure(open_failures=1, channel='USB-CDC')
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 20):
            self.assertEqual(self.run_command('hf search'), -5)
        self.assertEqual(len(self.open_attempts()), 2)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'])
        self.assertFalse(self.runner.session_open)

    def test_blueshark_open_errors_after_ready_or_a_started_frame_never_replay(self):
        cases = [dict(die_after_ready=True),
                 dict(die_command='hw version'), dict(die_command='hf search')]
        for error in (1168, 121):
            for config in cases:
                with self.subTest(error=error, config=config):
                    self.configure(**config, die_output=f'[!!] UART CreateFile failed for COM5 (Win32 error {error})')
                    before = len(self.open_attempts())
                    rf_before = sum(row[1] == 'hf search' for row in self.trace())
                    with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 20):
                        self.assertEqual(self.run_command('hf search'), -5)
                    self.assertEqual(len(self.open_attempts()) - before, 1)
                    self.assertEqual(sum(row[1] == 'hf search' for row in self.trace()) - rf_before,
                                     1 if config.get('die_command') == 'hf search' else 0)
                    self.assertFalse(self.runner.session_open)

    def test_cancel_during_open_backoff_prevents_delayed_open_and_old_rf(self):
        self.configure(open_failures=99)
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 150):
            self.runner.run(self.settings, 'hf search')
            self.wait(lambda: self.runner._session.phase == 'retry_wait')
            self.runner.cancel()
            self.assertEqual(self.results[-1][0], -2)
            self.assertFalse(self.runner._session.open_retry_timer.isActive())
            self.configure()
            self.assertEqual(self.run_command(), 0)
            self.settle(200)
        self.assertEqual(len(self.open_attempts()), 2)
        self.assertEqual([row[1] for row in self.trace()], ['hw version'])
        self.assertEqual([code for code, _ in self.results], [-2, 0])

    def test_shutdown_during_open_backoff_prevents_delayed_process(self):
        self.configure(open_failures=99)
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 150):
            self.runner.run(self.settings, 'hf search')
            self.wait(lambda: self.runner._session.phase == 'retry_wait')
            self.runner.shutdown()
            self.settle(200)
        self.assertEqual([code for code, _ in self.results], [-2])
        self.assertEqual(len(self.open_attempts()), 1)
        self.assertFalse(self.runner.busy)
        self.assertFalse(self.runner._session.has_process)
        self.assertFalse(self.runner._session.open_retry_timer.isActive())
        self.assertEqual(self.trace(), [])

    def test_open_backoff_does_not_extend_original_operation_timeout(self):
        self.configure(open_failures=99)
        with patch('pm3gui.persistent.BLUESHARK_OPEN_RETRY_DELAY_MS', 1000):
            self.runner.run(self.settings, 'hf search', timeout_ms=500)
            self.wait(lambda: self.runner._session.phase == 'retry_wait')
            self.wait(lambda: bool(self.results))
            self.settle(650)
        self.assertEqual([code for code, _ in self.results], [-3])
        self.assertEqual(len(self.open_attempts()), 1)
        self.assertFalse(self.runner._session.open_retry_timer.isActive())
        self.assertEqual(self.trace(), [])


if __name__ == '__main__':
    unittest.main()
