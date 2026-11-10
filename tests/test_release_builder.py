"""Distribution boundaries: no developer environment or personal lab payload."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
            public = ('README.md','LICENSE','pm3gui/app.py','pm3gui/locales/en.json',
                      'tests/test_product.py','tools/release_gui_sources.json',
                      'docs/USER_GUIDE_EN.md','docs/licenses/component/COPYING',
                      'docs/PRIVACY_RU.md','docs/PRIVACY_EN.md',
                      'installer/proxmark-studio.iss','installer/toolchain.json',
                      'docs/assets/banner.svg','docs/assets/interface-en.png','.gitattributes','CHANGELOG.md',
                      '.github/workflows/ci.yml')
            private = ('data/personal/library.sqlite3','data/personal/settings.json',
                       'outputs/backup.bin','docs/PRIVATE_HARDWARE_REPORT.md',
                       'tools/iphone-connection-lab/secret.py','reference/dump.bin',
                       'tests/test_connection_lab.py','tests/test_live_functional_harness.py',
                       'pm3gui/__pycache__/app.pyc','docs/assets/private-capture.png')
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


if __name__ == '__main__':
    unittest.main()
