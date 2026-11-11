"""Distribution boundaries: no developer environment or personal lab payload."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tools import build_release


class ReleaseBuilderTests(unittest.TestCase):
    def test_build_path_cannot_collect_foreign_native_libraries(self):
        with patch.dict(os.environ, {'PATH': r'C:\Foreign\Poppler;C:\Foreign\Qt',
                                     'PYTHONHOME':'foreign-python', 'PYTHONPATH':'foreign-modules',
                                     'VIRTUAL_ENV':'foreign-venv', 'QT_PLUGIN_PATH':'foreign-qt',
                                     'QT_QPA_PLATFORM_PLUGIN_PATH':'foreign-platforms'}, clear=True):
            environment = build_release.build_environment()
        self.assertNotIn('Foreign', environment['PATH'])
        for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
            self.assertNotIn(key, environment)
        self.assertIn('System32', environment['PATH'])

    def test_public_snapshot_copies_product_and_excludes_private_lab_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'source';root.mkdir()
            public = ('README.md','README.ru.md','LICENSE','pm3gui/app.py','pm3gui/locales/en.json',
                      'tests/test_product.py','tools/release_gui_sources.json',
                      'docs/USER_GUIDE_EN.md','docs/licenses/component/COPYING',
                      'docs/PRIVACY_RU.md','docs/PRIVACY_EN.md','docs/CARD_WORKSPACE.md','docs/CARD_TRANSFER.md','docs/STABILITY_AND_MOTION.md','docs/LIBRARY_BACKUPS.md',
                      'installer/proxmark-studio.iss','installer/toolchain.json',
                      'docs/assets/banner.svg','docs/assets/interface-en.png','.gitattributes','CHANGELOG.md',
                      '.github/workflows/ci.yml')
            public += ('tools/native-client-tests/production_usb_control_test.cpp',
                       'tools/client-patches/USB_HARDNESTED.md',
                       'tools/client-patches/usb-hardnested-sources.json')
            private = ('data/personal/library.sqlite3','data/personal/settings.json',
                       'outputs/backup.bin','docs/PRIVATE_HARDWARE_REPORT.md',
                       'tools/iphone-connection-lab/secret.py','reference/dump.bin',
                       'tests/test_connection_lab.py','tests/test_live_functional_harness.py',
                       'pm3gui/__pycache__/app.pyc','docs/assets/private-capture.png',
                       'pm3gui/private.pm3backup','tests/private.pm3backup',
                       'tools/client-patches/private-lab.json')
            for name in (*public,*private):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
            destination=Path(directory)/'publication'
            with patch.object(build_release,'ROOT',root):
                build_release.export_source(destination)
            for name in public:self.assertTrue((destination/name).is_file(),name)
            for name in private:self.assertFalse((destination/name).exists(),name)
            snapshot=json.loads((destination/'SOURCE_SNAPSHOT.json').read_text())
            self.assertEqual(set(snapshot['excluded_lab_tests']),build_release.PUBLIC_TEST_EXCLUDES)

    def test_export_never_overwrites_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                build_release.export_source(Path(directory))

    def test_backend_reuse_validates_before_copying_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'old';source.mkdir()
            destination = Path(directory)/'new';destination.mkdir()
            with patch('tools.verify_release.verify_release', return_value={'passed':False,'errors':['changed client']}):
                with self.assertRaisesRegex(ValueError,'changed client'):
                    build_release.reuse_native_bundle(source,destination)
            self.assertEqual(list(destination.iterdir()),[])

    def test_staging_retains_original_client_hash_for_utf8_proof_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            app=Path(directory)/'app';app.mkdir()
            (app/'ProxmarkStudio.exe').write_bytes(b'fixture GUI')
            original='a'*64
            def reuse(source,destination):
                (destination/'client').mkdir()
                (destination/'client/proxmark3.exe').write_bytes(b'fixture transformed client')
                (destination/'native-manifest.json').write_text(json.dumps({'original_client_sha256':original}))
            args=SimpleNamespace(gui_only=False,base_bundle=Path(directory)/'base')
            with patch.object(build_release,'reuse_native_bundle',side_effect=reuse), \
                 patch.object(build_release,'stage_public_documents'), \
                 patch('tools.release_gui_licenses.stage_gui_licenses'):
                build_release.stage(args,app)
            release=json.loads((app/'release.json').read_text())
            self.assertEqual(release['native_client_original_sha256'],original)
            self.assertEqual(release['native_client_sha256'],build_release.digest(app/'client/proxmark3.exe'))

    def test_backend_reuse_carries_client_copyleft_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'base';destination=Path(directory)/'new'
            source.mkdir();destination.mkdir()
            for name in ('client','firmware','licenses/proxmark3','licenses/native-runtime','sources/native-runtime'):
                (source/name).mkdir(parents=True,exist_ok=True)
            (source/'release.json').write_text(json.dumps({'bundle_kind':'complete'}))
            (source/'licenses/proxmark3/AUTHORS.md').write_text('Synthetic upstream notice')
            (source/'licenses/proxmark3/LICENSE.txt').write_text('Synthetic upstream license')
            for name in ('native-manifest.json','sources/proxmark3-corresponding-source.zip'):
                (source/name).write_bytes(b'fixture')
            with patch('tools.verify_release.verify_release',return_value={'passed':True}):
                build_release.reuse_native_bundle(source,destination)
            for name in ('AUTHORS.md','LICENSE.txt'):
                self.assertEqual((source/'licenses/proxmark3'/name).read_bytes(),(destination/'licenses/proxmark3'/name).read_bytes())


if __name__ == '__main__':
    unittest.main()
