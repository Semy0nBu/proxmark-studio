"""Synthetic decision tests only: no Qt, native client, files or hardware."""

from dataclasses import replace
import unittest

from pm3gui.operation_recovery import (
    Action, Backup, Binding, Dispatch, Issue, Link, ReadbackEvidence,
    Scope, SourceSelection, State, Termination, WriteSnapshot, decide_recovery,
)


BINDING = Binding("demo-operation", "demo-target", "demo-snapshot", "demo-plan")
SOURCE = SourceSelection("demo-library-version", "demo-snapshot", persisted=True)
BLOCKS = frozenset({4, 5, 6})


def state(**changes):
    return replace(WriteSnapshot(
        binding=BINDING, selected_source=SOURCE,
        termination=Termination.FAILED, dispatch=Dispatch.MAY_HAVE_WRITTEN,
        link=Link.READY, planned_blocks=BLOCKS, may_have_written=BLOCKS,
    ), **changes)


def readback(**changes):
    return replace(ReadbackEvidence(
        binding=BINDING, compared_blocks=BLOCKS, matched_blocks=BLOCKS,
        final_target_confirmed=True,
    ), **changes)


class OperationRecoveryTests(unittest.TestCase):
    def test_cancel_before_dispatch_keeps_source_and_requires_new_plan(self):
        source_state = state(
            termination=Termination.CANCELLED, dispatch=Dispatch.NOT_SENT,
            may_have_written=frozenset(),
        )
        result = decide_recovery(source_state)
        self.assertEqual(result.state, State.CANCELLED_BEFORE_WRITE)
        self.assertFalse(result.may_have_changed)
        self.assertIs(result.preserved_source, SOURCE)
        self.assertEqual(result.next_action, Action.PREPARE_NEW_PLAN)
        self.assertTrue(result.invalidate_previous_plan)
        self.assertFalse(result.reuse_previous_plan)
        self.assertFalse(result.auto_execute)
        self.assertEqual(source_state.selected_source, SOURCE)

    def test_partial_readback_separates_uncertain_and_unattempted_blocks(self):
        result = decide_recovery(state(
            may_have_written=frozenset({4, 5}),
            reported_verified_blocks=frozenset({4}),
            readback=readback(
                compared_blocks=frozenset({4}), matched_blocks=frozenset({4})
            ),
        ))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertEqual(result.verified_blocks, frozenset({4}))
        self.assertEqual(result.uncertain_blocks, frozenset({5}))
        self.assertEqual(result.not_attempted_blocks, frozenset({6}))
        self.assertEqual(result.steps, (
            Action.READ_AND_COMPARE, Action.PREPARE_NEW_PLAN
        ))

    def test_ack_and_exit_style_completion_do_not_verify_data(self):
        result = decide_recovery(state(
            termination=Termination.COMPLETED, acknowledged_blocks=BLOCKS
        ))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertFalse(result.verified_blocks)

    def test_ack_outside_known_possible_writes_invalidates_progress(self):
        for evidence in (None, readback(post_termination_check=True)):
            with self.subTest(evidence=evidence):
                result = decide_recovery(state(
                    may_have_written=frozenset({4}),
                    acknowledged_blocks=frozenset({5}), readback=evidence,
                ))
                self.assertEqual(result.state, State.INCONCLUSIVE)
                self.assertEqual(result.next_action, Action.REVIEW_EVIDENCE)
                self.assertIn(Issue.ACK_OUTSIDE_POSSIBLE_WRITE, result.issues)
                self.assertFalse(result.verified_blocks)
                self.assertFalse(result.compared_blocks)
                self.assertFalse(result.differing_blocks)
                self.assertIsNone(result.uncertain_blocks)
                self.assertIsNone(result.not_attempted_blocks)
                self.assertTrue(result.may_have_changed)

    def test_ack_without_dispatch_history_does_not_invent_attempted_blocks(self):
        result = decide_recovery(state(
            dispatch=Dispatch.UNKNOWN, may_have_written=None,
            acknowledged_blocks=frozenset({5}),
        ))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertFalse(result.verified_blocks)
        self.assertIsNone(result.uncertain_blocks)
        self.assertIsNone(result.not_attempted_blocks)
        self.assertIn(Issue.UNKNOWN_DISPATCH, result.issues)

    def test_reported_verified_counts_alone_are_not_readback_evidence(self):
        result = decide_recovery(state(reported_verified_blocks=BLOCKS))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertIn(Issue.REPORT_IS_NOT_READBACK, result.issues)
        self.assertFalse(result.verified_blocks)

    def test_readback_must_bind_operation_target_snapshot_and_plan(self):
        for field in ("operation_ref", "target_ref", "source_snapshot_ref", "plan_ref"):
            with self.subTest(field=field):
                wrong = replace(BINDING, **{field: "different-demo-reference"})
                result = decide_recovery(state(
                    termination=Termination.COMPLETED,
                    readback=readback(binding=wrong),
                ))
                self.assertEqual(result.state, State.WRITE_UNCERTAIN)
                self.assertIn(Issue.FOREIGN_READBACK, result.issues)
                self.assertFalse(result.verified_blocks)

    def test_bytes_without_target_confirmation_are_not_verified(self):
        result = decide_recovery(state(
            termination=Termination.COMPLETED,
            readback=readback(final_target_confirmed=False),
        ))
        self.assertNotEqual(result.state, State.VERIFIED)
        self.assertIn(Issue.TARGET_NOT_CONFIRMED, result.issues)
        self.assertFalse(result.verified_blocks)

    def test_all_expected_blocks_and_final_confirmation_can_verify_result(self):
        result = decide_recovery(state(
            termination=Termination.COMPLETED,
            reported_verified_blocks=BLOCKS, readback=readback(),
        ))
        self.assertEqual(result.state, State.VERIFIED)
        self.assertEqual(result.verified_blocks, BLOCKS)
        self.assertEqual(result.uncertain_blocks, frozenset())
        self.assertEqual(result.next_action, Action.RETURN_TO_CARD)
        self.assertFalse(result.saved.report)

    def test_cancel_after_block_checks_does_not_claim_clean_completion(self):
        result = decide_recovery(state(
            termination=Termination.CANCELLED, readback=readback()
        ))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertEqual(result.next_action, Action.READ_AND_COMPARE)

    def test_fresh_post_interruption_check_can_resolve_unknown_dispatch(self):
        result = decide_recovery(state(
            termination=Termination.CANCELLED, dispatch=Dispatch.UNKNOWN,
            may_have_written=None, error_present=True,
            readback=readback(post_termination_check=True),
        ))
        self.assertEqual(result.state, State.VERIFIED)
        self.assertEqual(result.verified_blocks, BLOCKS)
        self.assertEqual(result.uncertain_blocks, frozenset())
        self.assertIn(Issue.ORIGINAL_FAILURE_RETAINED, result.issues)
        self.assertIn(Issue.UNKNOWN_DISPATCH, result.issues)
        self.assertFalse(result.reuse_previous_plan)

    def test_unknown_dispatch_offline_requires_reconnect_then_compare(self):
        result = decide_recovery(state(
            dispatch=Dispatch.UNKNOWN, may_have_written=None,
            link=Link.UNAVAILABLE,
        ))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertEqual(result.steps, (
            Action.RECONNECT, Action.READ_AND_COMPARE, Action.PREPARE_NEW_PLAN
        ))
        self.assertIsNone(result.uncertain_blocks)
        self.assertIsNone(result.not_attempted_blocks)
        self.assertTrue(result.may_have_changed)

    def test_unconfirmed_session_requires_fresh_liveness_check(self):
        result = decide_recovery(state(link=Link.UNCONFIRMED))
        self.assertEqual(result.next_action, Action.VERIFY_CONNECTION)
        self.assertEqual(result.steps[1], Action.READ_AND_COMPARE)

    def test_failed_backup_before_dispatch_is_not_a_partial_write(self):
        result = decide_recovery(state(
            backup=Backup.FAILED, dispatch=Dispatch.NOT_SENT,
            may_have_written=frozenset(), error_present=True,
        ))
        self.assertEqual(result.state, State.BACKUP_FAILED)
        self.assertFalse(result.may_have_changed)
        self.assertEqual(result.next_action, Action.CHECK_BACKUP_STORAGE)
        self.assertIn(Action.PREPARE_NEW_PLAN, result.steps)

    def test_backup_failure_does_not_override_positive_dispatch_evidence(self):
        result = decide_recovery(state(backup=Backup.FAILED))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertTrue(result.may_have_changed)
        self.assertIn(Issue.BACKUP_FAILED_AFTER_DISPATCH, result.issues)
        self.assertEqual(result.next_action, Action.REVIEW_EVIDENCE)

    def test_never_sent_flag_cannot_override_possible_write(self):
        result = decide_recovery(state(dispatch=Dispatch.NOT_SENT))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertTrue(result.may_have_changed)
        self.assertIn(Issue.CONTRADICTORY_DISPATCH, result.issues)

    def test_missing_binding_or_planned_scope_cannot_claim_unchanged(self):
        for changes in ({"binding": None}, {"planned_blocks": None}):
            with self.subTest(changes=changes):
                result = decide_recovery(state(**changes))
                self.assertEqual(result.state, State.INCONCLUSIVE)
                self.assertEqual(result.next_action, Action.RESTORE_OPERATION_CONTEXT)
                self.assertTrue(result.may_have_changed)

    def test_not_sent_without_binding_cannot_claim_unchanged(self):
        result = decide_recovery(state(
            binding=None, dispatch=Dispatch.NOT_SENT,
            may_have_written=frozenset(),
        ))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertEqual(result.next_action, Action.RESTORE_OPERATION_CONTEXT)
        self.assertIn(Issue.MISSING_BINDING, result.issues)
        self.assertTrue(result.may_have_changed)
        self.assertIsNone(result.not_attempted_blocks)

    def test_not_sent_without_planned_scope_cannot_claim_unchanged(self):
        result = decide_recovery(state(
            planned_blocks=None, dispatch=Dispatch.NOT_SENT,
            may_have_written=frozenset(),
        ))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertEqual(result.next_action, Action.RESTORE_OPERATION_CONTEXT)
        self.assertIn(Issue.MISSING_SCOPE, result.issues)
        self.assertTrue(result.may_have_changed)
        self.assertIsNone(result.not_attempted_blocks)

    def test_missing_source_requires_explicit_restoration(self):
        result = decide_recovery(state(selected_source=None))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertEqual(result.next_action, Action.RESTORE_SOURCE_SNAPSHOT)
        self.assertIsNone(result.preserved_source)

    def test_changed_source_does_not_receive_old_green_verification(self):
        selected = replace(SOURCE, snapshot_ref="new-demo-editor-snapshot")
        result = decide_recovery(state(
            selected_source=selected, termination=Termination.COMPLETED,
            readback=readback(),
        ))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertFalse(result.verified_blocks)
        self.assertFalse(result.compared_blocks)
        self.assertFalse(result.differing_blocks)
        self.assertIsNone(result.uncertain_blocks)
        self.assertIsNone(result.not_attempted_blocks)
        self.assertIs(result.preserved_source, selected)
        self.assertEqual(result.binding, BINDING)
        self.assertEqual(result.next_action, Action.RESTORE_SOURCE_SNAPSHOT)

    def test_invalid_block_identifiers_are_not_coerced(self):
        for malformed in (frozenset({True}), frozenset({-1}), frozenset({"4"})):
            with self.subTest(malformed=malformed):
                result = decide_recovery(state(may_have_written=malformed))
                self.assertEqual(result.state, State.INCONCLUSIVE)
                self.assertEqual(result.next_action, Action.REVIEW_EVIDENCE)
                self.assertIn(Issue.INVALID_BLOCK_SET, result.issues)

    def test_progress_outside_plan_requires_review(self):
        result = decide_recovery(state(may_have_written=frozenset({4, 999})))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertIn(Issue.PROGRESS_OUTSIDE_PLAN, result.issues)

    def test_readback_matched_set_cannot_exceed_compared_set(self):
        result = decide_recovery(state(readback=readback(
            compared_blocks=frozenset({4}), matched_blocks=BLOCKS
        )))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertFalse(result.verified_blocks)
        self.assertFalse(result.compared_blocks)
        self.assertFalse(result.differing_blocks)
        self.assertIsNone(result.uncertain_blocks)
        self.assertIsNone(result.not_attempted_blocks)

    def test_compared_mismatches_are_known_differences_not_unknown_bytes(self):
        result = decide_recovery(state(readback=readback(
            matched_blocks=frozenset({4, 6})
        )))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertEqual(result.differing_blocks, frozenset({5}))
        self.assertEqual(result.uncertain_blocks, frozenset())
        self.assertEqual(result.compared_blocks, BLOCKS)

    def test_full_fresh_comparison_with_differences_moves_to_new_plan(self):
        planned = frozenset({4, 5, 6, 7})
        result = decide_recovery(state(
            planned_blocks=planned, may_have_written=None,
            dispatch=Dispatch.UNKNOWN, error_present=True,
            readback=readback(
                compared_blocks=planned, matched_blocks=frozenset({4, 5}),
                post_termination_check=True,
            ),
        ))
        self.assertEqual(result.state, State.DIFFERENCES_CONFIRMED)
        self.assertEqual(result.compared_blocks, planned)
        self.assertEqual(result.verified_blocks, frozenset({4, 5}))
        self.assertEqual(result.differing_blocks, frozenset({6, 7}))
        self.assertEqual(result.uncertain_blocks, frozenset())
        self.assertEqual(result.steps, (Action.PREPARE_NEW_PLAN,))
        self.assertIn(Issue.ORIGINAL_FAILURE_RETAINED, result.issues)
        self.assertFalse(result.reuse_previous_plan)
        self.assertFalse(result.auto_execute)

    def test_partial_post_check_does_not_skip_remaining_comparison(self):
        result = decide_recovery(state(readback=readback(
            compared_blocks=frozenset({4, 5}), matched_blocks=frozenset({4}),
            post_termination_check=True,
        )))
        self.assertEqual(result.state, State.WRITE_UNCERTAIN)
        self.assertEqual(result.differing_blocks, frozenset({5}))
        self.assertEqual(result.uncertain_blocks, frozenset({6}))
        self.assertEqual(result.next_action, Action.READ_AND_COMPARE)

    def test_old_or_unidentified_comparison_does_not_offer_new_plan_first(self):
        for changes in (
            {"post_termination_check": False},
            {"binding": replace(BINDING, operation_ref="other-demo-operation")},
            {"final_target_confirmed": False},
        ):
            with self.subTest(changes=changes):
                proof = readback(
                    matched_blocks=frozenset({4}),
                    post_termination_check=True,
                )
                result = decide_recovery(state(readback=replace(proof, **changes)))
                self.assertNotEqual(result.state, State.DIFFERENCES_CONFIRMED)
                self.assertNotEqual(result.next_action, Action.PREPARE_NEW_PLAN)

    def test_confirmed_differences_keep_link_requirement_when_connection_lost(self):
        result = decide_recovery(state(
            link=Link.UNAVAILABLE,
            readback=readback(
                matched_blocks=frozenset({4}), post_termination_check=True
            ),
        ))
        self.assertEqual(result.state, State.DIFFERENCES_CONFIRMED)
        self.assertEqual(result.steps, (Action.RECONNECT, Action.PREPARE_NEW_PLAN))

    def test_fresh_readback_does_not_prove_that_write_was_dispatched(self):
        result = decide_recovery(state(
            dispatch=Dispatch.NOT_SENT, may_have_written=frozenset(),
            termination=Termination.CANCELLED,
            readback=readback(post_termination_check=True),
        ))
        self.assertEqual(result.state, State.VERIFIED)
        self.assertFalse(result.may_have_changed)
        self.assertNotIn(Issue.CONTRADICTORY_DISPATCH, result.issues)

    def test_post_check_does_not_expand_original_dispatch_history(self):
        for matches, expected in (
            (BLOCKS, State.VERIFIED),
            (frozenset({4, 5}), State.DIFFERENCES_CONFIRMED),
        ):
            with self.subTest(matches=matches):
                snapshot = state(
                    may_have_written=frozenset({4}),
                    acknowledged_blocks=frozenset({4}),
                    readback=readback(
                        matched_blocks=matches, post_termination_check=True,
                    ),
                )
                result = decide_recovery(snapshot)
                self.assertEqual(result.state, expected)
                self.assertEqual(result.compared_blocks, BLOCKS)
                self.assertEqual(result.verified_blocks, matches)
                self.assertEqual(result.not_attempted_blocks, frozenset({5, 6}))
                self.assertEqual(snapshot.may_have_written, frozenset({4}))
                self.assertEqual(result.issues, ())

    def test_error_and_claimed_completed_readback_need_reconciliation(self):
        result = decide_recovery(state(
            termination=Termination.COMPLETED, error_present=True,
            readback=readback(),
        ))
        self.assertEqual(result.state, State.INCONCLUSIVE)
        self.assertIn(Issue.ERROR_WITH_COMPLETE_READBACK, result.issues)

    def test_saved_artifacts_need_matching_provenance_and_persistence(self):
        foreign = replace(BINDING, operation_ref="other-demo-operation")
        result = decide_recovery(state(
            report_persisted=True, report_binding=foreign,
            backup_persisted=True, backup_binding=BINDING,
            backup=Backup.PARTIAL,
        ))
        self.assertFalse(result.saved.report)
        self.assertTrue(result.saved.backup)
        self.assertFalse(result.saved.backup_complete)
        self.assertTrue(result.saved.source_version)

    def test_unsaved_editor_source_is_retained_but_not_called_saved(self):
        editor = replace(SOURCE, selection_ref="demo-editor", persisted=False)
        result = decide_recovery(state(selected_source=editor))
        self.assertIs(result.preserved_source, editor)
        self.assertFalse(result.saved.source_version)

    def test_running_operation_or_busy_runner_cannot_start_recovery(self):
        active = decide_recovery(state(termination=Termination.RUNNING))
        self.assertEqual(active.next_action, Action.WAIT)
        self.assertFalse(active.invalidate_previous_plan)
        terminal_busy = decide_recovery(state(link=Link.BUSY))
        self.assertEqual(terminal_busy.next_action, Action.WAIT)
        self.assertTrue(terminal_busy.invalidate_previous_plan)

    def test_firmware_is_not_routed_into_rf_write_recovery(self):
        result = decide_recovery(state(scope=Scope.FIRMWARE))
        self.assertEqual(result.state, State.OUTSIDE_SCOPE)
        self.assertEqual(result.steps, (Action.OPEN_FIRMWARE_STATUS,))
        self.assertFalse(result.invalidate_previous_plan)
        self.assertFalse(result.reuse_previous_plan)
        self.assertFalse(result.auto_execute)

    def test_empty_plan_does_not_create_vacuous_readback_success(self):
        result = decide_recovery(state(
            termination=Termination.COMPLETED, dispatch=Dispatch.NOT_SENT,
            planned_blocks=frozenset(), may_have_written=frozenset(),
            readback=readback(
                compared_blocks=frozenset(), matched_blocks=frozenset()
            ),
        ))
        self.assertEqual(result.state, State.NOT_WRITTEN)
        self.assertFalse(result.may_have_changed)


if __name__ == "__main__":
    unittest.main()
