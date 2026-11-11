"""Source/blank UX and approval guards, using independent synthetic cards."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,PropertyMock
from types import SimpleNamespace
import json
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QLabel
from pm3gui.app import MainWindow
from pm3gui.core import DumpData
from pm3gui.operations import Detection,CLASSIC_GEOMETRIES
from pm3gui.transfer import source_facts,hf_checks
from pm3gui.transfer_ui import TransferPanel
from pm3gui.i18n import set_language
from pm3gui.interface_mode import Guidance
import test_writing as fixture
import test_family_wizard as families
import test_lab_features as lab


class TransferFactsTests(unittest.TestCase):
    def test_all_classic_geometries_are_described_without_inventing_uid(self):
        for name,(_,size) in CLASSIC_GEOMETRIES.items():
            f=source_facts(DumpData(bytes(size),'bin',{},16))
            self.assertEqual((f.family,f.geometry,f.size,f.uid),('mfc',name,size,''))

    def test_raw_ntag_memory_is_supported_with_default_bin_block_size(self):
        f=source_facts(DumpData(bytes(180),'bin',{},16))
        self.assertEqual(f.family,'mfu');self.assertEqual(f.uid,'')

    def test_library_identity_is_separate_from_dump_bytes(self):
        f=source_facts(DumpData(fixture.classic_image(),'bin',{},16),{'name':'Saved source','uid':'01020304','protocol':'MIFARE Classic 1K'})
        self.assertEqual(f.uid,'01020304');self.assertEqual(f.name,'Saved source')

    def test_identifier_and_structured_records_do_not_claim_memory_coverage(self):
        for dump in (DumpData(bytes(5),'bin',{'protocol':'EM410x'},1),
                     DumpData(b'{}','json',{'protocol':'DESFire','structured_document':{'files':[]}},1)):
            f=source_facts(dump);self.assertIsNone(f.known)

    def test_malformed_metadata_is_an_explained_blocker(self):
        f=source_facts(DumpData(bytes(1024),'bin',{'source_json':[],'partial_dump':'bad'},16))
        self.assertTrue(f.error);self.assertEqual(hf_checks(f)[0].state,'blocked')

    def test_mismatched_profile_is_blocked_before_device_commands(self):
        f=source_facts(DumpData(fixture.classic_image(),'bin',{},16))
        self.assertTrue(any(c.title and c.state=='blocked' for c in hf_checks(f,options=SimpleNamespace(family='mfu',include_unknown=False))))

    def test_large_to_small_and_small_to_large_both_explain_exact_capacity(self):
        f=source_facts(DumpData(fixture.classic_image(),'bin',{},16))
        for size,bytes_ in (('mini',320),('4k',4096)):
            plan=SimpleNamespace(target=Detection('mfc','A1B2C3D4','MIFARE Classic',size,True),ready=False,error='',done=False)
            reason=next(c for c in hf_checks(f,plan) if c.state=='blocked')
            self.assertIn('1024',reason.detail);self.assertIn(str(bytes_),reason.detail)

    def test_wrong_target_family_is_not_compatible(self):
        f=source_facts(DumpData(fixture.classic_image(),'bin',{},16))
        plan=SimpleNamespace(target=Detection('mfu','01020304','NTAG 213',supported=True),ready=False,error='',done=False)
        self.assertTrue(any(c.state=='blocked' for c in hf_checks(f,plan)))

    def test_partial_source_and_override_are_visible_without_exposing_keys(self):
        dump=DumpData(fixture.classic_image(),'bin',fixture.partial_metadata(range(32)),16)
        f=source_facts(dump)
        for override in (False,True):
            rows=hf_checks(f,options=SimpleNamespace(family='mfc',include_unknown=override))
            self.assertTrue(any(c.state=='limit' for c in rows))
            self.assertNotIn('FFFFFFFFFFFF',' '.join(c.detail for c in rows))

    def test_identity_alone_never_confirms_access_or_backup(self):
        f=source_facts(DumpData(fixture.classic_image(),'bin',{},16))
        plan=SimpleNamespace(target=Detection('mfc','01020304','MIFARE Classic','1k',True),ready=False,error='',done=False)
        self.assertTrue(any(c.state=='pending' for c in hf_checks(f,plan)))


class TransferWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.enterContext(patch('pm3gui.app.available_ports',return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client',return_value=''))
        self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.run=self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.ops=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.w=MainWindow(self.root/'profile',language='en');self.addCleanup(self.cleanup)
    def cleanup(self):
        if self.w.read_job and self.w.read_job.get('flow'):self.w.read_job['flow'].close()
        self.w.read_job=None;self.w.dirty=False
        for name in ('write_dialog','family_wizard','lf_write_dialog'):
            d=getattr(self.w,name,None)
            if d is not None:d.close();d.deleteLater()
        self.w.close();self.w.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents()
        set_language('ru');self.temp.cleanup()
    def finish(self,out='',code=0):self.w.operation_output=out;self.w._finished(code,'Synthetic response')
    def source(self,data=None,metadata=None):
        raw=bytearray(data or fixture.classic_image());raw[16]=41
        cid=self.w.library.create_card('Saved source',DumpData(bytes(raw),'bin',metadata or {},16),uid='11223344',protocol='MIFARE Classic 1K')
        self.w.load_card(cid);self.w.open_write_dialog();return self.w.write_dialog
    def pump(self,card):
        for _ in range(1000):
            job=self.w.read_job
            if job is None:return
            plan=job.get('write_plan')
            if plan is None or plan.pending is None:return
            self.finish(*card.respond(plan.pending))
        self.fail('HF flow did not stop at a boundary')
    def prepare(self):
        d=self.source();d.first.setValue(1);d.last.setValue(1)
        d.prepare_button.click();card=fixture.Card(fixture.classic_image());self.pump(card)
        self.assertTrue(d.plan.ready);return d,card

    def test_two_cards_are_separate_and_opening_does_not_access_device(self):
        d=self.source()
        self.assertIn('11223344',d.transfer.source.text());self.assertNotIn('11223344',d.transfer.target.text())
        self.assertEqual(d.transfer.stage,0);self.run.assert_not_called();self.ops.assert_not_called()

    def test_full_hf_flow_rechecks_target_and_verifies_result(self):
        d,card=self.prepare();self.assertEqual(d.transfer.status,'ready')
        self.assertIn(fixture.UID,d.transfer.target.text());self.assertEqual(card.writes,[])
        d.write_button.click();self.pump(card)
        self.assertEqual(card.writes,[1]);self.assertEqual(d.transfer.status,'verified')
        self.assertEqual(d.dump.data[16],41);self.assertEqual(d.transfer.stage,4)

    def test_frozen_mode_probe_uses_real_controls_without_reader_access(self):
        from pm3gui.release_check import check_interface_modes
        self.source().close()
        result=check_interface_modes(self.w)
        self.assertTrue(result['passed']);self.assertFalse(result['physical_device_access'])
        self.assertEqual(self.w.interface_mode,'guided');self.assertIsNone(self.w.read_job)
        self.run.assert_not_called();self.ops.assert_not_called()

    def test_mode_switch_at_review_preserves_plan_and_requires_manual_approval(self):
        d,card=self.prepare();plan=d.plan;token=d.source_token()
        self.w.interface_mode_selector.setCurrentIndex(self.w.interface_mode_selector.findData('compact'))
        self.assertIs(d.plan,plan);self.assertTrue(d.configuration_unchanged(plan,token));self.assertEqual(card.writes,[])
        self.assertTrue(d.write_button.isEnabled());self.assertTrue(d.transfer.compact)
        self.assertFalse(d.transfer.details.header.isChecked());self.assertFalse(d.transfer.stage_strip.isVisible())
        self.assertIn('UID',d.transfer.important.findChildren(QLabel)[-1].text())
        d.write_button.click();self.pump(card);self.assertEqual(card.writes,[1]);self.assertEqual(d.transfer.status,'verified')

    def test_compact_mode_keeps_blocker_visible_outside_collapsed_details(self):
        self.w.interface_mode_selector.setCurrentIndex(1);d=self.source();d.prepare_button.click()
        self.pump(fixture.Card(bytes(4096)))
        self.assertFalse(d.write_button.isEnabled());self.assertTrue(d.transfer.important.isVisible())
        self.assertIn('4096',' '.join(l.text() for l in d.transfer.important.findChildren(QLabel)))
        self.assertFalse(d.transfer.details.header.isChecked())

    def test_compact_partial_placeholder_warning_is_not_hidden(self):
        self.w.interface_mode_selector.setCurrentIndex(1)
        d=self.source(metadata=fixture.partial_metadata(range(32)));d.include_unknown.setChecked(True)
        text=' '.join(l.text() for l in d.transfer.important.findChildren(QLabel) if not l.isHidden())
        self.assertIn('placeholder',text.lower());self.assertTrue(d.transfer.important.isVisible())

    def test_mode_persists_per_profile_and_preserves_other_settings(self):
        self.w.settings_path.write_text(json.dumps({'language':'en','future_setting':{'keep':True}}))
        self.w.interface_mode_selector.setCurrentIndex(1)
        settings=json.loads(self.w.settings_path.read_text())
        self.assertEqual(settings,{'language':'en','future_setting':{'keep':True},'interface_mode':'compact'})
        self.assertEqual(self.w._load_settings()['interface_mode'],'compact')
        self.run.assert_not_called();self.ops.assert_not_called()
        self.w.settings_path=self.root/'other-profile.json'
        self.assertEqual(self.w._load_settings()['interface_mode'],'guided')

    def test_failed_mode_save_rolls_back_selector_and_all_views(self):
        d=self.source()
        with patch('pm3gui.interface_mode.save_interface_mode',side_effect=OSError('Synthetic read-only profile')):
            self.w.interface_mode_selector.setCurrentIndex(1)
        self.assertEqual(self.w.interface_mode,'guided');self.assertFalse(d.transfer.compact)
        self.assertEqual(self.w.interface_mode_selector.currentData(),'guided')
        self.assertFalse(self.w.interface_mode_notice.isHidden());self.ops.assert_not_called()

    def test_invalid_saved_mode_uses_guided_view(self):
        for value in ('unknown',[],None,3):
            self.w.settings_path.write_text(json.dumps({'interface_mode':value}))
            self.assertEqual(self.w._load_settings()['interface_mode'],'guided')

    def test_mode_change_after_dialog_destruction_and_reopen_is_safe(self):
        d=self.source();d.close();d.deleteLater();self.w.write_dialog=None
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.w.interface_mode_selector.setCurrentIndex(1)
        self.w.open_write_dialog();self.assertTrue(self.w.write_dialog.transfer.compact)
        self.run.assert_not_called();self.ops.assert_not_called()

    def test_general_guidance_can_be_opened_in_compact_and_restored_live(self):
        hints=self.w.findChildren(Guidance);self.assertGreaterEqual(len(hints),10)
        self.w.interface_mode_selector.setCurrentIndex(1)
        for hint in hints:self.assertFalse(hint.header.isChecked());self.assertFalse(hint.header.isHidden())
        hints[0].setExpanded(True);self.assertTrue(hints[0].header.isChecked())
        self.w.interface_mode_selector.setCurrentIndex(0)
        for hint in hints:self.assertTrue(hint.header.isChecked());self.assertTrue(hint.header.isHidden())
        self.run.assert_not_called();self.ops.assert_not_called()

    def test_no_change_is_reported_without_sending_a_write(self):
        d=self.source();card=fixture.Card(d.dump.data)
        d.first.setValue(1);d.last.setValue(1);d.prepare_button.click();self.pump(card)
        d.write_button.click();self.pump(card)
        self.assertEqual(card.writes,[]);self.assertIn('No write',d.status.text())

    def test_hf_close_before_identity_waits_for_cancel_then_closes(self):
        d=self.source();d.prepare_button.click()
        with patch.object(type(self.w.runner),'busy',new_callable=PropertyMock,return_value=True),patch.object(self.w.runner,'cancel') as cancel:
            d.reject();cancel.assert_called_once();self.assertIsNotNone(self.w.read_job)
            self.assertTrue(d.isVisible())
        self.finish('',-1);self.app.processEvents()
        self.assertIsNone(self.w.read_job);self.assertFalse(d.isVisible())
        self.assertEqual(d.transfer.status,'stopped');self.assertIsNotNone(d.dump)

    def test_hf_close_at_review_never_writes_and_closes(self):
        d,card=self.prepare();d.reject();self.app.processEvents()
        self.assertIsNone(self.w.read_job);self.assertFalse(d.isVisible());self.assertEqual(card.writes,[])

    def test_hf_identification_error_preserves_source_and_releases_controller(self):
        d=self.source();d.prepare_button.click();self.finish('[!] No card found',-1)
        self.assertIsNone(self.w.read_job);self.assertIsNotNone(d.dump)
        self.assertFalse(d.write_button.isEnabled());self.assertEqual(d.transfer.status,'stopped')

    def test_changed_settings_after_review_cannot_approve_old_plan(self):
        d,card=self.prepare();d.first.setValue(2)
        self.w.approve_card_write(d);self.assertEqual(card.writes,[])
        self.assertFalse(d.write_button.isEnabled());self.assertEqual(d.transfer.status,'stopped')

    def test_changed_source_bytes_after_review_cannot_approve_old_plan(self):
        d,card=self.prepare();d.dump=DumpData(bytes(1024),'bin',{},16)
        self.w.approve_card_write(d);self.assertEqual(card.writes,[])
        self.assertFalse(d.write_button.isEnabled())

    def test_different_size_explains_rejection_and_preserves_source(self):
        d=self.source();before=d.dump.data;d.prepare_button.click()
        self.pump(fixture.Card(bytes(4096)))
        self.assertEqual(d.dump.data,before);self.assertEqual(d.transfer.status,'stopped')
        self.assertIn('4096',d.transfer.explanation.text());self.assertFalse(d.write_button.isEnabled())

    def test_auth_failure_has_no_success_state_and_keeps_retry_source(self):
        d=self.source();d.prepare_button.click();self.finish('[+] UID: '+fixture.UID+'\nMIFARE Classic 1K\n')
        for _ in range(20):
            if self.w.read_job is None:break
            self.finish('[!] Authentication failed',-1)
        self.assertIsNotNone(d.dump);self.assertEqual(d.transfer.status,'stopped');self.assertFalse(d.write_button.isEnabled())

    def test_uid_swap_after_approval_never_writes(self):
        d,card=self.prepare();card.uid='01010101';d.write_button.click();self.pump(card)
        self.assertEqual(card.writes,[]);self.assertEqual(d.transfer.status,'stopped')

    def test_verification_error_is_not_a_completed_copy(self):
        d,card=self.prepare();d.write_button.click()
        for _ in range(10):
            job=self.w.read_job
            if job is None:break
            plan=job['write_plan'];op=plan.pending
            if plan.phase=='verify':card.data[16]=99
            self.finish(*card.respond(op))
        self.assertEqual(card.writes,[1]);self.assertEqual(d.transfer.status,'stopped')

    def test_other_hf_family_is_routed_to_its_existing_wizard(self):
        from pm3gui.studio_data import structured_dump
        cid=self.w.library.create_card('DESFire source',structured_dump({'files':[]},'DESFire',fixture.UID),uid=fixture.UID,protocol='DESFire')
        self.w.load_card(cid);self.w.open_write_dialog();d=self.w.write_dialog
        self.assertFalse(d.prepare_button.isEnabled());self.assertEqual(d.transfer.status,'blocked')
        d.family_button.click();self.assertEqual(self.w.family_wizard.operation.currentData(),'desfire')
        self.assertEqual(self.w.family_wizard.sources.record['uid'],fixture.UID)
        self.assertIn(fixture.UID,self.w.family_wizard.transfer.source.text())
        self.run.assert_not_called();self.ops.assert_not_called()

    def test_pair_reflows_for_a_small_window_and_english_is_complete(self):
        d=self.source();panel=TransferPanel();panel.set_source(d.dump);panel.resize(450,700);panel.show();self.app.processEvents()
        self.addCleanup(panel.deleteLater)
        self.assertEqual(panel.cards.getItemPosition(panel.cards.indexOf(panel.target_box))[:2],(1,0))
        import re
        self.assertEqual([l.text() for l in panel.findChildren(QLabel) if re.search('[А-Яа-яЁё]',l.text())],[])

    def prepare_lf(self):
        self.w.library.create_card('LF source',DumpData(bytes.fromhex('0123456789'),'bin',{'protocol':'EM410x'},1),protocol='EM410x',uid='0123456789')
        self.w.open_lf_write();d=self.w.lf_write_dialog;d.prepare.click();card=lab.T55Card()
        self.pump_lf(card);return d,card

    def pump_lf(self,card):
        for _ in range(100):
            job=self.w.read_job
            if job is None or job['stage']=='review':return
            if job['stage'].startswith('cleanup_'):
                self.finish(families.VERSION if job['stage']=='cleanup_confirm' else '')
            else:self.finish(*card.respond(job['plan'].pending[1]))
        self.fail('LF did not settle')

    def test_lf_complete_flow_shows_t5577_and_verifies_identifier(self):
        d,card=self.prepare_lf();self.assertEqual(card.writes,0)
        self.assertEqual(d.transfer.status,'ready');self.assertIn('T5577',d.transfer.target.text())
        self.assertNotIn('0123456789',d.transfer.target.text())
        d.write.click();self.pump_lf(card)
        self.assertEqual(card.writes,1);self.assertEqual(d.transfer.status,'verified')

    def test_compact_lf_keeps_the_review_gate_and_verifies_result(self):
        self.w.interface_mode_selector.setCurrentIndex(1);d,card=self.prepare_lf()
        self.assertTrue(d.transfer.compact);self.assertEqual(card.writes,0);self.assertTrue(d.write.isEnabled())
        d.write.click();self.pump_lf(card)
        self.assertEqual(card.writes,1);self.assertEqual(d.transfer.status,'verified')

    def test_lf_settings_change_after_review_never_clones(self):
        d,card=self.prepare_lf();d.clock.setCurrentIndex(1)
        self.w.approve_lf_write(d);self.pump_lf(card)
        self.assertEqual(card.writes,0);self.assertNotEqual(d.transfer.status,'verified')

    def test_lf_close_at_review_waits_for_rf_cleanup_then_closes(self):
        d,card=self.prepare_lf();d.reject();self.assertIsNotNone(self.w.read_job)
        self.pump_lf(card);self.app.processEvents()
        self.assertIsNone(self.w.read_job);self.assertFalse(d.isVisible());self.assertEqual(card.writes,0)

    def test_lf_ordinary_read_only_card_is_not_called_a_writable_blank(self):
        self.w.library.create_card('LF source',DumpData(bytes.fromhex('0123456789'),'bin',{'protocol':'EM410x'},1),protocol='EM410x')
        self.w.open_lf_write();d=self.w.lf_write_dialog;d.prepare.click()
        self.finish('[+] EM 410x ID: 0123456789',0)
        self.pump_lf(lab.T55Card())
        self.assertFalse(d.write.isEnabled());self.assertNotEqual(d.transfer.status,'verified')

    def test_ultralight_write_preserves_service_pages_and_verifies_user_page(self):
        image=bytearray(180);image[16:20]=b'TEST'
        cid=self.w.library.create_card('NTAG',DumpData(bytes(image),'bin',{'read_validation':{'kind':'mfu'}},4),protocol='NTAG 213')
        self.w.load_card(cid);self.w.open_write_dialog();d=self.w.write_dialog
        d.first.setValue(4);d.last.setValue(4);d.prepare_button.click()
        card=fixture.Card(bytes(180),'mfu');self.pump(card)
        self.assertEqual(d.transfer.status,'ready');self.assertIn('NTAG 213',d.transfer.target.text())
        d.write_button.click();self.pump(card)
        self.assertEqual(card.writes,[4]);self.assertEqual(d.transfer.status,'verified')


class FamilyTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    setUp=families.PreparedWizardUiTests.setUp
    cleanup=families.PreparedWizardUiTests.cleanup
    finish=families.PreparedWizardUiTests.finish
    pending=families.PreparedWizardUiTests.pending
    prepare_mfp=families.PreparedWizardUiTests.prepare_mfp
    cleanup_rf=families.PreparedWizardUiTests.cleanup_rf

    def test_all_existing_family_modes_have_pair_panel_without_rf(self):
        d=self.w.open_family_wizard()
        for mode in (0,1):
            self.w.interface_mode_selector.setCurrentIndex(mode)
            for i in range(d.operation.count()):
                d.operation.setCurrentIndex(i);self.assertEqual(d.transfer.status,'pending')
        self.run.assert_not_called();self.ops.assert_not_called()

    def test_manual_uid_source_description_survives_mode_changes(self):
        d=self.w.open_family_wizard(operation='uid');before=d.transfer.source.text()
        self.w.interface_mode_selector.setCurrentIndex(1)
        self.assertEqual(d.transfer.source.text(),before);self.assertIn('UID',before)

    def test_review_shows_confirmed_scope_and_target_identity(self):
        d,card=self.prepare_mfp();self.assertEqual(d.transfer.status,'ready')
        self.assertIn(card.uid,d.transfer.target.text());self.assertEqual(card.writes,[])

    def test_changed_payload_invalidates_even_direct_controller_approval(self):
        d,card=self.prepare_mfp();digest=d.gate.digest;d.payload.setPlainText('BB'*16)
        self.assertFalse(d.commit.isEnabled());self.w.approve_studio_review(digest)
        self.assertEqual(card.writes,[]);self.cleanup_rf()
        self.assertNotEqual(d.transfer.status,'verified')

    def test_completed_family_write_has_verified_state_and_retains_source(self):
        d,card=self.prepare_mfp();source=d.payload.toPlainText();d.commit.click()
        for _ in range(15):
            if self.w.read_job['stage'].startswith('cleanup_'):break
            self.finish(*card.respond(self.pending()))
        self.cleanup_rf();self.assertEqual(d.transfer.status,'verified');self.assertEqual(d.payload.toPlainText(),source)

    def test_compact_family_mode_retains_review_and_verification(self):
        self.w.interface_mode_selector.setCurrentIndex(1);d,card=self.prepare_mfp()
        self.assertTrue(d.transfer.compact);self.assertEqual(card.writes,[])
        d.commit.click()
        for _ in range(15):
            if self.w.read_job['stage'].startswith('cleanup_'):break
            self.finish(*card.respond(self.pending()))
        self.cleanup_rf();self.assertEqual(d.transfer.status,'verified');self.assertEqual(len(card.writes),1)
        self.assertEqual(card.data,b'\xA5'*16)

    def test_form_recreation_does_not_include_deleted_widgets_in_signature(self):
        d=self.w.open_family_wizard();d.operation.setCurrentIndex(d.operation.findData('block:MIFARE Plus'))
        d.payload.setPlainText('A5'*16);before=d.configuration_signature()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.assertEqual(before,d.configuration_signature())

    def test_family_close_at_review_waits_for_rf_cleanup_without_writing(self):
        d,card=self.prepare_mfp();d.reject();self.assertIsNotNone(self.w.read_job)
        self.cleanup_rf();self.app.processEvents()
        self.assertIsNone(self.w.read_job);self.assertFalse(d.isVisible());self.assertEqual(card.writes,[])
