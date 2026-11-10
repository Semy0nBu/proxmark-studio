"""Synthetic whole-profile archives: WAL, history, crypto and hostile input."""
import hashlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pm3gui import library_backup as backup
from pm3gui.core import DumpData, Library
from pm3gui.keys import KeyLibrary


class LibraryBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.profile = self.root / 'original'; self.profile.mkdir()
        self.library = Library(self.profile / 'library.sqlite3')
        self.keys = KeyLibrary(self.profile / 'library.sqlite3')
        self.addCleanup(self.library.close); self.addCleanup(self.keys.close)
        self.library.connection.execute('PRAGMA journal_mode=WAL')
        self.cid = self.library.create_card('Учебная HF', DumpData(bytes(range(64)), 'bin', {}, 16),
                                            uid='01020304', protocol='MIFARE Classic Mini', notes='Synthetic only')
        self.library.save_revision(self.cid, DumpData(bytes(64), 'bin', {'partial_dump': {'known_blocks': [0]}}, 16), 'Partial')
        self.library.create_card('Synthetic LF', DumpData(b'12345', 'bin', {}, 1), uid='4A00468120', protocol='EM410x')
        self.keys.add_keys(['A0A1A2A3A4A5'], label='Synthetic', source='fixture')
        self.keys.record_verified_keys('01020304', [{'sector': 0, 'type': 'A', 'key_hex': 'FFFFFFFFFFFF'}], source='fixture')
        (self.profile / 'settings.json').write_text(json.dumps({'language': 'en', 'keep_connection': False, 'ui': {'zoom': 1}}))
        (self.profile / 'sessions').mkdir()
        (self.profile / 'sessions' / 'тест.bin').write_bytes(b'auxiliary\x00data')
        self.archive = self.root / 'test.pm3backup'; self.target = self.root / 'restored'

    def dump_tables(self, path):
        with closing(sqlite3.connect(path)) as db:
            return {table: db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
                    for table in backup._SCHEMA}

    def roundtrip(self, password=None):
        before = self.dump_tables(self.profile / 'library.sqlite3')
        exported = backup.create_backup(self.profile, self.archive, password=password)
        restored = backup.restore_backup(self.archive, self.target, password=password, active_profile=self.profile)
        self.assertEqual((restored['cards'], restored['revisions'], restored['keys']), (2, 3, 2))
        self.assertEqual(restored['encrypted'], password is not None)
        self.assertEqual(self.dump_tables(self.target / 'library.sqlite3'), before)
        self.assertEqual(self.dump_tables(self.profile / 'library.sqlite3'), before)
        for path in ('settings.json', 'sessions/тест.bin'):
            self.assertEqual((self.target/path).read_bytes(), (self.profile/path).read_bytes())
        self.assertFalse((self.target / 'library.sqlite3-wal').exists())
        self.assertEqual(backup.validate_profile(self.target)['revisions'], 3)
        return exported

    def test_plain_snapshot_preserves_committed_wal_and_all_versions_context_and_dates(self):
        self.assertTrue((self.profile / 'library.sqlite3-wal').exists())
        self.roundtrip()
        self.assertTrue(zipfile.is_zipfile(self.archive))
        # The open source connections remain usable.
        self.assertEqual(len(self.library.list_cards()), 2)

    def test_password_roundtrip_hides_names_and_key_values(self):
        self.roundtrip('пароль 🔑 2026')
        data = self.archive.read_bytes()
        self.assertTrue(data.startswith(backup.MAGIC))
        for value in (b'library.sqlite3', b'settings.json', b'FFFFFFFFFFFF', b'fixture'):
            self.assertNotIn(value, data)
        # Independently consume the specified format rather than testing only
        # two code paths that could share the same framing error.
        from Cryptodome.Cipher import AES
        from Cryptodome.Protocol.KDF import scrypt
        key=scrypt('пароль 🔑 2026'.encode('utf8'),data[11:27],32,131072,8,1)
        cipher=AES.new(key,AES.MODE_GCM,nonce=data[27:39]);cipher.update(data[:39])
        plain=cipher.decrypt_and_verify(data[39:-16],data[-16:])
        with zipfile.ZipFile(io.BytesIO(plain)) as z:
            self.assertEqual(json.loads(z.read('manifest.json'))['format'],backup.FORMAT)

    def test_password_randomization_and_wrong_password_never_create_profile(self):
        backup.create_backup(self.profile, self.archive, password='secret')
        second = self.root/'second.pm3backup'; backup.create_backup(self.profile, second, password='secret')
        self.assertNotEqual(self.archive.read_bytes()[11:39], second.read_bytes()[11:39])
        for password in (None, '', 'wrong'):
            with self.subTest(password=password), self.assertRaises(backup.BackupError):
                backup.restore_backup(self.archive, self.target, password=password)
            self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob('.pm3-restore-*')))

    def test_encrypted_header_ciphertext_tag_and_truncation_rejected(self):
        backup.create_backup(self.profile, self.archive, password='secret')
        original = self.archive.read_bytes()
        for position in (8, 12, 30, 45, len(original)-1):
            changed = bytearray(original); changed[position] ^= 1; self.archive.write_bytes(changed)
            with self.subTest(position=position), self.assertRaises(backup.BackupError):
                backup.restore_backup(self.archive, self.target, password='secret')
            self.assertFalse(self.target.exists())
        for length in (4, 15, 40, len(original)-3):
            self.archive.write_bytes(original[:length])
            with self.subTest(length=length), self.assertRaises(backup.BackupError):
                backup.restore_backup(self.archive, self.target, password='secret')
            self.assertFalse(self.target.exists())

    def test_existing_destinations_and_active_profile_are_never_overwritten(self):
        backup.create_backup(self.profile, self.archive)
        self.target.mkdir(); marker=self.target/'marker'; marker.write_text('keep')
        for target in (self.target, self.profile, self.profile/'new', self.root):
            with self.subTest(target=target), self.assertRaises(backup.BackupError):
                backup.restore_backup(self.archive, target, active_profile=self.profile)
        self.assertEqual(marker.read_text(), 'keep')
        original=self.archive.read_bytes()
        with self.assertRaises(backup.BackupError):backup.create_backup(self.profile, self.archive)
        self.assertEqual(self.archive.read_bytes(), original)
        with self.assertRaises(backup.BackupError):backup.create_backup(self.profile, self.profile/'backup.pm3backup')

    def test_uncommitted_database_change_is_not_in_snapshot(self):
        self.library.connection.execute("UPDATE cards SET name='uncommitted' WHERE id=?", (self.cid,))
        try:backup.create_backup(self.profile, self.archive)
        finally:self.library.connection.rollback()
        backup.restore_backup(self.archive, self.target)
        self.assertEqual(self.dump_tables(self.target/'library.sqlite3'), self.dump_tables(self.profile/'library.sqlite3'))

    def test_export_cancel_and_source_mutation_leave_no_partial_archive(self):
        stop=[False]
        def progress(*args):stop[0]=True
        with self.assertRaises(backup.BackupCancelled):
            backup.create_backup(self.profile, self.archive, cancelled=lambda:stop[0], progress=progress)
        self.assertFalse(self.archive.exists()); self.assertFalse(list(self.root.glob('.pm3-backup-*')))
        real=backup._fingerprint; calls=[0]
        def changed(info):
            calls[0]+=1
            return (*real(info), calls[0])
        with patch.object(backup, '_fingerprint', side_effect=changed), self.assertRaises(backup.BackupError):
            backup.create_backup(self.profile, self.archive)
        self.assertFalse(self.archive.exists())

    def test_restore_cancel_and_invalid_database_do_not_publish(self):
        backup.create_backup(self.profile, self.archive)
        stop=[False]
        with self.assertRaises(backup.BackupCancelled):
            backup.restore_backup(self.archive, self.target, cancelled=lambda:stop[0],
                                   progress=lambda *_:stop.__setitem__(0,True))
        self.assertFalse(self.target.exists()); self.assertFalse(list(self.root.glob('.pm3-restore-*')))
        self.rewrite({'profile/library.sqlite3': b'not sqlite'}, fix_manifest=True)
        with self.assertRaises(backup.BackupError):backup.restore_backup(self.archive, self.target)
        self.assertFalse(self.target.exists())

    def rewrite(self, changes=None, *, manifest_change=None, extra=None, fix_manifest=False):
        with zipfile.ZipFile(self.archive) as z:files={i.filename:z.read(i) for i in z.infolist()}
        files.update(changes or {})
        manifest=json.loads(files['manifest.json'])
        if fix_manifest:
            for item in manifest['files']:
                data=files['profile/'+item['path']]; item.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        if manifest_change:manifest_change(manifest)
        files['manifest.json']=json.dumps(manifest).encode()
        with zipfile.ZipFile(self.archive,'w',zipfile.ZIP_DEFLATED) as z:
            for name,data in files.items():z.writestr(name,data)
            if extra:z.writestr(*extra)

    def test_corrupted_checksums_invalid_settings_and_future_format_rejected(self):
        variants=[({'profile/settings.json':b'{}'},None,False),
                  ({'profile/settings.json':b'[]'},None,True),
                  (None,lambda m:m.update(version=99),False),
                  (None,lambda m:m['files'][0].update(bytes=backup.MAX_BYTES+1),False)]
        for changes,mutation,fix in variants:
            self.archive.unlink(missing_ok=True); backup.create_backup(self.profile,self.archive)
            self.rewrite(changes,manifest_change=mutation,fix_manifest=fix)
            with self.subTest(changes=changes,fix=fix),self.assertRaises(backup.BackupError):
                backup.restore_backup(self.archive,self.target)
            self.assertFalse(self.target.exists())

    def test_hostile_zip_names_links_duplicates_and_extra_entries_rejected(self):
        names=['../escape','/absolute','profile/../../escape',r'profile\escape','profile/a:stream',
               'profile/CON.txt','profile/trailing.','profile/trailing ','profile/SETTINGS.JSON','unlisted']
        for name in names:
            self.archive.unlink(missing_ok=True);backup.create_backup(self.profile,self.archive)
            self.rewrite(extra=(name,b'bad'))
            with self.subTest(name=name),self.assertRaises(backup.BackupError):backup.restore_backup(self.archive,self.target)
            self.assertFalse(self.target.exists())
        self.archive.unlink();backup.create_backup(self.profile,self.archive)
        link=zipfile.ZipInfo('profile/link');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16
        self.rewrite(extra=(link,b'../../escape'))
        with self.assertRaises(backup.BackupError):backup.restore_backup(self.archive,self.target)

    def test_bounds_duplicate_json_and_database_revision_integrity(self):
        with self.assertRaises(backup.BackupError):backup._json(b'{"version":1,"version":2}')
        with self.assertRaises(backup.BackupError):backup._json(b'{"x":NaN}')
        backup.create_backup(self.profile,self.archive)
        with patch.object(backup,'MAX_FILES',1),self.assertRaises(backup.BackupError):backup.restore_backup(self.archive,self.target)
        original=self.archive.read_bytes(); data=bytearray(original)
        struct.pack_into('<H',data,len(data)-12,1) # Lie about central-directory entry count.
        self.archive.write_bytes(data)
        with self.assertRaises(backup.BackupError):backup.restore_backup(self.archive,self.target)
        with self.library.connection:self.library.connection.execute("UPDATE revisions SET sha256=?",('0'*64,))
        with self.assertRaises(backup.BackupError):backup.create_backup(self.profile,self.root/'invalid.pm3backup')

    def test_source_links_are_not_followed(self):
        link=self.profile/'linked'
        try:link.symlink_to(self.root/'outside')
        except OSError:
            # Standard Windows users may not create links: exercise the exact
            # reparse/link rejection branch without requiring elevation.
            real=Path.is_symlink
            with patch.object(Path,'is_symlink',lambda p:p.name=='settings.json' or real(p)):
                with self.assertRaises(backup.BackupError):backup.create_backup(self.profile,self.archive)
        else:
            with self.assertRaises(backup.BackupError):backup.create_backup(self.profile,self.archive)

    def test_publication_races_do_not_replace_existing_archive_or_profile(self):
        publish=backup._publish_file
        def race(source,destination):
            destination.write_bytes(b'other writer')
            return publish(source,destination)
        with patch.object(backup,'_publish_file',side_effect=race),self.assertRaises(FileExistsError):
            backup.create_backup(self.profile,self.archive)
        self.assertEqual(self.archive.read_bytes(),b'other writer');self.archive.unlink()
        backup.create_backup(self.profile,self.archive)
        publish_profile=backup._publish_profile
        def profile_race(source,destination):
            destination.mkdir();(destination/'marker').write_text('other writer')
            return publish_profile(source,destination)
        with patch.object(backup,'_publish_profile',side_effect=profile_race),self.assertRaises(OSError):
            backup.restore_backup(self.archive,self.target)
        self.assertEqual((self.target/'marker').read_text(),'other writer')
        self.assertEqual(list(self.target.iterdir()),[self.target/'marker'])

    def test_zip_crc_and_invalid_compressed_bytes_fail_without_a_profile(self):
        backup.create_backup(self.profile,self.archive)
        with zipfile.ZipFile(self.archive) as z:info=z.getinfo('profile/library.sqlite3')
        data=bytearray(self.archive.read_bytes())
        name,extra=struct.unpack_from('<2H',data,info.header_offset+26)
        start=info.header_offset+30+name+extra
        # Invalid DEFLATE block type, exercising error normalization as well as CRC.
        data[start]=(data[start]&0xF8)|0x07;self.archive.write_bytes(data)
        with self.assertRaises(backup.BackupError):backup.restore_backup(self.archive,self.target)
        self.assertFalse(self.target.exists())

    def test_invalid_saved_settings_cannot_produce_a_successful_backup(self):
        (self.profile/'settings.json').write_text('[]')
        with self.assertRaises(backup.BackupError):backup.create_backup(self.profile,self.archive)
        self.assertFalse(self.archive.exists())

