"""Controller reconnection uses version probes, never a replayed RF command."""
import unittest
from unittest.mock import patch, PropertyMock
from PySide6.QtWidgets import QApplication
import test_app as fixtures

USB='[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test\n'


class IdleReconnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.application=QApplication.instance() or QApplication([])
    setUp=fixtures.AppTests.setUp
    tearDown=fixtures.AppTests.tearDown

    def start(self):
        self.window.ports.setEditText('COM3')
        self.window.keep_connection.setChecked(True)
        self.reconnect=self.window.reconnector
        self.reconnect.lost('idle link lost');self.reconnect.timer.stop()

    def reply(self,output='',code=-5):
        self.window.operation_output=output
        self.window._step_completed(self.window._step_token,code,'synthetic version probe')

    def test_idle_loss_restores_only_with_fresh_correct_channel(self):
        self.start();self.reconnect._dispatch()
        self.assertEqual(self.run_client.call_args.args[1],'hw version')
        self.reply(USB,0)
        self.assertIsNone(self.reconnect.profile)
        self.assertFalse(self.reconnect.timer.isActive())
        self.assertIn('восстановлено',self.window.device_info.text())
        self.run_operation.assert_not_called()

    def test_failed_reconnects_have_finite_attempts_without_rf(self):
        self.start()
        for _ in range(3):
            self.reconnect._dispatch();self.reply();self.reconnect.timer.stop()
        self.reconnect._dispatch()
        self.assertEqual([x.args[1] for x in self.run_client.call_args_list],['hw version']*3)
        self.assertIsNone(self.reconnect.profile)
        self.run_operation.assert_not_called()

    def test_wrong_channel_does_not_confirm_recovery(self):
        self.start();self.reconnect._dispatch()
        self.reply(USB.replace('USB-CDC','FPC UART'),0)
        self.assertIsNotNone(self.reconnect.profile)
        self.assertTrue(self.reconnect.timer.isActive())

    def test_user_profile_change_cancels_a_queued_retry(self):
        self.start();self.window.ports.setEditText('COM9')
        self.reconnect.timer.timeout.emit()
        self.run_client.assert_not_called()

    def test_stop_cancels_a_queued_retry(self):
        self.start();self.window.cancel_operation();self.reconnect.timer.timeout.emit()
        self.run_client.assert_not_called()

    def test_jobs_and_firmware_prevent_idle_reconnect(self):
        self.window.read_job={'stage':'uncertain'}
        self.window.reconnector.lost();self.assertIsNone(self.window.reconnector.profile)
        self.window.read_job=None
        with patch.object(type(self.window),'firmware_busy',new_callable=PropertyMock,return_value=True):
            self.window.reconnector.lost();self.assertIsNone(self.window.reconnector.profile)
        self.run_client.assert_not_called()

    def test_usb_renumbering_follows_only_the_same_serial(self):
        self.start();self.reconnect.identity='SYNTHETIC-SERIAL'
        with patch('pm3gui.app.available_ports',return_value=[{'device':'COM9','kind':'usb','serial_number':'SYNTHETIC-SERIAL'}]):
            self.reconnect._dispatch()
        self.assertEqual(self.run_client.call_args.args[0].port,'COM9')
        self.assertEqual(self.window._port(),'COM9')
        self.reply(USB,0);self.assertIsNone(self.reconnect.profile)

    def test_usb_wrong_or_ambiguous_serial_is_not_opened(self):
        self.start();self.reconnect.identity='SYNTHETIC-SERIAL'
        for ports in ([{'device':'COM9','kind':'usb','serial_number':'OTHER'}],
                      [{'device':x,'kind':'usb','serial_number':'SYNTHETIC-SERIAL'} for x in ('COM9','COM10')]):
            with patch('pm3gui.app.available_ports',return_value=ports):self.reconnect._dispatch()
            self.reconnect.timer.stop()
        self.run_client.assert_not_called()

    def test_deadline_prevents_late_probe_and_manual_rf_cancels_backoff(self):
        self.start();self.reconnect.clock=lambda:self.reconnect.deadline
        self.reconnect._dispatch();self.run_client.assert_not_called()
        self.reconnect.clock=lambda:0;self.reconnect.lost();self.reconnect.timer.stop()
        self.window.run_command('hf search',90000)
        self.assertIsNone(self.reconnect.profile)
        self.assertEqual(self.run_client.call_args.args[1],'hf search')
