"""Synthetic acceptance tests for the standalone semantic-diff proposal."""

import json
from pathlib import Path
import unittest
from collections import Counter
from dataclasses import replace

from pm3gui.semantic_diff import (  # noqa: E402
    ByteCategory, ClassicGeometry, ComparisonStatus, Protocol, Representation,
    ScopeKind, Snapshot, SnapshotModel, SnapshotScope, Transition,
    compare_snapshots, coverage, locate_classic_byte,
)


def classic_snapshot(data=None, *, known=None, geometry=ClassicGeometry.K1,
                     start=0, scope=ScopeKind.FULL_MODEL_MEMORY, sequence=None):
    """An explicitly labelled synthetic fixture, never a production adapter."""
    if data is None:
        data = bytes(geometry.size_bytes)
    return Snapshot(
        data=data,
        known_bytes=frozenset(range(len(data)) if known is None else known),
        model=SnapshotModel(Protocol.MIFARE_CLASSIC, True, geometry, True),
        scope=SnapshotScope(scope, start, len(data)),
        sequence=sequence,
    )


class TransitionTests(unittest.TestCase):
    def test_all_semantic_cases_partition_the_comparison(self):
        a = bytearray(16)
        b = bytearray(16)
        a[0], b[0] = 0x11, 0x99  # Unknown fillers differ: ignore.
        a[2], b[2] = 0x33, 0x55  # Knowledge lost, not a value difference.
        a[3] = b[3] = 0x44  # Equal known value.
        a[4], b[4] = 0x10, 0x20  # Both known and different.
        older = classic_snapshot(bytes(a), known={2, 3, 4}, start=16,
                                 scope=ScopeKind.SELECTED_REGION)
        newer = classic_snapshot(bytes(b), known={1, 3, 4}, start=16,
                                 scope=ScopeKind.SELECTED_REGION)
        summary = compare_snapshots(older, newer)
        self.assertEqual(summary.status, ComparisonStatus.COMPARABLE)
        self.assertEqual((summary.counts.became_known,
                          summary.counts.knowledge_lost,
                          summary.counts.known_values_differ), (1, 1, 1))
        self.assertEqual(summary.equal_known_bytes, 1)
        self.assertEqual(summary.unknown_in_both_bytes, 12)
        self.assertEqual(summary.counts.total + summary.equal_known_bytes
                         + summary.unknown_in_both_bytes, summary.compared_bytes)
        self.assertEqual([c.position.offset for c in summary.changes], [17, 18, 20])

    def test_unknown_to_known_zero_is_new_information_even_when_bytes_equal(self):
        older = classic_snapshot(known=set())
        newer = classic_snapshot(known={100})
        summary = compare_snapshots(older, newer)
        self.assertEqual(summary.counts.became_known, 1)
        self.assertEqual(summary.counts.known_values_differ, 0)
        self.assertEqual(summary.changes[0].transition, Transition.BECAME_KNOWN)

    def test_known_to_unknown_is_information_loss_even_when_bytes_equal(self):
        summary = compare_snapshots(classic_snapshot(known={100}),
                                    classic_snapshot(known=set()))
        self.assertEqual(summary.counts.knowledge_lost, 1)
        self.assertEqual(summary.counts.known_values_differ, 0)

    def test_unknown_fillers_never_create_differences(self):
        older = classic_snapshot(bytes([0xAA]) * 1024, known=set())
        newer = classic_snapshot(bytes([0x55]) * 1024, known=set())
        summary = compare_snapshots(older, newer)
        self.assertEqual(summary.changes, ())
        self.assertEqual(summary.unknown_in_both_bytes, 1024)
        self.assertEqual(summary.equal_known_bytes, 0)

    def test_newly_known_key_is_not_reported_as_changed_key(self):
        key_offset = 3 * 16
        older = classic_snapshot(known=set())
        newer = classic_snapshot(known=range(key_offset, key_offset + 6))
        summary = compare_snapshots(older, newer)
        key_a = next(c for c in summary.categories if c.category is ByteCategory.KEY_A)
        self.assertEqual(key_a.counts.became_known, 6)
        self.assertEqual(key_a.counts.known_values_differ, 0)

    def test_equal_values_do_not_create_provenance_or_verification_claims(self):
        summary = compare_snapshots(classic_snapshot(sequence=1),
                                    classic_snapshot(sequence=2))
        self.assertEqual(summary.equal_known_bytes, 1024)
        self.assertEqual(summary.counts.total, 0)
        payload = summary.to_dict()
        self.assertNotIn("post_write_verified", payload)
        self.assertNotIn("manually_edited", payload)
        self.assertNotIn("same_physical_card", payload)
        self.assertIn("Equality does not establish", " ".join(summary.interpretation_limits))

    def test_raw_keys_and_uid_bytes_are_not_in_summary_or_snapshot_repr(self):
        payload = bytearray(1024)
        payload[0:8] = bytes.fromhex("DEADBEEFCAFEBABE")
        payload[48:54] = bytes.fromhex("A1B2C3D4E5F6")
        newer = classic_snapshot(bytes(payload))
        summary = compare_snapshots(classic_snapshot(), newer)
        encoded = json.dumps(summary.to_dict()).upper()
        self.assertNotIn("DEADBEEFCAFEBABE", encoded)
        self.assertNotIn("A1B2C3D4E5F6", encoded)
        self.assertNotIn("data=", repr(newer))
        self.assertTrue(summary.changes)
        self.assertFalse(hasattr(summary.changes[0], "old_value"))
        self.assertFalse(hasattr(summary.changes[0], "new_value"))


class GeometryTests(unittest.TestCase):
    def test_all_classic_categories_have_exact_boundaries(self):
        cases = {
            0: ByteCategory.MANUFACTURER, 15: ByteCategory.MANUFACTURER,
            16: ByteCategory.USER_DATA, 47: ByteCategory.USER_DATA,
            48: ByteCategory.KEY_A, 53: ByteCategory.KEY_A,
            54: ByteCategory.ACCESS_BITS, 56: ByteCategory.ACCESS_BITS,
            57: ByteCategory.GPB, 58: ByteCategory.KEY_B, 63: ByteCategory.KEY_B,
            64: ByteCategory.USER_DATA,
        }
        for offset, category in cases.items():
            with self.subTest(offset=offset):
                self.assertEqual(locate_classic_byte(offset, ClassicGeometry.K1).category, category)

    def test_four_k_uses_sixteen_block_tail_sectors(self):
        for block in (128, 131, 135, 139, 142):
            with self.subTest(block=block):
                position = locate_classic_byte(block * 16, ClassicGeometry.K4)
                self.assertEqual(position.sector, 32)
                self.assertEqual(position.category, ByteCategory.USER_DATA)
        first_tail_trailer = locate_classic_byte(143 * 16, ClassicGeometry.K4)
        self.assertEqual((first_tail_trailer.sector, first_tail_trailer.category),
                         (32, ByteCategory.KEY_A))
        next_sector = locate_classic_byte(144 * 16, ClassicGeometry.K4)
        self.assertEqual((next_sector.sector, next_sector.category), (33, ByteCategory.USER_DATA))
        last = locate_classic_byte(4095, ClassicGeometry.K4)
        self.assertEqual((last.block, last.sector, last.byte_in_block, last.category),
                         (255, 39, 15, ByteCategory.KEY_B))

    def test_full_four_k_category_partition(self):
        totals = Counter(locate_classic_byte(i, ClassicGeometry.K4).category for i in range(4096))
        self.assertEqual(totals, {
            ByteCategory.USER_DATA: 3440, ByteCategory.KEY_A: 240,
            ByteCategory.KEY_B: 240, ByteCategory.ACCESS_BITS: 120,
            ByteCategory.GPB: 40, ByteCategory.MANUFACTURER: 16,
        })

    def test_each_geometry_has_the_correct_last_sector(self):
        for geometry, last_sector in ((ClassicGeometry.MINI, 4), (ClassicGeometry.K1, 15),
                                      (ClassicGeometry.K2, 31), (ClassicGeometry.K4, 39)):
            with self.subTest(geometry=geometry):
                p = locate_classic_byte(geometry.size_bytes - 1, geometry)
                self.assertEqual((p.sector, p.category), (last_sector, ByteCategory.KEY_B))
                with self.assertRaises(ValueError):
                    locate_classic_byte(geometry.size_bytes, geometry)

    def test_non_aligned_region_reports_absolute_addresses(self):
        older = classic_snapshot(bytes(3), known=set(), start=47, scope=ScopeKind.SELECTED_REGION)
        newer = classic_snapshot(bytes(3), start=47, scope=ScopeKind.SELECTED_REGION)
        summary = compare_snapshots(older, newer)
        self.assertEqual([c.position.offset for c in summary.changes], [47, 48, 49])
        self.assertEqual([c.position.category for c in summary.changes],
                         [ByteCategory.USER_DATA, ByteCategory.KEY_A, ByteCategory.KEY_A])


class CompatibilityTests(unittest.TestCase):
    def test_raw_1024_bytes_are_not_guessed_to_be_classic(self):
        raw_model = SnapshotModel(Protocol.UNTYPED_RAW, True, None, False)
        older = replace(classic_snapshot(), model=raw_model)
        summary = compare_snapshots(older, older)
        self.assertEqual(summary.status, ComparisonStatus.UNSUPPORTED_PROTOCOL)
        self.assertEqual(summary.compared_bytes, 0)
        self.assertIsNone(summary.coverage_a.model_size_bytes)

    def test_other_protocol_is_not_classic(self):
        model = SnapshotModel(Protocol.OTHER, True, None, False)
        source = replace(classic_snapshot(), model=model)
        self.assertEqual(compare_snapshots(source, source).status,
                         ComparisonStatus.UNSUPPORTED_PROTOCOL)

    def test_protocol_and_geometry_each_require_confirmation(self):
        base = classic_snapshot()
        models = (
            replace(base.model, protocol_confirmed=False),
            replace(base.model, geometry_confirmed=False),
            replace(base.model, geometry=None),
        )
        for model in models:
            with self.subTest(model=model):
                summary = compare_snapshots(replace(base, model=model), base)
                self.assertEqual(summary.status, ComparisonStatus.UNCONFIRMED_MODEL)
                self.assertEqual(summary.changes, ())

    def test_different_confirmed_geometries_are_incompatible(self):
        summary = compare_snapshots(classic_snapshot(geometry=ClassicGeometry.K1),
                                    classic_snapshot(geometry=ClassicGeometry.K4))
        self.assertEqual(summary.status, ComparisonStatus.MODEL_MISMATCH)
        self.assertEqual(summary.counts.total, 0)

    def test_structured_json_identifier_and_detection_are_not_flattened(self):
        base = classic_snapshot()
        for representation in (Representation.STRUCTURED_DOCUMENT, Representation.IDENTIFIER,
                               Representation.DETECTION):
            with self.subTest(representation=representation):
                source = replace(base, representation=representation)
                summary = compare_snapshots(source, source)
                self.assertEqual(summary.status, ComparisonStatus.UNSUPPORTED_REPRESENTATION)
                self.assertEqual(summary.compared_bytes, 0)
                self.assertFalse(summary.coverage_a.full_model_map_known)

    def test_source_length_mismatch_is_not_knowledge_loss(self):
        older = classic_snapshot(bytes(32), start=16, scope=ScopeKind.SELECTED_REGION)
        newer = classic_snapshot(bytes(16), start=16, scope=ScopeKind.SELECTED_REGION)
        summary = compare_snapshots(older, newer)
        self.assertEqual(summary.status, ComparisonStatus.SOURCE_BOUNDS_MISMATCH)
        self.assertEqual(summary.changes, ())
        self.assertEqual(summary.counts.knowledge_lost, 0)

    def test_equal_length_different_source_addresses_are_not_compared(self):
        older = classic_snapshot(bytes(16), start=16, scope=ScopeKind.SELECTED_REGION)
        newer = classic_snapshot(bytes(16), start=32, scope=ScopeKind.SELECTED_REGION)
        self.assertEqual(compare_snapshots(older, newer).status,
                         ComparisonStatus.SOURCE_BOUNDS_MISMATCH)

    def test_buffer_must_match_its_declared_length(self):
        base = classic_snapshot(bytes(16), start=16, scope=ScopeKind.SELECTED_REGION)
        wrong = replace(base, scope=SnapshotScope(ScopeKind.SELECTED_REGION, 16, 32))
        summary = compare_snapshots(wrong, base)
        self.assertEqual(summary.status, ComparisonStatus.INVALID_SOURCE_BOUNDS)
        self.assertIsNone(summary.coverage_a.known_fraction_within_scope)

    def test_full_scope_must_match_the_confirmed_geometry(self):
        wrong = classic_snapshot(bytes(1024), geometry=ClassicGeometry.K4)
        self.assertEqual(compare_snapshots(wrong, wrong).status,
                         ComparisonStatus.INVALID_SOURCE_BOUNDS)
        self.assertFalse(coverage(wrong).full_model_map_known)

    def test_region_cannot_extend_beyond_model(self):
        wrong = classic_snapshot(bytes(16), start=1016, scope=ScopeKind.SELECTED_REGION)
        summary = compare_snapshots(wrong, wrong)
        self.assertEqual(summary.status, ComparisonStatus.INVALID_SOURCE_BOUNDS)
        self.assertIsNone(summary.coverage_a.known_fraction_of_model_lower_bound)

    def test_equal_ranges_with_different_scope_kinds_need_resolution(self):
        full = classic_snapshot()
        selected = replace(full, scope=SnapshotScope(ScopeKind.SELECTED_REGION, 0, 1024))
        self.assertEqual(compare_snapshots(full, selected).status, ComparisonStatus.SCOPE_MISMATCH)

    def test_undeclared_scope_is_not_silently_upgraded(self):
        base = classic_snapshot()
        unknown = replace(base, scope=SnapshotScope(ScopeKind.UNDECLARED, 0, 1024))
        self.assertEqual(compare_snapshots(unknown, base).status, ComparisonStatus.UNDECLARED_SCOPE)
        self.assertIsNone(coverage(unknown).known_fraction_within_scope)

    def test_direction_is_checked_when_revision_sequence_is_available(self):
        first = classic_snapshot(sequence=10)
        second = classic_snapshot(sequence=11)
        self.assertTrue(compare_snapshots(first, second).sequence_order_checked)
        for a, b in ((second, first), (first, first), (first, replace(second, sequence=None))):
            with self.subTest(a=a.sequence, b=b.sequence):
                self.assertEqual(compare_snapshots(a, b).status, ComparisonStatus.CHRONOLOGY_MISMATCH)
        explicit_direction = compare_snapshots(classic_snapshot(), classic_snapshot())
        self.assertEqual(explicit_direction.status, ComparisonStatus.COMPARABLE)
        self.assertFalse(explicit_direction.sequence_order_checked)


class CoverageAndValidationTests(unittest.TestCase):
    def test_partial_full_length_buffer_has_partial_coverage(self):
        source = classic_snapshot(known=range(16))
        result = coverage(source)
        self.assertEqual((result.known_buffer_bytes, result.unknown_buffer_bytes), (16, 1008))
        self.assertEqual(result.known_fraction_within_scope, 16 / 1024)
        self.assertFalse(result.full_model_map_known)

    def test_completely_known_region_is_not_full_model_memory(self):
        source = classic_snapshot(bytes(16), geometry=ClassicGeometry.K4, start=16,
                                  scope=ScopeKind.SELECTED_REGION)
        result = coverage(source)
        self.assertTrue(result.all_declared_scope_bytes_known)
        self.assertEqual(result.known_fraction_within_scope, 1.0)
        self.assertEqual(result.known_fraction_of_model_lower_bound, 16 / 4096)
        self.assertFalse(result.full_model_map_known)

    def test_full_model_coverage_requires_all_addresses_known(self):
        result = coverage(classic_snapshot())
        self.assertTrue(result.full_model_map_known)
        self.assertEqual(result.known_fraction_of_model_lower_bound, 1.0)

    def test_known_map_must_be_explicit_and_inside_buffer(self):
        base = classic_snapshot()
        with self.assertRaises(TypeError):
            replace(base, known_bytes=None)
        for known in ({-1}, {1024}, {True}, {1.5}):
            with self.subTest(known=known), self.assertRaises(ValueError):
                replace(base, known_bytes=known)

    def test_snapshot_copies_known_set_and_requires_immutable_bytes(self):
        base = classic_snapshot()
        known = {16}
        snapshot = replace(base, known_bytes=known)
        known.add(17)
        self.assertEqual(snapshot.known_bytes, frozenset({16}))
        with self.assertRaises(TypeError):
            replace(base, data=bytearray(1024))

    def test_summary_is_json_serializable(self):
        summary = compare_snapshots(classic_snapshot(known=set()), classic_snapshot(known={54, 57}))
        encoded = json.dumps(summary.to_dict(), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["status"], "comparable")
        self.assertEqual(decoded["counts"]["became_known"], 2)


if __name__ == "__main__":
    unittest.main()
