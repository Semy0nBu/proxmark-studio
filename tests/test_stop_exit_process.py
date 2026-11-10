"""Stop -> real child completion -> fresh version -> close, without hardware."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication
from pm3gui.app import MainWindow
import test_persistent as fixtures


class StopExitProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    wait=fixtures.PersistentTests.wait

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.script=self.root/'fake.py';self.script.write_text(fixtures.FAKE,encoding='utf-8')
        for target, options in (
            ('pm3gui.app.available_ports',{'return_value':[]}),
            ('pm3gui.app.discover_client',{'return_value':''}),
            ('pm3gui.app.QMessageBox.warning',{}),
            ('pm3gui.app.QMessageBox.information',{}),
        ):self.enterContext(patch(target,**options))
        self.window=MainWindow(self.root/'profile');self.runner=self.window.runner
        self.runner._argument_prefix=[str(self.script)]
        self.window.client_path.setText(getattr(sys,'_base_executable',sys.executable))
        self.window.ports.setEditText('COM123');self.window.keep_connection.setChecked(True)

    def tearDown(self):
        self.runner.shutdown();self.window.read_job=None;self.window.antenna_job=None
        self.window.dirty=False;self.window.close();self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents();self.temp.cleanup()

    def configure(self,**values):
        (self.root/'config.json').write_text(json.dumps(values),encoding='utf-8')

    def exercise(self,*,transport='usb',soft=True,bad_version=False,early=False):
        config={'channel':'USB-CDC' if transport=='usb' else 'FPC UART',
                'bulk_wait':True,'bulk_cancel':soft}
        self.configure(**config)
        self.window.transport_combo.setCurrentIndex(self.window.transport_combo.findData(transport))
        self.window.ports.setEditText('COM123')
        self.window.read_hf_card()
        if not early:self.wait(lambda:'BULK_WAITING_FOR_CONTROL' in self.window.operation_output)
        old=self.window._step_token
        if bad_version:self.configure(**config,version_output='Client banner only')
        self.window.cancel_operation()
        self.wait(lambda:not self.runner.busy and (self.window.read_job is None or
                   self.window.read_job.get('stage')=='cancel_resync_failed'))
        if bad_version:
            self.assertEqual(self.window.read_job['stage'],'cancel_resync_failed')
        else:
            self.assertIsNone(self.window.read_job)
            self.assertTrue(self.window.ports.isEnabled())
        self.assertIsNot(self.window._step_token,old)
        self.assertEqual(self.window.library.list_cards(),[])
        trace=[json.loads(line)[1] for line in (self.root/'trace.jsonl').read_text().splitlines()]
        self.assertEqual(trace.count('hf search'),0 if early else 1)
        self.assertGreaterEqual(trace.count('hw version'),1 if early else 2)
        self.assertFalse(any(command.startswith('hf mf') for command in trace))
        event=QCloseEvent();self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        if bad_version:
            status=json.loads((self.window.data_dir/'last-exit-status.json').read_text())
            self.assertFalse(status['device_stop_confirmed'])

    def test_usb_framed_stop_finishes_and_closes(self):self.exercise()

    def test_usb_legacy_hard_stop_finishes_and_closes(self):self.exercise(soft=False)

    def test_blueshark_framed_stop_finishes_and_closes(self):self.exercise(transport='blueshark')

    def test_failed_fresh_check_still_allows_exit(self):self.exercise(bad_version=True)

    def test_stop_before_first_client_output_still_finishes_and_closes(self):self.exercise(early=True)
