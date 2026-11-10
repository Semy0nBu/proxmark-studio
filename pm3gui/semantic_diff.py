"""Compare two explicitly modelled Classic snapshots without device I/O.

The caller supplies A (older) and B (newer), known-byte maps and confirmed model
metadata. Nothing is inferred from a file extension, buffer length or equality.
Only counts and positions leave the comparison API; no raw keys/UIDs are emitted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class ClassicGeometry(str, Enum):
    MINI = "mini"
    K1 = "1k"
    K2 = "2k"
    K4 = "4k"

    @property
    def size_bytes(self) -> int:
        return _GEOMETRIES[self][1]

    @property
    def sectors(self) -> int:
        return _GEOMETRIES[self][0]


_GEOMETRIES = {
    ClassicGeometry.MINI: (5, 320),
    ClassicGeometry.K1: (16, 1024),
    ClassicGeometry.K2: (32, 2048),
    ClassicGeometry.K4: (40, 4096),
}


class Protocol(str, Enum):
    MIFARE_CLASSIC = "mifare_classic"
    UNTYPED_RAW = "untyped_raw"
    OTHER = "other"


class Representation(str, Enum):
    FLAT_MEMORY = "flat_memory"
    STRUCTURED_DOCUMENT = "structured_document"
    IDENTIFIER = "identifier"
    DETECTION = "detection"


class ScopeKind(str, Enum):
    FULL_MODEL_MEMORY = "full_model_memory"
    SELECTED_REGION = "selected_region"
    UNDECLARED = "undeclared"


class ByteCategory(str, Enum):
    USER_DATA = "user_data"
    KEY_A = "key_a"
    KEY_B = "key_b"
    ACCESS_BITS = "access_bits"
    GPB = "gpb"
    MANUFACTURER = "manufacturer_block_0"


class Transition(str, Enum):
    BECAME_KNOWN = "became_known"
    KNOWLEDGE_LOST = "knowledge_lost"
    KNOWN_VALUES_DIFFER = "known_values_differ"


class ComparisonStatus(str, Enum):
    COMPARABLE = "comparable"
    UNCONFIRMED_MODEL = "unconfirmed_model"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    UNSUPPORTED_REPRESENTATION = "unsupported_representation"
    MODEL_MISMATCH = "model_mismatch"
    UNDECLARED_SCOPE = "undeclared_scope"
    INVALID_SOURCE_BOUNDS = "invalid_source_bounds"
    SOURCE_BOUNDS_MISMATCH = "source_bounds_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"
    CHRONOLOGY_MISMATCH = "chronology_mismatch"


def _integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer at least {minimum}.")


@dataclass(frozen=True)
class SnapshotModel:
    protocol: Protocol
    protocol_confirmed: bool
    geometry: ClassicGeometry | None
    geometry_confirmed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.protocol, Protocol):
            raise TypeError("Use an explicit Protocol member.")
        if type(self.protocol_confirmed) is not bool or type(self.geometry_confirmed) is not bool:
            raise TypeError("Model confirmation fields must be bool.")
        if self.geometry is not None and not isinstance(self.geometry, ClassicGeometry):
            raise TypeError("Use an explicit ClassicGeometry member or None.")


@dataclass(frozen=True)
class SnapshotScope:
    kind: ScopeKind
    start_offset: int
    length_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScopeKind):
            raise TypeError("Use an explicit ScopeKind member.")
        _integer(self.start_offset, "start_offset")
        _integer(self.length_bytes, "length_bytes", minimum=1)

    @property
    def end_offset(self) -> int:
        """Absolute exclusive end of the declared scope."""
        return self.start_offset + self.length_bytes


@dataclass(frozen=True)
class Snapshot:
    data: bytes = field(repr=False)
    known_bytes: frozenset[int] = field(repr=False)
    model: SnapshotModel
    scope: SnapshotScope
    representation: Representation = Representation.FLAT_MEMORY
    sequence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("Snapshot data must be immutable bytes.")
        if not isinstance(self.model, SnapshotModel) or not isinstance(self.scope, SnapshotScope):
            raise TypeError("Snapshot requires explicit model and scope objects.")
        if not isinstance(self.representation, Representation):
            raise TypeError("Use an explicit Representation member.")
        if self.known_bytes is None:
            raise TypeError("known_bytes is required; missing metadata is not 'all known'.")
        known = frozenset(self.known_bytes)
        if any(type(i) is not int or not 0 <= i < len(self.data) for i in known):
            raise ValueError("known_bytes must contain local in-buffer integer offsets.")
        object.__setattr__(self, "known_bytes", known)
        if self.sequence is not None:
            _integer(self.sequence, "sequence")


@dataclass(frozen=True)
class Coverage:
    scope_kind: ScopeKind
    start_offset: int
    end_offset: int
    declared_scope_bytes: int
    available_buffer_bytes: int
    known_buffer_bytes: int
    unknown_buffer_bytes: int
    known_fraction_within_scope: float | None
    model_size_bytes: int | None
    known_fraction_of_model_lower_bound: float | None
    all_declared_scope_bytes_known: bool
    full_model_map_known: bool


@dataclass(frozen=True)
class BytePosition:
    offset: int
    block: int
    sector: int
    byte_in_block: int
    category: ByteCategory


@dataclass(frozen=True)
class PositionChange:
    position: BytePosition
    transition: Transition


@dataclass(frozen=True)
class TransitionCounts:
    became_known: int = 0
    knowledge_lost: int = 0
    known_values_differ: int = 0

    @property
    def total(self) -> int:
        return self.became_known + self.knowledge_lost + self.known_values_differ


@dataclass(frozen=True)
class CategorySummary:
    category: ByteCategory
    counts: TransitionCounts


INTERPRETATION_LIMITS = (
    "A is the caller-selected older snapshot; B is the newer snapshot.",
    "Known-value differences describe snapshots, not proven physical-card changes.",
    "Equality does not establish card identity, post-write verification or editing history.",
    "Coverage describes the declared memory scope, not hardware validation or freshness.",
)


@dataclass(frozen=True)
class DiffSummary:
    status: ComparisonStatus
    reason: str
    coverage_a: Coverage
    coverage_b: Coverage
    counts: TransitionCounts = TransitionCounts()
    categories: tuple[CategorySummary, ...] = ()
    changes: tuple[PositionChange, ...] = ()
    compared_bytes: int = 0
    equal_known_bytes: int = 0
    unknown_in_both_bytes: int = 0
    sequence_order_checked: bool = False
    interpretation_limits: tuple[str, ...] = INTERPRETATION_LIMITS

    def to_dict(self) -> dict:
        """JSON-compatible summary containing no source bytes, labels or identifiers."""
        return asdict(self)


def _confirmed_geometry(snapshot: Snapshot) -> ClassicGeometry | None:
    model = snapshot.model
    if (model.protocol is Protocol.MIFARE_CLASSIC and model.protocol_confirmed
            and model.geometry_confirmed and model.geometry is not None):
        return model.geometry
    return None


def _valid_extent(snapshot: Snapshot, geometry: ClassicGeometry) -> bool:
    scope = snapshot.scope
    if len(snapshot.data) != scope.length_bytes or scope.end_offset > geometry.size_bytes:
        return False
    if scope.kind is ScopeKind.FULL_MODEL_MEMORY:
        return scope.start_offset == 0 and scope.length_bytes == geometry.size_bytes
    return scope.kind is ScopeKind.SELECTED_REGION


def coverage(snapshot: Snapshot) -> Coverage:
    """Compute declared-scope coverage; do not upgrade length to read evidence."""
    known_count = len(snapshot.known_bytes)
    scope = snapshot.scope
    geometry = _confirmed_geometry(snapshot)
    valid_buffer = len(snapshot.data) == scope.length_bytes
    flat_memory = snapshot.representation is Representation.FLAT_MEMORY
    valid_model_extent = flat_memory and geometry is not None and _valid_extent(snapshot, geometry)
    scope_declared = flat_memory and scope.kind is not ScopeKind.UNDECLARED
    full_scope_known = valid_buffer and scope_declared and known_count == scope.length_bytes
    return Coverage(
        scope_kind=scope.kind,
        start_offset=scope.start_offset,
        end_offset=scope.end_offset,
        declared_scope_bytes=scope.length_bytes,
        available_buffer_bytes=len(snapshot.data),
        known_buffer_bytes=known_count,
        unknown_buffer_bytes=len(snapshot.data) - known_count,
        known_fraction_within_scope=(known_count / scope.length_bytes
                                     if valid_buffer and scope_declared else None),
        model_size_bytes=geometry.size_bytes if geometry else None,
        known_fraction_of_model_lower_bound=(known_count / geometry.size_bytes
                                             if valid_model_extent else None),
        all_declared_scope_bytes_known=full_scope_known,
        full_model_map_known=bool(full_scope_known and valid_model_extent
                                  and scope.kind is ScopeKind.FULL_MODEL_MEMORY),
    )


def locate_classic_byte(offset: int, geometry: ClassicGeometry) -> BytePosition:
    """Locate a byte in an explicitly supplied Classic geometry (including 4K)."""
    if not isinstance(geometry, ClassicGeometry):
        raise TypeError("Classic geometry must be explicit.")
    _integer(offset, "offset")
    if offset >= geometry.size_bytes:
        raise ValueError("Byte offset is outside the declared Classic geometry.")
    block, within = divmod(offset, 16)
    sector = block // 4 if block < 128 else 32 + (block - 128) // 16
    trailer = sector * 4 + 3 if sector < 32 else 128 + (sector - 32) * 16 + 15
    if block == 0:
        category = ByteCategory.MANUFACTURER
    elif block != trailer:
        category = ByteCategory.USER_DATA
    elif within < 6:
        category = ByteCategory.KEY_A
    elif within < 9:
        category = ByteCategory.ACCESS_BITS
    elif within == 9:
        category = ByteCategory.GPB
    else:
        category = ByteCategory.KEY_B
    return BytePosition(offset, block, sector, within, category)


def _counts(changes: tuple[PositionChange, ...]) -> TransitionCounts:
    return TransitionCounts(
        became_known=sum(c.transition is Transition.BECAME_KNOWN for c in changes),
        knowledge_lost=sum(c.transition is Transition.KNOWLEDGE_LOST for c in changes),
        known_values_differ=sum(c.transition is Transition.KNOWN_VALUES_DIFFER for c in changes),
    )


def compare_snapshots(older: Snapshot, newer: Snapshot) -> DiffSummary:
    """Compare A -> B without inferring model, provenance or hardware outcomes.

    Known-byte offsets are local to each data buffer. Reported positions are
    absolute within the confirmed memory model. Different source bounds are
    rejected explicitly, not represented as lost/gained bytes outside overlap.
    Optional sequence values must both be provided and strictly increase. When
    absent on both sides, temporal ordering is the caller's explicit choice.
    """
    a_coverage, b_coverage = coverage(older), coverage(newer)
    order_checked = older.sequence is not None and newer.sequence is not None

    def reject(status: ComparisonStatus, reason: str) -> DiffSummary:
        return DiffSummary(status, reason, a_coverage, b_coverage,
                           sequence_order_checked=order_checked)

    if older.representation is not Representation.FLAT_MEMORY or newer.representation is not Representation.FLAT_MEMORY:
        return reject(ComparisonStatus.UNSUPPORTED_REPRESENTATION,
                      "Only flat memory snapshots are supported; no JSON-tree or identifier flattening.")
    if (older.sequence is None) != (newer.sequence is None):
        return reject(ComparisonStatus.CHRONOLOGY_MISMATCH,
                      "Supply both sequence values or explicitly choose direction with neither.")
    if order_checked and older.sequence >= newer.sequence:
        return reject(ComparisonStatus.CHRONOLOGY_MISMATCH,
                      "A must be older than B in the same caller-defined revision sequence.")
    if older.model.protocol != newer.model.protocol:
        return reject(ComparisonStatus.MODEL_MISMATCH, "The declared protocols differ.")
    if older.model.protocol is not Protocol.MIFARE_CLASSIC:
        return reject(ComparisonStatus.UNSUPPORTED_PROTOCOL,
                      "Classic semantics are unavailable for untyped raw or other protocols.")
    a_geometry, b_geometry = _confirmed_geometry(older), _confirmed_geometry(newer)
    if a_geometry is None or b_geometry is None:
        return reject(ComparisonStatus.UNCONFIRMED_MODEL,
                      "Both protocol and Classic geometry require explicit confirmation.")
    if a_geometry is not b_geometry:
        return reject(ComparisonStatus.MODEL_MISMATCH, "The confirmed Classic geometries differ.")
    if older.scope.kind is ScopeKind.UNDECLARED or newer.scope.kind is ScopeKind.UNDECLARED:
        return reject(ComparisonStatus.UNDECLARED_SCOPE, "Both memory scopes must be declared.")
    if not _valid_extent(older, a_geometry) or not _valid_extent(newer, b_geometry):
        return reject(ComparisonStatus.INVALID_SOURCE_BOUNDS,
                      "A buffer, declared scope or model extent is inconsistent.")
    if (older.scope.start_offset, older.scope.end_offset) != (newer.scope.start_offset, newer.scope.end_offset):
        return reject(ComparisonStatus.SOURCE_BOUNDS_MISMATCH,
                      "Source ranges differ; no implicit truncation, padding or overlap comparison.")
    if older.scope.kind is not newer.scope.kind:
        return reject(ComparisonStatus.SCOPE_MISMATCH,
                      "Source scope kinds differ; an adapter must resolve them explicitly.")

    changes = []
    equal_known = unknown_both = 0
    for local_offset in range(len(older.data)):
        a_known = local_offset in older.known_bytes
        b_known = local_offset in newer.known_bytes
        if not a_known and not b_known:
            unknown_both += 1
            continue
        if not a_known:
            transition = Transition.BECAME_KNOWN
        elif not b_known:
            transition = Transition.KNOWLEDGE_LOST
        elif older.data[local_offset] != newer.data[local_offset]:
            transition = Transition.KNOWN_VALUES_DIFFER
        else:
            equal_known += 1
            continue
        position = locate_classic_byte(older.scope.start_offset + local_offset, a_geometry)
        changes.append(PositionChange(position, transition))

    immutable_changes = tuple(changes)
    return DiffSummary(
        status=ComparisonStatus.COMPARABLE,
        reason="Compared recorded values and known-byte state within matching declared scopes.",
        coverage_a=a_coverage,
        coverage_b=b_coverage,
        counts=_counts(immutable_changes),
        categories=tuple(CategorySummary(category, _counts(tuple(
            change for change in immutable_changes if change.position.category is category
        ))) for category in ByteCategory),
        changes=immutable_changes,
        compared_bytes=len(older.data),
        equal_known_bytes=equal_known,
        unknown_in_both_bytes=unknown_both,
        sequence_order_checked=order_checked,
    )
