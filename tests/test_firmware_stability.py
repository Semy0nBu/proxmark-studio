"""Large flash logs and re-enumeration are tested without touching hardware."""
import io
import json
from pathlib import Path
import tempfile
import sys
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_firmware as fixtures
from pm3gui.firmware_output import FlashTranscript, flash_diagnostics
from pm3gui.firmware import _SystemFlashIO, FirmwareError, execute_flash

TRANSITION='[+] Entering bootloader...\n[+] Trigger restart...\n'
WAIT='[+] Waiting for Proxmark3 to appear on COM3\n'
FOUND='[|]  49 found\n'
OPEN='[!!] UART CreateFile failed for COM3 (Win32 error 2)\n[!] ERROR: invalid serial port COM3\n'
WRITE='[+] Writing segments for file: selected-fullimage.elf\n'
RECONNECTED=TRANSITION+WAIT+OPEN+FOUND


class TranscriptTests(unittest.TestCase):
    def test_only_bootloader_transition_open_errors_are_transient(self):
        result=flash_diagnostics(RECONNECTED+WRITE+'[+] All done\n')
        self.assertTrue(result.all_done);self.assertFalse(result.fatal_lines);self.assertEqual(result.reconnect_lines,2)
        for output in (OPEN+WRITE+'All done',TRANSITION+WAIT+FOUND+WRITE+OPEN+'All done',
                       TRANSITION+'[!] Flash program error\nAll done',
                       TRANSITION+WAIT+OPEN.replace('error 2','error 5')+FOUND+WRITE+'All done'):
            with self.subTest(output=output):self.assertTrue(flash_diagnostics(output).fatal_lines)

    def test_fatal_middle_diagnostic_survives_truncation_and_terminal_controls(self):
        capture=FlashTranscript(1024)
        for chunk in [b'\x1b[32mWriting segments for file: test\x1b[0m\r', b'x'*5000,
                      b'\b\b\n[!] Error writing block 2\n', b'x'*5000,b'\r[+] All done\n']:
            capture.feed(chunk)
        result=capture.finish()
        self.assertTrue(capture.truncated);self.assertTrue(result.all_done)
        self.assertTrue(any('Error writing' in line for line in result.fatal_lines))
        self.assertLess(len(capture.retained()),1200)

    def test_partial_utf8_and_split_diagnostics_are_parsed(self):
        capture=FlashTranscript()
        for byte in (RECONNECTED+WRITE+'[+] All done').encode():capture.feed(bytes([byte]))
        result=capture.finish();self.assertTrue(result.all_done);self.assertFalse(result.fatal_lines)

    def test_countdown_prefixes_and_device_paths_in_confirmed_wait(self):
        messages=''.join(f'[{spinner}]  {60-i}[!!] UART CreateFile failed for \\\\.\\COM3 (Win32 error 2)\r'
                         for i,spinner in enumerate('/-\\|'*3))
        output=TRANSITION+WAIT+messages+FOUND+WRITE+'[+] All done\n'
        result=flash_diagnostics(output)
        self.assertFalse(result.fatal_lines);self.assertTrue(result.all_done)
        self.assertEqual(result.reconnect_lines,12)

    def test_countdown_and_phase_markers_can_arrive_one_byte_at_a_time(self):
        raw=(TRANSITION+WAIT+'\x1b[33m[/]  60[!!] UART CreateFile failed for \\\\.\\COM3 (Win32 error 2)\x1b[0m\r'+FOUND+WRITE+'All done').encode()
        for width in (1,3,7,31):
            with self.subTest(width=width):
                capture=FlashTranscript()
                for offset in range(0,len(raw),width):capture.feed(raw[offset:offset+width])
                result=capture.finish();self.assertFalse(result.fatal_lines)
                self.assertEqual(result.reconnect_lines,1);self.assertTrue(result.all_done)

    def test_missing_wait_missing_found_and_initial_wait_errors_remain_fatal(self):
        outputs=(TRANSITION+OPEN+FOUND+WRITE+'All done',
                 WAIT+OPEN+FOUND+WRITE+'All done',
                 TRANSITION+WAIT+OPEN,
                 TRANSITION+WAIT+OPEN+'All done',
                 TRANSITION+WAIT+OPEN+WRITE+'All done')
        for output in outputs:
            with self.subTest(output=output):
                result=flash_diagnostics(output)
                self.assertTrue(result.fatal_lines);self.assertEqual(result.reconnect_lines,0)

    def test_other_errors_wrong_ports_and_unpaired_invalid_port_remain_fatal(self):
        failures=[OPEN.replace('error 2',f'error {code}') for code in (5,31,87,121,1168)]
        failures += [OPEN.replace('COM3','COM9'), '[!] ERROR: invalid serial port COM3\n',
                     '[!] UART error while setting com state\n',
                     '[!!] UART GetCommState failed (Win32 error 2)\n',
                     '[!] Timeout waiting for bootloader\n',
                     '[unexpected] 60[!!] UART CreateFile failed for COM3 (Win32 error 2)\n']
        for failure in failures:
            with self.subTest(failure=failure):
                result=flash_diagnostics(TRANSITION+WAIT+failure+FOUND+WRITE+'All done')
                self.assertTrue(result.fatal_lines)

    def test_detached_device_error_is_transient_only_until_confirmed_found(self):
        output=TRANSITION+WAIT+OPEN.replace('error 2','error 1167')+FOUND+WRITE+'All done'
        result=flash_diagnostics(output)
        self.assertFalse(result.fatal_lines);self.assertEqual(result.reconnect_lines,2)

    def test_errors_after_found_flashing_writing_or_done_cannot_be_hidden(self):
        countdown='[/]  60[!!] UART CreateFile failed for COM3 (Win32 error 2)\n'
        for later in (FOUND,FOUND+'[+] Flashing...\n',FOUND+WRITE,FOUND+WRITE+'All done\n'):
            with self.subTest(later=later):
                result=flash_diagnostics(TRANSITION+WAIT+later+countdown+'All done\n')
                self.assertTrue(result.fatal_lines)

    def test_transition_allowance_can_be_disabled_for_verification(self):
        capture=FlashTranscript(allow_transition=False);capture.feed((RECONNECTED+WRITE+'All done').encode())
        result=capture.finish();self.assertTrue(result.fatal_lines);self.assertEqual(result.reconnect_lines,0)

    def test_unresolved_retry_evidence_is_bounded_and_never_becomes_success(self):
        capture=FlashTranscript(1024)
        capture.feed((TRANSITION+WAIT+OPEN*5000+'All done').encode())
        result=capture.finish();self.assertTrue(result.fatal_lines)
        self.assertLessEqual(len(result.fatal_lines),32);self.assertTrue(capture.truncated)
        self.assertLess(len(capture.retained()),1200);self.assertEqual(result.reconnect_lines,0)


class FirmwareStabilityTests(unittest.TestCase):
    setUp=fixtures.FirmwareTests.setUp
    tearDown=fixtures.FirmwareTests.tearDown
    write_manifest=fixtures.FirmwareTests.write_manifest
    prepared=fixtures.FirmwareTests.prepared

    def test_expected_reenumeration_diagnostics_still_require_full_readback(self):
        prepared=self.prepared();fake=fixtures.FakeIO(prepared)
        fake.flash_output=RECONNECTED+WRITE+'All done'
        result=execute_flash(prepared,io=fake)
        self.assertTrue(result['verified']);self.assertEqual(result['bootloader_reconnect_messages'],2)
        self.assertEqual(fake.flash_calls,1)
        self.assertEqual(fake.internal,[])

    def test_transient_messages_do_not_hide_a_readback_mismatch(self):
        prepared=self.prepared();fake=fixtures.FakeIO(prepared)
        fake.flash_output=RECONNECTED+WRITE+'All done';fake.internal[-1]=bytes(fixtures.INTERNAL_SIZE)
        with self.assertRaises(FirmwareError):execute_flash(prepared,io=fake)
        self.assertEqual(fake.flash_calls,1)

    def test_countdown_retries_still_require_readback_and_late_errors_are_fatal(self):
        countdown='[/]  60[!!] UART CreateFile failed for \\\\.\\COM3 (Win32 error 2)\n'
        prepared=self.prepared();fake=fixtures.FakeIO(prepared)
        fake.flash_output=TRANSITION+WAIT+countdown+FOUND+WRITE+'All done'
        result=execute_flash(prepared,io=fake)
        self.assertTrue(result['verified']);self.assertEqual(result['bootloader_reconnect_messages'],1)
        for failure in (TRANSITION+WAIT+countdown+WRITE+'All done',
                        RECONNECTED+WRITE+countdown+'All done'):
            with self.subTest(failure=failure):
                prepared=self.prepared();fake=fixtures.FakeIO(prepared);fake.flash_output=failure
                with self.assertRaises(FirmwareError):execute_flash(prepared,io=fake)
                self.assertEqual(fake.flash_calls,1)

    def test_oversized_process_output_does_not_kill_client_and_keeps_error_evidence(self):
        prepared=self.prepared()
        payload=(WRITE+'[!] Error writing block 2\n').encode()+b'x'*(5*1024*1024)+b'\nAll done\n'
        stream=io.BytesIO(payload);killed=[]
        process=SimpleNamespace(stdout=stream,poll=lambda:0,returncode=0,kill=lambda:killed.append(True))
        log=self.root/'large-client.log'
        with patch('pm3gui.firmware.subprocess.Popen',return_value=process):
            result=_SystemFlashIO().run_client(prepared,['--flash'],self.root,log,5)
        self.assertFalse(killed);self.assertTrue(result.log_truncated)
        self.assertEqual(result.output_bytes,len(payload))
        self.assertIn('Error writing block 2',result.output)
        self.assertTrue(result.flash_diagnostics.fatal_lines)
        self.assertLess(log.stat().st_size,4*1024*1024+20000)

    def test_same_usb_serial_must_be_stable_and_unambiguous(self):
        now=[0.0]
        def sleep(delta):now[0]+=delta
        io=_SystemFlashIO()
        def ports():
            if now[0]<0.4:return [SimpleNamespace(device='COM9',vid=0x9ac4,pid=0x4b8f,serial_number='S')]
            if now[0]<0.8:return []
            return [SimpleNamespace(device='COM10',vid=0x9ac4,pid=0x4b8f,serial_number='S')]
        with patch.object(io,'ports',side_effect=ports),patch('pm3gui.firmware.time.monotonic',side_effect=lambda:now[0]),patch('pm3gui.firmware.time.sleep',side_effect=sleep):
            self.assertEqual(io.wait_usb('COM3','S'),'COM10')
        self.assertGreaterEqual(now[0],1.6)

    def test_real_subprocess_finishes_after_eight_megabyte_progress_log(self):
        prepared=self.prepared()
        executable=Path(getattr(sys,'_base_executable',sys.executable))
        prepared=replace(prepared,client_path=executable,client_sha256=fixtures.digest(executable.read_bytes()))
        script=self.root/'fake_flasher.py'
        script.write_text("import os,time,pathlib\nos.write(1,b'Writing segments for file: synthetic\\n')\n"
            "os.write(1,b'x'*(8*1024*1024))\ntime.sleep(.2)\n"
            "pathlib.Path('continued-after-large-log.txt').write_text('alive')\n"
            "os.write(1,b'\\nAll done\\n')\n",encoding='utf-8')
        result=_SystemFlashIO().run_client(prepared,[str(script),'--flash'],self.root,self.root/'real-process.log',20)
        self.assertEqual(result.returncode,0)
        self.assertTrue(result.log_truncated)
        self.assertTrue(result.flash_diagnostics.all_done)
        self.assertFalse(result.flash_diagnostics.fatal_lines)
        self.assertTrue((self.root/'continued-after-large-log.txt').exists())
