"""Automatic intake: actual Qt/controller paths, simulated RF, no COM access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, PropertyMock
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from pm3gui.app import MainWindow
from pm3gui.auto_capture import IntakeCard, PresenceGate, scan_card, MultipleCards, UnclearScan, transport_failed
from pm3gui.operations import key_file_for_session, sector_trailer
from pm3gui.lf_capture import validate_lf_search_capture
from pm3gui.core import DumpData, parse_dump
from test_writing import Card, classic_image, UID

VERSION = '[+] Communicating with PM3 over USB-CDC\n[ ARM ]\n OS......... Iceman/GUI-BlueShark/test\n'
HF_EMPTY = '[!] No known/supported 13.56 MHz tags found'
LF_EMPTY = '[-] No known 125/134 kHz tags found!'
MFC = '[+] UID: '+UID+'\n[+] SAK: 08\nMIFARE Classic 1K\n[+] Valid ISO 14443-A tag found\n'
EM = '[+] EM 410x ID: 0123456789\n[+] Valid EM410x ID found!\n'
LF_CAPTURE = '[=] Reading 30000 bytes from device memory\n[+] Data fetched\n[=] Samples @ 8 bits/smpl, decimation 1:1\n'
NATIVE_CAPTURE_FAILURES = (
    '[!] Received packet frame with invalid CRC D725 <> 8181',
    '[!] Received packet with invalid CRC',
    '[!] Received packet frame with variable part too short? 8/512',
    '[!] Received packet frame without postamble',
    '[!] Received packet OLD frame with payload too short? 54/534',
    '[!] BigBuf download cancelled; partial data discarded',
    '[-] Timed out while trying to download data from device',
    '[-] ERROR: device disconnected during BigBuf download',
    '[-] ERROR: invalid or incomplete BigBuf download; partial data discarded',
    '[-] ERROR: failed to allocate BigBuf download staging buffer',
    '[-] ERROR: failed to send BigBuf download request',
    '[-] ERROR: BigBuf download outside device memory',
    '[!] sample download failed or was cancelled; graph buffer cleared',
    '[!] LF capture failed; search stopped without decoding old samples',
    '[!] incomplete Texkom ADC capture',
    '[-] Get samples error',
    '[!] failed to read sample format; graph buffer cleared',
    '[!] invalid sample data or format; graph buffer cleared',
)


class LFSearchCaptureProofTests(unittest.TestCase):
    def test_complete_native_transfer_and_sample_format_are_required(self):
        self.assertEqual(validate_lf_search_capture(LF_CAPTURE),{'samples':30000,'bits_per_sample':8,'decimation':1})
        packed=LF_CAPTURE.replace('8 bits','4 bits')+'[+] Unpacked 30000 samples\n'
        self.assertEqual(validate_lf_search_capture(packed)['bits_per_sample'],4)
        for text in (packed.replace('30000 samples','29999 samples'),packed.replace('[+] Unpacked 30000 samples\n','')):
            with self.subTest(text=text),self.assertRaises(ValueError):validate_lf_search_capture(text)

    def test_native_success_code_does_not_hide_real_crc_download_failures(self):
        for failure in ('[!] CRC D725 <> 8181','[!] Received packet with invalid CRC',
                        '[!] Dropped 8192 bytes','[!] OLD packet too short 54 / 534',
                        '[!] timeout while waiting for reply','[!] Received packet payload too short',
                        '[!] failed to get current device config','[!] Failed to allocate memory'):
            with self.subTest(failure=failure),self.assertRaises(ValueError):validate_lf_search_capture(LF_CAPTURE+failure,0)

    def test_partial_old_or_ambiguous_transfer_is_never_a_fresh_graphbuffer(self):
        for text in ('Command completed',LF_CAPTURE.replace('Data fetched','Data requested'),
                     LF_CAPTURE.replace('30000 bytes','8192 bytes'),LF_CAPTURE.replace('Samples @','Old samples @'),
                     LF_CAPTURE+'[=] Reading 8192 bytes from device memory\n',
                     LF_CAPTURE.replace('8 bits/smpl','0 bits/smpl'),LF_CAPTURE.replace('1:1','1:9')):
            with self.subTest(text=text),self.assertRaises(ValueError):validate_lf_search_capture(text)

    def test_nonzero_return_code_blocks_decoding_despite_positive_lines(self):
        for code in (-10,-5,-4,-3,-2,1):
            with self.subTest(code=code),self.assertRaises(ValueError):validate_lf_search_capture(LF_CAPTURE,code)


class IntakeDetectionTests(unittest.TestCase):
    def test_search_progress_does_not_misclassify_or_create_a_card(self):
        progress='Searching for FeliCa tag...\nSearching for LEGIC tag...\n'
        self.assertIsNone(scan_card('hf',progress+HF_EMPTY))
        self.assertEqual(scan_card('hf',progress+MFC).kind,'mfc')
        self.assertIsNone(scan_card('lf',LF_EMPTY))
        with self.assertRaises(UnclearScan):scan_card('hf','')
        with self.assertRaises(UnclearScan):scan_card('hf',progress)

    def test_14a_ntag_plus_and_desfire_are_distinct(self):
        self.assertEqual(scan_card('hf',MFC).classic_size,'1k')
        for name,body in [('mfu','NTAG 213'),('hf_profile','MIFARE DESFire'),('hf_profile','MIFARE Plus')]:
            card=scan_card('hf','UID: 04112233445566\n'+body+'\nValid ISO 14443-A tag found')
            self.assertEqual(card.kind,name)
            self.assertEqual(card.uid,'04112233445566')

    def test_eight_byte_hf_identifiers_do_not_enter_classic_uid_parser(self):
        for marker,profile in [('ISO 15693 tag','ISO15693 / NFC-V'),('ISO 18092 / FeliCa tag','FeliCa'),('iCLASS tag / PicoPass tag','iCLASS / PicoPass')]:
            result=scan_card('hf','UID: E004010203040506\n[+] Valid '+marker+' found')
            self.assertEqual(result.protocol,profile);self.assertEqual(result.uid,'E004010203040506')

    def test_lf_real_formats_and_unknown_profile_are_explicit(self):
        self.assertEqual(scan_card('lf',EM).uid,'0123456789')
        values=[('HID Prox','TAG ID: 2000123456','HID Prox'),
                ('Indala','Indala (len 64) Raw: 0111222233334444','Indala'),
                ('AWID','AWID - len: 26 FC: 3 Card: 4 - Wiegand: 1234, Raw: 011122223333444455556666','AWID'),
                ('IO Prox','IO Prox - XSF(01)03:00004, Raw: 1122334455667788 [CRC ok]','ioProx'),
                ('FDX-B','Animal ID........... 643-000000012345\nCountry Code...... 643 - Russia','FDX-B')]
        for marker,body,protocol in values:
            result=scan_card('lf',body+'\n[+] Valid '+marker+' ID found!')
            self.assertEqual(result.protocol,protocol);self.assertTrue(result.uid)
        result=scan_card('lf','[+] Valid NEDAP ID found!')
        self.assertEqual(result.kind,'detection');self.assertEqual(result.uid,'')

    def test_ambiguity_and_crc_failure_never_become_a_saved_identifier(self):
        for band,output in [('hf',MFC+'UID: 11223344'),('hf',MFC+'\nValid FeliCa tag found'),('lf',EM+'\nValid HID Prox ID found!')]:
            with self.assertRaises(MultipleCards):scan_card(band,output)
        with self.assertRaises(UnclearScan):scan_card('lf',EM+'\nCRC failed')

    def test_presence_is_per_band_and_requires_two_explicit_absences(self):
        gate=PresenceGate();hf=scan_card('hf',MFC);lf=scan_card('lf',EM)
        self.assertTrue(gate.observe('hf',hf));self.assertTrue(gate.observe('lf',lf))
        self.assertFalse(gate.observe('hf',hf));self.assertFalse(gate.observe('lf',lf))
        self.assertFalse(gate.observe('hf',IntakeCard('hf','MIFARE Classic-compatible',hf.uid,'mfc','1k')))
        gate.observe('hf',None);gate.unclear('hf');gate.observe('hf',None)
        self.assertFalse(gate.observe('hf',hf))
        gate.observe('hf',None);gate.observe('hf',None)
        self.assertTrue(gate.observe('hf',hf));self.assertFalse(gate.observe('lf',lf))
        changed=IntakeCard('hf',hf.protocol,'11223344',hf.kind,hf.classic_size)
        self.assertTrue(gate.observe('hf',changed))

    def test_transport_and_empty_search_are_different(self):
        self.assertFalse(transport_failed(HF_EMPTY,-10))
        self.assertTrue(transport_failed('UART CreateFile failed for COM5 (1168)',0))
        self.assertTrue(transport_failed('No response from Proxmark3',0))
        for code in (-2,-3,-4,-5,-8):self.assertTrue(transport_failed('',code))

    def test_native_capture_failures_override_positive_card_and_allowed_search_code(self):
        for diagnostic in NATIVE_CAPTURE_FAILURES:
            for code in (0, -10):
                with self.subTest(diagnostic=diagnostic, code=code):
                    self.assertTrue(transport_failed(MFC + diagnostic, code))
                    self.assertTrue(transport_failed(HF_EMPTY + '\n' + diagnostic, code))
        self.assertTrue(transport_failed(MFC + '\x1b[31mERROR: invalid or incomplete '
                                         'BigBuf download\x1b[0m', 0))

    def test_card_protocol_crc_failures_do_not_claim_native_transport_failure(self):
        for diagnostic in ('CRC failed', 'MIFARE response CRC check failed',
                           'ISO14443-A card CRC16 invalid', 'Card protocol: invalid CRC',
                           'Texkom CRC8 check failed', '[!] CRC D725 <> 8181'):
            for code in (0, -10):
                with self.subTest(diagnostic=diagnostic, code=code):
                    self.assertFalse(transport_failed(diagnostic, code))
                    self.assertFalse(transport_failed(MFC + diagnostic, code))
        self.assertFalse(transport_failed(HF_EMPTY, -10))
        self.assertFalse(transport_failed(LF_EMPTY, -10))


class IntakeControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.enterContext(patch('pm3gui.connection_checks.CLEANUP_VERSION_RECHECK_DELAY_MS',0))
        self.enterContext(patch('pm3gui.app.available_ports',return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client',return_value=''))
        self.run=self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.ops=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.questions=self.enterContext(patch('pm3gui.app.QMessageBox.question'))
        self.warning=self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.w=MainWindow(self.root/'profile');self.addCleanup(self.cleanup)
        self.w.client_path.setText(sys.executable);self.w.ports.setEditText('COM123')
    def cleanup(self):
        for name in ('auto_poll_timer','auto_card_timer'):
            if hasattr(self.w,name):getattr(self.w,name).stop()
        if self.w.read_job and self.w.read_job.get('flow'):self.w.read_job['flow'].close()
        self.w.auto_intake=None;self.w.read_job=None;self.w.dirty=False;self.w.runner.set_rf_lease(False)
        self.w.close();self.w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents();self.tmp.cleanup()
    def start(self):
        self.w.auto_capture_button.click();self.assertEqual(self.w.read_job['stage'],'connect');self.finish(VERSION)
    def finish(self,output='',code=0):
        self.w.operation_output=output;self.w._finished(code,'Fixture')
        if hasattr(self.w,'auto_poll_timer'):self.w.auto_poll_timer.stop()
        self.app.processEvents()
    def search(self,output,code=0):
        self.w.auto_poll_timer.stop();self.w._auto_poll();self.finish(output,code)
    def finish_cleanup(self):
        self.assertEqual(self.w.read_job['stage'],'auto_cleanup_break')
        version=VERSION.replace('USB-CDC','FPC UART') if self.w.auto_intake['profile']['transport']=='blueshark' else VERSION
        self.finish();self.finish();self.finish(version)
    def read_em(self):
        self.search(HF_EMPTY,-10);self.search(EM);self.assertEqual(self.w.read_job['stage'],'lf_identifier')
        self.finish(EM);self.assertEqual(self.w.read_job['stage'],'field_off');self.finish()

    def blue_lf_capture(self):
        self.w.transport_combo.setCurrentIndex(self.w.transport_combo.findData('blueshark'));self.w.ports.setEditText('COM123')
        self.w.auto_capture_button.click();self.finish(VERSION.replace('USB-CDC','FPC UART'))
        self.search(HF_EMPTY,-10);self.w._auto_poll()
        self.assertEqual(self.w.read_job['stage'],'lf_search_capture')
        self.assertEqual(self.ops.call_args.args[1].kind,'lf_capture_search_buffer')

    def test_blueshark_lf_capture_is_finite_and_only_new_buffer_is_decoded(self):
        self.blue_lf_capture();self.assertEqual(self.ops.call_args.args[1].render(),'lf read -v -s 30000')
        self.finish(LF_CAPTURE);self.assertEqual(self.ops.call_args.args[1].render(),'lf search -1')
        self.finish(EM);self.assertEqual(self.w.read_job['stage'],'lf_identifier');self.finish(EM);self.finish()
        self.assertEqual(self.w.auto_intake['saved'],1);self.assertEqual(self.w.auto_intake['last_lf_capture']['samples'],30000)
        self.search(HF_EMPTY,-10);self.w._auto_poll();self.finish(LF_CAPTURE);self.finish(EM)
        self.assertEqual(self.w.auto_intake['saved'],1)
        self.assertEqual(sum(c.args[1].kind=='lf_em410x_read' for c in self.ops.call_args_list),1)
        self.assertNotIn('lf search',[c.args[1] for c in self.run.call_args_list])

    def test_bad_blueshark_capture_never_decodes_or_rearms_previous_lf_presence(self):
        bad=[('Reading 30000 bytes from device memory\ntimeout while waiting for reply',0),
             (LF_CAPTURE+'[!] CRC D725 <> 8181\n',0),(LF_CAPTURE+'[!] Dropped 8192 bytes\n',0),
             (LF_CAPTURE+'[!] OLD packet too short 54 / 534\n',0),('Command completed',0),(LF_CAPTURE,-4)]
        for output,code in bad:
            with self.subTest(output=output,code=code):
                self.ops.reset_mock();self.blue_lf_capture()
                token=scan_card('lf',EM).presence_key;self.w.auto_intake['gate'].tokens['lf']=token
                self.finish(output,code)
                self.assertEqual(self.w.read_job['stage'],'auto_cleanup_break')
                self.assertFalse(any(c.args[1].kind=='lf_search_buffer' for c in self.ops.call_args_list))
                self.assertEqual(self.w.auto_intake['gate'].tokens['lf'],token)
                self.assertEqual(self.w.library.list_cards(),[]);self.finish_cleanup()

    def test_stop_during_blueshark_capture_does_not_decode_late_success(self):
        self.blue_lf_capture()
        with patch.object(type(self.w.runner),'busy',new_callable=PropertyMock,return_value=True):self.w.cancel_operation()
        self.finish(LF_CAPTURE)
        self.assertEqual(self.w.read_job['stage'],'auto_cleanup_break')
        self.assertFalse(any(c.args[1].kind=='lf_search_buffer' for c in self.ops.call_args_list))
        self.finish_cleanup();self.assertIsNone(self.w.auto_intake)

    def test_blueshark_detection_confirmation_requires_another_fresh_capture(self):
        self.blue_lf_capture();self.finish(LF_CAPTURE);self.finish(EM)
        changed=EM.replace('0123456789','1123456789');self.finish(changed)
        self.assertEqual(self.w.read_job['stage'],'lf_search_capture')
        self.assertEqual(self.w.read_job['lf_search_after_capture'],'confirm_detection')
        self.finish(LF_CAPTURE);self.assertEqual(self.w.read_job['stage'],'confirm_detection')
        self.assertEqual(self.ops.call_args.args[1].kind,'lf_search_buffer')
        self.finish(changed);self.finish();self.assertEqual(self.w.library.list_cards(),[])

    def test_usb_lf_poll_retains_legacy_broad_native_search(self):
        self.start();self.search(HF_EMPTY,-10);self.w._auto_poll()
        self.assertEqual(self.w.read_job['stage'],'search');self.assertEqual(self.run.call_args.args[1],'lf search')
        self.assertFalse(any(c.args[1].kind=='lf_capture_search_buffer' for c in self.ops.call_args_list))

    def test_mixed_positive_hf_then_native_capture_failure_stops_before_read_or_rearm(self):
        for diagnostic, code in (
                ('[-] ERROR: invalid or incomplete BigBuf download; partial data discarded', -10),
                ('[!] Received packet frame with invalid CRC D725 <> 8181', 0),
                ('[!] LF capture failed; search stopped without decoding old samples', 0)):
            with self.subTest(diagnostic=diagnostic, code=code):
                self.run.reset_mock();self.ops.reset_mock();self.start()
                state=self.w.auto_intake
                hf_token=scan_card('hf',MFC).presence_key
                lf_token=scan_card('lf',EM).presence_key
                state['gate'].tokens.update(hf=hf_token,lf=lf_token)
                state['gate'].misses.update(hf=1,lf=1)
                self.search(MFC+diagnostic,code)
                self.assertEqual(self.w.read_job['stage'],'auto_cleanup_break')
                self.assertTrue(state['stopping'])
                self.assertEqual(state['attempts'],0)
                self.assertEqual(state['saved'],0)
                self.assertEqual(state['gate'].tokens,{'hf':hf_token,'lf':lf_token})
                self.assertEqual(state['gate'].misses,{'hf':1,'lf':1})
                self.assertEqual(self.w.library.list_cards(),[])
                self.assertEqual([call.args[1] for call in self.run.call_args_list],['hw version','hf search'])
                self.assertEqual([call.args[1].kind for call in self.ops.call_args_list],['emulation_break'])
                self.finish_cleanup()

    def test_genuine_native_no_tag_rearms_only_after_two_absences_and_continues(self):
        self.start();state=self.w.auto_intake
        hf_token=scan_card('hf',MFC).presence_key
        state['gate'].tokens['hf']=hf_token
        self.search(HF_EMPTY,-10)
        self.assertFalse(state['stopping']);self.assertEqual(state['band'],'lf')
        self.assertEqual(state['gate'].tokens['hf'],hf_token)
        self.search(LF_EMPTY,-10);self.search(HF_EMPTY,-10)
        self.assertFalse(state['stopping']);self.assertNotIn('hf',state['gate'].tokens)
        self.assertEqual(state['band'],'lf');self.assertEqual(state['attempts'],0)

    def test_start_is_explicit_and_uses_one_frozen_client_session(self):
        self.assertIsNone(self.w.auto_intake);self.run.assert_not_called();self.ops.assert_not_called()
        self.w.ports.setEditText('COM18');self.w.keep_connection.setChecked(False)
        self.start();self.assertTrue(self.w.runner.rf_lease);self.assertFalse(self.w.read_button.isEnabled())
        self.assertTrue(self.w.auto_capture_button.isEnabled());self.assertTrue(self.w.auto_capture_button.isChecked())
        self.search(HF_EMPTY,-10);self.assertEqual(self.w.auto_intake['band'],'lf')
        self.search(LF_EMPTY,-10);self.assertEqual(self.w.auto_intake['band'],'hf')
        for call in self.run.call_args_list:
            settings=call.args[0];self.assertEqual(settings.port,'COM18');self.assertTrue(settings.keep_open);self.assertTrue(settings.require_session)

    def test_lf_saved_once_while_present_and_rearmed_after_real_removal(self):
        self.start();self.read_em();cards=self.w.library.list_cards();self.assertEqual(len(cards),1)
        cid=cards[0]['id'];self.assertEqual(cards[0]['rf_band'],'lf');self.assertEqual(self.w.library.get_dump(cid).data,bytes.fromhex('0123456789'))
        self.assertIn('0123456789',self.w.detection_label.text());self.assertEqual(self.w.workspace_bands.currentIndex(),1)
        self.search(HF_EMPTY,-10);self.search(EM)
        self.assertEqual(len(self.w.library.list_revisions(cid)),1);self.assertEqual(self.w.auto_intake['saved'],1)
        for _ in range(2):self.search(HF_EMPTY,-10);self.search(LF_EMPTY,-10)
        self.search(HF_EMPTY,-10);self.search(EM);self.finish(EM);self.finish()
        self.assertEqual(len(self.w.library.list_cards()),1);self.assertEqual(len(self.w.library.list_revisions(cid)),2)
        self.assertEqual(self.w.auto_intake['saved'],2);self.questions.assert_not_called()

    def test_switching_lf_identifier_does_not_require_an_empty_poll(self):
        self.start();self.read_em();self.search(HF_EMPTY,-10)
        second=EM.replace('0123456789','1123456789');self.search(second);self.finish(second);self.finish()
        self.assertEqual(len(self.w.library.list_cards()),2)

    def test_classic_uses_known_keys_and_saves_guarded_partial_without_dialog(self):
        self.w.recovery_enabled.setChecked(False);self.start();self.search(MFC)
        self.assertEqual(self.w.read_job['stage'],'info');self.finish('UID: '+UID+'\nSAK: 08')
        self.assertEqual(self.w.read_job['stage'],'keys')
        key_file_for_session(self.w.current_session,UID).write_bytes(bytes.fromhex('FFFFFFFFFFFF')*32)
        self.finish('[+] 000 | 003 | FFFFFFFFFFFF | 1 | ------------ | 0 |')
        card=Card(classic_image())
        for _ in range(20):
            if self.w.read_job['stage']!='partial_read':break
            self.finish(*card.respond(self.w.read_job['partial_reader'].pending))
        self.assertEqual(self.w.read_job['stage'],'field_off');self.finish()
        saved=self.w.library.get_dump(self.w.library.list_cards()[0]['id'])
        self.assertEqual(saved.metadata['partial_dump']['read_blocks'],[0,1,2,3])
        self.assertEqual(saved.data[64:128],bytes(64));self.assertEqual(self.w.auto_intake['saved'],1)
        self.assertTrue(self.w.key_library.list_card_keys(UID));self.questions.assert_not_called();self.assertEqual(card.writes,[])

    def test_classic_without_keys_saves_confirmed_detection_not_a_zero_dump(self):
        self.w.recovery_enabled.setChecked(False);self.start();self.search(MFC);self.finish('UID: '+UID+'\nSAK: 08')
        self.finish('No keys found')
        self.assertEqual(self.w.read_job['stage'],'confirm_detection');self.finish(MFC);self.finish()
        saved=self.w.library.get_dump(self.w.library.list_cards()[0]['id'])
        self.assertEqual(saved.metadata['snapshot_kind'],'detection-record')
        self.assertNotIn('partial_dump',saved.metadata);self.assertEqual(self.w.auto_intake['saved'],1)
        self.questions.assert_not_called();self.warning.assert_not_called()

    def test_complete_classic_dump_and_keys_are_saved_then_lf_scanning_resumes(self):
        self.start();self.search(MFC);self.finish('UID: '+UID+'\nSAK: 08')
        key_file_for_session(self.w.current_session,UID).write_bytes(bytes.fromhex('FFFFFFFFFFFF')*32)
        self.finish('\n'.join(f'[+] {s:03d} | {sector_trailer(s):03d} | FFFFFFFFFFFF | 1 | FFFFFFFFFFFF | 1 |' for s in range(16)))
        self.assertEqual(self.w.read_job['stage'],'dump');raw=classic_image()
        (self.w.current_session/'card.json').write_text(json.dumps({'FileType':'mfc v3','Card':{'UID':UID},
            'blocks':{str(b):raw[b*16:(b+1)*16].hex() for b in range(64)}}),encoding='utf-8')
        self.finish('\n'.join(f'[+] Sector... {s} block... {b} ( ok )' for s in range(16) for b in range(4)))
        self.finish('UID: '+UID+'\nSAK: 08');self.finish()
        card=self.w.library.list_cards()[0];dump=self.w.library.get_dump(card['id'])
        self.assertEqual(dump.data,raw);self.assertNotIn('partial_dump',dump.metadata)
        self.assertEqual(len(self.w.key_library.list_card_keys(UID)),32)
        self.assertEqual(self.w.auto_intake['band'],'lf');self.questions.assert_not_called()

    def test_ntag_dispatch_and_missing_dump_fallback_keep_real_identity(self):
        self.start();reply='UID: 04112233445566\nNTAG 213\n[+] Valid ISO 14443-A tag found'
        self.search(reply);self.assertEqual(self.ops.call_args.args[1].kind,'mfu_dump')
        self.finish('[!] Could not read card',-10);self.finish('UID: 04112233445566')
        self.assertEqual(self.w.read_job['stage'],'confirm_detection');self.finish(reply);self.finish()
        row=self.w.library.list_cards()[0];self.assertEqual(row['uid'],'04112233445566')
        self.assertEqual(self.w.library.get_dump(row['id']).metadata['snapshot_kind'],'detection-record')

    def test_bad_connection_settings_do_not_launch_or_require_a_power_reset(self):
        self.w.ports.setEditText('');self.w.auto_capture_button.click()
        self.assertIsNone(self.w.auto_intake);self.assertIsNone(self.w.read_job);self.assertFalse(self.w.runner.rf_lease)
        self.run.assert_not_called();self.ops.assert_not_called();self.assertFalse(self.w.auto_capture_button.isChecked())

    def test_detection_only_does_not_replace_a_previous_memory_dump(self):
        full=DumpData(classic_image(),'bin',{},16)
        cid=self.w.library.create_card('Full',full,uid=UID,protocol='MIFARE Classic 1K · 1K')
        self.w.recovery_enabled.setChecked(False);self.start();self.search(MFC);self.finish('UID: '+UID+'\nSAK: 08')
        self.finish('No keys found');self.finish(MFC);self.finish()
        self.assertEqual(self.w.library.get_dump(cid).data,full.data)
        self.assertEqual(len(self.w.library.list_revisions(cid)),1)
        self.assertEqual(len(self.w.library.list_cards()),2)

    def test_felica_tree_is_saved_with_eight_byte_idm(self):
        self.start();reply='IDm: 0102030405060708\n[+] Valid ISO 18092 / FeliCa tag found\n'
        self.search(reply);self.assertEqual(self.w.read_job['stage'],'family_read');self.finish(reply)
        doc={'systems':[{'code':'0003','idm':'0102030405060708','pmm':'1122334455667788','nodes':[]}]}
        (self.w.current_session/'card.json').write_text(json.dumps(doc),encoding='utf-8')
        self.finish();self.finish(reply);self.finish()
        card=self.w.library.list_cards()[0];self.assertEqual(card['uid'],'0102030405060708');self.assertEqual(card['rf_band'],'hf')
        self.assertEqual(self.w.library.get_dump(card['id']).metadata['structured_document'],doc)

    def test_lf_hid_uses_native_reader_and_preserves_identifier_fields(self):
        self.start();self.search(HF_EMPTY,-10)
        reply='TAG ID: 2000123456\n[+] Valid HID Prox ID found!'
        self.search(reply);self.assertEqual(self.ops.call_args.args[1].render(),'lf hid reader')
        self.finish(reply);self.finish()
        card=self.w.library.list_cards()[0];self.assertEqual(card['protocol'],'HID Prox');self.assertEqual(card['rf_band'],'lf')
        self.assertEqual(self.w.library.get_dump(card['id']).metadata['snapshot_kind'],'lf-identifier')

    def test_changed_or_removed_card_cannot_receive_the_old_read(self):
        self.start();self.search(HF_EMPTY,-10);self.search(EM)
        self.finish(EM.replace('0123456789','1123456789'))
        self.assertEqual(self.w.read_job['stage'],'confirm_detection')
        self.finish(EM.replace('0123456789','1123456789'));self.finish()
        self.assertEqual(self.w.library.list_cards(),[])

    def test_unknown_lf_retains_detection_evidence_after_confirmation(self):
        self.start();self.search(HF_EMPTY,-10);self.search('[+] Valid NEDAP ID found!')
        self.assertEqual(self.w.read_job['stage'],'confirm_detection');self.finish('[+] Valid NEDAP ID found!');self.finish()
        card=self.w.library.list_cards()[0];self.assertEqual(card['rf_band'],'lf');self.assertEqual(card['uid'],'')
        saved=self.w.library.get_dump(card['id']);self.assertEqual(saved.metadata['snapshot_kind'],'detection-record')
        exported=parse_dump(Path(saved.metadata['auto_intake']['session'])/'saved.json')
        self.assertEqual(exported.metadata['rf_band'],'lf')

    def test_ambiguous_scan_and_transient_empty_do_not_rearm_a_card(self):
        self.start();self.read_em();self.search(HF_EMPTY,-10);self.search(LF_EMPTY,-10)
        self.search(MFC+'UID: 11223344');self.search(EM)
        self.assertEqual(self.w.auto_intake['saved'],1);self.assertEqual(self.w.read_job['stage'],'waiting')

    def test_stop_during_exchange_cancels_once_and_finishes_rf_cleanup(self):
        self.start();self.w._auto_poll()
        with patch('pm3gui.device.ClientRunner.busy',new_callable=PropertyMock,return_value=True),patch.object(self.w.runner,'cancel') as cancel:
            self.w.cancel_operation();self.w.cancel_operation();cancel.assert_called_once()
        self.finish('',-2);self.finish_cleanup()
        self.assertIsNone(self.w.auto_intake);self.assertIsNone(self.w.read_job);self.assertFalse(self.w.runner.rf_lease)
        self.assertFalse(self.w.auto_poll_timer.isActive());self.assertFalse(self.w.auto_capture_button.isChecked())

    def start_cleanup_version(self,transport='blueshark'):
        w=self.w;w.transport_combo.setCurrentIndex(w.transport_combo.findData(transport))
        w.ports.setEditText('COM123')
        self.enterContext(patch.object(type(w.runner),'connection_transport',new_callable=PropertyMock,return_value=transport))
        reply=VERSION.replace('USB-CDC','FPC UART') if transport=='blueshark' else VERSION
        w.auto_capture_button.click();self.finish(reply);w.auto_poll_timer.stop()
        w.cancel_operation();self.finish();self.finish()
        self.assertEqual(w.read_job['stage'],'auto_cleanup_version')
        return w.auto_intake['root']

    def test_intake_stop_rechecks_only_version_then_releases_hardware(self):
        w=self.w;directory=self.start_cleanup_version();before=self.ops.call_count;before_search=self.run.call_count
        w.read_job['cleanup_version_deadline']-=1
        for _ in range(3):self.finish('Client banner only')
        self.assertEqual(w.read_job['stage'],'auto_cleanup_version');self.assertTrue(w.runner.rf_lease)
        self.assertEqual(self.ops.call_count,before);self.assertEqual(self.run.call_count,before_search+3)
        self.assertEqual(self.run.call_args.args[1],'hw version');self.assertLess(self.run.call_args.kwargs['timeout_ms'],20000)
        self.finish(VERSION.replace('USB-CDC','FPC UART'))
        self.assertIsNone(w.read_job);self.assertIsNone(w.auto_intake);self.assertFalse(w.runner.rf_lease)
        self.assertFalse(any(c.args[1] in ('hf search','lf search') for c in self.run.call_args_list))
        report=json.loads((directory/'auto-intake.json').read_text(encoding='utf-8'))
        self.assertEqual([c['connected'] for c in report['cleanup_version_checks']],[False,False,False,True])

    def test_intake_eighth_missing_os_keeps_uncertain_lock_without_polling(self):
        w=self.w;self.start_cleanup_version();versions=self.run.call_count
        for _ in range(8):self.finish('Client banner only')
        self.assertEqual(w.read_job['stage'],'uncertain');self.assertTrue(w.runner.rf_lease);self.assertFalse(w.read_button.isEnabled())
        self.assertEqual(self.run.call_count,versions+7)
        before=self.run.call_count+self.ops.call_count;w._auto_poll();self.assertEqual(self.run.call_count+self.ops.call_count,before)

    def test_intake_cleanup_does_not_retry_usb_errors_wrong_channel_or_expired_budget(self):
        cases=[('usb','Client banner only',0,False),('blueshark','Communicating with PM3 over USB-CDC',0,False),
               ('blueshark','No response from Proxmark3',0,False),('blueshark','OFFLINE mode',0,False),
               ('blueshark','Client banner only',-5,False),('blueshark','Client banner only',0,True)]
        for transport,output,code,expired in cases:
            with self.subTest(transport=transport,output=output,code=code,expired=expired):
                self.start_cleanup_version(transport);before=self.run.call_count
                if expired:self.w.read_job['cleanup_version_deadline']=0
                self.finish(output,code)
                self.assertEqual(self.w.read_job['stage'],'uncertain');self.assertEqual(self.run.call_count,before)
                with patch('pm3gui.auto_capture_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):self.w.acknowledge_auto_reset()

    def test_radio_loss_stops_polling_and_unconfirmed_stop_keeps_the_lock(self):
        self.start();self.search('UART CreateFile failed (Win32 error 1168)',0)
        self.assertTrue(self.w.auto_intake['stopping']);self.finish('',-5)
        self.assertEqual(self.w.read_job['stage'],'uncertain');self.assertTrue(self.w.runner.rf_lease)
        count=self.run.call_count+self.ops.call_count;self.w._auto_poll()
        self.assertEqual(self.run.call_count+self.ops.call_count,count);self.assertFalse(self.w.read_button.isEnabled())
        with patch('pm3gui.auto_capture_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
            self.w.acknowledge_auto_reset()
        self.assertIsNone(self.w.read_job);self.assertFalse(self.w.runner.rf_lease)

    def test_storage_failure_stops_instead_of_repeated_identifier_fallback(self):
        self.start();self.search(HF_EMPTY,-10);self.search(EM)
        with patch.object(self.w.library,'create_card',side_effect=sqlite3.OperationalError('disk full')):
            self.finish(EM)
        self.assertEqual(self.w.read_job['stage'],'auto_cleanup_break');self.finish_cleanup()
        self.assertIn('disk full',self.w.auto_capture_status.text());self.warning.assert_not_called()

    def test_saved_capture_does_not_discard_or_replace_a_dirty_editor(self):
        source=DumpData(classic_image(),'bin',{},16);cid=self.w.library.create_card('Editor',source,uid=UID,protocol='MIFARE Classic 1K')
        self.w.load_card(cid);self.w.hex_edit.setPlainText('AA BB');self.w.dirty=True
        self.start();self.read_em()
        self.assertEqual(self.w.card_id,cid);self.assertEqual(self.w.hex_edit.toPlainText(),'AA BB');self.assertTrue(self.w.dirty)
        self.questions.assert_not_called()

    def test_closing_requests_safe_stop_before_closing_the_window(self):
        self.start();event=QCloseEvent();self.w.closeEvent(event)
        self.assertFalse(event.isAccepted());self.assertTrue(self.w.auto_intake['close_requested'])
        self.assertEqual(self.w.read_job['stage'],'auto_cleanup_break')
        self.w.auto_intake['close_requested']=False;self.finish_cleanup()

    def test_three_unrecognised_search_replies_stop_without_false_empty_state(self):
        self.start()
        for _ in range(3):self.search('[+] Something else')
        self.assertEqual(self.w.read_job['stage'],'auto_cleanup_break');self.assertEqual(self.w.library.list_cards(),[])
