"""Installer packaging must reject unreviewed or mismatched payloads."""
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from pm3gui.version import APP_VERSION
from tools import build_installer


class InstallerBuilderTests(unittest.TestCase):
    def test_failed_payload_validation_stops_before_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(build_installer, 'verify_release', return_value={'passed': False, 'errors': ['modified DLL']}):
                with patch.object(build_installer, 'compiler_lock') as compiler:
                    with self.assertRaisesRegex(ValueError, 'modified DLL'):
                        build_installer.build_installer(root/'app', root/'out',
                                                        root/'ISCC.exe', root/'work')
                    compiler.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_gui_only_and_mismatched_release_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for metadata, error in (({'version': APP_VERSION, 'bundle_kind': 'gui-only'}, 'complete'),
                                    ({'version': '99.0.0', 'bundle_kind': 'complete'}, 'versions differ')):
                (root/'release.json').write_text(json.dumps(metadata), encoding='utf-8')
                with patch.object(build_installer, 'verify_release', return_value={'passed': True}):
                    with self.assertRaisesRegex(ValueError, error):
                        build_installer.validate_payload(root)

    def test_output_and_work_cannot_contaminate_application_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory).resolve()
            root = temporary_root/'app'
            root.mkdir()
            cases = ((root, temporary_root/'work'), (root/'setup', temporary_root/'work'),
                     (temporary_root/'out', root/'compile'))
            with patch.object(build_installer, 'validate_payload', return_value=root.resolve()):
                with patch.object(build_installer, 'compiler_lock') as compiler:
                    for output, work in cases:
                        with self.subTest(output=output, work=work):
                            with self.assertRaisesRegex(ValueError, 'outside'):
                                build_installer.build_installer(root, output, temporary_root/'ISCC.exe', work)
                    compiler.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_wrong_compiler_bytes_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'installer').mkdir()
            (root/'installer/toolchain.json').write_text(json.dumps({'compiler_sha256': '0'*64}), encoding='utf-8')
            executable = root/'ISCC.exe'
            executable.write_bytes(b'unreviewed executable')
            with patch.object(build_installer, 'ROOT', root), patch.object(build_installer.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'exact reviewed'):
                    build_installer.compiler_lock(executable)
                run.assert_not_called()


def pe64(payload=b'compiled installer fixture'):
    data = bytearray(96)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 64)
    data[64:68] = b'PE\0\0'
    struct.pack_into('<H', data, 68, 0x8664)
    struct.pack_into('<H', data, 88, 0x20b)
    return bytes(data)+payload


class InstallerPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source_dir = self.root/'compile'/'output'
        self.source_dir.mkdir(parents=True)
        self.output = self.root/'release'
        self.output.mkdir()
        self.name = f'ProxmarkStudio-{APP_VERSION}-Setup-x64.exe'
        self.source = self.source_dir/self.name
        self.data = pe64()
        self.source.write_bytes(self.data)
        self.target = self.output/self.name

    def assert_source_and_staging(self):
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual(list(self.output.glob('*.partial')), [])

    def test_copy_then_publish_renames_only_within_destination_volume(self):
        real = build_installer._publish_without_replace
        calls = []
        def guarded(source, target):
            calls.append((Path(source), Path(target)))
            self.assertEqual(Path(source).parent, Path(target).parent)
            self.assertNotEqual(Path(source), self.source)
            return real(source, target)
        with patch.object(build_installer, '_publish_without_replace', side_effect=guarded):
            result = build_installer.publish_installer(self.source, self.target)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(result, {'sha256': build_installer.digest(self.source), 'bytes': len(self.data)})
        self.assert_source_and_staging()

    def test_existing_target_without_replace_stops_before_copy(self):
        self.target.write_bytes(b'old installer')
        with patch.object(build_installer.shutil, 'copyfileobj') as copy:
            with self.assertRaises(FileExistsError):
                build_installer.publish_installer(self.source, self.target)
            copy.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'old installer')
        self.assert_source_and_staging()

    def test_copy_failure_preserves_both_originals_before_archiving(self):
        self.target.write_bytes(b'old installer')
        def interrupted(source, destination, **kwargs):
            destination.write(source.read(20))
            raise OSError('Destination disk full')
        with patch.object(build_installer.shutil, 'copyfileobj', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'disk full'):
                build_installer.publish_installer(self.source, self.target, replace=True)
        self.assertEqual(self.target.read_bytes(), b'old installer')
        self.assertFalse((self.output/'installer-history').exists())
        self.assert_source_and_staging()

    def test_incomplete_copy_is_rejected_before_touching_existing_target(self):
        self.target.write_bytes(b'old installer')
        with patch.object(build_installer.shutil, 'copyfileobj', side_effect=lambda source, destination, **kw: destination.write(source.read(20))):
            with self.assertRaisesRegex(RuntimeError, 'copy verification'):
                build_installer.publish_installer(self.source, self.target, replace=True)
        self.assertEqual(self.target.read_bytes(), b'old installer')
        self.assert_source_and_staging()

    def test_changed_copy_with_same_size_is_rejected(self):
        def corrupt(source, destination, **kwargs):
            content = bytearray(source.read())
            content[-1] ^= 1
            destination.write(content)
        with patch.object(build_installer.shutil, 'copyfileobj', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'copy verification'):
                build_installer.publish_installer(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assert_source_and_staging()

    def test_fsync_failure_keeps_compiled_source_and_old_target(self):
        self.target.write_bytes(b'old installer')
        with patch.object(build_installer.os, 'fsync', side_effect=OSError('flush failed')):
            with self.assertRaisesRegex(OSError, 'flush failed'):
                build_installer.publish_installer(self.source, self.target, replace=True)
        self.assertEqual(self.target.read_bytes(), b'old installer')
        self.assert_source_and_staging()

    def test_invalid_PE_not_published(self):
        self.source.write_bytes(b'not PE')
        with self.assertRaisesRegex(ValueError, 'Windows PE'):
            build_installer.publish_installer(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), b'not PE')
        self.assertFalse(self.target.exists())

    def test_target_created_during_copy_is_not_overwritten(self):
        copy = shutil.copyfileobj
        def concurrent(source, destination, **kwargs):
            copy(source, destination, **kwargs)
            self.target.write_bytes(b'concurrent installer')
        with patch.object(build_installer.shutil, 'copyfileobj', side_effect=concurrent):
            with self.assertRaises(FileExistsError):
                build_installer.publish_installer(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b'concurrent installer')
        self.assert_source_and_staging()

    def test_atomic_publication_cannot_overwrite_a_late_target(self):
        real = build_installer._publish_without_replace
        def concurrent(source, target):
            Path(target).write_bytes(b'concurrent installer')
            real(source, target)
        with patch.object(build_installer, '_publish_without_replace', side_effect=concurrent):
            with self.assertRaises(FileExistsError):
                build_installer.publish_installer(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b'concurrent installer')
        self.assert_source_and_staging()

    def test_explicit_replacement_keeps_old_installer_in_history(self):
        self.target.write_bytes(b'old installer')
        build_installer.publish_installer(self.source, self.target, replace=True, stamp='test-stamp')
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual((self.output/'installer-history/test-stamp'/self.name).read_bytes(), b'old installer')
        self.assert_source_and_staging()

    def test_failed_replacement_restores_old_target_without_losing_source(self):
        self.target.write_bytes(b'old installer')
        real = build_installer._publish_without_replace
        def failing(source, target):
            if Path(source).suffix == '.partial':
                raise OSError('publish failed')
            real(source, target)
        with patch.object(build_installer, '_publish_without_replace', side_effect=failing):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                build_installer.publish_installer(self.source, self.target, replace=True, stamp='test-stamp')
        self.assertEqual(self.target.read_bytes(), b'old installer')
        self.assert_source_and_staging()

    def test_failed_replacement_does_not_overwrite_new_racing_target_during_rollback(self):
        self.target.write_bytes(b'old installer')
        real = build_installer._publish_without_replace
        def failing(source, target):
            if Path(source).suffix == '.partial':
                Path(target).write_bytes(b'concurrent installer')
                raise FileExistsError('another publisher won')
            real(source, target)
        with patch.object(build_installer, '_publish_without_replace', side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, 'old installer remains'):
                build_installer.publish_installer(self.source, self.target, replace=True, stamp='test-stamp')
        self.assertEqual(self.target.read_bytes(), b'concurrent installer')
        self.assertEqual((self.output/'installer-history/test-stamp'/self.name).read_bytes(), b'old installer')
        self.assert_source_and_staging()

    def test_destination_directory_is_not_archived_as_an_installer(self):
        self.target.mkdir()
        with self.assertRaisesRegex(ValueError, 'regular file'):
            build_installer.publish_installer(self.source, self.target, replace=True)
        self.assertTrue(self.target.is_dir())
        self.assert_source_and_staging()

    def test_compiled_source_cannot_be_published_over_itself(self):
        with self.assertRaisesRegex(ValueError, 'must differ'):
            build_installer.publish_installer(self.source, self.source, replace=True)
        self.assert_source_and_staging()

    def test_finalize_existing_build_records_exact_file_without_compiling(self):
        application = self.root/'application'
        application.mkdir()
        (application/'files.sha256.json').write_bytes(b'payload manifest')
        lock = {'version': 'test', 'compiler_sha256': 'a'*64, 'download_url': 'https://example.test/compiler', 'download_sha256': 'b'*64}
        with patch.object(build_installer, 'validate_payload', return_value=application), patch.object(build_installer, 'compiler_lock', return_value=lock), patch.object(build_installer.subprocess, 'run') as run:
            result = build_installer.finalize_installer(application, self.output, self.root/'ISCC.exe', self.source)
            run.assert_not_called()
        manifest = json.loads((self.output/'installer-manifest.json').read_text(encoding='utf8'))
        self.assertEqual(manifest, result)
        self.assertEqual(manifest['sha256'], build_installer.digest(self.target))
        self.assertIn(manifest['sha256']+'  '+self.name, (self.output/'SHA256SUMS.txt').read_text())
        self.assert_source_and_staging()

    def test_finalize_refuses_sidecar_installer_parts(self):
        (self.source_dir/'setup-1.bin').write_bytes(b'part')
        with patch.object(build_installer, 'validate_payload', return_value=self.root/'app'), patch.object(build_installer, 'compiler_lock', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'single self-contained'):
                build_installer.finalize_installer(self.root/'app', self.output, self.root/'ISCC.exe', self.source)
        self.assertFalse(self.target.exists())
        self.assert_source_and_staging()


if __name__ == '__main__':
    unittest.main()
