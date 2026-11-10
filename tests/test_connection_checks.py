"""Cleanup timer ownership and deadlines; no device or visible GUI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pm3gui.connection_checks import schedule_cleanup_version_check, stop_cleanup_version_checks


class CleanupTimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.owner=QObject(self.app);self.addCleanup(self.owner.deleteLater)
        self.owner._start_step=Mock();self.failure=Mock()
        self.job={'stage':'cleanup_version','cleanup_version_generation':1,
                  'cleanup_version_probe_count':1,'cleanup_version_deadline':time.monotonic()+20}
        self.owner.read_job=self.job
        self.enterContext(patch('pm3gui.connection_checks.CLEANUP_VERSION_RECHECK_DELAY_MS',5))
        self.addCleanup(lambda:stop_cleanup_version_checks(self.job))
    def schedule(self):return schedule_cleanup_version_check(self.owner,self.job,'cleanup_version',self.failure)
    def wait_for(self,predicate):
        # Windows/Qt timer delivery may exceed 20 ms on a busy build host.
        deadline=time.monotonic()+1
        while not predicate() and time.monotonic()<deadline:QTest.qWait(10)
        self.assertTrue(predicate())
    def test_valid_timer_sends_only_version_and_keeps_remaining_deadline(self):
        self.job['cleanup_version_deadline']-=2;self.assertTrue(self.schedule());self.wait_for(lambda:self.owner._start_step.called)
        self.owner._start_step.assert_called_once();command,timeout=self.owner._start_step.call_args.args
        self.assertEqual(command,'hw version');self.assertLessEqual(timeout,18000);self.assertGreater(timeout,0)
        self.assertEqual(self.job['cleanup_version_probe_count'],2);self.failure.assert_not_called()
    def test_replacing_job_cannot_dispatch_an_old_timer_into_new_operation(self):
        self.assertTrue(self.schedule());self.owner.read_job={'stage':'running'};QTest.qWait(20)
        self.owner._start_step.assert_not_called();self.failure.assert_not_called()
    def test_cancelled_queued_callback_cannot_send_after_same_job_is_reused(self):
        self.assertTrue(self.schedule());timer=self.job['cleanup_version_timer'];stop_cleanup_version_checks(self.job)
        timer.timeout.emit();self.owner._start_step.assert_not_called();self.failure.assert_not_called()
    def test_changed_stage_invalidates_callback_without_changing_new_state(self):
        self.assertTrue(self.schedule());self.job['stage']='uncertain';QTest.qWait(20)
        self.owner._start_step.assert_not_called();self.failure.assert_not_called();self.assertEqual(self.job['stage'],'uncertain')
    def test_deadline_expiring_while_waiting_retains_failure_without_command(self):
        self.assertTrue(self.schedule());self.job['cleanup_version_deadline']=time.monotonic()-1;self.wait_for(lambda:self.failure.called)
        self.owner._start_step.assert_not_called();self.failure.assert_called_once()
    def test_maximum_and_expired_budget_do_not_even_schedule_another_probe(self):
        self.job['cleanup_version_probe_count']=8;self.assertFalse(self.schedule())
        self.job['cleanup_version_probe_count']=1;self.job['cleanup_version_deadline']=0;self.assertFalse(self.schedule())
        self.assertNotIn('cleanup_version_timer',self.job);self.owner._start_step.assert_not_called()
    def test_duplicate_scheduling_cannot_create_parallel_version_probes(self):
        self.assertTrue(self.schedule());timer=self.job['cleanup_version_timer'];self.assertTrue(self.schedule())
        self.assertIs(self.job['cleanup_version_timer'],timer);self.wait_for(lambda:self.owner._start_step.called);self.owner._start_step.assert_called_once()
