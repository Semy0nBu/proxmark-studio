"""Relocatable firmware bundles use synthetic files and never open hardware."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pm3gui.firmware import FirmwareCatalog, FirmwareError
from tests.test_firmware import elf_bytes
from tools.release_firmware import stage_firmware


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseFirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'private source checkout'
        self.release = self.root / 'release'
        self.setup = self.source / 'outputs/device-setup'
        self.setup.mkdir(parents=True)
        native = self.source / 'native/client'
        native.mkdir(parents=True)
        self.native_client = native / 'proxmark3.exe'
        self.native_client.write_bytes(b'MZ synthetic client')
        runtime = native / 'libs'
        runtime.mkdir()
        dll = runtime / 'runtime.dll'
        dll.write_bytes(b'reviewed runtime')
        (runtime / '.pm3gui-runtime-manifest.json').write_text(json.dumps({
            'kind': 'proxmark3-gui-local-ucrt64-runtime-v1',
            'files': [{'relative_target': 'runtime.dll', 'sha256': digest(dll)}]}))
        license_path = self.source / 'docs/licenses/proxmark3-LICENSE.txt'
        license_path.parent.mkdir(parents=True)
        license_path.write_text('Synthetic license fixture\n')
        self.original_manifests = {}
        for key, stem, extras in [('usb', 'gui-cancel', []), ('blueshark', 'gui-blueshark', ['BTADDON'])]:
            folder = self.source / 'artifacts' / key
            folder.mkdir(parents=True)
            image = folder / 'fullimage.elf'
            image.write_bytes(elf_bytes())
            metadata = folder / 'default_version_pm3.c'
            metadata.write_text('/* Generated version fixture */\n')
            value = {'platform': 'PM3RDV4', 'flash_capacity': 524288, 'platform_extras': extras,
                     'custom_version': 'Iceman/test-release', 'arm_sources_checksum': '123456789',
                     'source_tag': 'v4.23346', 'source_commit': 'a' * 40,
                     'artifact_files': {
                         'fullimage.elf': {'path': str(image), 'sha256': digest(image)},
                         'default_version_pm3.c': {'path': str(metadata), 'sha256': digest(metadata)},
                         'proxmark3.exe': {'path': str(self.native_client), 'sha256': digest(self.native_client)}}}
            path = self.setup / (stem + '-build-manifest.json')
            path.write_text(json.dumps(value))
            self.original_manifests[key] = path
            # A release must never sweep up a user's old backup from artifacts.
            (folder / 'device-flash-before.bin').write_bytes(b'PRIVATE FLASH')
            (folder / 'library.sqlite3').write_bytes(b'PRIVATE LIBRARY')
            (folder / 'client.log').write_text('PRIVATE LOG')
        self.staged_client = self.release / 'client/proxmark3.exe'
        shutil.copytree(native, self.staged_client.parent)

    def stage(self):
        return stage_firmware(self.source, self.release, self.staged_client)

    def test_stage_uses_allowlist_relative_paths_and_survives_relocation(self):
        with patch('pm3gui.firmware.serial.Serial', side_effect=AssertionError('port opened')), \
                patch('pm3gui.firmware.list_ports.comports', side_effect=AssertionError('enumeration')):
            result = self.stage()
            self.assertTrue(result['passed'])
            folder = self.release / 'firmware'
            self.assertEqual({p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}, {
                'LICENSE.txt', 'gui-cancel-build-manifest.json', 'gui-blueshark-build-manifest.json',
                'rdv4-usb/fullimage.elf', 'rdv4-usb/default_version_pm3.c', 'rdv4-usb/Makefile.platform',
                'rdv4-bt/fullimage.elf', 'rdv4-bt/default_version_pm3.c', 'rdv4-bt/Makefile.platform'})
            for path in folder.glob('*.json'):
                text = path.read_text()
                self.assertNotIn(str(self.root), text)
                self.assertNotIn('private source checkout', text)
                value = json.loads(text)
                for item in value['artifact_files'].values():
                    self.assertFalse(Path(item['path']).is_absolute())
            moved = self.root / 'moved folder with spaces' / 'Приложение'
            moved.parent.mkdir()
            shutil.move(str(self.release), str(moved))
            local = self.root / 'user-local'
            with patch('sys.frozen', True, create=True), patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
                catalog = FirmwareCatalog(moved)
                self.assertEqual(catalog.setup_dir, moved / 'firmware')
                self.assertEqual([profile.available for profile in catalog.profiles()], [True, True])
                for profile in catalog.profiles():
                    plan = catalog.prepare(profile.key)
                    self.assertEqual(plan.client_path, moved / 'client/proxmark3.exe')
                    self.assertTrue(plan.image_path.is_relative_to(moved))
                    self.assertTrue(plan.backup_directory.is_relative_to(local / 'Proxmark Studio'))
                    self.assertFalse(plan.backup_directory.exists())
                    self.assertNotIn('--unlock-bootloader', plan.flash_argv())
                    self.assertEqual(plan.flash_argv().count('--image'), 1)
            self.assertFalse(local.exists())

    def test_changed_client_or_runtime_rejected_before_firmware_files_written(self):
        self.staged_client.write_bytes(b'MZ changed client')
        with self.assertRaisesRegex(ValueError, 'client does not match'):
            self.stage()
        self.assertFalse((self.release / 'firmware').exists())
        self.staged_client.write_bytes(self.native_client.read_bytes())
        (self.staged_client.parent / 'libs/runtime.dll').write_bytes(b'changed runtime')
        with self.assertRaisesRegex(ValueError, 'runtime is incomplete or changed'):
            self.stage()
        self.assertFalse((self.release / 'firmware').exists())
        runtime = self.staged_client.parent / 'libs'
        manifest = runtime / '.pm3gui-runtime-manifest.json'
        value = json.loads(manifest.read_text())
        value['files'][0]['sha256'] = digest(runtime / 'runtime.dll')
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'runtime is incomplete or changed'):
            self.stage()
        self.assertFalse((self.release / 'firmware').exists())

    def test_metadata_and_source_image_tamper_never_create_partial_catalog(self):
        manifest = self.original_manifests['blueshark']
        value = json.loads(manifest.read_text())
        metadata = Path(value['artifact_files']['default_version_pm3.c']['path'])
        metadata.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'metadata is missing or changed'):
            self.stage()
        self.assertFalse((self.release / 'firmware').exists())
        metadata.write_text('/* Generated version fixture */\n')
        Path(value['artifact_files']['fullimage.elf']['path']).write_bytes(b'invalid ELF')
        with self.assertRaises(FirmwareError):
            self.stage()
        self.assertFalse((self.release / 'firmware').exists())

    def test_preparing_portable_catalog_is_read_only_and_detects_later_tamper(self):
        self.stage()
        with patch('sys.frozen', True, create=True):
            catalog = FirmwareCatalog(self.release)
            before = {p.relative_to(self.release): p.read_bytes() for p in self.release.rglob('*') if p.is_file()}
            plan = catalog.prepare('usb', backup_root=self.root / 'explicit user backups')
            self.assertFalse(plan.backup_directory.exists())
            after = {p.relative_to(self.release): p.read_bytes() for p in self.release.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            plan.image_path.write_bytes(b'changed ELF')
            self.assertFalse(catalog.profiles()[0].available)
            with self.assertRaises(FirmwareError):
                catalog.prepare('usb')

    def test_never_overwrites_an_existing_firmware_staging_directory(self):
        target = self.release / 'firmware'
        target.mkdir()
        sentinel = target / 'existing.txt'
        sentinel.write_text('keep me')
        with self.assertRaisesRegex(ValueError, 'empty firmware staging'):
            self.stage()
        self.assertEqual(sentinel.read_text(), 'keep me')


if __name__ == '__main__':
    unittest.main()
