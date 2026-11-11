"""Allowlisted native payload and exact local Proxmark source for a release.

Only paths present in the native HEAD tree and explicit generated build inputs
are eligible. Captures, user dictionaries and build folders are never globbed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import zipfile


RESOURCE_DIRS = ('cmdscripts', 'lualibs', 'luascripts', 'pyscripts', 'resources', 'dictionaries')
GENERATED_INPUTS = (
    'Makefile.platform', 'client/lualibs/pm3_cmd.lua', 'client/lualibs/mfc_default_keys.lua',
    'client/src/comms_bulk_download.h', 'client/src/comms_handshake.h',
    'client/src/uart/frame_reader.h', 'client/src/uart/tx_lifecycle.h',
    'client/src/uart/uart_win32_io.h', 'client/src/uart/uart_win32_wait.h',
    'client/src/uart/uart_win32_bluetooth.h',
    'client/src/pm3_luawrap.c', 'client/src/pm3_pywrap.c',
    'client/src/version_pm3.c', 'armsrc/version_pm3.c',
)
_PRIVATE_PARTS = {'.git', '.proxmark3', 'data', 'outputs', '__pycache__', '.pytest_cache'}
_PYTHON_EXCLUDED = {'__pycache__', 'site-packages', 'test', 'tests', 'idlelib',
                    'tkinter', 'turtledemo', 'ensurepip', 'config-3.14'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _relative(value):
    path = PurePosixPath(str(value).replace('\\', '/'))
    if path.is_absolute() or not path.parts or any(part in ('..', '.') for part in path.parts):
        raise ValueError('Unsafe release path: '+str(value))
    if ':' in str(path) or any(part in _PRIVATE_PARTS for part in path.parts):
        raise ValueError('Private or absolute release path: '+str(value))
    return path


def _git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def _tracked(root):
    # HEAD excludes an accidentally staged local dictionary or capture.
    return [name for name in _git(root, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').decode('utf-8').split('\0') if name]


def _copy(source, destination):
    if source.is_symlink() or not source.is_file():
        raise ValueError('Missing file or symlink in native payload: '+str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {'relative_target': destination.name, 'sha256': digest(destination), 'size': destination.stat().st_size}


def _dll_imports(path, objdump):
    environment = os.environ.copy()
    environment['PATH'] = str(objdump.parent)+os.pathsep+environment.get('PATH', '')
    result = subprocess.run([str(objdump), '-p', str(path)], capture_output=True, text=True,
                            env=environment, timeout=30)
    if result.returncode:
        raise ValueError('Cannot inspect native DLL imports: '+path.name)
    return re.findall(r'DLL Name:\s*(\S+)', result.stdout, re.I)


def _system_dll(name):
    if name.lower().startswith(('api-ms-win-', 'ext-ms-win-')):
        return True
    return (Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/name).is_file()


def _python_files(prefix, version):
    library = prefix/'lib'/('python'+version)
    if not (library/'encodings/__init__.py').is_file():
        raise ValueError('The matching MinGW Python standard library is missing.')
    for source in sorted(library.rglob('*')):
        relative = source.relative_to(prefix)
        if not source.is_file() or any(part in _PYTHON_EXCLUDED for part in relative.parts):
            continue
        if source.suffix.lower() in {'.pyc', '.pyo', '.a'} or source.name.startswith(('_test', 'xx', '_tkinter')):
            continue
        yield source, relative
    executable = prefix/'bin/python.exe'
    if not executable.is_file():
        raise ValueError('The private MinGW Python executable is missing.')
    yield executable, Path('bin/python.exe')


def _package_notices(msys_root, source_paths, destination):
    """Copy the actual installed package notices, recording exact package versions."""
    wanted = {str(path.relative_to(msys_root)).replace('\\', '/') for path in source_paths
              if path.is_relative_to(msys_root)}
    packages = []
    database = msys_root/'var/lib/pacman/local'
    if not database.is_dir():
        raise ValueError('Native runtime package/license inventory is unavailable.')
    for directory in sorted(database.iterdir()):
        if not (directory/'files').is_file() or not (directory/'desc').is_file():
            continue
        files = (directory/'files').read_text(encoding='utf-8').splitlines()
        included = sorted(set(files) & wanted)
        if not included:
            continue
        description = {}
        for chunk in (directory/'desc').read_text(encoding='utf-8').split('\n\n'):
            lines = chunk.strip().splitlines()
            if lines and lines[0].startswith('%'):
                description[lines[0].strip('%')] = lines[1:]
        name = description['NAME'][0]
        notices = []
        for filename in files:
            is_notice = '/share/licenses/' in filename or ('/share/' in filename and
                re.fullmatch(r'(?:LICENSE|COPYING|COPYRIGHT|NOTICE)(?:\..*)?', Path(filename).name, re.I))
            if not is_notice or not (msys_root/filename).is_file():
                continue
            relative = _relative(filename.split('/share/', 1)[1])
            target = destination/name/str(relative)
            _copy(msys_root/filename, target)
            notices.append(str(target.relative_to(destination)).replace('\\', '/'))
        component = (description.get('BASE') or [''])[0].removeprefix('mingw-w64-')
        extras = Path(__file__).resolve().parents[1]/'docs/licenses/native-runtime-source'/component
        if not notices and extras.is_dir():
            for source in sorted(extras.iterdir()):
                if source.is_file():
                    target = destination/name/source.name
                    _copy(source, target)
                    notices.append(str(target.relative_to(destination)).replace('\\', '/'))
        if not notices:
            raise ValueError('A native runtime component has no bundled notice: '+name)
        packages.append({'name': name, 'version': description['VERSION'][0],
                         'licenses': description.get('LICENSE', []), 'upstream': description.get('URL', []),
                         'package_base': description.get('BASE', []),
                         'files': included, 'notice_files': notices})
    if not packages:
        raise ValueError('No installed runtime packages matched the supplied files.')
    return packages


def stage_native(source_root, destination_root, *, python_prefix=None, objdump=None):
    """Stage into destination_root/client; never mutate the original native tree."""
    source_root, destination_root = Path(source_root).resolve(), Path(destination_root).resolve()
    client = destination_root/'client'
    if destination_root.is_relative_to(source_root) or (client.exists() and any(client.iterdir())):
        raise ValueError('Native staging requires a separate empty client directory.')
    original = json.loads((source_root/'client/libs/.pm3gui-runtime-manifest.json').read_text(encoding='utf-8'))
    if original.get('kind') != 'proxmark3-gui-local-ucrt64-runtime-v1':
        raise ValueError('Unsupported native runtime manifest.')
    objdump = Path(objdump or original.get('objdump', '')).resolve()
    if not objdump.is_file():
        raise ValueError('The build machine needs objdump to inspect private Python extensions.')
    python_entry = next((entry for entry in original['files']
                         if re.fullmatch(r'libpython(\d+\.\d+)\.dll', entry['relative_target'])), None)
    if python_entry is None:
        raise ValueError('The selected native client has no known embedded Python runtime.')
    version = re.fullmatch(r'libpython(\d+\.\d+)\.dll', python_entry['relative_target'])[1]
    prefix = Path(python_prefix or Path(python_entry['source']).parent.parent).resolve()
    private_python = list(_python_files(prefix, version))
    records, dependency_sources = {}, []
    def copy(source, target):
        relative = str(target.relative_to(destination_root)).replace('\\', '/')
        _relative(relative)
        record = _copy(source, target)
        record['relative_target'] = relative
        records[relative] = record
    copy(source_root/'client/proxmark3.exe', client/'proxmark3.exe')
    library_records = {}
    for item in original['files']:
        relative = _relative(item['relative_target'])
        source = source_root/'client/libs'/str(relative)
        if digest(source) != item['sha256']:
            raise ValueError('Native runtime hash mismatch: '+str(relative))
        copy(source, client/'libs'/str(relative))
        library_records[str(relative)] = {'relative_target': str(relative),
            'sha256': item['sha256'], 'size': source.stat().st_size}
        dependency_sources.append(Path(item['source']).resolve())
    offscreen = source_root/'client/libs/platforms/qoffscreen.dll'
    copy(offscreen, client/'libs/platforms/qoffscreen.dll')
    library_records['platforms/qoffscreen.dll'] = {'relative_target': 'platforms/qoffscreen.dll',
        'sha256': digest(offscreen), 'size': offscreen.stat().st_size}
    dependency_sources.append(prefix/'share/qt6/plugins/platforms/qoffscreen.dll')
    tracked = _tracked(source_root)
    changed_resources = subprocess.run(['git', '-C', str(source_root), 'diff', '--quiet', 'HEAD', '--',
        *('client/'+directory for directory in RESOURCE_DIRS)], timeout=30)
    if changed_resources.returncode:
        raise ValueError('Native public resources were modified; review them before release.')
    resource_names = {name for name in tracked if any(name.startswith('client/'+directory+'/') for directory in RESOURCE_DIRS)}
    resource_names.update(('client/lualibs/pm3_cmd.lua', 'client/lualibs/mfc_default_keys.lua'))
    for name in sorted(resource_names):
        _relative(name)
        copy(source_root/name, destination_root/name)
    for source, relative in private_python:
        copy(source, client/'python'/relative)
    bootstrap = client/'python/lib'/('python'+version)/'sitecustomize.py'
    bootstrap.write_text('"""DLL search path for the private Proxmark Python runtime."""\n'
        'import os\nimport sys\n'
        'sys.dont_write_bytecode = True\n'
        'if os.name == "nt":\n'
        '    _pm3_dll_path = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "bin"))\n'
        '    _pm3_dll_directory = os.add_dll_directory(_pm3_dll_path)\n', encoding='utf-8')
    relative_bootstrap = str(bootstrap.relative_to(destination_root)).replace('\\', '/')
    records[relative_bootstrap] = {'relative_target': relative_bootstrap, 'sha256': digest(bootstrap),
                                    'size': bootstrap.stat().st_size}
    # MinGW searches this ZIP before its filesystem standard library, including
    # early encodings imports that run before sitecustomize can disable caches.
    standard_zip = client/'python/lib'/('python'+version.replace('.', '')+'.zip')
    standard_library = client/'python/lib'/('python'+version)
    with zipfile.ZipFile(standard_zip, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source in sorted(standard_library.rglob('*.py')):
            archive.write(source, source.relative_to(standard_library).as_posix())
    relative_zip = standard_zip.relative_to(destination_root).as_posix()
    records[relative_zip] = {'relative_target': relative_zip, 'sha256': digest(standard_zip),
                             'size': standard_zip.stat().st_size}
    python_dll = prefix/'bin'/python_entry['relative_target']
    copy(python_dll, client/'python/bin'/python_dll.name)
    queue = [source for source, _ in private_python if source.suffix.lower() in {'.pyd', '.dll', '.exe'}]
    queue += [python_dll, offscreen]
    seen = set()
    while queue:
        binary = queue.pop()
        if binary in seen:
            continue
        seen.add(binary)
        for name in _dll_imports(binary, objdump):
            source = prefix/'bin'/name
            if source.is_file():
                target = client/'python/bin'/name
                copy(source, target)
                dependency_sources.append(source)
                queue.append(source)
            elif not _system_dll(name):
                raise ValueError('Unresolved private Python dependency: '+name)
    msys_root = prefix.parent
    packages = _package_notices(msys_root, dependency_sources, destination_root/'licenses/native-runtime')
    for name in tracked:
        if re.fullmatch(r'(?:.*/)?(?:LICENSE[^/]*|COPYING[^/]*|NOTICE[^/]*|AUTHORS[^/]*|COPYRIGHT[^/]*)', name, re.I):
            _relative(name)
            copy(source_root/name, destination_root/'licenses/proxmark3'/name)
    runtime = {'kind': original['kind'], 'files': list(library_records.values()),
               'client': '../proxmark3.exe', 'client_sha256': digest(client/'proxmark3.exe')}
    (client/'libs/.pm3gui-runtime-manifest.json').write_text(json.dumps(runtime, indent=2)+'\n', encoding='utf-8')
    report = {'kind': 'proxmark-studio-native-payload-v1', 'native_commit': _git(source_root, 'rev-parse', 'HEAD').decode().strip(),
              'client_sha256': runtime['client_sha256'], 'runtime_files': len(library_records),
              'python_version': version, 'python_home': 'client/python',
              'optional_script_packages_not_bundled': ['amiibo', 'bitarray', 'numpy', 'pyaudio'],
              'resource_directories': list(RESOURCE_DIRS), 'files': list(records.values()),
              'packages': packages, 'hardware_access': False}
    destination = destination_root/'native-manifest.json'
    destination.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


def build_native_source_archive(source_root, archive_path):
    """Archive actual build sources at trusted HEAD paths plus known new inputs."""
    source_root, archive_path = Path(source_root).resolve(), Path(archive_path).resolve()
    if archive_path.exists():
        raise ValueError('The source archive already exists.')
    files = set(_tracked(source_root))
    files.update(name for name in GENERATED_INPUTS if (source_root/name).is_file())
    records = []
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(files):
            relative = _relative(name)
            source = source_root/str(relative)
            if source.is_symlink() or not source.is_file():
                raise ValueError('Missing source or symlink: '+name)
            archive.write(source, 'proxmark3/'+str(relative))
            records.append({'path': str(relative), 'sha256': digest(source), 'size': source.stat().st_size})
        metadata = {'source_commit': _git(source_root, 'rev-parse', 'HEAD').decode().strip(),
                    'generated_utc': datetime.now(timezone.utc).isoformat(), 'files': records}
        archive.writestr('proxmark3/PROXMARK_STUDIO_SOURCE_MANIFEST.json', json.dumps(metadata, indent=2)+'\n')
        archive.writestr('proxmark3/PROXMARK_STUDIO_BUILD.md',
            '# Proxmark Studio native build\n\n'
            'This is the exact working source used for the accompanying patched client and RDV4 images. '
            'It includes upstream licensed resources and the local changes; no user captures or libraries are included.\n\n'
            'Base: RRG/Iceman v4.23346. Build with the documented ProxSpace UCRT64 environment. '
            'Use `make -j6 client` and `make -j6 fullimage PLATFORM=PM3RDV4 PLATFORM_EXTRAS=BTADDON`. '
            'For the USB-only image use `PLATFORM_EXTRAS=`. The recorded compiler was GCC 16.2.0. '
            'The source manifest binds the files; build timestamps/toolchains can change binary hashes.\n\n'
            'Upstream license: LICENSE.txt; authors: AUTHORS.md. Other bundled components retain their own licenses.\n')
    return {'path': archive_path.name, 'sha256': digest(archive_path), 'bytes': archive_path.stat().st_size,
            'source_files': len(records), 'source_commit': metadata['source_commit']}


def apply_native_utf8_manifest(application):
    from tools.release_windows_manifest import apply_native_utf8_manifest as transform
    return transform(application)
