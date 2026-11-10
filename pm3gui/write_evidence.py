"""Adapters for the snapshot's real HFWritePlan and write-report v1.

No commands are sent and no report is written here. A v1 file has no operation,
revision, known-map or options binding; it is therefore never live readback.
Only a captured, unchanged plan instance can supply authoritative final native
verification. The Qt owner must additionally gate callbacks by job/generation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from uuid import uuid4

from pm3gui.core import DumpData
from pm3gui.writing import HFWritePlan, WriteOptions

from .card_snapshot import known_byte_mask
from .operation_recovery import (
    Backup, Binding, Dispatch, Link, ReadbackEvidence, SourceSelection,
    Termination, WriteSnapshot,
)


_REPORT_STATUSES = frozenset({
    "prepared", "review", "approved", "writing", "verifying", "verified",
    "failed", "not_written",
})


@dataclass(frozen=True)
class PlanCapture:
    """One read-only review binding; neither an approval nor a write token."""

    binding: Binding
    selected_source: SourceSelection
    _plan: HFWritePlan = field(repr=False, compare=False)
    _source_digest: str = field(repr=False)
    _plan_digest: str = field(repr=False)


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(*parts: bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _block_set(values, size: int) -> frozenset[int]:
    if (not isinstance(values, (list, tuple, set, frozenset))
            or any(type(block) is not int or not 0 <= block < size for block in values)
            or len(values) != len(set(values))):
        raise ValueError("Invalid native block progress.")
    return frozenset(values)


def _source_digest(plan: HFWritePlan, selection_ref: str) -> str:
    source = plan.source
    if (not isinstance(source, DumpData) or not isinstance(source.data, bytes)
            or not isinstance(plan.options, WriteOptions)):
        raise ValueError("A native immutable source/options snapshot is required.")
    classic_size = getattr(plan.target, "classic_size", "") if plan.options.family == "mfc" else ""
    known = known_byte_mask(
        source,
        protocol="MIFARE Classic" if plan.options.family == "mfc" else "Ultralight/NTAG",
        classic_size=classic_size,
    )
    native_known = _block_set(plan._known, len(source.data))
    # Include both the UI-normalized coverage and the exact writer selection.
    # Old read_validation can affect this binding, but never grants readback.
    attributes = {
        "selection_ref": selection_ref,
        "format": source.format,
        "block_size": source.block_size,
        "metadata": source.metadata,
        "native_known_bytes": sorted(native_known),
        "options": asdict(plan.options),
    }
    return _digest(b"pm3-source-v1", source.data, bytes(known), _json_bytes(attributes))


def _plan_digest(plan: HFWritePlan) -> str:
    if plan.options.family not in {"mfc", "mfu"}:
        raise ValueError("Only native HFWritePlan RF writes are supported.")
    unit = 16 if plan.options.family == "mfc" else 4
    if getattr(plan, "unit_size", None) != unit or len(plan.source.data) % unit:
        raise ValueError("Native write unit is not confirmed.")
    count = len(plan.source.data) // unit
    _entries(plan.entries, count, unit)
    target_uid = getattr(plan.target, "uid", "")
    if not isinstance(target_uid, str) or not target_uid:
        raise ValueError("The review has no confirmed native target context.")
    # entries['new'] is the effective write plan. Classic trailer ACL may be
    # preserved from the target, so it must not be replaced by raw source bytes.
    return _digest(b"pm3-plan-v1", _json_bytes({
        "entries": plan.entries,
        "family": plan.options.family,
        "unit_size": unit,
        "target_uid": target_uid,
        "expected_uid": plan.expected_uid,
    }))


def _entries(entries, count: int, unit: int) -> frozenset[int]:
    if not isinstance(entries, list):
        raise ValueError("Native report entries must be a list.")
    blocks = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid native report entry.")
        blocks.append(entry.get("block"))
        for name in ("old", "new"):
            if not isinstance(entry.get(name), str) or re.fullmatch(
                    r"[0-9A-Fa-f]{" + str(unit * 2) + r"}", entry[name]) is None:
                raise ValueError("Invalid native entry byte length.")
        if (entry.get("type") not in {"", "A", "B"}
                or not isinstance(entry.get("key"), str)
                or type(entry.get("trailer")) is not bool):
            raise ValueError("Invalid native entry authentication fields.")
    return _block_set(blocks, count)


def capture_plan(
    plan: HFWritePlan, *, operation_ref: str, target_ref: str,
    selection_ref: str, persisted: bool = False,
) -> PlanCapture:
    """Capture the native read-only review, before approve().

    References are opaque local IDs. target_ref is an operation-local target
    context, not a claim that a UID uniquely identifies a physical card.
    persisted is supplied only for a real selected Library revision.
    """
    if not isinstance(plan, HFWritePlan):
        raise ValueError("A real HFWritePlan instance is required.")
    if (not plan.ready or plan.approved or plan.done or plan.pending is not None
            or plan.error):
        raise ValueError("Capture the completed read-only review before approval.")
    if any(not isinstance(ref, str) or not ref.strip()
           for ref in (operation_ref, target_ref, selection_ref)):
        raise ValueError("Operation, target and selected-source references are required.")
    source_digest = _source_digest(plan, selection_ref)
    plan_digest = _plan_digest(plan)
    binding = Binding(
        operation_ref=operation_ref,
        target_ref=target_ref,
        source_snapshot_ref="source:sha256:" + source_digest,
        plan_ref="plan:" + uuid4().hex + ":" + plan_digest,
    )
    return PlanCapture(
        binding,
        SourceSelection(selection_ref, binding.source_snapshot_ref, persisted is True),
        plan, source_digest, plan_digest,
    )


def _unknown_snapshot(capture, termination, link) -> WriteSnapshot:
    return WriteSnapshot(
        binding=None,
        selected_source=capture.selected_source if isinstance(capture, PlanCapture) else None,
        termination=termination, dispatch=Dispatch.UNKNOWN, link=link,
        planned_blocks=None, may_have_written=None,
    )


def _validate_report_v1(report: dict) -> tuple[frozenset[int], frozenset[int], frozenset[int]]:
    if (not isinstance(report, dict) or type(report.get("version")) is not int
            or report["version"] != 1):
        raise ValueError("Expected native write-report version 1.")
    if report.get("family") not in {"mfc", "mfu"} or report.get("status") not in _REPORT_STATUSES:
        raise ValueError("Unknown native report family/status.")
    if not isinstance(report.get("source_sha256"), str) or re.fullmatch(
            r"[0-9a-f]{64}", report["source_sha256"]) is None:
        raise ValueError("Native report source hash is missing.")
    if any(not isinstance(report.get(key), str) for key in ("uid", "expected_uid", "error")):
        raise ValueError("Native report identity/error fields are invalid.")
    unit = 16 if report["family"] == "mfc" else 4
    planned = _entries(report.get("entries"), 256, unit)
    attempted = _block_set(report.get("may_have_written"), 256)
    verified = _block_set(report.get("verified_blocks"), 256)
    _block_set(report.get("skipped_unknown_blocks"), 256)
    backup = report.get("backup")
    if (not isinstance(backup, dict)
            or report.get("backup_scope") != "selected_data_blocks_and_known_sector_keys"):
        raise ValueError("Unknown native backup scope.")
    for block, raw in backup.items():
        if (not isinstance(block, str) or re.fullmatch(r"0|[1-9][0-9]*", block) is None
                or not 0 <= int(block) < 256 or not isinstance(raw, str)
                or re.fullmatch(r"[0-9A-Fa-f]{" + str(unit * 2) + r"}", raw) is None):
            raise ValueError("Invalid native backup block.")
    return planned, attempted, verified


def snapshot_from_report_v1(report: dict, *, link: Link = Link.UNCONFIRMED) -> WriteSnapshot:
    """Import a v1 report as unbound historical claims, never a current success.

    Empty may_have_written in a file cannot prove that this operation sent
    nothing. No live capture or selected revision can be reconstructed from v1.
    """
    planned, _attempted, reported = _validate_report_v1(report)
    return WriteSnapshot(
        binding=None, selected_source=None, termination=Termination.UNKNOWN,
        dispatch=Dispatch.UNKNOWN, link=link, planned_blocks=planned,
        may_have_written=None, reported_verified_blocks=reported,
        error_present=bool(report["error"]),
    )


def _report_matches_plan(report: dict, plan: HFWritePlan) -> bool:
    _validate_report_v1(report)
    expected = {
        "uid": plan.target.uid,
        "expected_uid": plan.expected_uid,
        "family": plan.options.family,
        "source_sha256": hashlib.sha256(plan.source.data).hexdigest(),
        "backup": {str(key): value for key, value in plan.backup.items()},
        "entries": deepcopy(plan.entries),
        "skipped_unknown_blocks": list(plan.skipped),
        "verified_blocks": list(plan.verified),
        "may_have_written": list(plan.may_have_written),
        "error": plan.error,
    }
    if any(report.get(key) != value for key, value in expected.items()):
        return False
    if plan.done:
        status = ("failed" if plan.may_have_written else "not_written") if plan.error else "verified"
        return report["status"] == status
    return report["status"] not in {"failed", "not_written", "verified"}


def snapshot_from_live_plan(
    plan: HFWritePlan, capture: PlanCapture, *, termination: Termination,
    link: Link, report: dict | None = None, report_persisted: bool = False,
) -> WriteSnapshot:
    """Adapt an unchanged live plan plus an optional freshly read native report.

    report_persisted=True means the caller actually read that file from this
    plan's directory. A dictionary alone never proves successful persistence.
    Invalid/foreign/stale context yields an unbound inconclusive snapshot.
    Partial native verified progress remains a report claim: only completion
    through final_identity supplies authoritative final readback here.
    """
    unknown = _unknown_snapshot(capture, termination, link)
    if (not isinstance(capture, PlanCapture) or not isinstance(plan, HFWritePlan)
            or capture._plan is not plan):
        return unknown
    try:
        if (_source_digest(plan, capture.selected_source.selection_ref) != capture._source_digest
                or _plan_digest(plan) != capture._plan_digest):
            return unknown
        unit = 16 if plan.options.family == "mfc" else 4
        count = len(plan.source.data) // unit
        planned = _entries(plan.entries, count, unit)
        attempted = _block_set(plan.may_have_written, count)
        reported = _block_set(plan.verified, count)
        # verified is a result of this writer's dispatch/consume chain. It
        # cannot include untouched blocks, unlike a separate post-check.
        if not reported <= attempted <= planned:
            return unknown
        if report is not None and not _report_matches_plan(report, plan):
            return unknown
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return unknown

    final_native_readback = (
        termination == Termination.COMPLETED
        and plan.done is True and plan.phase == "final_identity"
        and plan.pending is None and not plan.error
        and bool(planned) and reported == planned and attempted == planned
    )
    readback = ReadbackEvidence(
        capture.binding, reported, reported, final_target_confirmed=True,
        post_termination_check=False,
    ) if final_native_readback else None
    saved = report is not None and report_persisted is True
    backed_up = frozenset(plan.backup)
    backup = (Backup.COMPLETE if bool(planned) and planned <= backed_up
              else Backup.PARTIAL if backed_up else Backup.UNKNOWN)
    return WriteSnapshot(
        binding=capture.binding, selected_source=capture.selected_source,
        termination=termination, link=link,
        dispatch=Dispatch.MAY_HAVE_WRITTEN if attempted else Dispatch.NOT_SENT,
        planned_blocks=planned, may_have_written=attempted,
        # v1 and HFWritePlan expose no independent ACK ledger. Do not invent it.
        acknowledged_blocks=frozenset(), reported_verified_blocks=reported,
        readback=readback, backup=backup, backup_persisted=saved and bool(backed_up),
        backup_binding=capture.binding if saved and backed_up else None,
        report_persisted=saved, report_binding=capture.binding if saved else None,
        error_present=bool(plan.error) or termination == Termination.FAILED,
    )
