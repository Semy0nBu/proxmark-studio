"""Installer acceptance uses synthetic payloads; no installation or COM launch."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_verify_release import pe64
from tools import verify_installer_install as validation


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))


def manifest(root):
    value = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in root.rglob('*') if path.is_file() and path.name != validation.MANIFEST}
    write(root / validation.MANIFEST, json.dumps(value, indent=2) + '\n')


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}


class InstallerValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = self.root / 'portable payload'
        write(self.payload / 'ProxmarkStudio.exe', pe64())
        write(self.payload / 'client/proxmark3.exe', pe64())
        write(self.payload / 'docs/readme.txt', 'Test payload documentation\n')
        write(self.payload / 'release.json', json.dumps({'platform': 'windows-x64', 'bundle_kind': 'complete'}))
        manifest(self.payload)
        self.number = 0
        self.verify = self.enterContext(patch.object(validation, 'verify_release',
            return_value={'passed': True, 'errors': [], 'warnings': []}))

    def installed(self):
        self.number += 1
        target = self.root / ('Installed copy ' + str(self.number))
        shutil.copytree(self.payload, target)
        write(target / 'unins000.exe', b'Installer-generated uninstaller fixture')
        write(target / 'unins000.dat', b'Installer-generated state fixture')
        return target

    def test_matches_payload_and_manifest_bytes_with_only_inno_files_and_no_mutation(self):
        target = self.installed()
        write(target / 'unins000.msg', 'Optional installer language messages')
        before = snapshot(target)
        result = validation.verify_installation(target, self.payload)
        self.assertTrue(result['passed'], result['errors'])
        self.assertTrue(result['read_only'])
        self.assertFalse(result['hardware_access'])
        self.assertEqual(snapshot(target), before)
        self.verify.assert_called_once_with(validation._io_path(self.payload.resolve()))
        architecture = next(item['details'] for item in result['checks'] if item['name'] == 'installed-pe-architecture')
        self.assertEqual(architecture, {'ProxmarkStudio.exe': 'windows-x64', 'client/proxmark3.exe': 'windows-x64'})
        json.dumps(result)  # Reports must remain JSON serializable for actual acceptance.

    def test_rejects_changed_missing_extra_and_manifest_only_modifications(self):
        cases = [
            ('changed tracked bytes', lambda root: write(root / 'docs/readme.txt', 'changed'), 'Changed payload bytes'),
            ('changed manifest whitespace', lambda root: write(root / validation.MANIFEST,
                (root / validation.MANIFEST).read_bytes() + b' '), validation.MANIFEST),
            ('foreign file', lambda root: write(root / 'extra.txt', 'foreign'), 'Foreign installed files'),
            ('nested uninstaller', lambda root: write(root / 'nested/unins000.exe', 'foreign'), 'Foreign installed files'),
            ('foreign empty directory', lambda root: (root / 'foreign-empty').mkdir(), 'Foreign installed directories'),
            ('missing uninstaller', lambda root: (root / 'unins000.dat').unlink(), 'Missing installer files'),
            ('missing payload', lambda root: (root / 'docs/readme.txt').unlink(), 'Missing payload'),
        ]
        for title, change, expected in cases:
            with self.subTest(title=title):
                target = self.installed();change(target)
                result = validation.verify_installation(target, self.payload)
                self.assertFalse(result['passed'])
                self.assertIn(expected, '\n'.join(result['errors']))

    def test_rejects_unverified_payload_escaping_manifest_and_wrong_pe_architecture(self):
        target = self.installed()
        self.verify.return_value = {'passed': False, 'errors': ['Unverified payload'], 'warnings': []}
        with patch.object(validation, '_installed_entries', side_effect=AssertionError('must stop before installed scan')):
            result = validation.verify_installation(target, self.payload)
        self.assertFalse(result['passed'])
        self.verify.return_value = {'passed': True, 'errors': [], 'warnings': []}
        write(self.payload / validation.MANIFEST, json.dumps({'../outside.txt': 'a' * 64}))
        self.assertFalse(validation.verify_installation(target, self.payload)['passed'])
        binary = bytearray(pe64());struct.pack_into('<H', binary, 132, 0x14c)
        write(self.payload / 'client/proxmark3.exe', bytes(binary));manifest(self.payload)
        target = self.installed()
        result = validation.verify_installation(target, self.payload)
        self.assertFalse(result['passed'])
        self.assertIn('Expected a Windows x64 executable', '\n'.join(result['errors']))

    def test_cli_writes_only_an_external_report_and_refuses_report_inside_payload_or_installation(self):
        target = self.installed()
        for output in (target / 'report.json', self.payload / 'report.json'):
            arguments = ['verify', '--application', str(target), '--payload', str(self.payload), '--output', str(output)]
            with patch('sys.argv', arguments), patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as failure:
                validation.main()
            self.assertEqual(failure.exception.code, 2)
            self.assertFalse(output.exists())
        output = self.root / 'external-report.json'
        before = snapshot(target)
        with patch('sys.argv', ['verify', '--application', str(target), '--payload', str(self.payload), '--output', str(output)]), \
                patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(validation.main(), 0)
        self.assertTrue(json.loads(output.read_text())['passed'])
        self.assertEqual(snapshot(target), before)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended paths are Windows-only')
    def test_windows_paths_beyond_max_path_are_hashed_with_normal_report_paths(self):
        target = self.installed()
        relative = 'licenses/native/dependency/third-party-notices/' + 'license-' + 'x' * 142 + '.txt'
        while len(str(target / relative)) <= 280:
            relative = 'deeper-directory/' + relative
        content = b'Long-path upstream license fixture\n'
        self.addCleanup((validation._io_path(self.payload) / relative).unlink, missing_ok=True)
        self.addCleanup((validation._io_path(target) / relative).unlink, missing_ok=True)
        write(validation._io_path(self.payload) / relative, content)
        write(validation._io_path(target) / relative, content)
        entries = json.loads((self.payload / validation.MANIFEST).read_text())
        entries[relative] = hashlib.sha256(content).hexdigest()
        raw = json.dumps(entries, indent=2) + '\n'
        write(self.payload / validation.MANIFEST, raw)
        write(target / validation.MANIFEST, raw)
        files, _ = validation._installed_entries(target)
        self.assertGreater(len(str(target / relative)), 260)
        self.assertTrue(str(files[relative]).startswith('\\\\?\\'))
        self.assertEqual(validation.digest(files[relative]), entries[relative])
        before = {name: path.read_bytes() for name, path in files.items()}
        result = validation.verify_installation(target, self.payload)
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['application_directory'], str(target.resolve()))
        self.assertEqual(result['payload_directory'], str(self.payload.resolve()))
        self.assertNotIn('\\\\?\\', json.dumps(result))
        after, _ = validation._installed_entries(target)
        self.assertEqual({name: path.read_bytes() for name, path in after.items()}, before)
        unc = Path(r'\\server\share\folder')
        self.assertEqual(validation._normal_path(validation._io_path(unc)), unc)

    @unittest.skipUnless(os.name == 'nt', 'Windows shortcut COM properties are Windows-only')
    def test_shortcuts_use_readonly_com_and_reject_wrong_target_workdir_arguments_or_runas(self):
        target = self.installed()
        desktop, start_menu = self.root / "Desk's shortcut.lnk", self.root / 'Start menu.lnk'
        header = struct.pack('<I', 76) + validation.SHELL_LINK_CLSID + struct.pack('<I', 0) + bytes(52)
        write(desktop, header);write(start_menu, header)
        properties = {'TargetPath': str(target / 'ProxmarkStudio.exe'),
                      'WorkingDirectory': str(target), 'Arguments': ''}
        with patch.object(validation.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(properties))) as run:
            result = validation.verify_installation(target, self.payload, desktop_shortcut=desktop,
                                                     start_menu_shortcut=start_menu)
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(run.call_count, 2)
        for call, link in zip(run.call_args_list, (desktop, start_menu)):
            self.assertEqual(call.kwargs['env']['PM3_INSTALLER_SHORTCUT'], str(link.resolve()))
            self.assertNotIn(str(link), call.args[0][-1])
            self.assertNotIn('.Save', call.args[0][-1])
            self.assertIn('-NonInteractive', call.args[0])
        for field, wrong, error in [('TargetPath', str(self.root / 'different.exe'), 'different application'),
                                     ('WorkingDirectory', str(self.root), 'working directory'),
                                     ('Arguments', '--unexpected', 'command-line arguments')]:
            with self.subTest(field=field), patch.object(validation.subprocess, 'run',
                    return_value=SimpleNamespace(stdout=json.dumps(dict(properties, **{field: wrong})))):
                result = validation.verify_installation(target, self.payload, desktop_shortcut=desktop)
                self.assertFalse(result['passed'])
                self.assertIn(error, '\n'.join(result['errors']))
        write(desktop, struct.pack('<I', 76) + validation.SHELL_LINK_CLSID + struct.pack('<I', validation.RUN_AS_USER) + bytes(52))
        with patch.object(validation.subprocess, 'run', side_effect=AssertionError('no COM for an elevated shortcut')):
            result = validation.verify_installation(target, self.payload, desktop_shortcut=desktop)
        self.assertFalse(result['passed'])
        self.assertIn('Run as user/administrator', '\n'.join(result['errors']))


if __name__ == '__main__':
    unittest.main()
