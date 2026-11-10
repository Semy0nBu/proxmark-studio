"""Native HFWritePlan/report-v1 adapter tests with the project's fake RF card."""

from dataclasses import replace
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import unittest

from pm3gui.core import DumpData
from pm3gui.writing import HFWritePlan, WriteOptions

from pm3gui.write_evidence import (
    capture_plan, snapshot_from_live_plan, snapshot_from_report_v1,
)
from pm3gui.operation_recovery import (
    Action, Dispatch, Issue, Link, State, Termination, decide_recovery,
)


# Reuse the source repository's independent in-memory RF responder. Native
# HFWritePlan.consume(), command parsing, backup and JSON reports really run.
_native_path = Path(inspect.getfile(HFWritePlan)).resolve().parent.parent / "tests" / "test_writing.py"
_spec = importlib.util.spec_from_file_location("_native_write_adapter_fixture", _native_path)
_native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_native)


class WriteReportAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prepare(self, *, two=False, metadata=None, family="mfc"):
        initial = _native.classic_image() if family == "mfc" else bytes(180)
        data = bytearray(initial)
        data[16] = 7
        if two:
            data[32] = 9
        self.card = _native.Card(initial, family)
        options = WriteOptions(family, first=1 if family == "mfc" else 4,
                               last=2 if family == "mfc" else 4)
        self.plan = HFWritePlan(DumpData(bytes(data), "bin", metadata or {},
                                        16 if family == "mfc" else 4),
                                options, self.root)
        for _ in range(1000):
            op = self.plan.next_operation()
            if op is None:
                break
            self.plan.consume(*self.card.respond(op))
        else:
            self.fail("Native preflight did not terminate.")
        self.assertTrue(self.plan.ready)
        self.assertEqual(self.card.writes, [])
        self.capture = capture_plan(
            self.plan, operation_ref="operation:1", target_ref="target:1",
            selection_ref="card:fixture/revision:old", persisted=True,
        )
        return self.plan

    def execute(self):
        self.plan.approve()
        for _ in range(1000):
            op = self.plan.next_operation()
            if op is None:
                break
            self.plan.consume(*self.card.respond(op))
        else:
            self.fail("Native write did not terminate.")
        self.assertTrue(self.plan.done)

    def report(self):
        return json.loads((self.plan.directory / "write-report.json").read_text(encoding="utf-8"))

    def snapshot(self, termination=Termination.COMPLETED, **kwargs):
        return snapshot_from_live_plan(
            self.plan, self.capture, termination=termination, link=Link.READY, **kwargs,
        )

    def assert_inconclusive(self, snapshot):
        result = decide_recovery(snapshot)
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertEqual(result.verified_blocks, frozenset())
        self.assertTrue(result.may_have_changed)
        self.assertFalse(result.saved.report)
        self.assertFalse(result.saved.backup)
        return result

    def test_real_native_success_and_fresh_report_are_bound_to_capture(self):
        self.prepare()
        self.execute()
        report = self.report()
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["status"], "verified")
        snapshot = self.snapshot(report=report, report_persisted=True)
        result = decide_recovery(snapshot)
        self.assertEqual(result.state, State.VERIFIED)
        self.assertEqual(result.verified_blocks, frozenset({1}))
        self.assertFalse(snapshot.readback.post_termination_check)
        self.assertEqual(snapshot.acknowledged_blocks, frozenset())
        self.assertTrue(result.saved.report)
        self.assertTrue(result.saved.backup_complete)
        self.assertTrue(result.saved.source_version)
        self.assertFalse(result.auto_execute)
        self.assertFalse(result.reuse_previous_plan)

    def test_imported_verified_v1_is_not_authoritative_live_readback(self):
        self.prepare()
        self.execute()
        imported = snapshot_from_report_v1(self.report(), link=Link.READY)
        self.assertIsNone(imported.readback)
        self.assertEqual(imported.dispatch, Dispatch.UNKNOWN)
        self.assertIsNone(imported.may_have_written)
        result = self.assert_inconclusive(imported)
        self.assertEqual(result.next_action, Action.RESTORE_OPERATION_CONTEXT)

    def test_imported_not_written_v1_cannot_assert_no_change(self):
        self.prepare()
        self.plan.fail("Cancelled before native approve")
        report = self.report()
        self.assertEqual(report["status"], "not_written")
        self.assertEqual(report["may_have_written"], [])
        self.assert_inconclusive(snapshot_from_report_v1(report))

    def test_ack_without_readback_does_not_turn_green(self):
        self.prepare()
        self.plan.approve()
        self.plan.consume(*self.card.respond(self.plan.next_operation()))  # guard
        op = self.plan.next_operation()
        self.assertEqual(op.kind, "mfc_write_block")
        self.plan.consume(*self.card.respond(op))  # ACK, no following readback
        self.plan.fail("Disconnected before verification")
        snapshot = self.snapshot(Termination.FAILED, report=self.report(), report_persisted=True)
        result = decide_recovery(snapshot)
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertEqual(result.uncertain_blocks, frozenset({1}))
        self.assertIsNone(snapshot.readback)
        self.assertEqual(snapshot.acknowledged_blocks, frozenset())

    def test_partial_native_verified_is_a_claim_until_final_identity(self):
        self.prepare(two=True)
        self.plan.approve()
        for _ in range(20):
            op = self.plan.next_operation()
            if getattr(op, "kind", "") == "mfc_write_block" and op.block == 2:
                with self.assertRaises(ValueError):
                    self.plan.consume("[!] Write ( fail )", 0)
                self.plan.fail("Second block ACK missing")
                break
            self.plan.consume(*self.card.respond(op))
        else:
            self.fail("Did not reach second native write.")
        self.assertEqual(self.plan.verified, [1])
        snapshot = self.snapshot(Termination.FAILED, report=self.report(), report_persisted=True)
        result = decide_recovery(snapshot)
        self.assertEqual(snapshot.reported_verified_blocks, frozenset({1}))
        self.assertIsNone(snapshot.readback)
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertIn(Issue.REPORT_IS_NOT_READBACK, result.issues)

    def test_final_uid_failure_after_all_verified_is_not_success(self):
        self.prepare()
        self.plan.approve()
        for _ in range(20):
            op = self.plan.next_operation()
            if self.plan.phase == "final_identity":
                self.assertEqual(self.plan.verified, [1])
                self.card.uid = "11223344"
                with self.assertRaises(ValueError):
                    self.plan.consume(*self.card.respond(op))
                self.plan.fail("Final UID changed")
                break
            self.plan.consume(*self.card.respond(op))
        else:
            self.fail("Did not reach final identity check.")
        snapshot = self.snapshot(Termination.FAILED, report=self.report(), report_persisted=True)
        self.assertIsNone(snapshot.readback)
        self.assertNotEqual(decide_recovery(snapshot).state, State.VERIFIED)

    def test_live_cancellation_at_review_can_prove_no_write(self):
        self.prepare()
        self.plan.fail("Cancelled before approve")
        snapshot = self.snapshot(Termination.CANCELLED, report=self.report(), report_persisted=True)
        result = decide_recovery(snapshot)
        self.assertEqual(result.state, State.CANCELLED_BEFORE_WRITE)
        self.assertFalse(result.may_have_changed)
        self.assertEqual(self.card.writes, [])
        self.assertEqual(result.preserved_source, self.capture.selected_source)

    def test_capture_requires_native_review(self):
        plan = HFWritePlan(DumpData(_native.classic_image(), "bin", {}, 16),
                           WriteOptions("mfc"), self.root)
        with self.assertRaises(ValueError):
            capture_plan(plan, operation_ref="o", target_ref="t", selection_ref="s")

    def test_capture_cannot_be_created_after_approval(self):
        self.prepare()
        self.plan.approve()
        with self.assertRaises(ValueError):
            capture_plan(self.plan, operation_ref="o", target_ref="t", selection_ref="s")

    def test_capture_is_bound_to_the_same_plan_instance(self):
        self.prepare()
        other = HFWritePlan(self.plan.source, self.plan.options, self.root / "other")
        self.assert_inconclusive(snapshot_from_live_plan(
            other, self.capture, termination=Termination.FAILED, link=Link.READY,
        ))

    def test_changed_source_bytes_invalidate_capture(self):
        self.prepare()
        self.plan.source.data = bytes([1]) + self.plan.source.data[1:]
        self.assert_inconclusive(self.snapshot(Termination.FAILED))

    def test_changed_native_known_map_invalidates_capture(self):
        self.prepare()
        self.plan._known.remove(17)
        self.assert_inconclusive(self.snapshot(Termination.FAILED))

    def test_changed_metadata_known_map_invalidates_capture(self):
        self.prepare()
        self.plan.source.metadata["partial_dump"] = {"known_bytes": [16]}
        self.assert_inconclusive(self.snapshot(Termination.FAILED))

    def test_changed_options_invalidate_capture(self):
        self.prepare()
        self.plan.options = replace(self.plan.options, include_unknown=True)
        self.assert_inconclusive(self.snapshot(Termination.FAILED))

    def test_changed_effective_entries_invalidate_capture(self):
        self.prepare()
        self.plan.entries[0]["new"] = "AA" * 16
        self.assert_inconclusive(self.snapshot(Termination.FAILED))

    def test_foreign_source_report_cannot_supply_saved_or_green_state(self):
        self.prepare()
        self.execute()
        report = self.report()
        report["source_sha256"] = "0" * 64
        self.assert_inconclusive(self.snapshot(report=report, report_persisted=True))

    def test_stale_report_progress_is_rejected_even_with_correct_source_hash(self):
        self.prepare()
        stale = self.report()
        self.execute()
        self.assert_inconclusive(self.snapshot(report=stale, report_persisted=True))

    def test_dict_alone_does_not_claim_report_or_backup_persisted(self):
        self.prepare()
        self.execute()
        result = decide_recovery(self.snapshot(report=self.report()))
        self.assertEqual(result.state, State.VERIFIED)
        self.assertFalse(result.saved.report)
        self.assertFalse(result.saved.backup)

    def test_selected_revision_participates_in_source_binding(self):
        self.prepare()
        other = capture_plan(self.plan, operation_ref="operation:1", target_ref="target:1",
                             selection_ref="card:fixture/revision:new", persisted=True)
        self.assertNotEqual(self.capture.binding.source_snapshot_ref,
                            other.binding.source_snapshot_ref)

    def test_unsaved_editor_source_is_kept_but_not_claimed_as_saved_revision(self):
        self.prepare()
        self.capture = capture_plan(self.plan, operation_ref="operation:1", target_ref="target:1",
                                    selection_ref="editor:working", persisted=False)
        self.execute()
        result = decide_recovery(self.snapshot())
        self.assertEqual(result.state, State.VERIFIED)
        self.assertFalse(result.saved.source_version)
        self.assertEqual(result.preserved_source.selection_ref, "editor:working")

    def test_old_read_validation_does_not_create_current_readback(self):
        self.prepare(metadata={"read_validation": {"kind": "mfc", "complete": True,
                                                    "all_key_slots_known": True}})
        self.plan.fail("Cancelled before native writes")
        snapshot = self.snapshot(Termination.CANCELLED)
        self.assertIsNone(snapshot.readback)
        self.assertEqual(snapshot.reported_verified_blocks, frozenset())
        self.assertEqual(decide_recovery(snapshot).state, State.CANCELLED_BEFORE_WRITE)

    def test_real_mfu_plan_has_page_scope_and_native_verification(self):
        self.prepare(family="mfu")
        self.execute()
        snapshot = self.snapshot(report=self.report(), report_persisted=True)
        self.assertEqual(snapshot.planned_blocks, frozenset({4}))
        self.assertEqual(decide_recovery(snapshot).state, State.VERIFIED)
        self.assertEqual(self.card.writes, [4])

    def test_capture_repr_does_not_dump_native_keys_or_payload(self):
        self.prepare()
        text = repr(self.capture)
        self.assertNotIn("FFFFFFFFFFFF", text)
        self.assertNotIn("A1B2C3D4", text)
        self.assertNotIn("HFWritePlan object", text)

    def test_unknown_report_version_is_rejected(self):
        self.prepare()
        report = self.report()
        report["version"] = 2
        with self.assertRaises(ValueError):
            snapshot_from_report_v1(report)
        self.assert_inconclusive(self.snapshot(Termination.FAILED, report=report))


if __name__ == "__main__":
    unittest.main()
