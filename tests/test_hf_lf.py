"""RF band separation without serial access or physical writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QMessageBox
from pm3gui.app import MainWindow
from pm3gui.card_band import card_band
from pm3gui.core import DumpData
from pm3gui.device import DeviceSettings,build_operation_invocation
from pm3gui.operations import Detection,Operation,identity_dump
from pm3gui.lf_catalog import COMMANDS,render_lf_command
from pm3gui.hf_forms import parameters,form_arguments
from pm3gui.hf_tools_ui import ParameterForm
from test_writing import classic_image,UID


class BandClassificationTests(unittest.TestCase):
    def test_existing_protocols_and_metadata_are_classified_without_size_or_uid_guesses(self):
        for protocol,metadata,expected in (
            ('MIFARE Classic 4K',{},'hf'),('HID iCLASS',{},'hf'),('HID Prox',{},'lf'),('EM410x',{},'lf'),
            ('T5577',{},'lf'),('ISO14443-A',{},'hf'),('unknown',{'source_json':{'FileType':'t55x7'}},'lf'),
            ('',{'source_json':{'FileType':'15693 v4'}},'hf'),('',{'read_validation':{'kind':'mfc'}},'hf'),
            ('',{'record_kind':'identifier','protocol':'EM410x'},'lf'),('',{},'unknown'),
            ('',{'source_json':None,'read_validation':None},'unknown')):
            with self.subTest(protocol=protocol,metadata=metadata):
                self.assertEqual(card_band({'protocol':protocol,'uid':'0123456789','size':1024},metadata),expected)

    def test_lf_catalogue_is_typed_and_cannot_accept_hf_or_command_chains(self):
        self.assertEqual(len(COMMANDS),251)
        for cmd in COMMANDS:
            self.assertTrue(cmd.startswith('lf '));fields=parameters(cmd)
            self.assertEqual(len({p.name for p in fields}),len(fields),cmd)
        args=form_arguments('lf em 410x clone',{'id':'01 23 45 67 89','q5':True})
        op=Operation('lf_tool',lf_command='lf em 410x clone',lf_args=args)
        self.assertEqual(op.render(),'lf em 410x clone --id 0123456789 --q5')
        settings=DeviceSettings(__import__('sys').executable,'COM123')
        argv=build_operation_invocation(settings,op)[1]
        self.assertEqual(argv[argv.index('-c')+1],op.render())
        for cmd,arguments in (('hf search',''),('lf search','; hf mf wipe'),('lf search','\nquit')):
            with self.assertRaises(ValueError):render_lf_command(cmd,arguments)
        with self.assertRaises(ValueError):Operation('hf_tool',hf_command='lf search').render()
        with self.assertRaises(ValueError):Operation('lf_tool',lf_command='lf search',hf_args='--x').render()


class BandAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        for name,value in (('available_ports',[]),('discover_client','')):
            self.enterContext(patch('pm3gui.app.'+name,return_value=value))
        self.warning=self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.enterContext(patch('pm3gui.app.QMessageBox.question',return_value=QMessageBox.StandardButton.Cancel))
        self.run=self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.operations=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.operations.side_effect=lambda settings,operation,**kwargs:operation.render()
        self.window=MainWindow(Path(self.temp.name));self.addCleanup(self.cleanup)

    def cleanup(self):
        window=self.window;window.read_job=None;window.dirty=False
        for name in ('hf_tools_dialog','lf_tools_dialog','write_dialog'):
            if getattr(window,name,None) is not None:getattr(window,name).close()
        window.close();window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents();self.temp.cleanup()

    def seed(self):
        w=self.window
        hf=w.library.create_card('HF',DumpData(classic_image(),'bin',{},16),uid=UID,protocol='MIFARE Classic 1K')
        lf=w.library.create_card('LF',identity_dump(Detection('em410x','0123456789','EM410x',supported=True)),uid='0123456789',protocol='EM410x')
        unknown=w.library.create_card('Unknown',DumpData(classic_image(),'bin',{},16))
        w.refresh_library();return hf,lf,unknown

    def select_band(self,band):
        tabs=self.window.library_bands;tabs.setCurrentIndex(next(i for i in range(tabs.count()) if tabs.tabData(i)==band))

    def finish(self,text,code=0):self.window.operation_output=text;self.window._finished(code,'Test response')

    def test_filters_preserve_unknown_entries_and_search_combines_with_band(self):
        w=self.window;self.seed()
        self.assertEqual(w.cards.count(),3)
        for band,name in (('hf','HF'),('lf','LF'),('unknown','Unknown')):
            self.select_band(band);self.assertEqual(w.cards.count(),1);self.assertIn(name,w.cards.item(0).text())
        self.select_band('hf');w.search.setText('LF');self.assertEqual(w.cards.count(),0)
        self.select_band('lf');self.assertEqual(w.cards.count(),1)
        self.run.assert_not_called();self.operations.assert_not_called()

    def test_tabs_change_recent_cards_and_never_send_device_commands(self):
        w=self.window;self.seed();self.assertIn('HF',w.recent_cards.item(0).text())
        w.workspace_bands.setCurrentIndex(1);self.assertIn('LF',w.recent_cards.item(0).text())
        w.open_band_library();self.assertEqual(w.library_bands.tabData(w.library_bands.currentIndex()),'lf')
        self.run.assert_not_called();self.operations.assert_not_called()

    def test_lf_button_starts_lf_directly_and_saves_confirmed_identifier_in_lf(self):
        w=self.window;w.lf_read_button.click();self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[1],'lf search')
        self.finish('[+] EM 410x ID: 0123456789\n');self.assertEqual(self.operations.call_args.args[1].kind,'lf_em410x_read')
        self.finish('[+] EM 410x ID: 0123456789\n');self.assertIsNone(w.read_job)
        cards=w.library.list_cards();self.assertEqual(len(cards),1);self.assertEqual(cards[0]['rf_band'],'lf')
        self.assertEqual(w.library.get_dump(cards[0]['id']).metadata['record_kind'],'identifier');self.warning.assert_not_called()

    def test_hf_button_never_falls_back_to_lf_and_busy_job_cannot_start_a_second_read(self):
        w=self.window;w.read_button.click();self.assertEqual(self.run.call_args.args[1],'hf search')
        w.read_lf_card();self.assertEqual(self.run.call_count,1)
        self.finish('[-] No known/supported 13.56 MHz tags found.\n')
        self.assertIsNone(w.read_job);self.assertEqual(self.run.call_count,1)

    def test_loading_lf_reveals_its_category_and_hf_writer_excludes_lf_sources(self):
        w=self.window;hf,lf,_=self.seed();self.select_band('hf');w.load_card(lf)
        self.assertEqual(w.library_bands.tabData(w.library_bands.currentIndex()),'lf');self.assertEqual(w.write_card_button.text(),'Запись LF…')
        self.assertTrue(w.card_editors.widget(1).isHidden());self.assertTrue(w.card_keys_button.isHidden())
        w.open_write_dialog();self.assertNotIn(('card',lf),[w.write_dialog.sources.itemData(i) for i in range(w.write_dialog.sources.count())])
        self.assertIn(('card',hf),[w.write_dialog.sources.itemData(i) for i in range(w.write_dialog.sources.count())])
        w.write_dialog.close();w.load_card(hf)
        self.assertFalse(w.card_editors.widget(1).isHidden());self.assertFalse(w.card_keys_button.isHidden())

    def test_lf_write_review_prefills_id_and_exclusive_target_without_automatic_execution(self):
        w=self.window;_,lf,_=self.seed();w.load_card(lf);w.open_lf_tools(write=True);dialog=w.lf_tools_dialog
        self.assertEqual(dialog.band,'lf');self.assertEqual(dialog.parameter_form.fields['id'].text(),'0123456789')
        target=dialog.parameter_form.target_type
        target.setCurrentIndex(target.findData('q5'));target.setCurrentIndex(target.findData('em'))
        op=dialog.operation();self.assertIn('--em',op.render());self.assertNotIn('--q5',op.render())
        self.run.assert_not_called();self.operations.assert_not_called();dialog.run_button.click();self.operations.assert_not_called()
        with patch('pm3gui.hf_tools_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Ok) as review:
            dialog.run_button.click()
        self.assertIn('EM4305',review.call_args.args[2]);self.operations.assert_called_once()
        self.assertEqual(self.operations.call_args.args[1].kind,'lf_tool')

    def test_all_lf_forms_construct_without_contacting_hardware(self):
        for cmd in COMMANDS:
            form=ParameterForm(cmd,lambda:None);form.deleteLater()
        self.run.assert_not_called();self.operations.assert_not_called()


if __name__=='__main__':unittest.main()
