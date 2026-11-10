"""Actual MainWindow/WriteDialog tests; ClientRunner is replaced with fake I/O."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import pm3gui
from pm3gui.app import MainWindow, format_hex
from pm3gui.core import DumpData
from pm3gui.memory_map import MemoryMapDialog, sector_info
from pm3gui.workspace_bridge import StudioAdapterBridge
from pm3gui.operation_recovery import Action, Link, State, Termination


_fixture_path = Path(pm3gui.__file__).resolve().parent.parent / "tests" / "test_writing.py"
_spec = importlib.util.spec_from_file_location("kit_qt_native_write_fixture", _fixture_path)
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


class QtBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.enterContext(patch("pm3gui.app.available_ports", return_value=[]))
        self.enterContext(patch("pm3gui.app.discover_client", return_value=""))
        self.enterContext(patch("pm3gui.app.QMessageBox.warning"))
        self.enterContext(patch("pm3gui.app.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel))
        self.raw = self.enterContext(patch("pm3gui.app.ClientRunner.run"))
        self.typed = self.enterContext(patch("pm3gui.app.ClientRunner.run_operation"))
        self.typed.side_effect = lambda settings, op, **kwargs: op.render()
        self.window = MainWindow(self.root / "profile")
        self.bridge = StudioAdapterBridge(self.window)
        self.addCleanup(self.cleanup)
        self.image = bytearray(_fixtures.classic_image())
        self.image[16:32] = bytes(range(16))
        self.card_id = self.window.library.create_card(
            "Synthetic adapter card", DumpData(bytes(self.image), "bin", {}, 16),
            uid="A1B2C3D4", protocol="MIFARE Classic 1K",
        )
        self.window.load_card(self.card_id)

    def cleanup(self):
        self.window.read_job = None
        self.window.antenna_job = None
        self.window.dirty = False
        if getattr(self.window, "write_dialog", None) is not None:
            self.window.write_dialog.close()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def pump(self, card):
        for _ in range(2000):
            job = self.window.read_job
            if job is None:
                return
            plan = job["write_plan"]
            if plan.pending is None:
                return
            output, code = card.respond(plan.pending)
            self.window.operation_output = output
            self.window._finished(code, "synthetic callback")
        self.fail("The real controller did not reach a boundary.")

    def review(self):
        self.window.open_write_dialog()
        dialog = self.window.write_dialog
        dialog.first.setValue(1)
        dialog.last.setValue(1)
        dialog.prepare_button.click()
        card = _fixtures.Card(_fixtures.classic_image())
        self.pump(card)
        plan = dialog.plan
        self.assertTrue(plan.ready)
        epoch = self.bridge.bind_review()
        return dialog, plan, card, epoch

    def cancel_result(self):
        dialog, plan, card, epoch = self.review()
        self.window.cancel_operation()
        self.assertTrue(self.bridge.capture_finished(
            epoch, plan, termination=Termination.CANCELLED, link=Link.READY,
        ))
        return dialog, plan, card, epoch

    def test_real_revision_selector_keeps_explicit_old_revision(self):
        old = self.window.loaded_revision
        data = bytearray(self.image)
        data[32] = 73
        self.window.library.save_revision(self.card_id, DumpData(bytes(data), "bin", {}, 16), "new")
        self.window.load_card(self.card_id)
        self.window.revisions.setCurrentIndex(self.window.revisions.findData(old))
        source = self.bridge.current_source()
        self.assertEqual(source.revision_id, old)
        self.assertEqual(source.snapshot.data, bytes(self.image))
        self.assertTrue(source.persisted)
        self.raw.assert_not_called()
        self.typed.assert_not_called()

    def test_real_unsaved_hex_is_not_replaced_by_latest_saved_revision(self):
        old = self.window.loaded_revision
        data = bytearray(self.image)
        data[32] = 42
        self.window.hex_edit.setPlainText(format_hex(bytes(data), 16))
        source = self.bridge.current_source()
        self.assertFalse(source.persisted)
        self.assertFalse(source.rf_verified)
        self.assertEqual(source.snapshot.data, bytes(data))
        comparison = self.bridge.compare_with_revision(old)
        self.assertEqual(comparison.generic.counts.known_values_differ, 1)
        self.assertEqual(self.window.loaded_revision, old)
        self.raw.assert_not_called()
        self.typed.assert_not_called()

    def test_review_binding_and_approval_guard_do_not_send_commands(self):
        _, _, card, epoch = self.review()
        before = (self.raw.call_count, self.typed.call_count)
        self.assertTrue(self.bridge.can_approve(epoch))
        self.assertFalse(self.bridge.can_approve(epoch - 1))
        self.assertEqual(card.writes, [])
        self.assertEqual(before, (self.raw.call_count, self.typed.call_count))

    def test_cancelled_plan_during_cleanup_cannot_be_approved(self):
        _, plan, _, epoch = self.review()
        plan.fail("cancelled; cleanup not released")
        self.assertIsNotNone(self.window.read_job)
        self.assertFalse(self.bridge.can_approve(epoch))

    def test_cancel_marker_blocks_approval_before_plan_fail_callback(self):
        _, _, _, epoch = self.review()
        self.window.read_job["write_cancelled"] = True
        self.assertFalse(self.bridge.can_approve(epoch))

    def test_changed_native_known_map_cannot_pass_review_approval(self):
        _, plan, _, epoch = self.review()
        plan._known.remove(16)
        self.assertFalse(self.bridge.can_approve(epoch))

    def test_native_memory_map_receives_normalized_mask_without_changing_library(self):
        dump = DumpData(bytes(self.image), "bin", {"read_validation": {"kind": "mfc"}}, 16)
        self.window.library.save_revision(self.card_id, dump, "historical read")
        self.window.load_card(self.card_id)
        original_metadata = dict(self.window.loaded_dump.metadata)
        view = self.bridge.create_memory_map()
        self.assertIsInstance(view, MemoryMapDialog)
        self.addCleanup(view.close)
        info = sector_info(view.dump, 0)
        self.assertEqual(info["state"], "partial")
        self.assertFalse(info["keys"]["A"])
        self.assertFalse(info["keys"]["B"])
        self.assertEqual(info["total_known"], 52)
        self.assertEqual(self.window.loaded_dump.metadata, original_metadata)
        self.assertNotIn("partial_dump", self.window.loaded_dump.metadata)
        self.raw.assert_not_called()
        self.typed.assert_not_called()

    def test_raw_length_alone_does_not_enable_native_classic_map(self):
        self.window.protocol.setText("")
        with self.assertRaisesRegex(ValueError, "Classic"):
            self.bridge.create_memory_map()

    def test_write_dialog_old_saved_revision_remains_the_bound_source(self):
        old = self.window.loaded_revision
        newer = bytearray(self.image)
        newer[32] = 99
        self.window.library.save_revision(self.card_id, DumpData(bytes(newer), "bin", {}, 16), "newer")
        self.window.open_write_dialog()
        dialog = self.window.write_dialog
        index = next(i for i in range(dialog.sources.count())
                     if dialog.sources.itemData(i) == ("card", self.card_id))
        dialog.sources.setCurrentIndex(index)
        dialog.source_versions.setCurrentIndex(dialog.source_versions.findData(old))
        dialog.first.setValue(1)
        dialog.last.setValue(1)
        dialog.prepare_button.click()
        self.pump(_fixtures.Card(_fixtures.classic_image()))
        epoch = self.bridge.bind_review()
        plan = dialog.plan
        self.window.cancel_operation()
        self.assertTrue(self.bridge.capture_finished(
            epoch, plan, termination=Termination.CANCELLED, link=Link.READY,
        ))
        result = self.bridge.decision()
        self.assertTrue(result.saved.source_version)
        self.assertIn(old, result.preserved_source.selection_ref)
        self.assertEqual(self.window.loaded_revision, old)

    def test_changed_target_invalidates_review_and_disables_old_button(self):
        dialog, _, _, epoch = self.review()
        dialog.target_uid.setText("11223344")
        self.assertFalse(self.bridge.can_approve(epoch))
        self.assertFalse(dialog.write_button.isEnabled())
        self.assertEqual(self.bridge.last_rejection, "source_target_or_options_changed")

    def test_changed_parameters_invalidate_review(self):
        dialog, _, _, epoch = self.review()
        dialog.last.setValue(2)
        self.assertFalse(self.bridge.can_approve(epoch))
        self.assertFalse(dialog.write_button.isEnabled())

    def test_changed_source_selector_invalidates_review(self):
        dialog, _, _, epoch = self.review()
        index = next(i for i in range(dialog.sources.count())
                     if dialog.sources.itemData(i) == ("card", self.card_id))
        dialog.sources.setCurrentIndex(index)
        self.assertFalse(self.bridge.can_approve(epoch))
        self.assertFalse(dialog.write_button.isEnabled())

    def test_late_queued_callback_cannot_replace_new_operation_context(self):
        _, first, _, old_epoch = self.review()
        self.window.cancel_operation()
        _, second, _, new_epoch = self.review()
        accepted = []
        QTimer.singleShot(0, lambda: accepted.append(self.bridge.capture_finished(
            old_epoch, first, termination=Termination.COMPLETED, link=Link.READY,
        )))
        self.app.processEvents()
        self.assertEqual(accepted, [False])
        self.assertNotEqual(old_epoch, new_epoch)
        self.assertTrue(self.bridge.can_approve(new_epoch))
        self.assertIs(self.window.read_job["write_plan"], second)
        self.assertIsNone(self.bridge.decision())

    def test_foreign_job_is_not_released_by_a_stale_result(self):
        _, plan, _, epoch = self.review()
        foreign = {"job_kind": "studio", "stage": "cleanup"}
        self.window.read_job = foreign
        self.assertFalse(self.bridge.capture_finished(
            epoch, plan, termination=Termination.FAILED, link=Link.READY,
        ))
        self.assertIs(self.window.read_job, foreign)
        self.assertIsNone(self.bridge.decision())

    def test_result_waits_for_existing_job_and_cleanup_even_with_idle_runner(self):
        _, plan, _, epoch = self.review()
        plan.fail("synthetic cancellation")
        before = (self.raw.call_count, self.typed.call_count)
        self.assertTrue(self.bridge.capture_finished(
            epoch, plan, termination=Termination.CANCELLED, link=Link.READY,
        ))
        self.assertFalse(self.window.runner.busy)
        self.assertEqual(self.bridge.decision().state, State.WAITING)
        self.window.read_job = {"stage": "cancel_resync_failed"}
        self.assertEqual(self.bridge.decision().next_action, Action.WAIT)
        self.window.read_job = None
        self.assertEqual(self.bridge.decision().state, State.CANCELLED_BEFORE_WRITE)
        self.assertEqual(before, (self.raw.call_count, self.typed.call_count))

    def test_antenna_or_firmware_activity_keeps_result_actions_waiting(self):
        self.cancel_result()
        self.window.antenna_job = {"stage": "cleanup"}
        self.assertEqual(self.bridge.decision().next_action, Action.WAIT)
        self.window.antenna_job = None
        with patch.object(MainWindow, "firmware_busy", new_callable=PropertyMock, return_value=True):
            self.assertEqual(self.bridge.decision().next_action, Action.WAIT)

    def test_terminal_result_rejects_duplicate_callback_and_old_approval(self):
        dialog, plan, _, epoch = self.cancel_result()
        self.assertFalse(self.bridge.can_approve(epoch))
        self.assertFalse(dialog.write_button.isEnabled())
        self.assertFalse(self.bridge.capture_finished(
            epoch, plan, termination=Termination.COMPLETED, link=Link.READY,
        ))
        self.assertEqual(self.bridge.decision().state, State.CANCELLED_BEFORE_WRITE)

    def test_full_real_controller_flow_uses_only_fake_io_and_live_readback(self):
        dialog, plan, card, epoch = self.review()
        self.assertTrue(self.bridge.can_approve(epoch))
        dialog.write_button.click()
        self.pump(card)
        self.assertIsNone(self.window.read_job)
        before = (self.raw.call_count, self.typed.call_count)
        self.assertTrue(self.bridge.capture_finished(
            epoch, plan, termination=Termination.COMPLETED, link=Link.READY,
        ))
        decision = self.bridge.decision()
        self.assertEqual(decision.state, State.VERIFIED)
        self.assertEqual(decision.verified_blocks, frozenset({1}))
        self.assertTrue(decision.saved.report)
        self.assertFalse(decision.auto_execute)
        self.assertFalse(decision.reuse_previous_plan)
        self.assertEqual(card.writes, [1])
        self.assertEqual(before, (self.raw.call_count, self.typed.call_count))

    def test_missing_report_file_never_claims_persistence(self):
        _, plan, _, epoch = self.review()
        self.window.cancel_operation()
        (plan.directory / "write-report.json").unlink()
        self.assertTrue(self.bridge.capture_finished(
            epoch, plan, termination=Termination.CANCELLED, link=Link.READY,
        ))
        self.assertFalse(self.bridge.decision().saved.report)
        self.assertFalse(self.bridge.decision().saved.backup)


if __name__ == "__main__":
    unittest.main()
