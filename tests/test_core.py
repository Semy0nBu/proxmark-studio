import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pm3gui.core import DumpData, Library, decode_bytes, encode_text, export_dump, parse_dump, parse_hex


class DumpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def test_binary_roundtrip_does_not_guess_protocol(self):
        data = bytes(range(256)) * 4
        for suffix in ("bin", "mfd"):
            with self.subTest(suffix=suffix):
                dump = parse_dump(self.write("in." + suffix, data))
                self.assertEqual(dump.data, data)
                self.assertNotIn("source_json", dump.metadata)
                out = self.root / ("out." + suffix)
                export_dump(dump, out)
                self.assertEqual(out.read_bytes(), data)

    def test_eml_preserves_original_and_encodes_edited_blocks(self):
        raw = b"00 01 02 03\r\n04 05 06 07\r\n"
        dump = parse_dump(self.write("in.eml", raw))
        self.assertEqual(dump.block_size, 4)
        self.assertEqual(dump.data, bytes(range(8)))
        out = self.root / "out.eml"
        export_dump(dump, out)
        self.assertEqual(out.read_bytes(), raw)
        dump.data = b"\xff" + dump.data[1:]
        export_dump(dump, out)
        self.assertEqual(parse_dump(out).data, dump.data)

    def test_eml_comments_are_ignored_and_roundtrip_exact_bytes(self):
        raw = "# Синтетические данные\r\n00010203\r\n\r\n  # another comment\r\n04050607\r\n".encode("utf-8")
        dump = parse_dump(self.write("comments.eml", raw))
        self.assertEqual(dump.data, bytes(range(8)))
        out = self.root / "comments-roundtrip.eml"
        export_dump(dump, out)
        self.assertEqual(out.read_bytes(), raw)
        dump.data = b"\xff" + dump.data[1:]
        export_dump(dump, out)
        self.assertEqual(parse_dump(out).data, dump.data)
        with self.assertRaises(ValueError):
            parse_dump(self.write("inline.eml", b"00010203 # not a full comment\n"))

    def test_json_preserves_metadata_and_reflects_edits(self):
        source = {"FileType": "mfc v2", "Card": {"UID": "AABBCCDD", "ATQA": "0400"},
                  "blocks": {"1": "11" * 16, "0": "00" * 16},
                  "SectorKeys": {"0": {"KeyA": "FFFFFFFFFFFF"}}, "custom": [1, "Пример"]}
        text = json.dumps(source, ensure_ascii=False)
        dump = parse_dump(self.write("in.json", text))
        self.assertEqual(dump.data, b"\0" * 16 + b"\x11" * 16)
        out = self.root / "out.json"
        export_dump(dump, out)
        self.assertEqual(out.read_text("utf-8"), text)
        dump.data = b"\x22" * 32
        export_dump(dump, out)
        edited = json.loads(out.read_text("utf-8"))
        self.assertEqual(edited["blocks"]["0"], "22" * 16)
        self.assertEqual(edited["Card"], source["Card"])
        self.assertEqual(edited["custom"], source["custom"])
        self.assertNotIn("SectorKeys", edited)
        self.assertEqual(dump.metadata["source_json"], source)

    def test_raw_json_uses_actual_pm3_raw_field(self):
        dump = parse_dump(self.write("in.json", '{"FileType":"raw","raw":"0001AABB","other":true}'))
        self.assertEqual(dump.data, b"\0\1\xaa\xbb")
        dump.data += b"\xcc"
        out = self.root / "out.json"
        export_dump(dump, out)
        self.assertEqual(json.loads(out.read_text())["raw"], "0001AABBCC")
        self.assertTrue(json.loads(out.read_text())["other"])

    def test_json_with_bom_roundtrips_exact_bytes(self):
        source = b'\xef\xbb\xbf{ "FileType": "raw", "raw": "0001" }\r\n'
        dump = parse_dump(self.write("bom.json", source))
        out = self.root / "roundtrip.json"
        export_dump(dump, out)
        self.assertEqual(out.read_bytes(), source)

    def test_sparse_duplicate_and_invalid_blocks_rejected(self):
        cases = [
            '{"FileType":"mfu","blocks":{"0":"00000000","2":"11111111"}}',
            '{"FileType":"mfu","blocks":{"00":"00000000"}}',
            '{"FileType":"mfu","blocks":{"0":"00000000","0":"11111111"}}',
            '{"FileType":"mfu","blocks":{"0":"0000000X"}}',
            '{"FileType":"mfu","blocks":{"0":"00"}}',
            '{"FileType":"raw","Raw":"00"}',
            '{"FileType":"raw","raw":"00","bad":NaN}',
        ]
        for source in cases:
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_dump(self.write("bad.json", source))

    def test_header_formats_cannot_silently_export_memory_as_pm3_bin(self):
        for filetype, block in (("mfu", "00010203"), ("15693 v5", "00010203"),
                                ("mfc v3", "00" * 16), ("topaz", "00" * 8)):
            with self.subTest(filetype=filetype):
                dump = parse_dump(self.write("in.json", json.dumps({
                    "FileType": filetype, "Card": {"Signature": "AB"}, "blocks": {"0": block}})))
                with self.assertRaisesRegex(ValueError, "заголовка"):
                    export_dump(dump, self.root / "bad.bin")
                self.assertFalse((self.root / "bad.bin").exists())
                export_dump(dump, self.root / "safe.json")
                self.assertEqual(parse_dump(self.root / "safe.json").metadata["source_json"]["Card"],
                                 {"Signature": "AB"})

    def test_unknown_json_type_is_preserved_but_not_assumed_flat_binary(self):
        dump = parse_dump(self.write("unknown.json", json.dumps({
            "FileType": "future protocol", "Card": {"Header": "ABCD"}, "blocks": {"0": "00112233"}})))
        for suffix in ("bin", "mfd"):
            out = self.root / ("blocked." + suffix)
            with self.assertRaisesRegex(ValueError, "не проверена"):
                export_dump(dump, out)
            self.assertFalse(out.exists())
        export_dump(dump, self.root / "preserved.json")
        self.assertEqual(parse_dump(self.root / "preserved.json").data, dump.data)

    def test_source_verified_flat_json_converts_without_invented_headers(self):
        for filetype, size in (("mfc v2", 16), ("fudan", 4), ("hitagu", 4),
                               ("iclass", 8), ("t55x7", 4), ("EM4X50", 4),
                               ("14b v2", 4), ("lto", 32), ("cryptorf", 8), ("ndef", 16)):
            with self.subTest(filetype=filetype):
                data = bytes(range(size * 2))
                dump = parse_dump(self.write("flat.json", json.dumps({
                    "FileType": filetype, "blocks": {"0": data[:size].hex(), "1": data[size:].hex()}})))
                export_dump(dump, self.root / "flat.bin")
                self.assertEqual((self.root / "flat.bin").read_bytes(), data)

    def test_strict_hex_and_codecs(self):
        self.assertEqual(parse_hex("00 ab\r\nCD\tEF"), bytes.fromhex("00ABCDEF"))
        for invalid in ("ABC", "0x00", "00:01", "GG", "00; hf search", "00\N{NO-BREAK SPACE}01"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_hex(invalid)
        text = "Привет, мир"
        for encoding in ("utf-8", "cp1251", "cp866", "koi8-r", "utf-16-le"):
            self.assertEqual(decode_bytes(encode_text(text, encoding), encoding), text)
        with self.assertRaises(UnicodeDecodeError):
            decode_bytes(b"\xff", "utf-8")
        with self.assertRaises(UnicodeEncodeError):
            encode_text("Привет", "ascii")

    def test_empty_or_unequal_eml_rejected(self):
        for raw in (b"", b"0001\n02\n", b"GG\n"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_dump(self.write("bad.eml", raw))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(self.root / "cards.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.library.close())

    def test_persistence_search_and_immutable_revisions(self):
        original = DumpData(b"original bytes!!", "bin", {"nested": {"x": 1}}, 16)
        card_id = self.library.create_card("Тестовая карта", original, uid="AABBCCDD", protocol="MIFARE", notes="Дом")
        first_id = self.library.list_revisions(card_id)[0]["id"]
        original.metadata["nested"]["x"] = 99
        self.assertEqual(self.library.get_dump(card_id).metadata["nested"]["x"], 1)
        edited = self.library.get_dump(card_id)
        edited.data = b"edited bytes!!!!"
        second_id = self.library.save_revision(card_id, edited, "Изменение")
        self.library.update_card(card_id, name="Карточка", uid="AABBCCDD", protocol="MIFARE", notes="Работа")
        self.assertEqual(self.library.get_dump(card_id, first_id).data, b"original bytes!!")
        self.assertEqual(self.library.get_dump(card_id).data, edited.data)
        self.assertEqual(self.library.list_revisions(card_id)[0]["id"], second_id)
        self.assertEqual(self.library.get_card(card_id)["revision_count"], 2)
        self.assertEqual(self.library.get_card(card_id)["size"], len(edited.data))
        for query in ("КАРТОЧКА", "aabb", "mifare", "РАБОТА"):
            self.assertEqual(self.library.list_cards(query)[0]["id"], card_id)
        self.assertEqual(self.library.list_cards("несуществующая"), [])
        self.library.close()
        self.library = Library(self.root / "cards.sqlite3")
        self.assertEqual(self.library.get_dump(card_id, first_id).data, b"original bytes!!")
        self.assertEqual(self.library.get_dump(card_id).data, edited.data)

    def test_foreign_revision_and_missing_ids_rejected(self):
        dump = DumpData(b"1234", "bin", {}, 4)
        first = self.library.create_card("One", dump)
        second = self.library.create_card("Two", dump)
        other_revision = self.library.list_revisions(second)[0]["id"]
        with self.assertRaises(KeyError):
            self.library.get_dump(first, other_revision)
        for call in (lambda: self.library.get_card("missing"),
                     lambda: self.library.save_revision("missing", dump),
                     lambda: self.library.list_revisions("missing")):
            with self.assertRaises(KeyError):
                call()

    def test_invalid_save_is_atomic_and_checksum_detects_corruption(self):
        with self.assertRaises(ValueError):
            self.library.create_card("", DumpData(b"1234", "bin", {}, 4))
        self.assertEqual(self.library.list_cards(), [])
        card_id = self.library.create_card("Checksum", DumpData(b"1234", "bin", {}, 4))
        with self.assertRaises(ValueError):
            self.library.save_revision(card_id, DumpData(b"", "bin", {}, 4))
        self.assertEqual(self.library.get_card(card_id)["revision_count"], 1)
        self.library.connection.execute("UPDATE revisions SET data=? WHERE card_id=?", (b"bad!", card_id))
        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            self.library.get_dump(card_id)


if __name__ == "__main__":
    unittest.main()
