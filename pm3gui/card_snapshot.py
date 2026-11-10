"""Adapters to the snapshot's real DumpData, Library and editor contract.

Known means a recorded value, not a fresh read or an authenticated key.  No
historical metadata becomes post-write verification.  This module does no RF I/O.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import re

from pm3gui.core import DumpData, Library, _checked
from pm3gui.operations import sector_trailer
from pm3gui.partial import validate_partial_metadata
from pm3gui.studio_data import compare_dumps
from .semantic_diff import (
    ClassicGeometry, DiffSummary, Protocol, Representation,
    ScopeKind, Snapshot, SnapshotModel, SnapshotScope, TransitionCounts,
    compare_snapshots,
)


@dataclass(frozen=True)
class CardSnapshot:
    snapshot: Snapshot
    source_ref: str
    persisted: bool
    fingerprint: str
    block_size: int
    source_kind: str = "recorded_dump"
    card_id: str | None = None
    revision_id: str | None = None
    issues: tuple[str, ...] = ()

    @property
    def rf_verified(self) -> bool:
        """Saved data and metadata cannot establish fresh RF verification."""
        return False


@dataclass(frozen=True)
class GenericDiff:
    status: str
    counts: TransitionCounts = TransitionCounts()
    # Same row fields as studio_data.compare_dumps, plus explicit presence.
    changes: tuple[dict, ...] = field(default=(), repr=False)
    added_offsets: tuple[int, ...] = ()
    removed_offsets: tuple[int, ...] = ()
    equal_known_bytes: int = 0
    unknown_in_both_bytes: int = 0
    compared_bytes: int = 0


@dataclass(frozen=True)
class CardComparison:
    generic: GenericDiff
    semantic: DiffSummary | None


def _model(dump: DumpData, protocol: str, classic_size: str) -> SnapshotModel:
    metadata = dump.metadata
    partial = metadata.get("partial_dump") or {}
    validation = metadata.get("read_validation") or {}
    source = metadata.get("source_json") or {}
    if not all(isinstance(x, dict) for x in (partial, validation, source)):
        raise ValueError("Invalid dump metadata objects.")
    labels = " ".join((protocol, str(metadata.get("protocol", ""))))
    classic = (
        partial.get("kind") == "mfc" or validation.get("kind") == "mfc"
        or source.get("FileType") in {"mfcard", "mfc v2", "mfc v3"}
        or re.search(r"\b(?:mifare\s+classic|mfc|classic)\b", labels, re.I) is not None
    )
    if classic and (
        partial.get("kind") not in (None, "mfc")
        or validation.get("kind") not in (None, "mfc")
        or source.get("FileType") not in (None, "raw", "mfcard", "mfc v2", "mfc v3")
    ):
        raise ValueError("Conflicting explicit card families.")
    other_declared = bool(labels.strip() or source.get("FileType") not in (None, "raw")
                          or partial.get("kind") or validation.get("kind"))
    declared_sizes = []
    if classic_size:
        if not classic:
            raise ValueError("Classic size alone does not declare a Classic family.")
        declared_sizes.append(ClassicGeometry(classic_size.lower()))
    if classic:
        if partial.get("classic_size"):
            declared_sizes.append(ClassicGeometry(partial["classic_size"].lower()))
        for found in re.findall(r"\b(mini|[124]k)\b", labels, re.I):
            declared_sizes.append(ClassicGeometry(found.lower()))
    if len(set(declared_sizes)) > 1:
        raise ValueError("Conflicting explicit Classic geometries.")
    geometry = declared_sizes[0] if declared_sizes else None
    return SnapshotModel(
        Protocol.MIFARE_CLASSIC if classic else Protocol.OTHER if other_declared else Protocol.UNTYPED_RAW,
        classic or other_declared, geometry, geometry is not None,
    )


def _known_offsets(dump: DumpData, model: SnapshotModel) -> tuple[frozenset[int], tuple[str, ...]]:
    metadata = dump.metadata
    issues = []
    validation = metadata.get("read_validation") or {}
    if validation:
        issues.append("historical_read_metadata_not_fresh_verification")
    partial = metadata.get("partial_dump")
    if partial is not None:
        checked = validate_partial_metadata(partial, dump.data)
        if dump.block_size != checked["block_size"]:
            raise ValueError("DumpData block size disagrees with its known-byte map.")
        return frozenset(checked["known_bytes"]), tuple(issues + ["validated_partial_known_map"])

    # Real parse_dump and Library full records store literal bytes without a
    # bitmap.  All such values are recorded, including zero; this is not a
    # claim that the chip's full memory was read or that it is still present.
    known = set(range(len(dump.data)))
    issues.append("recorded_values_without_bitmap")
    if validation.get("kind") == "mfc" and model.protocol is Protocol.MIFARE_CLASSIC:
        if dump.block_size != 16 or len(dump.data) % 16:
            raise ValueError("Classic read metadata requires 16-byte blocks.")
        # Full block reads still mask A and may substitute B.  Apply the same
        # key-evidence distinction as partial_classic.  No length -> geometry
        # inference is needed to locate complete trailer blocks in the buffer.
        slots = validation.get("known_key_slots", [])
        if not isinstance(slots, list):
            raise ValueError("known_key_slots must be a list.")
        supplied = {}
        ambiguous = set()
        for entry in slots:
            if not isinstance(entry, dict):
                raise ValueError("Invalid known-key slot.")
            sector, kind, key = entry.get("sector"), entry.get("type", entry.get("key_type")), entry.get("key_hex")
            if type(sector) is not int or not 0 <= sector < 40 or kind not in ("A", "B") or not isinstance(key, str) or not re.fullmatch(r"[0-9A-Fa-f]{12}", key):
                raise ValueError("Invalid known-key slot.")
            slot = (sector, kind)
            raw = bytes.fromhex(key)
            if slot in supplied and supplied[slot] != raw:
                ambiguous.add(slot)
            supplied[slot] = raw
        for sector in range(40):
            base = sector_trailer(sector) * 16
            if base + 16 > len(dump.data):
                break
            for kind, offset in (("A", 0), ("B", 10)):
                span = range(base + offset, base + offset + 6)
                known.difference_update(span)
                slot = (sector, kind)
                if slot not in ambiguous and supplied.get(slot) == dump.data[base + offset:base + offset + 6]:
                    known.update(span)
        if len(known) != len(dump.data):
            issues.append("classic_masked_key_fields_unknown")
    if validation.get("secret_fields_may_be_unreadable") is True:
        # MFU records have no precise secret-field bitmap in this snapshot.
        # Keep literal file values but prevent a claim of fully read secrets.
        issues.append("secret_field_read_coverage_not_established")
    return frozenset(known), tuple(issues)


def known_byte_mask(dump: DumpData, *, protocol: str = "", classic_size: str = "") -> tuple[bool, ...]:
    """Shared normalized mask for adapters and captured write-plan binding."""
    _checked(dump)
    model = _model(dump, protocol, classic_size)
    known, _ = _known_offsets(dump, model)
    return tuple(i in known for i in range(len(dump.data)))


def adapt_dump(dump: DumpData, *, source_ref: str, persisted: bool = False,
               protocol: str = "", classic_size: str = "") -> CardSnapshot:
    _checked(dump)
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ValueError("An explicit source reference is required.")
    if type(persisted) is not bool:
        raise ValueError("persisted must be bool.")
    model = _model(dump, protocol, classic_size)
    known, issues = _known_offsets(dump, model)
    metadata = dump.metadata
    if "structured_document" in metadata:
        representation = (Representation.DETECTION if metadata.get("snapshot_kind") == "detection-record"
                          else Representation.STRUCTURED_DOCUMENT)
    elif metadata.get("record_kind") == "identifier":
        representation = Representation.IDENTIFIER
    else:
        representation = Representation.FLAT_MEMORY
    kind = ScopeKind.SELECTED_REGION
    if representation is not Representation.FLAT_MEMORY:
        kind = ScopeKind.UNDECLARED
    elif model.geometry is not None:
        if len(dump.data) == model.geometry.size_bytes and dump.block_size == 16:
            kind = ScopeKind.FULL_MODEL_MEMORY
        else:
            kind = ScopeKind.UNDECLARED
            issues += ("classic_extent_not_full_model",)
    snapshot = Snapshot(bytes(dump.data), known, model, SnapshotScope(kind, 0, len(dump.data)), representation)
    # Identity includes selection, bytes, mask, representation and model/scope;
    # equal byte content from another revision is still another selection.
    params = {"source_ref": source_ref, "block_size": dump.block_size,
              "format": dump.format, "declared_protocol": protocol,
              "metadata_protocol": metadata.get("protocol"),
              "source_filetype": (metadata.get("source_json") or {}).get("FileType"),
              "representation": representation.value, "scope": kind.value,
              "protocol": model.protocol.value, "protocol_confirmed": model.protocol_confirmed,
              "geometry": model.geometry.value if model.geometry else None,
              "length": len(dump.data)}
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode())
    digest.update(dump.data)
    digest.update(bytes(i in known for i in range(len(dump.data))))
    return CardSnapshot(snapshot, source_ref, persisted, digest.hexdigest(), dump.block_size, issues=issues)


def load_revision(library: Library, card_id: str, revision_id: str, *, classic_size: str = "") -> CardSnapshot:
    """Load the requested immutable revision, never silently select latest."""
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("An explicit revision ID is required.")
    card = library.get_card(card_id)
    result = adapt_dump(library.get_dump(card_id, revision_id),
                        source_ref=f"library:{card_id}:{revision_id}", persisted=True,
                        protocol=card["protocol"], classic_size=classic_size)
    return replace(result, source_kind="saved_revision", card_id=card_id, revision_id=revision_id)


def working_snapshot(owner, *, classic_size: str = "") -> CardSnapshot:
    """Read MainWindow's current editor through its existing _working_dump API.

    A working buffer remains an unsaved selection even if its bytes equal the
    selected saved revision.  Inherited read_validation is never new evidence.
    """
    if owner.loaded_dump is None:
        raise ValueError("No editor snapshot selected.")
    card_id = owner.card_id
    revision_id = owner.loaded_revision
    if not card_id or not revision_id:
        raise ValueError("The editor's base revision is not selected.")
    dump = deepcopy(owner._working_dump())
    # The real Library owns card/revision identity and checks stored SHA256.
    base = owner.library.get_dump(card_id, revision_id)
    result = adapt_dump(dump, source_ref=f"editor:{card_id}:{revision_id}",
                        protocol=owner.protocol.text(), classic_size=classic_size)
    issues = result.issues + ("working_copy_not_saved_revision",)
    if dump.data != base.data or dump.metadata != base.metadata or dump.block_size != base.block_size:
        issues += ("working_copy_changed",)
    return replace(result, source_kind="working_copy", card_id=card_id,
                   revision_id=revision_id, issues=issues)


def compare_card_snapshots(a: CardSnapshot, b: CardSnapshot) -> CardComparison:
    """Use the project's generic diff, filtered by accepted known-byte state.

    Generic offsets are record offsets; unequal lengths retain added/removed
    bytes separately. Classic semantics are an optional separate layer.
    """
    left, right = a.snapshot, b.snapshot
    allowed = {Representation.FLAT_MEMORY, Representation.IDENTIFIER}
    if left.representation not in allowed or right.representation not in allowed or left.representation is not right.representation:
        return CardComparison(GenericDiff("unsupported_representation"), None)
    aa = DumpData(left.data, "bin", {"partial_dump": {"known_bytes": sorted(left.known_bytes)}}, a.block_size)
    bb = DumpData(right.data, "bin", {"partial_dump": {"known_bytes": sorted(right.known_bytes)}}, b.block_size)
    rows = []
    gained = lost = changed = 0
    added, removed = [], []
    overlap = min(len(left.data), len(right.data))
    for original in compare_dumps(aa, bb):
        i = original["offset"]
        a_present, b_present = i < len(left.data), i < len(right.data)
        # Existing compare_dumps emits unequal unknown fillers: remove them.
        if a_present and b_present and not original["a_known"] and not original["b_known"]:
            continue
        row = {**original, "a_present": a_present, "b_present": b_present}
        if not row["a_known"]:
            row["a"] = None
        if not row["b_known"]:
            row["b"] = None
        rows.append(row)
        if not a_present:
            added.append(i)
        elif not b_present:
            removed.append(i)
        elif not row["a_known"]:
            gained += 1
        elif not row["b_known"]:
            lost += 1
        else:
            changed += 1
    common_known = left.known_bytes & right.known_bytes
    generic = GenericDiff(
        "comparable", TransitionCounts(gained, lost, changed), tuple(rows), tuple(added), tuple(removed),
        sum(left.data[i] == right.data[i] for i in common_known),
        sum(i not in left.known_bytes and i not in right.known_bytes for i in range(overlap)), overlap,
    )
    semantic = None
    if (left.model.protocol is Protocol.MIFARE_CLASSIC and right.model.protocol is Protocol.MIFARE_CLASSIC):
        # Snapshot sequences remain None: direction is explicit UI A -> B,
        # including reverse and same-version comparisons.
        semantic = compare_snapshots(left, right)
    return CardComparison(generic, semantic)
