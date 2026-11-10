"""Integrated native Qt panels and real controller routing with synthetic I/O."""
from dataclasses import asdict
import unittest
from PySide6.QtWidgets import QApplication, QPushButton
import test_qt_bridge as fixtures
from test_write_readback import VERSION
from pm3gui.core import DumpData
from pm3gui.operation_recovery import State


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        fixtures.QtBridgeTests.setUp(self)
        self.bridge=self.window.workspace_bridge
    cleanup=fixtures.QtBridgeTests.cleanup
    pump=fixtures.QtBridgeTests.pump

    def review(self):
        self.window.open_write_dialog();dialog=self.window.write_dialog
        dialog.first.setValue(1);dialog.last.setValue(1);dialog.prepare_button.click()
        card=fixtures._fixtures.Card(fixtures._fixtures.classic_image())
        self.pump(card)
        self.assertTrue(dialog.plan.ready)
        return dialog,dialog.plan,card,self.window.read_job['workspace_epoch']

    def finish_studio(self,card,fail_cleanup=False):
        for _ in range(100):
            job=self.window.read_job
            if job is None:return
            self.assertEqual(job['job_kind'],'studio')
            if job['stage']=='uncertain':return
            request=job.get('request')
            if job['stage'].startswith('cleanup_'):
                output,code=(VERSION,0) if job['stage']=='cleanup_version' else ('',0)
                if fail_cleanup and job['stage']=='cleanup_version':output,code='UART failed',-5
            else:
                op=request.operation()
                if op.kind=='expert_tool':output,code=(VERSION,0) if op.client_command=='hw version' else card.respond('hf search')
                else:output,code=card.respond(op)
            self.window.operation_output=output
            self.window._step_completed(self.window._step_token,code,'synthetic')
        self.fail('Readback did not release the controller')

    def test_native_tabs_normalize_masked_keys_and_do_no_io(self):
        self.window.library.save_revision(self.card_id,DumpData(bytes(self.image),'bin',{'read_validation':{'kind':'mfc'}},16),'masked read')
        self.window.load_card(self.card_id);self.window.card_workspace.refresh()
        self.assertIn('832',self.window.card_workspace.coverage.text())
        tiles=self.window.card_workspace.passport.findChildren(QPushButton)
        self.assertEqual(len([b for b in tiles if b.text().isdigit()]),16)
        self.assertTrue(self.window.card_workspace.map_button.isEnabled())
        self.raw.assert_not_called();self.typed.assert_not_called()
        self.assertEqual(self.window.card_tabs.count(),4)

    def test_lf_identifier_does_not_claim_full_card_memory(self):
        card=self.window.library.create_card('LF',DumpData(bytes.fromhex('4A00468120'),'bin',{'record_kind':'identifier'},5),protocol='EM410x')
        self.window.load_card(card);self.window.card_workspace.refresh()
        self.assertTrue(self.window.card_workspace.progress.isHidden())
        self.assertFalse(self.window.card_workspace.map_button.isEnabled())
        self.assertEqual(self.window.card_workspace.grid.count(),0)

    def test_comparison_preserves_selected_old_revision_and_keys_never_appear(self):
        old=self.window.loaded_revision
        image=bytearray(self.image);image[16]=99;image[48:54]=bytes.fromhex('112233445566')
        new=self.window.library.save_revision(self.card_id,DumpData(bytes(image),'bin',{},16),'synthetic new')
        self.window.load_card(self.card_id)
        self.window.revisions.setCurrentIndex(self.window.revisions.findData(old))
        view=self.window.card_workspace;view.refresh()
        view.left.setCurrentIndex(view.left.findData(old));view.right.setCurrentIndex(view.right.findData(new));view.compare()
        self.assertEqual(view.diff_table.rowCount(),7)
        text=' '.join(view.diff_table.item(r,c).text() for r in range(7) for c in range(4))
        self.assertNotIn('112233445566',text);self.assertIn('Key A',text)
        self.assertEqual(self.window.loaded_revision,old)
        self.assertEqual(self.window.library.get_dump(self.card_id,old).data,bytes(self.image))

    def test_successful_native_write_shows_bound_result(self):
        dialog,plan,card,epoch=self.review()
        dialog.write_button.click();self.pump(card)
        self.assertIsNone(self.window.read_job)
        self.assertEqual(self.bridge.decision().state,State.VERIFIED)
        self.assertFalse(dialog.recovery_panel.isHidden())
        self.assertFalse(dialog.write_button.isEnabled())
        self.assertFalse(self.bridge.can_approve(epoch))

    def test_cancel_after_dispatch_readback_then_prepare_new_plan(self):
        dialog,plan,card,epoch=self.review()
        dialog.write_button.click()
        # Consume UID guard; the write has been dispatched, response unknown.
        self.window.operation_output,self.code=card.respond(plan.pending)
        self.window._step_completed(self.window._step_token,self.code,'guard')
        self.assertEqual(plan.phase,'write');self.assertEqual(plan.may_have_written,[1])
        self.window.cancel_operation()
        self.assertEqual(self.bridge.decision().state,State.WRITE_UNCERTAIN)
        self.assertFalse(dialog.recovery_panel.prepare.isEnabled())
        self.window.check_interrupted_write();self.finish_studio(card)
        result=self.bridge.decision()
        self.assertEqual(result.state,State.DIFFERENCES_CONFIRMED)
        self.assertEqual(plan.may_have_written,[1]);self.assertEqual(card.writes,[])
        self.assertTrue(dialog.recovery_panel.prepare.isEnabled())
        self.window.prepare_recovery_write()
        self.assertIsNot(self.window.write_dialog,dialog)
        self.assertIsNone(self.window.write_dialog.plan)
        self.assertEqual(card.writes,[])

    def test_readback_cleanup_failure_does_not_publish_a_match(self):
        dialog,plan,card,epoch=self.review()
        self.window.cancel_operation()
        before=self.bridge._snapshot
        self.window.check_interrupted_write();self.finish_studio(card,fail_cleanup=True)
        self.assertEqual(self.window.read_job['stage'],'uncertain')
        self.assertIs(self.bridge._snapshot,before)
        self.assertEqual(self.bridge.decision().state,State.WAITING)

    def test_foreign_completion_cannot_advance_current_step(self):
        dialog,plan,card,epoch=self.review()
        dialog.write_button.click();pending=plan.pending
        self.window.operation_output=card.respond(pending)[0]
        self.window._step_completed(object(),0,'stale completion')
        self.assertIs(plan.pending,pending)
        self.assertEqual(card.writes,[])

    def test_late_postcheck_binding_cannot_replace_new_result(self):
        dialog,plan,card,epoch=self.review()
        self.window.cancel_operation();binding=self.bridge._snapshot.binding
        self.bridge.invalidate('new operation')
        self.assertFalse(self.bridge.accept_postcheck(epoch,binding,{'binding':asdict(binding),
            'compared_blocks':[1],'matched_blocks':[1],'final_target_confirmed':True}))

    def test_memory_action_and_library_dump_keep_same_normalized_knowledge(self):
        dump=DumpData(bytes(self.image),'bin',{'read_validation':{'kind':'mfc'}},16)
        self.window.library.save_revision(self.card_id,dump,'masked')
        self.window.load_card(self.card_id)
        self.window.open_memory_map();self.addCleanup(self.window.memory_dialog.close)
        self.assertEqual(len(self.window.memory_dialog.dump.metadata['partial_dump']['known_bytes']),832)
        self.assertEqual(self.window.library.get_dump(self.card_id).metadata,dump.metadata)

    def test_invalid_working_hex_clears_old_passport_and_disables_actions(self):
        view=self.window.card_workspace;view.refresh()
        self.assertTrue(view.next_button.isEnabled())
        self.window.hex_edit.setPlainText('GG');view.refresh()
        self.assertEqual(view.identity.text(),'');self.assertEqual(view.coverage.text(),'')
        self.assertFalse(view.next_button.isEnabled());self.assertFalse(view.map_button.isEnabled())

    def test_frozen_workspace_diagnostic_runs_without_any_client_call(self):
        from pm3gui.release_check import check_card_workspace
        result=check_card_workspace(self.window)
        self.assertTrue(result['passed'])
        self.assertEqual(result['known_classic_bytes'],832)
        self.raw.assert_not_called();self.typed.assert_not_called()

    def test_new_plan_authenticates_changed_keys_without_assuming_ack(self):
        source=bytearray(self.image)
        source[48:54]=bytes.fromhex('112233445566');source[112:118]=bytes.fromhex('A0A1A2A3A4A5')
        self.window.library.save_revision(self.card_id,DumpData(bytes(source),'bin',{},16),'key change')
        self.window.load_card(self.card_id);self.window.open_write_dialog();dialog=self.window.write_dialog
        dialog.include_keys.setChecked(True);dialog.selected_blocks.setText('3,7')
        dialog.prepare_button.click();card=fixtures._fixtures.Card(fixtures._fixtures.classic_image());self.pump(card)
        plan=dialog.plan;self.assertEqual([e['block'] for e in plan.entries],[3,7])
        dialog.write_button.click()
        self.window.operation_output,code=card.respond(plan.pending)
        self.window._step_completed(self.window._step_token,code,'guard')
        card.respond(plan.pending)  # Actual write, ACK never delivered.
        self.window.cancel_operation()
        self.window.check_interrupted_write();self.finish_studio(card)
        self.assertEqual(self.bridge.decision().state,State.DIFFERENCES_CONFIRMED)
        self.window.prepare_recovery_write();new=self.window.write_dialog
        new.prepare_button.click();self.pump(card)
        self.assertTrue(new.plan.ready)
        self.assertEqual([e['block'] for e in new.plan.entries],[7])
        self.assertEqual(card.writes,[3])
        self.window.cancel_operation()
