"""Packaged startup keeps writable data outside the distribution directory."""
import json
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from pm3gui import paths
from pm3gui.app import MainWindow, main
from pm3gui.autowrite import parser as autowrite_parser
from pm3gui.device import ClientRunner, DeviceSettings, build_invocation, discover_client
from pm3gui.i18n import language, set_language
from pm3gui import process_env


class _PathFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.bundle = self.root / 'Read only installation' / 'Proxmark Studio'
        self.bundle.mkdir(parents=True)
        self.exe = self.bundle / 'Proxmark Studio.exe'
        self.exe.touch()
        self.user = self.root / 'User profile'
        self.local = self.user / 'AppData' / 'Local'

    def tearDown(self):
        self.temp.cleanup()

    def frozen(self):
        self.enterContext(patch.object(paths.sys, 'frozen', True, create=True))
        self.enterContext(patch.object(paths.sys, 'executable', str(self.exe)))
        self.enterContext(patch.dict(os.environ, {'LOCALAPPDATA': str(self.local)}))


class RuntimePathTests(_PathFixture, unittest.TestCase):
    def test_version_and_self_test_do_not_create_the_normal_profile_or_start_gui(self):
        with patch('pm3gui.app.default_data_dir', side_effect=AssertionError('No normal profile')), \
                patch('pm3gui.app.QApplication', side_effect=AssertionError('No main GUI')):
            text = io.StringIO()
            with contextlib.redirect_stdout(text), self.assertRaises(SystemExit) as stop:
                main(['--version'])
            self.assertEqual(stop.exception.code, 0)
            self.assertIn('Proxmark Studio', text.getvalue())
            checker = Mock(return_value=17)
            with patch.dict(sys.modules, {'pm3gui.release_check':SimpleNamespace(run_release_check=checker)}):
                report = self.root / 'report.json'
                self.assertEqual(main(['--self-test', str(report)]), 17)
                checker.assert_called_once_with(report, language='en')

    def test_source_checkout_keeps_existing_personal_and_demo_paths(self):
        source = self.root / 'source checkout'
        marker = source / 'data' / 'personal' / 'keep.txt'
        marker.parent.mkdir(parents=True);marker.write_text('existing local data')
        with patch.object(paths, 'is_frozen', return_value=False), \
                patch.object(paths, 'application_root', return_value=source):
            self.assertEqual(paths.default_data_dir(), marker.parent)
            self.assertEqual(paths.default_data_dir(demo=True), source / 'data' / 'demo')
            self.assertEqual(paths.resource_root(), source)
            self.assertEqual(paths.firmware_catalog_dir(), source / 'outputs' / 'device-setup')
        self.assertEqual(marker.read_text(), 'existing local data')

    def test_frozen_resources_and_user_data_are_independent_of_current_directory(self):
        self.frozen()
        internal = self.bundle / '_internal'
        with patch.object(paths.sys, '_MEIPASS', str(internal), create=True):
            self.assertEqual(paths.application_root(), self.bundle)
            self.assertEqual(paths.resource_root(), internal)
            self.assertEqual(paths.default_data_dir(), self.local / 'Proxmark Studio')
            self.assertEqual(paths.default_data_dir(demo=True), self.local / 'Proxmark Studio' / 'demo')
            self.assertEqual(paths.firmware_catalog_dir(), self.bundle / 'firmware')
            self.assertEqual(paths.startup_error_path(), self.local / 'Proxmark Studio' / 'startup-error.log')
        self.assertFalse(paths.default_data_dir().exists())

    def test_missing_localappdata_uses_user_profile_not_installation(self):
        self.frozen()
        with patch.dict(os.environ, {'LOCALAPPDATA': ''}), patch.object(Path, 'home', return_value=self.user):
            self.assertEqual(paths.default_data_dir(), self.user / 'AppData' / 'Local' / 'Proxmark Studio')

    def test_missing_pyinstaller_resource_marker_falls_back_to_executable_directory(self):
        self.frozen()
        with patch.object(paths.sys, '_MEIPASS', None, create=True):
            self.assertEqual(paths.resource_root(), self.bundle)

    def test_frozen_discovery_prefers_shipped_client_over_reference_and_source_root(self):
        self.frozen()
        bundled = self.bundle / 'client' / 'proxmark3.exe'
        bundled.parent.mkdir();bundled.touch()
        other = self.root / 'old checkout'
        old = other / 'client' / 'proxmark3.exe';old.parent.mkdir(parents=True);old.touch()
        self.assertEqual(discover_client(other), str(bundled))
        self.assertEqual(discover_client(), str(bundled))
        bundled.unlink();old.unlink()
        legacy = other / 'reference' / 'legacy' / 'old' / 'client' / 'proxmark3.exe'
        legacy.parent.mkdir(parents=True);legacy.touch()
        self.assertEqual(discover_client(other), '')

    def test_autowrite_defaults_to_same_writable_profile_and_allows_explicit_override(self):
        self.frozen()
        self.assertEqual(autowrite_parser().parse_args(['--dump', 'sample.bin']).data_dir,
                         self.local / 'Proxmark Studio')
        explicit = self.root / 'Chosen library'
        self.assertEqual(autowrite_parser().parse_args(['--dump', 'sample.bin', '--data-dir', str(explicit)]).data_dir,
                         explicit)


class PackagedProfileTests(_PathFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_fresh_profile_and_missing_saved_client_use_bundle_without_touching_installation(self):
        self.frozen()
        client = self.bundle / 'client' / 'proxmark3.exe';client.parent.mkdir();client.touch()
        profile = paths.default_data_dir()
        profile.mkdir(parents=True)
        settings = profile / 'settings.json'
        saved = {'language': 'en', 'client_path': str(self.root / 'removed version' / 'proxmark3.exe')}
        settings.write_text(json.dumps(saved), encoding='utf-8')
        before = sorted(str(p.relative_to(self.bundle)) for p in self.bundle.rglob('*'))
        previous = language()
        with patch('pm3gui.app.ROOT', self.bundle), \
                patch('pm3gui.app.available_ports', return_value=[]), \
                patch('pm3gui.app.ClientRunner.run') as run, \
                patch('pm3gui.app.ClientRunner.run_operation') as operation:
            window = MainWindow(profile)
            try:
                self.assertEqual(window.project_root, self.bundle)
                self.assertEqual(window.client_path.text(), str(client))
                self.assertTrue((profile / 'library.sqlite3').is_file())
                self.assertEqual(window.library.list_cards(), [])
                self.assertEqual(window.key_library.list_keys(), [])
                self.assertEqual(json.loads(settings.read_text(encoding='utf-8')), saved)
                self.assertEqual(sorted(str(p.relative_to(self.bundle)) for p in self.bundle.rglob('*')), before)
                run.assert_not_called();operation.assert_not_called()
            finally:
                window.dirty = False;window.close();window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents();set_language(previous)

    def test_existing_custom_client_is_preserved_in_memory_and_on_disk(self):
        self.frozen()
        client = self.root / 'User chosen client' / 'proxmark3.exe'
        client.parent.mkdir();client.touch()
        profile = paths.default_data_dir();profile.mkdir(parents=True)
        saved = {'client_path': str(client)}
        (profile / 'settings.json').write_text(json.dumps(saved), encoding='utf-8')
        with patch('pm3gui.app.ROOT', self.bundle), patch('pm3gui.app.available_ports', return_value=[]), \
                patch('pm3gui.app.ClientRunner.run') as run, \
                patch('pm3gui.app.ClientRunner.run_operation') as operation:
            window = MainWindow(profile)
            try:
                self.assertEqual(window.client_path.text(), str(client))
                self.assertEqual(window.settings['client_path'], str(client))
                self.assertEqual(json.loads((profile / 'settings.json').read_text()), saved)
                run.assert_not_called();operation.assert_not_called()
            finally:
                window.dirty = False;window.close();window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_packaged_autowrite_preparation_discovers_client_without_developer_settings(self):
        from pm3gui.autowrite import main as autowrite_main
        self.frozen()
        client = self.bundle / 'client' / 'proxmark3.exe';client.parent.mkdir();client.touch()
        source = self.root / 'synthetic.bin'
        data = bytearray(1024)
        for sector in range(16):
            start = (sector * 4 + 3) * 16
            data[start:start+16] = bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')
        source.write_bytes(data)
        output = self.root / 'cli-output'
        with patch('pm3gui.autowrite.ROOT', self.bundle), \
                patch('pm3gui.autowrite.CommandExecutor') as executor, \
                patch('pm3gui.autowrite.run_transaction', return_value=False) as transaction, \
                contextlib.redirect_stdout(io.StringIO()):
            result = autowrite_main(['--dump', str(source), '--port', 'COM1', '--output-dir', str(output)])
        self.assertEqual(result, 0)
        self.assertEqual(executor.call_args.args[0].client_path, str(client))
        self.assertFalse(transaction.call_args.args[2])
        self.assertTrue(next(output.glob('*/scenario.pm3write.json')).is_file())
        self.assertFalse((self.bundle / 'data').exists())


class NativePythonEnvironmentTests(unittest.TestCase):
    def test_private_native_python_does_not_inherit_gui_python_or_qt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();client = root / 'client' / 'proxmark3.exe'
            client.parent.mkdir();client.touch();(client.parent / 'libs' / 'platforms').mkdir(parents=True)
            prefix = client.parent / 'python';encodings = prefix / 'lib' / 'python3.14' / 'encodings'
            encodings.mkdir(parents=True);(encodings / '__init__.py').touch();(prefix / 'bin').mkdir()
            scripts = client.parent / 'pyscripts';scripts.mkdir()
            wrong = {'PYTHONHOME': 'wrong-gui-home', 'PYTHONPATH': 'wrong-gui-site-packages',
                     'VIRTUAL_ENV': 'wrong-gui-venv',
                     'QT_PLUGIN_PATH': 'wrong-gui-qt', 'QT_QPA_PLATFORM_PLUGIN_PATH': 'wrong-gui-platforms'}
            with patch.dict(os.environ, wrong):
                before = dict(os.environ)
                _, _, _, env = build_invocation(DeviceSettings(str(client), 'COM1'), 'hw version')
                self.assertEqual(env.value('PYTHONHOME'), str(prefix))
                self.assertEqual(env.value('PYTHONPATH'), str(scripts))
                self.assertEqual(env.value('PYTHONNOUSERSITE'), '1')
                self.assertEqual(env.value('PYTHONDONTWRITEBYTECODE'), '1')
                self.assertFalse(env.contains('VIRTUAL_ENV'))
                self.assertIn(str(prefix / 'bin'), env.value('PATH').split(os.pathsep))
                self.assertEqual(env.value('QT_PLUGIN_PATH'), str(client.parent / 'libs'))
                self.assertEqual(env.value('QT_QPA_PLATFORM_PLUGIN_PATH'), str(client.parent / 'libs' / 'platforms'))
                self.assertEqual(dict(os.environ), before)

    def test_incomplete_private_prefix_never_replaces_an_external_client_python_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve();client = root / 'proxmark3.exe';client.touch();(root / 'python').mkdir()
            with patch.dict(os.environ, {'PYTHONHOME': 'external-python'}):
                _, _, _, env = build_invocation(DeviceSettings(str(client), 'COM1'), 'hw version')
                self.assertEqual(env.value('PYTHONHOME'), 'external-python')


class ExternalProcessLibraryTests(unittest.TestCase):
    class Kernel:
        def __init__(self):
            self.directory = r'C:\GUI\_internal'
            self.calls = []
        def GetDllDirectoryW(self, size, buffer):
            if buffer is None:return len(self.directory) + 1 if self.directory else 0
            buffer.value = self.directory
            return len(self.directory)
        def SetDllDirectoryW(self, value):
            self.calls.append(value);self.directory = value
            return 1

    def setUp(self):
        self.kernel = self.Kernel()

    def frozen(self):
        self.enterContext(patch.object(process_env, 'is_frozen', return_value=True))
        self.enterContext(patch.object(process_env.os, 'name', 'nt'))
        self.enterContext(patch.object(process_env, '_kernel32', return_value=self.kernel))
        self.enterContext(patch.object(process_env.ctypes, 'set_last_error', return_value=0, create=True))
        self.enterContext(patch.object(process_env.ctypes, 'get_last_error', return_value=0, create=True))

    def test_child_creation_uses_system_dll_search_and_parent_is_restored(self):
        self.frozen()
        with process_env.external_process_libraries():
            self.assertIsNone(self.kernel.directory)
        self.assertEqual(self.kernel.directory, r'C:\GUI\_internal')
        self.assertEqual(self.kernel.calls, [None, r'C:\GUI\_internal'])

    def test_failed_child_creation_still_restores_parent_and_propagates_error(self):
        self.frozen()
        with self.assertRaisesRegex(ValueError, 'failed spawn'):
            with process_env.external_process_libraries():
                raise ValueError('failed spawn')
        self.assertEqual(self.kernel.directory, r'C:\GUI\_internal')

    def test_nested_contexts_restore_each_previous_directory(self):
        self.frozen()
        with process_env.external_process_libraries():
            with process_env.external_process_libraries():
                self.assertIsNone(self.kernel.directory)
            self.assertIsNone(self.kernel.directory)
        self.assertEqual(self.kernel.directory, r'C:\GUI\_internal')

    def test_existing_empty_dll_directory_is_preserved(self):
        self.frozen()
        self.kernel.directory = ''
        self.kernel.GetDllDirectoryW = lambda size, buffer:1 if buffer is None else 0
        with process_env.external_process_libraries():
            self.assertIsNone(self.kernel.directory)
        self.assertEqual(self.kernel.directory, '')

    def test_unfrozen_source_launch_never_changes_dll_search(self):
        with patch.object(process_env, 'is_frozen', return_value=False), \
                patch.object(process_env, '_kernel32', side_effect=AssertionError('No WinAPI in source')):
            with process_env.external_process_libraries():pass

    def test_setup_failure_prevents_child_creation(self):
        self.frozen()
        self.kernel.SetDllDirectoryW = lambda _:False
        with patch.object(process_env.ctypes, 'WinError', side_effect=lambda _:OSError('DLL setup denied'), create=True), \
                patch.object(process_env.ctypes, 'get_last_error', return_value=5, create=True):
            with self.assertRaisesRegex(OSError, 'DLL setup denied'):
                with process_env.external_process_libraries():
                    self.fail('The child cannot be started with contaminated DLL search')


class FailedPackagedSpawnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_context_setup_error_releases_busy_state_for_single_and_persistent_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            for persistent in (False, True):
                with self.subTest(persistent=persistent):
                    runner = ClientRunner();results = []
                    runner.finished.connect(lambda code, text:results.append((code,text)))
                    target = 'pm3gui.persistent.external_process_libraries' if persistent else 'pm3gui.device.external_process_libraries'
                    with patch(target, side_effect=OSError('DLL setup failed')):
                        runner.run(DeviceSettings(sys.executable, 'COM1', directory, keep_open=persistent), 'hw version')
                    self.assertFalse(runner.busy)
                    self.assertEqual(len(results), 1)
                    self.assertNotEqual(results[0][0], 0)
                    self.assertIn('DLL setup failed', results[0][1])
                    runner.shutdown();runner.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
