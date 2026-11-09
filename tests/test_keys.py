"""Synthetic key and Flipper imports; no device or real credentials involved."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from pm3gui.core import DumpData, Library
from pm3gui.keys import (
    KeyLibrary, format_key_timestamp, normalize_key, parse_dictionary, parse_flipper_key_cache,
    parse_flipper_nfc_keys,
)


KEY_A = "112233445566"
KEY_B = "A1B2C3D4E5F6"


def nfc(kind="1K", blocks=None, version="2"):
    fields = ["Filetype: Flipper NFC device", "Version: 4", "Device type: Mifare Classic",
              "UID: A1 B2 C3 D4", f"Mifare Classic type: {kind}", f"Data format version: {version}"]
    for number, data in (blocks or {}).items():
        fields.append(f"Block {number}: {data}")
    return "\n".join(fields) + "\n"


def trailer(a=KEY_A, b=KEY_B, access="FF 07 80 69"):
    def spaced(value):
        if value == "??":
            return "?? ?? ?? ?? ?? ??"
        return " ".join(value[i:i+2] for i in range(0, 12, 2))
    return f"{spaced(a)} {access} {spaced(b)}"


def cache(a="0000000000000001", b="0000000000000002", extra=""):
    return ("Filetype: Flipper NFC keys\nVersion: 1\nMifare Classic type: 1K\n"
            f"Key A map: {a}\nKey B map: {b}\nKey A sector 0: 11 22 33 44 55 66\n"
            f"Key B sector 1: A1 B2 C3 D4 E5 F6\n{extra}")


class ParserTests(unittest.TestCase):
    def test_key_timestamps_use_moscow_and_keep_unknown_dates_unknown(self):
        for value, expected in [
            ('2026-11-08T22:15:07.123456+00:00', '09.11.2026 01:15:07'),
            ('2026-11-08T22:15:07Z', '09.11.2026 01:15:07'),
            ('2026-11-08T22:15:07', '09.11.2026 01:15:07'),
            ('2026-11-08 22:15:07', '09.11.2026 01:15:07'),
            ('2026-11-08 22:15:07 UTC', '09.11.2026 01:15:07'),
            ('2026-11-09T04:15:07+0300', '09.11.2026 04:15:07'),
            (None, '—'), ('', '—'), ('unknown', '—'), ('2026-11-09', '—'),
            ('2026-13-09T01:02:03+00:00', '—'),
            ('2026-11-09T01:02:03+25:00', '—'),
        ]:
            with self.subTest(value=value):
                self.assertEqual(format_key_timestamp(value), expected)

    def test_dictionary_normalizes_comments_whitespace_and_deduplicates(self):
        text = "\ufeff# Пример\r\n 11 22 33 44 55 66 \n112233445566 # duplicate\n\na1b2c3d4e5f6\n"
        self.assertEqual(parse_dictionary(text), [KEY_A, KEY_B])
        self.assertEqual(normalize_key("11\t22 33 44 55 66"), KEY_A)
        for invalid in ("1234", "11223344556600", "11:22:33:44:55:66", "11223344556G", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_key(invalid)
        with self.assertRaisesRegex(ValueError, "Строка 2"):
            parse_dictionary(KEY_A + "\nunknown\n")

    def test_flipper_unknown_halves_are_skipped_independently(self):
        text = nfc(blocks={3: trailer(a="??"), 7: trailer(b="??", access="?? ?? ?? ??")})
        self.assertEqual(parse_flipper_nfc_keys(text), [KEY_B, KEY_A])
        self.assertEqual(parse_flipper_nfc_keys(nfc(blocks={3: trailer("??", "??")})), [])

    def test_legacy_file_version_two_still_requires_unambiguous_classic_data_format(self):
        text = nfc(blocks={3: trailer(KEY_A, "??")}).replace('Version: 4\n', 'Version: 2\n')
        self.assertEqual(parse_flipper_nfc_keys(text), [KEY_A])
        legacy_zero_fillers = nfc(blocks={3: trailer('000000000000', '000000000000')}, version='1').replace('Version: 4\n', 'Version: 2\n')
        with self.assertRaises(ValueError):
            parse_flipper_nfc_keys(legacy_zero_fillers)
        with self.assertRaises(ValueError):
            parse_flipper_nfc_keys(text.replace('Data format version: 2\n', ''))

    def test_flipper_4k_large_sector_geometry(self):
        text = nfc("4K", {127: trailer(KEY_A, "??"), 131: trailer("000000000001", "??"),
                          143: trailer(KEY_B, "??"), 255: trailer("010203040506", "??")})
        self.assertEqual(parse_flipper_nfc_keys(text), [KEY_A, KEY_B, "010203040506"])

    def test_old_unknown_geometry_duplicate_malformed_and_wrong_protocol_rejected(self):
        for text in [nfc(version="1"), nfc(version="3"), nfc(kind="2K"),
                     nfc(blocks={64: trailer()}), nfc(blocks={3: "00 01"}),
                     nfc(blocks={3: trailer()}) + "Block 3: " + trailer() + "\n",
                     nfc().replace("Device type: Mifare Classic", "Device type: NTAG")]:
            with self.subTest(text=text[:60]), self.assertRaises(ValueError):
                parse_flipper_nfc_keys(text)

    def test_cache_checks_maps_and_sector_values(self):
        self.assertEqual(parse_flipper_key_cache(cache()), [KEY_A, KEY_B])
        for text in [cache(a="1"), cache(a="8000000000000001"),
                     cache().replace("Key A sector 0:", "Key A sector 1:"),
                     cache(extra="Key A sector 2: 00 11 22 33 44 55\n"),
                     cache().replace("Version: 1", "Version: 2")]:
            with self.subTest(text=text[:70]), self.assertRaises(ValueError):
                parse_flipper_key_cache(text)

    def test_cache_4k_high_sector_bit_is_not_byte_reversed(self):
        text = ("Filetype: Flipper NFC keys\nVersion: 1\nMifare Classic type: 4K\n"
                "Key A map: 0000008000000000\nKey B map: 0000000000000000\n"
                "Key A sector 39: 11 22 33 44 55 66\n")
        self.assertEqual(parse_flipper_key_cache(text), [KEY_A])


class KeyLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "shared.sqlite3"
        self.library = KeyLibrary(self.database)

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    def file(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_add_deduplicate_persistence_and_provenance_search(self):
        report = self.library.add_keys([KEY_A, KEY_A, KEY_B.lower()], label="Тест", source="Первый источник")
        self.assertEqual(report, {"added": 2, "duplicates": 1, "total": 2})
        self.library.add_keys([KEY_A], label="Другой ярлык", source="Второй источник")
        record = self.library.list_keys("ВТОРОЙ")[0]
        self.assertEqual(record["key_hex"], KEY_A)
        self.assertEqual(record["provenance_count"], 2)
        self.assertFalse(record["verified"])
        self.library.close()
        self.library = KeyLibrary(self.database)
        self.assertEqual(len(self.library.list_keys()), 2)

    def test_duplicate_and_reimport_keep_first_global_addition_date(self):
        first = '2026-11-08T10:00:00+00:00'
        later = '2026-11-09T11:00:00+00:00'
        path = self.file('dated.dic', KEY_A + '\n')
        with patch('pm3gui.keys._now', return_value=first):
            self.library.import_file(path)
        with patch('pm3gui.keys._now', return_value=later):
            report = self.library.import_file(path)
            self.library.add_keys([KEY_A], label='Другой источник', source='synthetic')
        self.assertEqual(report['duplicates'], 1)
        record = self.library.list_keys()[0]
        self.assertEqual(record['created_at'], first)
        origins = {entry['source']: entry['created_at'] for entry in record['origins']}
        self.assertEqual(origins['Импорт: dated.dic'], first)
        self.assertEqual(origins['synthetic'], later)
        self.library.close()
        self.library = KeyLibrary(self.database)
        self.assertEqual(self.library.list_keys()[0]['created_at'], first)

    def test_context_added_date_and_latest_verification_date_are_independent(self):
        global_added = '2026-11-08T10:00:00+00:00'
        context_added = '2026-11-08T11:00:00+00:00'
        checked = '2026-11-08T12:00:00+00:00'
        reimported = '2026-11-08T13:00:00+00:00'
        checked_again = '2026-11-08T14:00:00+00:00'
        path = self.file('A1B2C3D4.keys', cache())
        with patch('pm3gui.keys._now', return_value=global_added):
            self.library.add_keys([KEY_A], source='synthetic')
        with patch('pm3gui.keys._now', return_value=context_added):
            self.library.import_file(path)
        entry = {'sector': 0, 'type': 'A', 'key_hex': KEY_A}
        before = self.library.list_card_keys('A1B2C3D4')[0]
        self.assertEqual(before['created_at'], context_added)
        self.assertIsNone(before['verified_at'])
        with patch('pm3gui.keys._now', return_value=checked):
            self.library.record_verified_keys('A1B2C3D4', [entry])
        with patch('pm3gui.keys._now', return_value=reimported):
            self.library.import_file(path)
        after = self.library.list_card_keys('A1B2C3D4')[0]
        self.assertEqual(after['created_at'], context_added)
        self.assertEqual(after['verified_at'], checked)
        self.assertEqual(self.library.list_keys(KEY_A)[0]['created_at'], global_added)
        with patch('pm3gui.keys._now', return_value=checked_again):
            self.library.record_verified_keys('A1B2C3D4', [entry])
        latest = self.library.list_card_keys('A1B2C3D4')[0]
        self.assertEqual(latest['created_at'], context_added)
        self.assertEqual(latest['verified_at'], checked_again)
        self.assertTrue(latest['verified'])

    def test_invalid_import_is_atomic(self):
        for action in [lambda: self.library.add_keys([KEY_A, "broken"]),
                       lambda: self.library.import_file(self.file("bad.dic", KEY_A + "\nABC\n")),
                       lambda: self.library.add_keys(KEY_A)]:
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(self.library.list_keys(), [])

    def test_real_flipper_user_dictionary_name_is_plain_dictionary(self):
        path = self.file("mf_classic_dict_user.nfc", f"# user keys\n{KEY_A}\n{KEY_B}\n{KEY_A}\n")
        report = self.library.import_file(path)
        self.assertEqual(report["format"], "dictionary")
        self.assertEqual((report["added"], report["duplicates"]), (2, 1))
        self.assertTrue(all(not row["verified"] for row in self.library.list_keys()))

    def test_dict_alias_offered_by_gui_imports_as_plain_dictionary(self):
        report = self.library.import_file(self.file("user.dict", KEY_A + "\n"))
        self.assertEqual(report["format"], "dictionary")
        self.assertEqual(report["added"], 1)

    def test_nfc_import_preserves_uid_sector_candidate_context(self):
        report = self.library.import_file(self.file("карта.nfc", nfc(blocks={3: trailer(KEY_A, "??")})))
        self.assertEqual(report["format"], "flipper-nfc")
        self.assertEqual(report["uid"], "A1B2C3D4")
        self.assertEqual(report["skipped_unknown"], 31)
        context = self.library.list_card_keys("a1 b2 c3 d4")
        self.assertEqual(len(context), 1)
        self.assertEqual((context[0]["sector"], context[0]["type"], context[0]["key_hex"]), (0, "A", KEY_A))
        self.assertFalse(context[0]["verified"])

    def test_full_shadow_import_keeps_partial_original_and_verified_context(self):
        unknown = ' '.join(['??'] * 16)
        partial = {block: unknown for block in range(256)}
        partial[3] = trailer(KEY_A, '??')
        original = self.file('карта.nfc', nfc('4K', partial))
        original_bytes = original.read_bytes()
        first = self.library.import_file(original)
        self.assertFalse(first['shadow'])
        self.assertEqual(first['skipped_unknown'], 79)
        self.library.record_verified_keys('A1B2C3D4', [{'sector': 0, 'type': 'A', 'key_hex': KEY_A}])

        full = {block: ' '.join(['00'] * 16) for block in range(256)}
        for sector in range(40):
            index = sector * 4 + 3 if sector < 32 else 128 + (sector - 32) * 16 + 15
            full[index] = trailer(f'{0xA00000000000 + sector:012X}', f'{0xB00000000000 + sector:012X}')
        shadow = self.file('карта.SHD', nfc('4K', full))
        shadow_bytes = shadow.read_bytes()
        report = self.library.import_file(shadow)
        self.assertEqual((report['format'], report['shadow'], report['imported'], report['skipped_unknown']),
                         ('flipper-nfc', True, 80, 0))
        contexts = self.library.list_card_keys('A1B2C3D4')
        self.assertEqual(len(contexts), 81)
        old = next(row for row in contexts if row['key_hex'] == KEY_A)
        self.assertTrue(old['verified'])
        self.assertIn('Proxmark3', old['source'])
        candidates = [row for row in contexts if row['key_hex'] != KEY_A]
        self.assertEqual(len(candidates), 80)
        self.assertTrue(all(not row['verified'] and 'карта.SHD' in row['source'] for row in candidates))
        self.assertEqual({(row['sector'], row['type']) for row in candidates},
                         {(sector, kind) for sector in range(40) for kind in ('A', 'B')})
        self.assertTrue(all('карта.SHD' in row['source'] and not row['verified']
                            for row in self.library.list_keys() if row['key_hex'] != KEY_A))
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(shadow.read_bytes(), shadow_bytes)
        self.library.import_file(original)
        self.assertEqual(len(self.library.list_card_keys('A1B2C3D4')), 81)

    def test_shadow_requires_supported_card_header_and_data_format_atomically(self):
        self.library.add_keys([KEY_A], source='existing candidate')
        before = self.library.list_keys()
        unsupported = [KEY_B + '\n', cache(),
                       nfc(blocks={3: trailer(KEY_A, KEY_B)}, version='1'),
                       nfc(blocks={3: trailer()}).replace('Device type: Mifare Classic', 'Device type: NTAG'),
                       nfc(blocks={3: trailer()}).replace('Data format version: 2\n', ''),
                       nfc(blocks={3: trailer()}).replace('Filetype: Flipper NFC device', 'Filetype: Other')]
        for text in unsupported:
            with self.subTest(header=text.splitlines()[0]):
                path = self.file('unsupported.shd', text)
                raw = path.read_bytes()
                with self.assertRaises(ValueError):
                    self.library.import_file(path)
                self.assertEqual(self.library.list_keys(), before)
                self.assertEqual(self.library.list_card_keys('A1B2C3D4'), [])
                self.assertEqual(path.read_bytes(), raw)

    def test_shadow_keeps_existing_import_size_limit(self):
        shadow = self.file('too-large.shd', nfc(blocks={3: trailer()}))
        raw = shadow.read_bytes()
        with patch('pm3gui.keys.MAX_IMPORT_BYTES', len(raw) - 1):
            with self.assertRaises(ValueError):
                self.library.import_file(shadow)
        self.assertEqual(self.library.list_keys(), [])
        self.assertEqual(shadow.read_bytes(), raw)

    def test_legacy_file_version_two_import_keeps_keys_as_candidates(self):
        text = nfc(blocks={3: trailer(KEY_A, '??')}).replace('Version: 4\n', 'Version: 2\n')
        report = self.library.import_file(self.file('legacy-v2.nfc', text))
        self.assertEqual(report['format'], 'flipper-nfc')
        self.assertEqual(report['added'], 1)
        self.assertEqual(report['skipped_unknown'], 31)
        self.assertTrue(all(not row['verified'] for row in self.library.list_keys()))

    def test_cache_import_keeps_candidates_and_filename_uid(self):
        report = self.library.import_file(self.file("A1B2C3D4.keys", cache()))
        self.assertEqual(report["format"], "flipper-cache")
        self.assertEqual(report["added"], 2)
        self.assertEqual(len(self.library.list_card_keys("A1B2C3D4")), 2)
        self.assertFalse(any(row["verified"] for row in self.library.list_keys()))

    def test_verified_context_promotes_candidate_only_after_explicit_authentication(self):
        self.library.import_file(self.file("A1B2C3D4.keys", cache()))
        report = self.library.record_verified_keys("A1B2C3D4", [{"sector": 0, "type": "A", "key_hex": KEY_A}])
        self.assertEqual(report["recorded"], 1)
        self.assertTrue(self.library.list_keys(KEY_A)[0]["verified"])
        self.assertFalse(self.library.list_keys(KEY_B)[0]["verified"])
        contexts = self.library.list_card_keys("A1B2C3D4")
        self.assertTrue(contexts[0]["verified"])
        self.assertIsNotNone(contexts[0]["verified_at"])
        self.assertIn("Proxmark3", contexts[0]["source"])
        # Reimport is a candidate observation and must not erase prior proof.
        self.library.import_file(self.file("A1B2C3D4.keys", cache()))
        self.assertTrue(self.library.list_card_keys("A1B2C3D4")[0]["verified"])
        self.assertIn("Proxmark3", self.library.list_card_keys("A1B2C3D4")[0]["source"])

    def test_bad_verified_results_cannot_partially_change_library(self):
        good = {"sector": 0, "key_type": "A", "key_hex": KEY_A}
        bad_entries = [
            [good, {"sector": 40, "type": "B", "key_hex": KEY_B}],
            [good, {"sector": 0, "type": "A", "key_hex": KEY_B}],
            [good, {"sector": True, "type": "B", "key_hex": KEY_B}],
        ]
        for entries in bad_entries:
            with self.assertRaises(ValueError):
                self.library.record_verified_keys("A1B2C3D4", entries)
            self.assertEqual(self.library.list_keys(), [])

    def test_export_is_ascii_lf_no_bom_and_roundtrips(self):
        self.library.add_keys([KEY_B, KEY_A])
        path = self.root / "mf_classic_dict_user.nfc"
        self.library.export_dictionary(path)
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(parse_dictionary(raw.decode("ascii")), sorted([KEY_A, KEY_B]))

    def test_core_library_coexists_and_deletion_cascades(self):
        cards = Library(self.database)
        card_id = cards.create_card("Синтетика", DumpData(b"data", "bin", {}, 16))
        try:
            self.library.record_verified_keys("A1B2C3D4", [{"sector": 0, "type": "A", "key_hex": KEY_A}])
            key_id = self.library.list_keys()[0]["id"]
            self.assertEqual(self.library.delete_keys([key_id, key_id]), 1)
            self.assertEqual(self.library.list_card_keys("A1B2C3D4"), [])
            self.assertEqual(cards.get_dump(card_id).data, b"data")
        finally:
            cards.close()


class KeyDateDisplayTests(unittest.TestCase):
    def test_global_and_card_tables_use_their_own_dates_without_hardware(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication, QLabel, QTableWidget
        from pm3gui.app import MainWindow

        application = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('pm3gui.app.available_ports', return_value=[]), \
                    patch('pm3gui.app.discover_client', return_value=''), \
                    patch('pm3gui.app.ClientRunner.run') as run, \
                    patch('pm3gui.app.ClientRunner.run_operation') as run_operation, \
                    patch('pm3gui.app.ClientRunner.cancel'):
                window = MainWindow(root / 'library')
                try:
                    with patch('pm3gui.keys._now', return_value='2026-11-08T10:00:00+00:00'):
                        window.key_library.add_keys([KEY_A, KEY_B])
                    path = root / 'A1B2C3D4.keys'
                    path.write_text(cache(), encoding='utf-8')
                    with patch('pm3gui.keys._now', return_value='2026-11-08T11:00:00+00:00'):
                        window.key_library.import_file(path)
                    with patch('pm3gui.keys._now', return_value='2026-11-08T12:00:00+00:00'):
                        window.key_library.record_verified_keys('A1B2C3D4', [
                            {'sector': 0, 'type': 'A', 'key_hex': KEY_A}])
                    window.key_library.connection.execute("UPDATE mf_keys SET created_at='unknown' WHERE key_hex=?", (KEY_B,))
                    window.key_library.connection.commit()
                    window.refresh_keys()
                    added = next(i for i in range(window.key_table.columnCount())
                                 if window.key_table.horizontalHeaderItem(i).text() == 'Добавлен')
                    by_key = {window.key_table.item(row, 0).text(): window.key_table.item(row, added).text()
                              for row in range(window.key_table.rowCount())}
                    self.assertEqual(by_key[KEY_A], '08.11.2026 13:00:00')
                    self.assertEqual(by_key[KEY_B], '—')
                    window.key_search.setText(KEY_A)
                    self.assertEqual(window.key_table.rowCount(), 1)
                    self.assertIn('Ключей: 2', window.key_count.text())
                    window.card_id = window.library.create_card('Синтетика', DumpData(b'data', 'bin', {}, 16), uid='A1B2C3D4')
                    captured = {}

                    def inspect_dialog(dialog):
                        table = dialog.findChild(QTableWidget)
                        captured['headers'] = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
                        captured['rows'] = [[table.item(row, col).text() for col in range(table.columnCount())]
                                            for row in range(table.rowCount())]
                        return 0

                    with patch('pm3gui.app.QDialog.exec', inspect_dialog):
                        window.show_card_keys()
                    row = next(row for row in captured['rows'] if KEY_A in row)
                    self.assertEqual(row[captured['headers'].index('Добавлен')], '08.11.2026 14:00:00')
                    self.assertEqual(row[captured['headers'].index('Проверен')], '08.11.2026 15:00:00')
                    candidate = next(row for row in captured['rows'] if KEY_B in row)
                    self.assertEqual(candidate[captured['headers'].index('Проверен')], '—')
                    run.assert_not_called()
                    run_operation.assert_not_called()
                finally:
                    window.dirty = False
                    window.read_job = None
                    window.close()
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    application.processEvents()


if __name__ == "__main__":
    unittest.main()
