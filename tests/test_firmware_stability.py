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
OPEN='[!!] UART CreateFile failed for COM3 (Win32 error 2)\n[!] ERROR: invalid serial port COM3\n'
WRITE='[+] Writing segments for file: selected-fullimage.elf\n'


class TranscriptTests(unittest.TestCase):
    def test_only_bootloader_transition_open_errors_are_transient(self):
        result=flash_diagnostics(TRANSITION+OPEN+WRITE+'[+] All done\n')
        self.assertTrue(result.all_done);self.assertFalse(result.fatal_lines);self.assertEqual(result.reconnect_lines,2)
        for output in (OPEN+WRITE+'All done',TRANSITION+WRITE+OPEN+'All done',
                       TRANSITION+'[!] Flash program error\nAll done',
                       TRANSITION+OPEN.replace('error 2','error 5')+WRITE+'All done'):
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
        for byte in (TRANSITION+OPEN+WRITE+'[+] All done').encode():capture.feed(bytes([byte]))
        result=capture.finish();self.assertTrue(result.all_done);self.assertFalse(result.fatal_lines)


class FirmwareStabilityTests(unittest.TestCase):
    setUp=fixtures.FirmwareTests.setUp
    tearDown=fixtures.FirmwareTests.tearDown
    write_manifest=fixtures.FirmwareTests.write_manifest
    prepared=fixtures.FirmwareTests.prepared

    def test_expected_reenumeration_diagnostics_still_require_full_readback(self):
        prepared=self.prepared();fake=fixtures.FakeIO(prepared)
        fake.flash_output=TRANSITION+OPEN+WRITE+'All done'
        result=execute_flash(prepared,io=fake)
        self.assertTrue(result['verified']);self.assertEqual(result['bootloader_reconnect_messages'],2)
        self.assertEqual(fake.flash_calls,1)
        self.assertEqual(fake.internal,[])

    def test_transient_messages_do_not_hide_a_readback_mismatch(self):
        prepared=self.prepared();fake=fixtures.FakeIO(prepared)
        fake.flash_output=TRANSITION+OPEN+WRITE+'All done';fake.internal[-1]=bytes(fixtures.INTERNAL_SIZE)
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
