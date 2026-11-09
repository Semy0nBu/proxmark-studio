"""Installer packaging must reject unreviewed or mismatched payloads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pm3gui.version import APP_VERSION
from tools import build_installer


class InstallerBuilderTests(unittest.TestCase):
    def test_failed_payload_validation_stops_before_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(build_installer, 'verify_release', return_value={'passed': False, 'errors': ['modified DLL']}):
                with patch.object(build_installer, 'compiler_lock') as compiler:
                    with self.assertRaisesRegex(ValueError, 'modified DLL'):
                        build_installer.build_installer(Path(directory)/'app', Path(directory)/'out',
                                                        Path(directory)/'ISCC.exe', Path(directory)/'work')
                    compiler.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_gui_only_and_mismatched_release_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for metadata, error in (({'version': APP_VERSION, 'bundle_kind': 'gui-only'}, 'complete'),
                                    ({'version': '99.0.0', 'bundle_kind': 'complete'}, 'versions differ')):
                (root/'release.json').write_text(json.dumps(metadata), encoding='utf-8')
                with patch.object(build_installer, 'verify_release', return_value={'passed': True}):
                    with self.assertRaisesRegex(ValueError, error):
                        build_installer.validate_payload(root)

    def test_output_and_work_cannot_contaminate_application_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'app'
            root.mkdir()
            cases = ((root, Path(directory)/'work'), (root/'setup', Path(directory)/'work'),
                     (Path(directory)/'out', root/'compile'))
            with patch.object(build_installer, 'validate_payload', return_value=root):
                with patch.object(build_installer, 'compiler_lock') as compiler:
                    for output, work in cases:
                        with self.subTest(output=output, work=work):
                            with self.assertRaisesRegex(ValueError, 'outside'):
                                build_installer.build_installer(root, output, Path(directory)/'ISCC.exe', work)
                    compiler.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_wrong_compiler_bytes_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'installer').mkdir()
            (root/'installer/toolchain.json').write_text(json.dumps({'compiler_sha256': '0'*64}), encoding='utf-8')
            executable = root/'ISCC.exe'
            executable.write_bytes(b'unreviewed executable')
            with patch.object(build_installer, 'ROOT', root), patch.object(build_installer.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'exact reviewed'):
                    build_installer.compiler_lock(executable)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
