"""Controller integration without Computer Use, serial ports or real writes."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import PropertyMock
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtCore import QCoreApplication,QEvent
from pm3gui.app import MainWindow
from pm3gui.core import DumpData
from pm3gui.operations import Operation
from pm3gui.hf_catalog import COMMANDS,render_hf_command
from test_writing import Card,classic_image


class HFAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.enterContext(patch('pm3gui.app.available_ports',return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client',return_value=''))
        self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.enterContext(patch('pm3gui.app.QMessageBox.question',return_value=QMessageBox.StandardButton.Cancel))
        self.client=self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.operations=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.operations.side_effect=lambda settings,op,**kwargs:op.render()
        self.window=MainWindow(self.root/'profile')
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        if self.window.read_job and self.window.read_job.get('job_kind')=='write':self.window.cancel_operation()
        self.window.read_job=None;self.window.dirty=False
        for name in ('write_dialog','hf_tools_dialog'):
            if getattr(self.window,name,None) is not None:getattr(self.window,name).close()
        self.window.close();self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents();self.temp.cleanup()

    def source(self):
        data=bytearray(classic_image());data[16:32]=bytes(range(16))
        cid=self.window.library.create_card('Write fixture',DumpData(bytes(data),'bin',{},16),uid='A1B2C3D4',protocol='MIFARE Classic 1K')
        self.window.load_card(cid)
        self.window.open_write_dialog()
        return self.window.write_dialog

    def finish(self,response):
        output,code=response
        self.window.operation_output=output
        self.window._finished(code,'Test response')

    def pump(self,card,until_review=False):
        for _ in range(1000):
            job=self.window.read_job
            if job is None:return
            plan=job['write_plan']
            if plan.pending is None:return
            self.finish(card.respond(plan.pending))
        self.fail('Controller did not terminate')

    def test_preview_is_read_only_then_real_controller_verifies_all_written_bytes(self):
        dialog=self.source()
        self.client.assert_not_called();self.operations.assert_not_called()
        card=Card(classic_image())
        dialog.prepare_button.click()
        self.client.assert_called_once()
        self.pump(card)
        self.assertIsNotNone(self.window.read_job)
        self.assertTrue(dialog.write_button.isEnabled())
        self.assertEqual(card.writes,[])
        self.assertFalse(self.window.read_button.isEnabled())
        self.assertFalse(self.window.operation_progress.isVisible())
        dialog.write_button.click()
        self.pump(card)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(card.writes,[1])
        self.assertIn('проверена чтением',dialog.status.text())
        self.assertTrue(self.window.read_button.isEnabled())
        self.assertEqual(dialog.first.value(),0)
        self.assertEqual(dialog.last.value(),63)
        report=json.loads((dialog.plan.directory/'write-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'verified')

    def test_cancel_at_review_never_sends_write(self):
        dialog=self.source();card=Card(classic_image())
        dialog.prepare_button.click();self.pump(card)
        self.window.cancel_operation()
        self.assertIsNone(self.window.read_job)
        self.assertEqual(card.writes,[])
        self.assertNotIn('mfc_write_block',[call.args[1].kind for call in self.operations.call_args_list])

    def test_uid_change_after_review_unlocks_ui_without_writing(self):
        dialog=self.source();card=Card(classic_image())
        dialog.prepare_button.click();self.pump(card)
        card.uid='11223344';dialog.write_button.click();self.pump(card)
        self.assertIsNone(self.window.read_job)
        self.assertIn('UID',dialog.status.text())
        self.assertEqual(card.writes,[])
        self.assertTrue(self.window.read_button.isEnabled())

    def test_stop_during_a_write_never_replays_it_and_reports_uncertain_effect(self):
        dialog=self.source();card=Card(classic_image())
        dialog.first.setValue(1);dialog.last.setValue(1)
        dialog.prepare_button.click();self.pump(card)
        dialog.write_button.click()
        plan=self.window.read_job['write_plan']
        self.finish(card.respond(plan.pending))  # UID guard -> write pending.
        self.assertEqual(plan.pending.kind,'mfc_write_block')
        before=self.operations.call_count
        with patch('pm3gui.app.ClientRunner.busy',new_callable=PropertyMock,return_value=True),patch.object(self.window.runner,'cancel') as cancel:
            self.window.cancel_operation();cancel.assert_called_once()
        self.finish(('[+] Write ( ok )',0))
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.operations.call_count,before)
        report=json.loads((plan.directory/'write-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['may_have_written'],[1])
        self.assertEqual(report['verified_blocks'],[])
        self.assertEqual(dialog.first.value(),1)
        self.assertEqual(dialog.last.value(),1)

    def test_hf_catalogue_and_help_do_not_contact_hardware(self):
        self.window.open_hf_tools()
        dialog=self.window.hf_tools_dialog
        for word in ('desfire','restore','iclass','15'):
            dialog.search.setText(word)
            self.app.processEvents()
        self.assertGreater(len(COMMANDS),500)
        for name in ('hf mf wrbl','hf mfu wrbl','hf mfdes write','hf iclass wrbl'):
            self.assertIn(name,COMMANDS)
        self.client.assert_not_called();self.operations.assert_not_called()

    def test_native_read_fields_execute_one_typed_command_without_raw_input(self):
        self.window.open_hf_tools();dialog=self.window.hf_tools_dialog
        dialog.search.setText('hf mf rdbl')
        self.assertFalse(dialog.manual.isChecked())
        self.assertFalse(dialog.advanced.header.isChecked())
        form=dialog.parameter_form
        form.fields['blk'].setText('4');form.fields['key'].setText('FF '*6)
        form.key_type.setCurrentIndex(1)
        self.assertIn('Прочитать блок',dialog.action_heading.text())
        dialog.run_button.click()
        self.operations.assert_called_once()
        operation=self.operations.call_args.args[1]
        self.assertEqual(operation.render(),'hf mf rdbl --blk 4 -b --key FFFFFFFFFFFF')

    def test_native_write_asks_for_review_of_fields_and_manual_editor_is_optional(self):
        self.window.open_hf_tools();dialog=self.window.hf_tools_dialog;dialog.search.setText('hf mf wrbl')
        form=dialog.parameter_form;form.fields['blk'].setText('1');form.fields['data'].setText('00 '*16)
        dialog.run_button.click();self.operations.assert_not_called()
        with patch('pm3gui.hf_tools_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Ok) as review:
            dialog.run_button.click()
        self.assertIn('Блок / страница: 1',review.call_args.args[2])
        self.operations.assert_called_once()
        self.assertFalse(dialog.manual.isChecked())

    def test_autowrite_source_picker_preserves_main_editor_and_old_versions(self):
        dialog=self.source();cid=self.window.card_id;old=self.window.loaded_revision
        self.window.library.save_revision(cid,DumpData(classic_image(),'bin',{},16),'New revision')
        index=next(i for i in range(dialog.sources.count()) if dialog.sources.itemData(i)==('card',cid))
        dialog.sources.setCurrentIndex(index)
        self.assertEqual(dialog.dump.data,classic_image())
        dialog.source_versions.setCurrentIndex(dialog.source_versions.findData(old))
        self.assertEqual(dialog.dump.data,self.window.loaded_dump.data)
        self.assertEqual(self.window.loaded_revision,old)
        self.client.assert_not_called();self.operations.assert_not_called()

    def test_autowrite_scenario_can_be_saved_and_loaded_without_hardware(self):
        from pm3gui.write_scenario import load_scenario
        dialog=self.source();path=self.root/'script.pm3write.json'
        dialog.first.setValue(1);dialog.last.setValue(1);dialog.target_uid.setText('A1B2C3D4')
        with patch('pm3gui.write_dialog.QFileDialog.getSaveFileName',return_value=(str(path),'')):dialog.export_scenario()
        dump,options,_=load_scenario(path)
        self.assertEqual(dump.data,dialog.dump.data);self.assertEqual(options.first,1)
        dialog.target_uid.clear()
        with patch('pm3gui.write_dialog.QFileDialog.getOpenFileName',return_value=(str(path),'')):dialog.choose_scenario()
        self.assertEqual(dialog.target_uid.text(),'A1B2C3D4');self.assertEqual(dialog.last.value(),1)
        self.client.assert_not_called();self.operations.assert_not_called()

    def test_tool_requires_confirmation_and_rejects_chaining_and_lf(self):
        self.window.open_hf_tools();dialog=self.window.hf_tools_dialog
        dialog.search.setText('hf mf wrbl')
        dialog.manual.setChecked(True)
        dialog.args.setText('--blk 1 -k FFFFFFFFFFFF -d '+'00'*16)
        dialog.run_button.click()  # Cancelled by the question fixture.
        self.client.assert_not_called();self.operations.assert_not_called()
        for name,args in [('lf search',''),('hf mf wrbl','--blk 1; hf mf wipe'),('hf mf wrbl','-f "unclosed')]:
            with self.assertRaises(ValueError):render_hf_command(name,args)
        with patch('pm3gui.hf_tools_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Ok):
            dialog.run_button.click()
        self.operations.assert_called_once()
        operation=self.operations.call_args.args[1]
        self.assertEqual(operation.kind,'hf_tool')
        self.assertEqual(operation.hf_command,'hf mf wrbl')
        self.assertIn('--blk 1',operation.render())


if __name__=='__main__':unittest.main()
