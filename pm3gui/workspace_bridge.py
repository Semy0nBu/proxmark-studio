"""Opt-in adapter for the real MainWindow and HF write dialog.

The host controller calls bind_review at its existing review boundary and
capture_finished before releasing that job.  A captured epoch must travel with
each callback; the runner's untagged ``finished`` signal is not evidence of its
operation.  This object never sends a device command or changes a job's lifetime.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from pm3gui.core import DumpData
from pm3gui.memory_map import MemoryMapDialog
from pm3gui.writing import HFWritePlan

from .card_snapshot import (
    compare_card_snapshots, load_revision, working_snapshot,
)
from .write_evidence import capture_plan, snapshot_from_live_plan
from .operation_recovery import (
    Link, RecoveryDecision, Termination, ReadbackEvidence, decide_recovery,
)
from .semantic_diff import Protocol, ScopeKind

if TYPE_CHECKING:
    from pm3gui.app import MainWindow


class StudioAdapterBridge(QObject):
    """Use existing controls and controller state; add no duplicate version UI."""

    source_changed = Signal()
    result_changed = Signal()
    invalidated = Signal(str)

    def __init__(self, owner: MainWindow):
        super().__init__(owner)
        self.owner = owner
        self.epoch = 0
        self.last_rejection = ""
        self._job = self._plan = self._capture = self._dialog = None
        self._source = self._options = self._selection = None
        self._snapshot = None
        self._terminal = False
        self._dialog_signals = []
        # MainWindow may rebuild the QListWidget inside its selection slot.
        # A second currentItemChanged receiver would get deleted item pointers.
        # The controller schedules a refresh after load_card has completed.
        owner.revisions.currentIndexChanged.connect(self._source_signal)
        owner.hex_edit.textChanged.connect(self._source_signal)
        owner.protocol.textChanged.connect(self._source_signal)
        owner.runner.busy_changed.connect(self._busy_signal)

    def _source_signal(self, *_):
        # load_card/revision may emit multiple editor signals while rebuilding.
        # Consumers fetch after the event and existing loading guard clears.
        if not self.owner.loading:
            self.source_changed.emit()

    def _busy_signal(self, *_):
        self.result_changed.emit()

    @property
    def occupied(self) -> bool:
        """An idle process does not release the controller's cleanup fence."""
        return bool(
            self.owner.runner.busy or self.owner.read_job is not None
            or self.owner.antenna_job is not None or self.owner.firmware_busy
        )

    def current_source(self):
        if self.owner.loading:
            raise ValueError("The existing revision controller is loading.")
        working = working_snapshot(self.owner)
        saved = load_revision(self.owner.library, self.owner.card_id, self.owner.loaded_revision)
        if (not self.owner.dirty and working.snapshot == saved.snapshot
                and working.block_size == saved.block_size):
            return saved
        return working

    def compare_with_revision(self, revision_id: str):
        current = self.current_source()
        previous = load_revision(
            self.owner.library, self.owner.card_id, revision_id,
        )
        return compare_card_snapshots(previous, current)

    def create_memory_map(self) -> MemoryMapDialog:
        """Reuse the native read-only map with the accepted byte-knowledge mask.

        Its projection is view-only, never saved, exported or supplied to a
        writer.  Original metadata is untouched.  Classic is declared before
        calling the legacy map, whose own constructor infers size from length.
        """
        source = self.current_source()
        snapshot = source.snapshot
        if (snapshot.model.protocol is not Protocol.MIFARE_CLASSIC
                or not snapshot.model.protocol_confirmed
                or not snapshot.model.geometry_confirmed
                or snapshot.scope.kind is not ScopeKind.FULL_MODEL_MEMORY):
            raise ValueError("A confirmed Classic model and memory extent are required.")
        view = DumpData(
            snapshot.data, "bin",
            {"partial_dump": {"known_bytes": sorted(snapshot.known_bytes)}}, 16,
        )
        return MemoryMapDialog(self.owner, view)

    def _selection_ref(self, dialog) -> tuple[str, bool]:
        selection = dialog.sources.currentData()
        if not isinstance(selection, tuple) or len(selection) != 2:
            raise ValueError("A concrete source selection is required.")
        kind, value = selection
        if kind == "card":
            revision = dialog.source_versions.currentData()
            if not revision:
                raise ValueError("An explicit saved revision is required.")
            saved = self.owner.library.get_dump(value, revision)
            if saved != dialog.dump:
                raise ValueError("The selected revision differs from the dialog source.")
            return f"card:{value}/revision:{revision}", True
        if kind == "editor":
            # The dialog owns its deep copy.  MainWindow can later select a
            # different card; never re-label that copy as a newly saved revision.
            return "editor-copy", False
        if kind == "file":
            path_ref = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
            return "file:" + path_ref, False
        raise ValueError("Unsupported source selection.")

    def _disconnect_dialog(self):
        for signal in self._dialog_signals:
            try:
                signal.disconnect(self._dialog_changed)
            except (RuntimeError, TypeError):
                pass
        self._dialog_signals = []

    def _connect_dialog(self, dialog):
        self._disconnect_dialog()
        for name in ("sources", "source_versions", "family"):
            self._dialog_signals.append(getattr(dialog, name).currentIndexChanged)
        for name in ("key_a", "key_b", "mfu_key", "target_uid", "selected_blocks"):
            self._dialog_signals.append(getattr(dialog, name).textChanged)
        for name in ("secure", "include_keys", "include_unknown"):
            self._dialog_signals.append(getattr(dialog, name).toggled)
        for name in ("first", "last"):
            self._dialog_signals.append(getattr(dialog, name).valueChanged)
        for signal in self._dialog_signals:
            signal.connect(self._dialog_changed)

    def _dialog_matches(self) -> bool:
        if self._dialog is None:
            return False
        try:
            return (
                self._dialog.dump == self._source
                and self._dialog.options() == self._options
                and self._selection_ref(self._dialog) == self._selection
            )
        except (ValueError, KeyError, TypeError, OSError, RuntimeError):
            return False

    def _dialog_changed(self, *_):
        if self._capture is not None and not self._dialog_matches():
            self.invalidate("source_target_or_options_changed")

    def invalidate(self, reason: str):
        """Invalidate presentation/approval; let the host finish its own cleanup."""
        self.epoch += 1
        self.last_rejection = reason
        self._capture = None
        self._snapshot = None
        self._terminal = True
        if self._dialog is not None:
            self._dialog.write_button.setEnabled(False)
        self.invalidated.emit(reason)
        self.result_changed.emit()

    def bind_review(self, *, operation_ref: str | None = None,
                    target_ref: str | None = None) -> int:
        """Call once at a completed preflight review, before approve()."""
        job = self.owner.read_job
        if (self.owner.runner.busy or self.owner.antenna_job is not None
                or self.owner.firmware_busy or not isinstance(job, dict)
                or job.get("job_kind") != "write"
                or job.get("stage") != "write_transaction"):
            raise ValueError("No idle HF write review owned by this controller.")
        plan, dialog = job.get("write_plan"), job.get("write_dialog")
        if (not isinstance(plan, HFWritePlan) or dialog is None
                or dialog.plan is not plan or not plan.ready
                or plan.approved or plan.pending is not None):
            raise ValueError("The HF write plan is not at its review boundary.")
        selection, persisted = self._selection_ref(dialog)
        if dialog.dump != plan.source or dialog.options() != plan.options:
            raise ValueError("The review no longer matches the selected source/options.")
        capture = capture_plan(
            plan, operation_ref=operation_ref or "operation:" + uuid4().hex,
            target_ref=target_ref or "target-observation:" + uuid4().hex,
            selection_ref=selection, persisted=persisted,
        )
        self.epoch += 1
        self._job, self._plan, self._dialog = job, plan, dialog
        self._capture = capture
        self._source = deepcopy(dialog.dump)
        self._options = deepcopy(dialog.options())
        self._entries = deepcopy(plan.entries)
        self._target = deepcopy(plan.target)
        self._selection = (selection, persisted)
        self._snapshot = None
        self._terminal = False
        self.last_rejection = ""
        self._connect_dialog(dialog)
        return self.epoch

    def can_approve(self, epoch: int) -> bool:
        """Additional guard for the host's existing approve_card_write method."""
        return bool(
            epoch == self.epoch and self._capture is not None
            and not self._terminal and self.owner.read_job is self._job
            and self._job.get("job_kind") == "write"
            and self._job.get("stage") == "write_transaction"
            and not self._job.get("write_cancelled")
            and self._job.get("write_plan") is self._plan
            and self._dialog.plan is self._plan
            and not self._plan.approved and self._plan.ready
            and not self._plan.done and not self._plan.error
            and self._plan.pending is None and not self.owner.runner.busy
            and self.owner.antenna_job is None and not self.owner.firmware_busy
            and self._plan.source == self._source and self._plan.options == self._options
            and self._plan.entries == self._entries and self._plan.target == self._target
            and self._dialog_matches()
            and snapshot_from_live_plan(
                self._plan, self._capture,
                termination=Termination.RUNNING, link=Link.READY,
            ).binding == self._capture.binding
        )

    def capture_finished(self, epoch: int, plan: HFWritePlan, *,
                         termination: Termination, link: Link) -> bool:
        """Call for this job's terminal callback, before/after _end_write.

        Reading the local report only establishes its persistence.  Binding and
        readback evidence still come from the same captured live plan.
        """
        if (epoch != self.epoch or plan is not self._plan
                or self._capture is None or self._terminal):
            self.last_rejection = "late_or_foreign_callback"
            return False
        current_job = self.owner.read_job
        if current_job is not None and current_job is not self._job:
            self.last_rejection = "different_controller_job"
            return False
        if (self._job.get("write_plan") is not plan
                or self._job.get("write_dialog") is not self._dialog
                or getattr(self.owner, "write_dialog", self._dialog) is not self._dialog
                or not self._dialog_matches()):
            self.invalidate("review_context_changed")
            return False
        report = None
        try:
            report = json.loads((plan.directory / "write-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            pass
        self._snapshot = snapshot_from_live_plan(
            plan, self._capture, termination=termination, link=link,
            report=report, report_persisted=report is not None,
        )
        self._terminal = True
        self._dialog.write_button.setEnabled(False)
        self.result_changed.emit()
        return True

    def decision(self) -> RecoveryDecision | None:
        if self._snapshot is None:
            return None
        snapshot = self._snapshot
        if self.occupied:
            snapshot = replace(snapshot, link=Link.BUSY)
        return decide_recovery(snapshot)

    def accept_postcheck(self, epoch, binding, result):
        """Only the explicit fresh readback, after successful RF cleanup."""
        from dataclasses import asdict
        if (epoch != self.epoch or self._capture is None or self._snapshot is None
                or binding != self._capture.binding or self._snapshot.binding != binding
                or result.get('binding') != asdict(binding) or self.occupied):
            return False
        compared = frozenset(result['compared_blocks'])
        matched = frozenset(result['matched_blocks'])
        if not matched <= compared <= self._snapshot.planned_blocks or result.get('final_target_confirmed') is not True:
            return False
        self._snapshot = replace(self._snapshot, link=Link.READY,
            readback=ReadbackEvidence(binding, compared, matched, True, True))
        self.result_changed.emit()
        return True

