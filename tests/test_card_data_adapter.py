"""Regression tests against actual snapshot DumpData/Library/partial builders."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from pm3gui.core import DumpData, Library, parse_dump
from pm3gui.operations import Detection, validate_read_dump
from pm3gui.partial import partial_classic, partial_ultralight
from pm3gui.studio_data import structured_dump
from pm3gui.semantic_diff import ClassicGeometry, ComparisonStatus, Protocol, Representation, coverage
from pm3gui.card_snapshot import adapt_dump, compare_card_snapshots, known_byte_mask, load_revision, working_snapshot


class CardDataAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = Library(self.root / "cards.sqlite")
        self.addCleanup(self.library.close)

    def bin_dump(self, data, name="import.bin"):
        path = self.root / name
        path.write_bytes(data)
        return parse_dump(path)

    def partial_classic_dump(self):
        path = self.root / "partial.json"
        path.write_text(json.dumps({
            "FileType": "mfc v2", "Card": {"UID": "A1B2C3D4"},
            "blocks": {"0": "A1B2C3D4" + "00" * 12, "1": "00" * 16,
                       "3": "112233445566FF078069A0A1A2A3A4A5"},
        }))
        output = "\n".join(f"[+] Sector... 0 block... {b} ( ok )" for b in (0, 1, 3))
        return partial_classic(path, Detection("mfc", "A1B2C3D4", "MIFARE Classic 1K", "1k", True),
                               output, "A1B2C3D4", [{"sector": 0, "type": "A", "key_hex": "112233445566"}])

    def full_classic_read(self):
        data = bytearray(1024)
        for sector in range(16):
            base = (sector * 4 + 3) * 16
            data[base:base + 16] = bytes.fromhex("112233445566FF078069A0A1A2A3A4A5")
        path = self.root / "full.json"
        path.write_text(json.dumps({"FileType": "mfc v2", "Card": {"UID": "A1B2C3D4"},
                                    "blocks": {str(i): data[i * 16:(i + 1) * 16].hex() for i in range(64)}}))
        output = "\n".join(f"[+] Sector... {s} block... {b} ( ok )" for s in range(16) for b in range(4))
        return validate_read_dump(path, Detection("mfc", "A1B2C3D4", "MIFARE Classic 1K", "1k", True), output)

    def test_full_zero_bin_import_without_bitmap_has_recorded_values(self):
        item = adapt_dump(self.bin_dump(bytes(1024)), source_ref="file:zero")
        self.assertEqual(len(item.snapshot.known_bytes), 1024)
        self.assertEqual(item.snapshot.model.protocol, Protocol.UNTYPED_RAW)
        self.assertIsNone(item.snapshot.model.geometry)
        self.assertFalse(coverage(item.snapshot).full_model_map_known)
        self.assertFalse(item.rf_verified)

    def test_plain_json_import_without_bitmap_is_not_all_unknown(self):
        path = self.root / "ntag.json"
        path.write_text(json.dumps({"FileType": "mfu", "blocks": {str(i): "00000000" for i in range(45)}}))
        item = adapt_dump(parse_dump(path), source_ref="file:ntag")
        self.assertEqual(len(item.snapshot.known_bytes), 180)
        self.assertEqual(item.snapshot.model.protocol, Protocol.OTHER)
        self.assertFalse(item.rf_verified)

    def test_full_read_without_bitmap_does_not_trust_masked_keys(self):
        dump = self.full_classic_read()
        item = adapt_dump(dump, source_ref="session:read", protocol="MIFARE Classic 1K")
        self.assertEqual(len(item.snapshot.known_bytes), 1024 - 16 * 12)
        self.assertTrue(set(range(54, 58)) <= item.snapshot.known_bytes)
        self.assertTrue(set(range(48, 54)).isdisjoint(item.snapshot.known_bytes))
        self.assertTrue(set(range(58, 64)).isdisjoint(item.snapshot.known_bytes))
        self.assertFalse(item.rf_verified)

    def test_complete_key_flag_without_slots_does_not_make_keys_known(self):
        dump = self.full_classic_read()
        dump.metadata["read_validation"]["all_key_slots_known"] = True
        self.assertEqual(sum(known_byte_mask(dump, protocol="MIFARE Classic", classic_size="1k")), 832)

    def test_full_read_with_matching_slots_has_full_recorded_coverage(self):
        dump = self.full_classic_read()
        dump.metadata["read_validation"]["known_key_slots"] = [
            {"sector": sector, "type": kind, "key_hex": key}
            for sector in range(16)
            for kind, key in (("A", "112233445566"), ("B", "A0A1A2A3A4A5"))
        ]
        item = adapt_dump(dump, source_ref="full-read", protocol="MIFARE Classic 1K")
        self.assertEqual(len(item.snapshot.known_bytes), 1024)
        self.assertTrue(coverage(item.snapshot).full_model_map_known)
        self.assertFalse(item.rf_verified)

    def test_matching_key_slots_restore_only_their_fields(self):
        dump = self.full_classic_read()
        dump.metadata["read_validation"]["known_key_slots"] = [
            {"sector": 0, "type": "A", "key_hex": "112233445566"},
            {"sector": 0, "type": "B", "key_hex": "FFFFFFFFFFFF"},
        ]
        item = adapt_dump(dump, source_ref="session:read", protocol="MIFARE Classic 1K")
        self.assertTrue(set(range(48, 54)) <= item.snapshot.known_bytes)
        self.assertTrue(set(range(58, 64)).isdisjoint(item.snapshot.known_bytes))
        self.assertEqual(len(item.snapshot.known_bytes), 838)

    def test_conflicting_duplicate_key_slots_are_unknown(self):
        dump = self.full_classic_read()
        dump.metadata["read_validation"]["known_key_slots"] = [
            {"sector": 0, "type": "A", "key_hex": "FFFFFFFFFFFF"},
            {"sector": 0, "type": "A", "key_hex": "112233445566"},
        ]
        self.assertFalse(known_byte_mask(dump, protocol="MIFARE Classic 1K")[48])

    def test_real_partial_builder_masks_a_b_and_retains_real_zero_block(self):
        dump = self.partial_classic_dump()
        item = adapt_dump(dump, source_ref="read:partial")
        self.assertEqual(item.snapshot.model.geometry, ClassicGeometry.K1)
        self.assertEqual(item.snapshot.known_bytes, frozenset(dump.metadata["partial_dump"]["known_bytes"]))
        self.assertIn(16, item.snapshot.known_bytes)
        self.assertNotIn(32, item.snapshot.known_bytes)
        self.assertIn(48, item.snapshot.known_bytes)
        self.assertNotIn(58, item.snapshot.known_bytes)

    def test_corrupt_partial_map_is_rejected_by_actual_validator(self):
        dump = self.partial_classic_dump()
        dump.metadata["partial_dump"]["known_bytes"].append(1024)
        with self.assertRaises(ValueError):
            adapt_dump(dump, source_ref="bad:map")

    def test_generic_unknown_filler_changes_are_ignored(self):
        a = self.partial_classic_dump()
        b = deepcopy(a)
        data = bytearray(b.data)
        data[32:48] = b"\xAB" * 16
        b.data = bytes(data)
        result = compare_card_snapshots(adapt_dump(a, source_ref="a"), adapt_dump(b, source_ref="b"))
        self.assertEqual(result.generic.changes, ())
        self.assertEqual(result.generic.counts.total, 0)
        self.assertEqual(result.semantic.counts.total, 0)

    def test_other_family_generic_diff_suppresses_unknown_fillers(self):
        path = self.root / "partial-ntag.json"
        uid = "04112233445566"
        path.write_text(json.dumps({"FileType": "mfu", "Card": {"UID": uid},
                                    "blocks": {str(i): "00000000" for i in range(10)}}))
        header = bytearray(56)
        header[11] = 9
        path.with_suffix(".bin").write_bytes(header + bytes(40))
        a = partial_ultralight(path, Detection("mfu", uid, "NTAG 213", supported=True),
                               "Reading tag memory\nPartial dump created. (10 of 45 blocks)", uid)
        b = deepcopy(a)
        b.data = b"\x01" + b.data[1:40] + b"\xAB" * 140
        result = compare_card_snapshots(adapt_dump(a, source_ref="a"), adapt_dump(b, source_ref="b"))
        self.assertIsNone(result.semantic)
        self.assertEqual([r["offset"] for r in result.generic.changes], [0])
        self.assertEqual(result.generic.counts.known_values_differ, 1)
        self.assertEqual(result.generic.unknown_in_both_bytes, 140)

    def test_generic_known_gain_and_loss_are_directional(self):
        partial = self.partial_classic_dump()
        full = DumpData(partial.data, "bin", {}, 16)
        a = adapt_dump(partial, source_ref="partial")
        b = adapt_dump(full, source_ref="full", protocol="MIFARE Classic 1K")
        forward, backward = compare_card_snapshots(a, b), compare_card_snapshots(b, a)
        missing = 1024 - len(a.snapshot.known_bytes)
        self.assertEqual(forward.generic.counts.became_known, missing)
        self.assertEqual(backward.generic.counts.knowledge_lost, missing)
        self.assertEqual(forward.semantic.counts.became_known, missing)
        self.assertEqual(backward.semantic.counts.knowledge_lost, missing)

    def test_generic_different_lengths_retain_added_removed_offsets(self):
        a = adapt_dump(self.bin_dump(b"AB", "a.bin"), source_ref="a")
        b = adapt_dump(self.bin_dump(b"ABC", "b.bin"), source_ref="b")
        result = compare_card_snapshots(a, b).generic
        self.assertEqual(result.added_offsets, (2,))
        self.assertEqual(result.counts.total, 0)
        self.assertFalse(result.changes[0]["a_present"])
        self.assertEqual(compare_card_snapshots(b, a).generic.removed_offsets, (2,))

    def test_explicit_geometry_required_for_semantic_layer(self):
        dump = DumpData(bytes(1024), "bin", {}, 16)
        raw = adapt_dump(dump, source_ref="raw")
        family = adapt_dump(dump, source_ref="family", protocol="MIFARE Classic")
        explicit = adapt_dump(dump, source_ref="explicit", protocol="MIFARE Classic", classic_size="1k")
        self.assertIsNone(raw.snapshot.model.geometry)
        self.assertIsNone(family.snapshot.model.geometry)
        self.assertEqual(compare_card_snapshots(family, family).semantic.status, ComparisonStatus.UNCONFIRMED_MODEL)
        self.assertEqual(compare_card_snapshots(explicit, explicit).semantic.status, ComparisonStatus.COMPARABLE)
        self.assertEqual(compare_card_snapshots(explicit, explicit).semantic.counts.total, 0)

    def test_size_without_family_and_conflicting_profiles_are_rejected(self):
        dump = DumpData(bytes(1024), "bin", {}, 16)
        with self.assertRaises(ValueError):
            adapt_dump(dump, source_ref="raw", classic_size="1k")
        with self.assertRaises(ValueError):
            adapt_dump(dump, source_ref="conflict", protocol="MIFARE Classic 4K", classic_size="1k")
        mfu = DumpData(bytes(180), "json", {"source_json": {"FileType": "mfu"}}, 4)
        with self.assertRaises(ValueError):
            adapt_dump(mfu, source_ref="conflict", protocol="MIFARE Classic 1K")

    def test_geometry_mismatched_buffer_is_not_full_card_or_semantic_success(self):
        item = adapt_dump(DumpData(bytes(64), "bin", {}, 16), source_ref="short", protocol="MIFARE Classic 1K")
        self.assertFalse(coverage(item.snapshot).full_model_map_known)
        self.assertEqual(compare_card_snapshots(item, item).semantic.status, ComparisonStatus.UNDECLARED_SCOPE)

    def test_structured_document_not_flattened_to_chip_memory(self):
        item = adapt_dump(structured_dump({"applications": []}, "DESFire"), source_ref="document")
        self.assertEqual(item.snapshot.representation, Representation.STRUCTURED_DOCUMENT)
        self.assertEqual(compare_card_snapshots(item, item).generic.status, "unsupported_representation")
        self.assertIsNone(compare_card_snapshots(item, item).semantic)

    def test_identifier_uses_generic_record_bytes_only(self):
        dump = DumpData(bytes(5), "bin", {"record_kind": "identifier", "protocol": "EM410x"}, 5)
        item = adapt_dump(dump, source_ref="identifier")
        self.assertEqual(item.snapshot.representation, Representation.IDENTIFIER)
        self.assertEqual(compare_card_snapshots(item, item).generic.status, "comparable")
        self.assertFalse(coverage(item.snapshot).full_model_map_known)

    def test_selected_old_revision_does_not_become_latest(self):
        card = self.library.create_card("Lab", self.bin_dump(b"old"))
        old = self.library.list_revisions(card)[0]["id"]
        new = self.library.save_revision(card, self.bin_dump(b"new"), "changed")
        selected = load_revision(self.library, card, old)
        self.assertEqual(selected.snapshot.data, b"old")
        self.assertEqual(selected.revision_id, old)
        self.assertNotEqual(selected.fingerprint, load_revision(self.library, card, new).fingerprint)
        self.assertTrue(selected.persisted)
        self.assertFalse(selected.rf_verified)
        with self.assertRaises(ValueError):
            load_revision(self.library, card, None)

    def test_library_checksum_and_revision_ownership_are_not_bypassed(self):
        card = self.library.create_card("Lab", self.bin_dump(b"abc"))
        rev = self.library.list_revisions(card)[0]["id"]
        other = self.library.create_card("Other", self.bin_dump(b"def"))
        with self.assertRaises(KeyError):
            load_revision(self.library, other, rev)
        self.library.connection.execute("UPDATE revisions SET data=? WHERE id=?", (b"bad", rev))
        with self.assertRaises(ValueError):
            load_revision(self.library, card, rev)

    def test_working_copy_keeps_selected_base_and_drops_verification_claims(self):
        saved = self.full_classic_read()
        card = self.library.create_card("Lab", saved, protocol="MIFARE Classic 1K")
        old = self.library.list_revisions(card)[0]["id"]
        self.library.save_revision(card, DumpData(b"\xFF" * 1024, "bin", {}, 16), "newest")
        changed = deepcopy(saved)
        changed.data = b"\x10" + changed.data[1:]
        owner = SimpleNamespace(library=self.library, card_id=card, loaded_revision=old,
                                loaded_dump=saved, protocol=SimpleNamespace(text=lambda: "MIFARE Classic 1K"),
                                _working_dump=lambda: changed)
        current = working_snapshot(owner)
        selected = load_revision(self.library, card, old)
        self.assertEqual(current.revision_id, old)
        self.assertEqual(current.snapshot.data[0], 16)
        self.assertEqual(selected.snapshot.data[0], 0)
        self.assertFalse(current.persisted)
        self.assertFalse(current.rf_verified)
        self.assertIn("working_copy_changed", current.issues)
        self.assertEqual(compare_card_snapshots(selected, current).generic.counts.known_values_differ, 1)
        self.assertEqual(saved.data[0], 0)

    def test_binding_fingerprint_covers_known_map_revision_and_parameters(self):
        partial = self.partial_classic_dump()
        no_map = DumpData(partial.data, "bin", {}, 16)
        a = adapt_dump(partial, source_ref="same")
        b = adapt_dump(no_map, source_ref="same", protocol="MIFARE Classic 1K")
        c = adapt_dump(partial, source_ref="another-revision")
        self.assertEqual(a.snapshot.data, b.snapshot.data)
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.fingerprint, c.fingerprint)
        raw = adapt_dump(no_map, source_ref="same")
        self.assertNotEqual(b.fingerprint, raw.fingerprint)
        ntag = adapt_dump(no_map, source_ref="same", protocol="NTAG 213")
        t55 = adapt_dump(no_map, source_ref="same", protocol="T5577")
        self.assertNotEqual(ntag.fingerprint, t55.fingerprint)

    def test_mfu_read_metadata_is_never_fresh_secret_coverage(self):
        dump = DumpData(bytes(180), "json", {"read_validation": {"kind": "mfu", "secret_fields_may_be_unreadable": True}}, 4)
        item = adapt_dump(dump, source_ref="saved:read")
        self.assertEqual(len(item.snapshot.known_bytes), 180)
        self.assertIn("secret_field_read_coverage_not_established", item.issues)
        self.assertFalse(item.rf_verified)


if __name__ == "__main__":
    unittest.main()
