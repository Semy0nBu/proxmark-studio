"""Library organization belongs to entries, without changing memory or RF state."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from pm3gui.app import MainWindow
from pm3gui.core import DumpData, Library
from pm3gui.i18n import set_language
from pm3gui.keys import KeyLibrary
from pm3gui import library_backup


def snapshot():
    return DumpData(b'original bytes!!', 'bin', {'synthetic': True, 'nested': {'value': 7}}, 16)


class LibraryOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = Library(self.root / 'library.sqlite3')
        self.addCleanup(self.library.close)

    def test_v1_migration_preserves_card_revision_and_key_rows_and_is_repeatable(self):
        legacy = self.root / 'legacy.sqlite3'
        # This is the pre-organization schema, rather than a database created by
        # the new Library constructor and then treated as an old installation.
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript('''
                CREATE TABLE cards(id TEXT PRIMARY KEY,name TEXT NOT NULL,uid TEXT NOT NULL,
                    protocol TEXT NOT NULL,notes TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE revisions(id TEXT PRIMARY KEY,card_id TEXT NOT NULL REFERENCES cards(id),
                    created_at TEXT NOT NULL,note TEXT NOT NULL,data BLOB NOT NULL,format TEXT NOT NULL,
                    metadata TEXT NOT NULL,block_size INTEGER NOT NULL,sha256 TEXT NOT NULL);
                PRAGMA user_version=1;
            ''')
            db.execute('INSERT INTO cards VALUES(?,?,?,?,?,?,?)',
                       ('legacy', 'Старая запись', '01020304', 'Synthetic', 'keep', 'old', 'old'))
            for index, data in enumerate((b'old bytes', b'new bytes')):
                db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?)',
                    ('rev'+str(index), 'legacy', '2026-01-0'+str(index+1), 'unchanged note', data,
                     'bin', '{"kept":true}', 1, hashlib.sha256(data).hexdigest()))
            db.commit()
        with closing(KeyLibrary(legacy)) as keys:
            keys.add_keys(['A0A1A2A3A4A5'], label='Synthetic only', source='fixture')
            keys.record_verified_keys('01020304', [{'sector': 0, 'type': 'A', 'key_hex': 'A0A1A2A3A4A5'}], source='fixture')
        tables = ('cards', 'revisions', 'mf_keys', 'mf_key_origins', 'mf_card_keys')
        def contents():
            with closing(sqlite3.connect(legacy)) as db:
                return {name: db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall() for name in tables}
        before = contents()
        for _ in range(2):
            with closing(Library(legacy)) as library:
                card = library.get_card('legacy')
                self.assertEqual((card['collection'], card['tags']), ('', []))
                self.assertEqual(library.get_dump('legacy', 'rev0').data, b'old bytes')
                self.assertEqual(library.get_dump('legacy').metadata, {'kept': True})
                self.assertEqual(library.connection.execute('PRAGMA user_version').fetchone()[0], 1)
                self.assertIsNone(library.connection.execute('PRAGMA foreign_key_check').fetchone())
            self.assertEqual(contents(), before)

    def test_metadata_belongs_to_entry_even_when_uid_and_bytes_are_shared(self):
        first = self.library.create_card('First', snapshot(), uid='01020304', collection='Lab', tags=['Test'])
        second = self.library.create_card('Second', snapshot(), uid='01020304', collection='Office', tags=['Access'])
        self.assertEqual([c['id'] for c in self.library.list_cards(collection='lab')], [first])
        self.assertEqual([c['id'] for c in self.library.list_cards(tag='ACCESS')], [second])
        self.assertEqual(self.library.get_card(first)['tags'], ['Test'])

    def test_unicode_search_and_combined_filters(self):
        first = self.library.create_card('Одна', snapshot(), collection='Лаборатория', tags='Тестовая, Ångström, тестовая')
        self.library.create_card('Другая', snapshot(), collection='Работа', tags=['Тестовая'])
        ungrouped = self.library.create_card('Без группы', snapshot())
        for query in ('ЛАБОРАТОРИЯ', 'ångström', 'A\u030angstro\u0308m'):
            with self.subTest(query=query):
                self.assertEqual([c['id'] for c in self.library.list_cards(query)], [first])
        self.assertEqual([c['id'] for c in self.library.list_cards('ОДНА', collection='лаборатория', tag='ТЕСТОВАЯ')], [first])
        self.assertEqual(self.library.list_cards(collection='Работа', tag='Ångström'), [])
        self.assertEqual([c['id'] for c in self.library.list_cards(collection='')], [ungrouped])
        self.assertEqual(self.library.organization_options(),
                         {'collections': ['Лаборатория', 'Работа'], 'tags': ['Ångström', 'Тестовая']})

    def test_organization_edit_does_not_change_versions_or_source_hashes(self):
        card_id = self.library.create_card('First', snapshot())
        self.library.save_revision(card_id, DumpData(b'new memory', 'bin', {'other': True}, 1), 'new')
        revisions = self.library.list_revisions(card_id)
        dumps = {r['id']: self.library.get_dump(card_id, r['id']) for r in revisions}
        self.library.update_card(card_id, name='Renamed', uid='', protocol='', notes='', collection='Tests', tags=['HF'])
        self.assertEqual(self.library.list_revisions(card_id), revisions)
        for revision, dump in dumps.items():self.assertEqual(self.library.get_dump(card_id, revision), dump)
        self.library.update_card(card_id, name='Renamed again', uid='', protocol='', notes='old caller')
        self.assertEqual(self.library.get_card(card_id)['collection'], 'Tests')
        self.assertEqual(self.library.get_card(card_id)['tags'], ['HF'])
        self.library.update_card(card_id, name='Clear', uid='', protocol='', notes='', collection='', tags=[])
        self.assertEqual(self.library.organization_options(), {'collections': [], 'tags': []})

    def test_invalid_values_never_partially_update_or_create_an_entry(self):
        card_id = self.library.create_card('Original', snapshot(), collection='Keep', tags=['Keep'])
        for fields in ({'collection': 'x'*97}, {'collection': 'bad\x00name'},
                       {'tags': ['x'*49]}, {'tags': ['bad\nname']}, {'tags': ['bad,tag']},
                       {'tags': [None]}, {'tags': ['x']*33}, {'tags': {'object': 'wrong'}}):
            with self.subTest(fields=fields):
                before = self.library.get_card(card_id)
                with self.assertRaises(ValueError):
                    self.library.update_card(card_id, name='Must not save', uid='', protocol='', notes='', **fields)
                self.assertEqual(self.library.get_card(card_id), before)
                with self.assertRaises(ValueError):self.library.create_card('Must not exist', snapshot(), **fields)
                self.assertEqual(len(self.library.list_cards()), 1)

    def test_password_backup_restore_preserves_organization_and_old_revisions(self):
        profile = self.root / 'profile'
        profile.mkdir()
        with closing(Library(profile / 'library.sqlite3')) as library, closing(KeyLibrary(profile / 'library.sqlite3')):
            card_id = library.create_card('Synthetic', snapshot(), collection='Лаб', tags=['HF', 'Test'])
            old = library.list_revisions(card_id)[0]['id']
            library.save_revision(card_id, DumpData(b'later', 'bin', {}, 1))
            before = library.get_card(card_id)
            before_revisions = library.list_revisions(card_id)
            (profile / 'settings.json').write_text(json.dumps({'language': 'en'}), encoding='utf-8')
            archive, restored = self.root / 'saved.pm3backup', self.root / 'restored'
            library_backup.create_backup(profile, archive, password='synthetic-password')
            library_backup.restore_backup(archive, restored, password='synthetic-password', active_profile=profile)
        with closing(Library(restored / 'library.sqlite3')) as library:
            self.assertEqual(library.get_card(card_id), before)
            self.assertEqual(library.list_revisions(card_id), before_revisions)
            self.assertEqual(library.get_dump(card_id, old), snapshot())


class LibraryOrganizationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.run = self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.operation = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.warning = self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.question = self.enterContext(patch('pm3gui.app.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel))
        self.window = MainWindow(self.root / 'profile', language='en')
        self.first = self.window.library.create_card('First', snapshot(), collection='Lab', tags=['HF', 'Test'])
        self.second = self.window.library.create_card('Second', snapshot(), collection='Office', tags=['LF'])
        self.window.refresh_library()

    def tearDown(self):
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        set_language('ru')
        self.temp.cleanup()

    def visible_ids(self):
        return {self.window.cards.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.window.cards.count())}

    def test_filters_and_search_remain_local_and_preserve_unsaved_input(self):
        w = self.window
        w.load_card(self.first)
        w.card_collection.setEditText('Unsaved collection')
        w.card_tags.setText('Unsaved tag')
        w.library_collection_filter.setCurrentIndex(w.library_collection_filter.findData('Office'))
        self.assertEqual(self.visible_ids(), {self.second})
        self.assertEqual(w.card_collection.currentText(), 'Unsaved collection')
        self.assertEqual(w.card_tags.text(), 'Unsaved tag')
        self.assertTrue(w.dirty)
        w.library_tag_filter.setCurrentIndex(w.library_tag_filter.findData('HF'))
        self.assertEqual(self.visible_ids(), set())
        w.library_collection_filter.setCurrentIndex(0)
        self.assertEqual(self.visible_ids(), {self.first})
        w.search.setText('test')
        self.assertEqual(self.visible_ids(), {self.first})
        self.assertEqual(w.library.get_card(self.first)['collection'], 'Lab')
        self.run.assert_not_called(); self.operation.assert_not_called(); self.question.assert_not_called()

    def test_save_metadata_keeps_selected_old_revision_and_updates_filters(self):
        w = self.window
        old = w.library.list_revisions(self.first)[0]['id']
        w.library.save_revision(self.first, DumpData(b'later bytes', 'bin', {}, 1))
        w.load_card(self.first, revision_id=old)
        before = w.library.list_revisions(self.first)
        w.card_collection.setEditText('Field kit')
        w.card_tags.setText('Travel, HF, travel')
        self.assertTrue(w.save_card())
        self.assertEqual(w.library.get_card(self.first)['collection'], 'Field kit')
        self.assertEqual(w.library.get_card(self.first)['tags'], ['HF', 'Travel'])
        self.assertEqual(w.loaded_revision, old)
        self.assertEqual(w.loaded_dump, snapshot())
        self.assertEqual(w.library.list_revisions(self.first), before)
        self.assertGreaterEqual(w.library_collection_filter.findData('Field kit'), 0)
        self.assertGreaterEqual(w.library_tag_filter.findData('Travel'), 0)
        self.assertFalse(w.dirty)
        self.run.assert_not_called(); self.operation.assert_not_called(); self.warning.assert_not_called()

    def test_cancel_card_switch_retains_dirty_organization(self):
        w = self.window
        w.load_card(self.first); w.refresh_library()
        w.card_tags.setText('Unsaved')
        for i in range(w.cards.count()):
            if w.cards.item(i).data(Qt.ItemDataRole.UserRole) == self.second:w.cards.setCurrentRow(i);break
        self.assertEqual(w.card_id, self.first)
        self.assertEqual(w.card_tags.text(), 'Unsaved')
        self.assertEqual(w.library.get_card(self.first)['tags'], ['HF', 'Test'])
        self.question.assert_called_once()

    def test_invalid_tags_do_not_create_revision_when_hex_is_also_edited(self):
        w = self.window; w.load_card(self.first)
        revisions = w.library.list_revisions(self.first)
        w.hex_edit.setPlainText('01 02 03 04')
        w.card_tags.setText('too long ' + 'x'*49)
        self.assertFalse(w.save_card())
        self.assertEqual(w.library.list_revisions(self.first), revisions)
        self.assertEqual(w.library.get_card(self.first)['tags'], ['HF', 'Test'])
        self.assertTrue(w.dirty); self.warning.assert_called_once()


if __name__ == '__main__':unittest.main()
