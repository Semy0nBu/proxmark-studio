"""Pure reference policy for the proposed RF-write recovery panel.

This module neither imports Proxmark Studio nor opens files, ports or Qt.
Inputs must be normalized by an adapter that owns operation provenance.
An action is a suggestion for an explicit user gesture, never an instruction
to execute automatically. Firmware has a deliberately separate outcome.
"""

from dataclasses import dataclass
from enum import Enum


class Scope(str, Enum):
    RF_WRITE = "rf_write"
    FIRMWARE = "firmware"


class Termination(str, Enum):
    RUNNING = "running"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class Dispatch(str, Enum):
    NOT_SENT = "not_sent"
    MAY_HAVE_WRITTEN = "may_have_written"
    UNKNOWN = "unknown"


class Link(str, Enum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    UNCONFIRMED = "unconfirmed"
    BUSY = "busy"


class Backup(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class State(str, Enum):
    WAITING = "waiting"
    CANCELLED_BEFORE_WRITE = "cancelled_before_write"
    NOT_WRITTEN = "not_written"
    BACKUP_FAILED = "backup_failed"
    WRITE_UNCERTAIN = "write_uncertain"
    DIFFERENCES_CONFIRMED = "differences_confirmed"
    VERIFIED = "verified"
    INCONCLUSIVE = "inconclusive"
    OUTSIDE_SCOPE = "outside_scope"


class Action(str, Enum):
    WAIT = "wait"
    RECONNECT = "reconnect"
    VERIFY_CONNECTION = "verify_connection"
    RESTORE_OPERATION_CONTEXT = "restore_operation_context"
    RESTORE_SOURCE_SNAPSHOT = "restore_source_snapshot"
    REVIEW_EVIDENCE = "review_evidence"
    CHECK_BACKUP_STORAGE = "check_backup_storage"
    READ_AND_COMPARE = "read_and_compare"
    PREPARE_NEW_PLAN = "prepare_new_plan"
    RETURN_TO_CARD = "return_to_card"
    OPEN_FIRMWARE_STATUS = "open_firmware_status"


class Issue(str, Enum):
    MISSING_BINDING = "missing_binding"
    MISSING_SCOPE = "missing_scope"
    INVALID_BLOCK_SET = "invalid_block_set"
    PROGRESS_OUTSIDE_PLAN = "progress_outside_plan"
    ACK_OUTSIDE_POSSIBLE_WRITE = "ack_outside_possible_write"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_CHANGED = "source_changed"
    FOREIGN_READBACK = "foreign_readback"
    INVALID_READBACK = "invalid_readback"
    REPORT_IS_NOT_READBACK = "report_is_not_readback"
    CONTRADICTORY_DISPATCH = "contradictory_dispatch"
    BACKUP_FAILED_AFTER_DISPATCH = "backup_failed_after_dispatch"
    ERROR_WITH_COMPLETE_READBACK = "error_with_complete_readback"
    UNKNOWN_DISPATCH = "unknown_dispatch"
    TARGET_NOT_CONFIRMED = "target_not_confirmed"
    ORIGINAL_FAILURE_RETAINED = "original_failure_retained"


@dataclass(frozen=True)
class Binding:
    """Opaque local references, not card UIDs, keys or dump contents."""

    operation_ref: str
    target_ref: str
    source_snapshot_ref: str
    plan_ref: str


@dataclass(frozen=True)
class SourceSelection:
    selection_ref: str
    snapshot_ref: str
    persisted: bool = False


@dataclass(frozen=True)
class ReadbackEvidence:
    binding: Binding
    compared_blocks: frozenset[int]
    matched_blocks: frozenset[int]
    final_target_confirmed: bool
    post_termination_check: bool = False


@dataclass(frozen=True)
class WriteSnapshot:
    binding: Binding | None
    selected_source: SourceSelection | None
    termination: Termination
    dispatch: Dispatch
    link: Link
    planned_blocks: frozenset[int] | None
    may_have_written: frozenset[int] | None
    acknowledged_blocks: frozenset[int] = frozenset()
    reported_verified_blocks: frozenset[int] = frozenset()
    readback: ReadbackEvidence | None = None
    backup: Backup = Backup.UNKNOWN
    backup_persisted: bool = False
    backup_binding: Binding | None = None
    report_persisted: bool = False
    report_binding: Binding | None = None
    error_present: bool = False
    scope: Scope = Scope.RF_WRITE


@dataclass(frozen=True)
class SavedFacts:
    report: bool
    backup: bool
    backup_complete: bool
    source_version: bool


@dataclass(frozen=True)
class RecoveryDecision:
    state: State
    message_key: str
    link: Link
    steps: tuple[Action, ...]
    binding: Binding | None
    preserved_source: SourceSelection | None
    saved: SavedFacts
    verified_blocks: frozenset[int]
    compared_blocks: frozenset[int]
    differing_blocks: frozenset[int]
    uncertain_blocks: frozenset[int] | None
    not_attempted_blocks: frozenset[int] | None
    may_have_changed: bool
    issues: tuple[Issue, ...]
    invalidate_previous_plan: bool
    auto_execute: bool = False
    reuse_previous_plan: bool = False

    @property
    def next_action(self) -> Action:
        return self.steps[0]


def _binding_valid(value: Binding | None) -> bool:
    return isinstance(value, Binding) and all(
        isinstance(part, str) and bool(part.strip())
        for part in (value.operation_ref, value.target_ref,
                     value.source_snapshot_ref, value.plan_ref)
    )


def _same_binding(left: Binding | None, right: Binding | None) -> bool:
    return _binding_valid(left) and _binding_valid(right) and left == right


def _blocks_valid(value: frozenset[int] | None) -> bool:
    return isinstance(value, frozenset) and all(
        type(block) is int and block >= 0 for block in value
    )


def decide_recovery(snapshot: WriteSnapshot) -> RecoveryDecision:
    """Describe the next user step; never authorize execution of a write.

    reported_verified_blocks and acknowledged_blocks are untrusted progress
    claims until corroborated by scoped readback evidence. Unknown dispatch
    or incomplete provenance cannot produce "not written" or "verified".
    """
    issues: list[Issue] = []
    source = snapshot.selected_source
    source_matches = (
        isinstance(source, SourceSelection)
        and bool(source.selection_ref)
        and _binding_valid(snapshot.binding)
        and source.snapshot_ref == snapshot.binding.source_snapshot_ref
    )
    backup_saved = snapshot.backup_persisted is True and _same_binding(
        snapshot.backup_binding, snapshot.binding
    )
    saved = SavedFacts(
        report=snapshot.report_persisted is True and _same_binding(
            snapshot.report_binding, snapshot.binding
        ),
        backup=backup_saved,
        backup_complete=backup_saved and snapshot.backup == Backup.COMPLETE,
        source_version=bool(source_matches and source.persisted is True),
    )
    verified = frozenset()
    compared = frozenset()
    differing = frozenset()
    uncertain: frozenset[int] | None = None
    not_attempted: frozenset[int] | None = None
    may_have_changed = True

    def result(state: State, steps: tuple[Action, ...]) -> RecoveryDecision:
        return RecoveryDecision(
            state=state, message_key="recovery." + state.value,
            link=snapshot.link, steps=steps, binding=snapshot.binding,
            preserved_source=source, saved=saved,
            verified_blocks=verified, compared_blocks=compared,
            differing_blocks=differing, uncertain_blocks=uncertain,
            not_attempted_blocks=not_attempted, may_have_changed=may_have_changed,
            issues=tuple(dict.fromkeys(issues)),
            invalidate_previous_plan=(
                snapshot.scope == Scope.RF_WRITE
                and snapshot.termination != Termination.RUNNING
            ),
        )

    if snapshot.scope != Scope.RF_WRITE:
        return result(State.OUTSIDE_SCOPE, (Action.OPEN_FIRMWARE_STATUS,))
    if snapshot.termination == Termination.RUNNING or snapshot.link == Link.BUSY:
        return result(State.WAITING, (Action.WAIT,))

    if not _binding_valid(snapshot.binding):
        issues.append(Issue.MISSING_BINDING)
    if snapshot.planned_blocks is None:
        issues.append(Issue.MISSING_SCOPE)
    for blocks in (snapshot.planned_blocks, snapshot.may_have_written,
                   snapshot.acknowledged_blocks, snapshot.reported_verified_blocks):
        if blocks is not None and not _blocks_valid(blocks):
            issues.append(Issue.INVALID_BLOCK_SET)
    if snapshot.may_have_written is None or snapshot.dispatch == Dispatch.UNKNOWN:
        issues.append(Issue.UNKNOWN_DISPATCH)
    if source is None:
        issues.append(Issue.SOURCE_UNAVAILABLE)
    elif not source_matches:
        issues.append(Issue.SOURCE_CHANGED)

    if Issue.INVALID_BLOCK_SET in issues:
        return result(State.INCONCLUSIVE, (Action.REVIEW_EVIDENCE,))
    planned = snapshot.planned_blocks
    attempted = snapshot.may_have_written
    if planned is not None:
        for blocks in (attempted, snapshot.acknowledged_blocks,
                       snapshot.reported_verified_blocks):
            if blocks is not None and not blocks <= planned:
                issues.append(Issue.PROGRESS_OUTSIDE_PLAN)
    if attempted is not None and not snapshot.acknowledged_blocks <= attempted:
        # ACK cannot describe a block excluded by known dispatch history.
        # A later comparison may include untouched blocks; never merge its
        # compared/matched sets into the original possible-write history.
        issues.append(Issue.ACK_OUTSIDE_POSSIBLE_WRITE)

    evidence = snapshot.readback
    full_readback = False
    full_comparison = False
    if evidence is not None:
        if not _same_binding(evidence.binding, snapshot.binding):
            issues.append(Issue.FOREIGN_READBACK)
        elif (not _blocks_valid(evidence.compared_blocks)
              or not _blocks_valid(evidence.matched_blocks)
              or planned is None
              or not evidence.matched_blocks <= evidence.compared_blocks <= planned):
            issues.append(Issue.INVALID_READBACK)
        elif evidence.final_target_confirmed is not True:
            issues.append(Issue.TARGET_NOT_CONFIRMED)
        else:
            compared = evidence.compared_blocks
            verified = evidence.matched_blocks
            differing = compared - verified
            full_comparison = bool(planned) and compared == planned
            full_readback = bool(planned) and (
                verified == planned and evidence.compared_blocks == planned
                and evidence.final_target_confirmed is True
            )
    if not snapshot.reported_verified_blocks <= verified:
        issues.append(Issue.REPORT_IS_NOT_READBACK)

    positive_write_evidence = bool(
        attempted or snapshot.acknowledged_blocks
        or snapshot.reported_verified_blocks
    )
    never_sent = snapshot.dispatch == Dispatch.NOT_SENT and attempted == frozenset()
    if snapshot.dispatch == Dispatch.NOT_SENT and positive_write_evidence:
        issues.append(Issue.CONTRADICTORY_DISPATCH)
    if snapshot.backup == Backup.FAILED and positive_write_evidence:
        issues.append(Issue.BACKUP_FAILED_AFTER_DISPATCH)
    post_check = bool(full_comparison and evidence.post_termination_check is True)
    if (full_readback or post_check) and snapshot.error_present:
        issues.append(Issue.ORIGINAL_FAILURE_RETAINED if post_check
                      else Issue.ERROR_WITH_COMPLETE_READBACK)
    may_have_changed = not never_sent or positive_write_evidence
    if (attempted is not None and planned is not None
            and snapshot.dispatch != Dispatch.UNKNOWN):
        uncertain = attempted - compared
        not_attempted = planned - attempted

    if not _binding_valid(snapshot.binding) or planned is None:
        verified = compared = differing = frozenset()
        uncertain = None
        not_attempted = None
        # NOT_SENT is meaningful only inside an accepted operation context.
        may_have_changed = True
        return result(State.INCONCLUSIVE, (Action.RESTORE_OPERATION_CONTEXT,))
    if not source_matches:
        verified = frozenset()
        compared = frozenset()
        differing = frozenset()
        uncertain = None
        not_attempted = None
        return result(State.INCONCLUSIVE, (Action.RESTORE_SOURCE_SNAPSHOT,))
    blocking_issues = {
        Issue.PROGRESS_OUTSIDE_PLAN, Issue.INVALID_READBACK,
        Issue.ACK_OUTSIDE_POSSIBLE_WRITE,
        Issue.CONTRADICTORY_DISPATCH, Issue.BACKUP_FAILED_AFTER_DISPATCH,
        Issue.ERROR_WITH_COMPLETE_READBACK,
    }
    if blocking_issues.intersection(issues):
        # Resolve inconsistent provenance before proposing any device action.
        verified = frozenset()
        compared = frozenset()
        differing = frozenset()
        uncertain = None
        not_attempted = None
        return result(State.INCONCLUSIVE, (Action.REVIEW_EVIDENCE,))

    prefix: tuple[Action, ...] = ()
    if snapshot.link == Link.UNAVAILABLE:
        prefix = (Action.RECONNECT,)
    elif snapshot.link != Link.READY:
        prefix = (Action.VERIFY_CONNECTION,)

    if post_check and differing:
        uncertain = frozenset()
        return result(State.DIFFERENCES_CONFIRMED, (
            *prefix, Action.PREPARE_NEW_PLAN
        ))
    if (post_check or (full_readback and attempted == planned
                       and snapshot.dispatch == Dispatch.MAY_HAVE_WRITTEN
                       and snapshot.termination == Termination.COMPLETED)):
        uncertain = frozenset()
        return result(State.VERIFIED, (Action.RETURN_TO_CARD,))

    if never_sent:
        may_have_changed = False
        if snapshot.backup == Backup.FAILED:
            return result(State.BACKUP_FAILED, (
                Action.CHECK_BACKUP_STORAGE, *prefix, Action.PREPARE_NEW_PLAN
            ))
        state = (State.CANCELLED_BEFORE_WRITE
                 if snapshot.termination == Termination.CANCELLED
                 else State.NOT_WRITTEN)
        return result(state, (*prefix, Action.PREPARE_NEW_PLAN))
    return result(State.WRITE_UNCERTAIN, (
        *prefix, Action.READ_AND_COMPARE, Action.PREPARE_NEW_PLAN
    ))
