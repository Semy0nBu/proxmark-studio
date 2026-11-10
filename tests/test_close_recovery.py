"""Missing ports and failed cleanup must not trap the desktop window."""
import json
import unittest
from unittest.mock import patch, PropertyMock
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox
import test_app as fixtures
from pm3gui.connection_checks import transport_never_opened

OPEN_ERROR='[+] Using UART port COM3\n[!!] UART CreateFile failed for COM3 (Win32 error 2)\n[!] ERROR: invalid serial port COM3\n'


class CloseRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.application=QApplication.instance() or QApplication([])
    setUp=fixtures.AppTests.setUp
    tearDown=fixtures.AppTests.tearDown
    finish_step=fixtures.AppTests.finish_step

    def test_missing_port_does_not_schedule_a_manual_search_cleanup_loop(self):
        self.window.run_command('hf search',90000)
        calls=self.run_client.call_count
        self.finish_step(OPEN_ERROR,code=-5)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.run_client.call_count,calls)
        self.assertTrue(self.window.ports.isEnabled())
        event=QCloseEvent();self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())

    def test_missing_port_during_read_leaves_window_available(self):
        self.window.read_hf_card()
        calls=self.run_client.call_count
        self.finish_step(OPEN_ERROR,code=-5)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.run_client.call_count,calls)
        self.assertIn('код',self.warning.call_args.args[2])

    def test_failed_cleanup_can_close_without_retry_or_false_stop_confirmation(self):
        self.window.read_job={'stage':'cancel_resync_failed','session':self.root}
        timer=QTimer(self.window);timer.setSingleShot(True);timer.start(60000)
        self.window.read_job['cleanup_version_timer']=timer
        with patch.object(self.window.runner,'shutdown') as shutdown:
            event=QCloseEvent();self.window.closeEvent(event)
            self.assertTrue(event.isAccepted());shutdown.assert_called_once()
        self.assertIsNone(self.window.read_job);self.assertFalse(timer.isActive())
        status=json.loads((self.window.data_dir/'last-exit-status.json').read_text())
        self.assertFalse(status['device_stop_confirmed'])
        self.assertEqual(status['jobs'][0]['stage'],'cancel_resync_failed')
        self.run_client.assert_not_called();self.run_operation.assert_not_called()

    def test_unconfirmed_auto_intake_exits_without_restarting_its_cleanup(self):
        self.window.read_job={'stage':'uncertain','auto_capture':True,'job_kind':'auto_capture'}
        self.window.auto_intake={'close_requested':True}
        self.window.auto_poll_timer=QTimer(self.window);self.window.auto_poll_timer.start(60000)
        self.window.auto_card_timer=QTimer(self.window);self.window.auto_card_timer.start(60000)
        event=QCloseEvent();self.window.closeEvent(event)
        self.assertTrue(event.isAccepted());self.assertIsNone(self.window.auto_intake)
        self.assertFalse(self.window.auto_poll_timer.isActive())
        self.run_client.assert_not_called();self.run_operation.assert_not_called()

    def test_unsaved_cancel_preserves_failed_job_and_blocks_shutdown(self):
        card=self.window.library.create_card('Synthetic',fixtures.DumpData(b'test','bin',{},16))
        self.window.load_card(card);self.window.card_name.setText('Unsaved')
        job={'stage':'cancel_resync_failed'};self.window.read_job=job
        self.question.return_value=QMessageBox.StandardButton.Cancel
        with patch.object(self.window.runner,'shutdown') as shutdown:
            event=QCloseEvent();self.window.closeEvent(event)
            self.assertFalse(event.isAccepted());shutdown.assert_not_called()
        self.assertIs(self.window.read_job,job)
        self.assertFalse((self.window.data_dir/'last-exit-status.json').exists())

    def test_active_client_and_firmware_still_block_close(self):
        with patch('pm3gui.app.QMessageBox.information'):
            with patch.object(type(self.window.runner),'busy',new_callable=PropertyMock,return_value=True):
                event=QCloseEvent();self.window.closeEvent(event);self.assertFalse(event.isAccepted())
            with patch.object(type(self.window),'firmware_busy',new_callable=PropertyMock,return_value=True):
                event=QCloseEvent();self.window.closeEvent(event);self.assertFalse(event.isAccepted())

    def test_cleanup_failure_after_real_exchange_is_still_locked_until_exit(self):
        self.window.run_command('hf search',90000)
        self.finish_step('[+] Communicating with PM3 over USB-CDC\n[!] Download failed\n',code=-5)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.finish_step(OPEN_ERROR,code=-5)
        self.assertEqual(self.window.read_job['stage'],'cancel_resync_failed')
        self.assertFalse(self.window.ports.isEnabled())

    def test_stop_hands_matching_completion_to_cancel_wait_then_allows_exit(self):
        self.window.read_hf_card()
        token=self.window._step_token
        with patch.object(type(self.window.runner),'busy',new_callable=PropertyMock,return_value=True):
            self.window.cancel_operation()
        self.window.runner.completed.emit(token,-2,'Cancelled synthetic search')
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.assertEqual(self.run_client.call_args.args[1],'hw version')
        check_token=self.window._step_token
        self.window.runner.completed.emit(check_token,-5,'Synthetic disconnected device')
        self.assertEqual(self.window.read_job['stage'],'cancel_resync_failed')
        event=QCloseEvent();self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertIsNone(self.window.read_job)

    def test_manual_search_cancellation_keeps_its_completion_token(self):
        self.window.run_command('hf search',90000)
        token=self.window._step_token
        with patch.object(type(self.window.runner),'busy',new_callable=PropertyMock,return_value=True):
            self.window.cancel_operation()
        self.window.runner.completed.emit(token,-2,'Cancelled synthetic search')
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.window._log('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test\n')
        self.window.runner.completed.emit(self.window._step_token,0,'Fresh version')
        self.assertIsNone(self.window.read_job)
        self.assertTrue(self.window.ports.isEnabled())

    def test_second_stop_does_not_orphan_waiting_completion(self):
        self.window.read_hf_card();token=self.window._step_token
        with patch.object(type(self.window.runner),'busy',new_callable=PropertyMock,return_value=True):
            self.window.cancel_operation();waiting=self.window.read_job
            self.window.cancel_operation()
        self.assertIs(self.window.read_job,waiting)
        self.window.runner.completed.emit(token,-2,'Cancelled synthetic search')
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.assertEqual(self.run_client.call_count,2)

    def test_stop_with_an_idle_orphan_starts_check_without_waiting_for_absent_process(self):
        self.window.read_hf_card()
        self.window.cancel_operation()
        self.assertEqual(self.window.read_job['stage'],'cancel_resync')
        self.assertEqual(self.run_client.call_args.args[1],'hw version')
        self.window.runner.completed.emit(self.window._step_token,-5,'Device unavailable')
        event=QCloseEvent();self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())

    def test_old_cancelled_token_cannot_finish_the_fresh_check(self):
        self.window.read_hf_card();old_token=self.window._step_token
        with patch.object(type(self.window.runner),'busy',new_callable=PropertyMock,return_value=True):
            self.window.cancel_operation()
        self.window.runner.completed.emit(old_token,-2,'Cancelled search')
        new_token=self.window._step_token;job=self.window.read_job
        self.window.runner.completed.emit(old_token,0,'Late search result')
        self.assertIs(self.window._step_token,new_token)
        self.assertIs(self.window.read_job,job)
        self.assertEqual(self.run_client.call_count,2)

    def test_frozen_release_check_exercises_the_completion_signal(self):
        from pm3gui.release_check import check_stop_completion
        result=check_stop_completion(self.window)
        self.assertTrue(result['passed'])
        self.run_client.assert_not_called();self.run_operation.assert_not_called()


class OpenFailureProofTests(unittest.TestCase):
    def test_only_explicit_uncontradicted_open_failure_is_accepted(self):
        self.assertTrue(transport_never_opened(OPEN_ERROR))
        for output in ('','[!] timeout','[!] ERROR: invalid serial port COM3',
                       '[+] Communicating with PM3 over USB-CDC\n'+OPEN_ERROR,
                       '[+] UID: A1B2C3D4\n'+OPEN_ERROR,
                       'OS......... Iceman/test\n'+OPEN_ERROR):
            with self.subTest(output=output):self.assertFalse(transport_never_opened(output))
