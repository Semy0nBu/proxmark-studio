"""Read-only acceptance checks for an installed Proxmark Studio payload.

The portable payload is validated first. Installation may add only Inno Setup's
root unins000.exe/.dat and optional .msg; no operation launches the application,
the uninstaller, a device client, or the contents of a shortcut.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.verify_release import _pe64, digest, verify_release


MANIFEST = 'files.sha256.json'
UNINSTALLER_FILES = {'unins000.exe', 'unins000.dat', 'unins000.msg'}
REQUIRED_UNINSTALLER_FILES = {'unins000.exe', 'unins000.dat'}
SHA = re.compile(r'[0-9a-f]{64}\Z')
SHELL_LINK_CLSID = bytes.fromhex('0114020000000000c000000000000046')
# SLDF_RUNAS_USER: https://learn.microsoft.com/windows/win32/api/shlobj_core/ne-shlobj_core-shell_link_data_flags
RUN_AS_USER = 0x00002000


def _normal_path(path):
    """Keep extended filesystem prefixes out of reports and COM arguments."""
    text = str(path)
    if text.startswith('\\\\?\\UNC\\'):
        return Path('\\\\' + text[8:])
    if text.startswith('\\\\?\\'):
        return Path(text[4:])
    return Path(text)


def _io_path(path):
    """Use the extended Windows namespace independently of MAX_PATH policy."""
    path = Path(path).absolute()
    if os.name != 'nt':
        return path
    text = str(path)
    if text.startswith('\\\\?\\'):
        return path
    if text.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + text[2:])
    return Path('\\\\?\\' + text)


def _resolved_path(path):
    return _normal_path(_io_path(path).resolve())


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate manifest entry: ' + key)
        value[key] = item
    return value


def _manifest(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Oversized file checksum manifest.')
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or not value:
        raise ValueError('A nonempty file checksum object is required.')
    for name, expected in value.items():
        if not isinstance(name, str) or not name or '\\' in name:
            raise ValueError('A checksum path is empty or noncanonical.')
        relative = PurePosixPath(name)
        if (relative.is_absolute() or PureWindowsPath(name).drive or '..' in relative.parts
                or any(':' in part for part in relative.parts) or relative.as_posix() != name
                or name == MANIFEST):
            raise ValueError('A checksum path is absolute, escaping, or reserved.')
        if not isinstance(expected, str) or not SHA.fullmatch(expected):
            raise ValueError('An invalid SHA256 was declared for ' + name)
    if len({name.casefold() for name in value}) != len(value):
        raise ValueError('Case-ambiguous Windows checksum paths.')
    return value, raw


def _installed_entries(root):
    root = _resolved_path(root)
    io_root = _io_path(root)
    files, directories = {}, set()
    pending = [io_root]
    while pending:
        folder = pending.pop()
        for path in folder.iterdir():
            name = path.relative_to(io_root).as_posix()
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('A link or junction exists in the installation: ' + name)
            if not _resolved_path(path).is_relative_to(root):
                raise ValueError('An installed path escapes the application directory: ' + name)
            if path.is_dir():
                directories.add(name)
                pending.append(path)
            elif path.is_file():
                files[name] = path
            else:
                raise ValueError('An unexpected filesystem entry exists: ' + name)
    if len({name.casefold() for name in files}) != len(files):
        raise ValueError('Case-ambiguous installed Windows file paths.')
    return files, directories


def _shortcut_properties(path):
    if os.name != 'nt':
        raise OSError('Resolving Windows shortcut targets requires Windows.')
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = os.environ.copy()
    environment['PM3_INSTALLER_SHORTCUT'] = str(path)
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        "$OutputEncoding=[Console]::OutputEncoding;"
        "$shell=New-Object -ComObject WScript.Shell;"
        "$link=$shell.CreateShortcut($env:PM3_INSTALLER_SHORTCUT);"
        "[pscustomobject]@{TargetPath=$link.TargetPath;WorkingDirectory=$link.WorkingDirectory;"
        "Arguments=$link.Arguments}|ConvertTo-Json -Compress"
    )
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                            env=environment, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', check=True, timeout=20,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    value = json.loads(result.stdout.lstrip('\ufeff'))
    if not isinstance(value, dict):
        raise ValueError('The shortcut reader did not return an object.')
    return value


def _shortcut(path, application, *, expected_shortcut_arguments=''):
    if not isinstance(expected_shortcut_arguments, str):
        raise ValueError('Expected shortcut arguments must be a string.')
    path = _resolved_path(path)
    io_path = _io_path(path)
    if path.suffix.lower() != '.lnk' or not io_path.is_file() or io_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('A bounded existing .lnk shortcut is required.')
    with io_path.open('rb') as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:4] != struct.pack('<I', 76) or header[4:20] != SHELL_LINK_CLSID:
        raise ValueError('The shortcut is not a Shell Link file.')
    flags = struct.unpack_from('<I', header, 20)[0]
    if flags & RUN_AS_USER:
        raise ValueError('The shortcut requests Run as user/administrator.')
    value = _shortcut_properties(path)
    target, working, arguments = (value.get(field) for field in ('TargetPath', 'WorkingDirectory', 'Arguments'))
    if not all(isinstance(item, str) for item in (target, working, arguments)):
        raise ValueError('The shortcut target, working directory, and arguments must be strings.')
    if not target or _resolved_path(target) != application / 'ProxmarkStudio.exe':
        raise ValueError('The shortcut points to a different application.')
    if not working or _resolved_path(working) != application:
        raise ValueError('The shortcut working directory differs from the installation.')
    if arguments != expected_shortcut_arguments:
        raise ValueError('The shortcut supplies unexpected command-line arguments.')
    return {'path': str(path), 'target': str(_resolved_path(target)), 'working_directory': str(_resolved_path(working)),
            'arguments': arguments, 'run_as_user_or_admin': False}


def verify_installation(application_dir, payload_dir, *, desktop_shortcut=None, start_menu_shortcut=None,
                        expected_shortcut_arguments=''):
    if not isinstance(expected_shortcut_arguments, str):
        raise ValueError('Expected shortcut arguments must be a string.')
    application, payload = _resolved_path(application_dir), _resolved_path(payload_dir)
    report = {'passed': False, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'application_directory': str(application), 'payload_directory': str(payload),
              'read_only': True, 'hardware_access': False,
              'expected_shortcut_arguments': expected_shortcut_arguments,
              'checks': [], 'errors': [], 'warnings': []}

    def check(name, function, summarize=None):
        try:
            details = function()
            report['checks'].append({'name': name, 'passed': True,
                                     'details': summarize(details) if summarize else details})
            return details
        except (OSError, ValueError, KeyError, TypeError, struct.error, subprocess.SubprocessError) as error:
            report['checks'].append({'name': name, 'passed': False, 'error': str(error)})
            report['errors'].append(name + ': ' + str(error))
            return None

    validated = check('portable-payload', lambda: verify_release(_io_path(payload)),
                      lambda value: {key: value.get(key) for key in ('passed', 'errors', 'warnings')})
    if not validated or validated.get('passed') is not True:
        report['checks'][-1]['passed'] = False
        report['errors'].append('Portable payload validation failed; the installation was not accepted.')
        return report
    if application == payload or not _io_path(application).is_dir():
        report['errors'].append('A distinct existing installation directory is required.')
        return report
    manifest_data = check('portable-file-manifest', lambda: _manifest(_io_path(payload) / MANIFEST),
                          lambda value: {'files': len(value[0]), 'sha256': hashlib.sha256(value[1]).hexdigest()})
    installed = check('installed-directory-inventory', lambda: _installed_entries(application),
                      lambda value: {'files': len(value[0]), 'directories': len(value[1])})
    if manifest_data is None or installed is None:
        return report
    expected, raw_manifest = manifest_data
    files, directories = installed

    def coverage():
        declared = set(expected) | {MANIFEST}
        extras = set(files) - declared
        if extras - UNINSTALLER_FILES:
            raise ValueError('Foreign installed files: ' + ', '.join(sorted(extras - UNINSTALLER_FILES)[:20]))
        if REQUIRED_UNINSTALLER_FILES - extras:
            raise ValueError('Missing installer files: ' + ', '.join(sorted(REQUIRED_UNINSTALLER_FILES - extras)))
        missing = declared - set(files)
        if missing:
            raise ValueError('Missing payload files: ' + ', '.join(sorted(missing)[:20]))
        allowed_directories = {parent.as_posix() for name in declared for parent in PurePosixPath(name).parents
                               if parent != PurePosixPath('.')}
        if directories - allowed_directories:
            raise ValueError('Foreign installed directories: ' + ', '.join(sorted(directories - allowed_directories)[:20]))
        return {'payload_files': len(declared), 'installer_files': sorted(extras)}

    check('exact-installed-file-coverage', coverage)

    def compare():
        missing = (set(expected) | {MANIFEST}) - set(files)
        if missing:
            raise ValueError('Missing payload bytes: ' + ', '.join(sorted(missing)[:20]))
        changed = [name for name, expected_sha in expected.items()
                   if name in files and digest(files[name]) != expected_sha]
        if MANIFEST not in files or digest(files[MANIFEST]) != hashlib.sha256(raw_manifest).hexdigest():
            changed.append(MANIFEST)
        if changed:
            raise ValueError('Changed payload bytes: ' + ', '.join(sorted(changed)[:20]))
        return {'compared_files': len(expected) + 1, 'manifest_itself_compared': True}

    check('installed-bytes-match-portable-payload', compare)

    def architecture():
        names = ['ProxmarkStudio.exe']
        if 'client/proxmark3.exe' in expected:
            names.append('client/proxmark3.exe')
        result = {}
        for name in names:
            if name not in expected:
                raise ValueError('The executable is absent from the inherited checksum manifest: ' + name)
            _pe64(_io_path(application) / name)
            result[name] = 'windows-x64'
        return result

    check('installed-pe-architecture', architecture)
    for name, path in [('desktop-shortcut', desktop_shortcut), ('start-menu-shortcut', start_menu_shortcut)]:
        if path is not None:
            check(name, lambda path=path: _shortcut(path, application,
                  expected_shortcut_arguments=expected_shortcut_arguments))
    report['passed'] = not report['errors']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, required=True)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--desktop-shortcut', type=Path)
    parser.add_argument('--start-menu-shortcut', type=Path)
    parser.add_argument('--expected-shortcut-arguments', default='',
                        help='Exact shortcut arguments to accept (empty by default; never executed).')
    args = parser.parse_args()
    output = _resolved_path(args.output)
    if any(output.is_relative_to(_resolved_path(path)) for path in (args.application, args.payload)):
        parser.error('The report must be outside both the installation and portable payload.')
    if any(output == _resolved_path(path) for path in (args.desktop_shortcut, args.start_menu_shortcut) if path is not None):
        parser.error('The report cannot replace a shortcut.')
    report = verify_installation(args.application, args.payload, desktop_shortcut=args.desktop_shortcut,
                                 start_menu_shortcut=args.start_menu_shortcut,
                                 expected_shortcut_arguments=args.expected_shortcut_arguments)
    io_output = _io_path(output)
    io_output.parent.mkdir(parents=True, exist_ok=True)
    io_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'errors': report['errors']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
