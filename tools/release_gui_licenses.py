"""Collect exact GUI runtime source archives and their license notices.

Archive hashes are pinned in release_gui_sources.json from the upstream Qt,
Python and PyPI release metadata. Nothing is downloaded or installed when the
GUI runs; this is a release-build tool, and its default mode is offline.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import posixpath
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import urllib.request


LOCK = Path(__file__).with_name('release_gui_sources.json')
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_NOTICE_BYTES = 8 * 1024 * 1024
VERSION_PROBE = """
import importlib.metadata as m,json,sys
import PySide6
from PySide6.QtCore import qVersion
packages={}
for name in ('PySide6-Essentials','shiboken6','pyserial','PyInstaller','pycryptodomex'):
    d=m.distribution(name)
    packages[name]={'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License','')}
print(json.dumps({'python':'.'.join(map(str,sys.version_info[:3])),'pyside':PySide6.__version__,'qt':qVersion(),'packages':packages}))
"""


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def probe_runtime(python_executable: Path) -> dict:
    result = subprocess.run([str(python_executable), '-c', VERSION_PROBE], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=30, check=True)
    return json.loads(result.stdout)


def _verify_versions(runtime: dict, lock: dict) -> None:
    expected = {key: lock[key] for key in ('python', 'pyside', 'qt')}
    for key, version in expected.items():
        if runtime.get(key) != version:
            raise ValueError(f'{key} version differs from the reviewed source lock.')
    for package, key in [('PySide6-Essentials', 'pyside'), ('shiboken6', 'pyside'),
                         ('PyInstaller', 'pyinstaller'), ('pyserial', 'pyserial'), ('pycryptodomex', 'pycryptodomex')]:
        if runtime.get('packages', {}).get(package, {}).get('version') != lock[key]:
            raise ValueError(f'{package} version differs from the reviewed source lock.')


def _cached_source(component: dict, cache: Path, download: bool) -> Path:
    name = component['file']
    if Path(name).name != name or not re.fullmatch(r'[0-9a-f]{64}', component['sha256']):
        raise ValueError('Invalid source lock entry.')
    path = cache / name
    if path.is_file():
        if digest(path) != component['sha256']:
            raise ValueError(f'Cached source checksum mismatch: {name}')
        return path
    if not download:
        raise ValueError(f'Missing reviewed source archive: {name}; rerun with --download.')
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cache / (name + '.download')
    errors = []
    # A listed mirror may be used when Qt's primary TLS endpoint is unavailable;
    # the authoritative release hash is checked before the cache file is kept.
    for url in dict.fromkeys(filter(None, (component.get('mirror'), component['url']))):
        try:
            with urllib.request.urlopen(url, timeout=45) as response, temporary.open('wb') as output:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_ARCHIVE_BYTES:
                        raise ValueError('Source archive exceeds the release size limit.')
                    output.write(chunk)
            if digest(temporary) != component['sha256']:
                raise ValueError(f'Downloaded source checksum mismatch: {name}')
            if 'bytes' in component and temporary.stat().st_size != component['bytes']:
                raise ValueError(f'Downloaded source size mismatch: {name}')
            temporary.replace(path)
            return path
        except (OSError, ValueError) as error:
            errors.append(str(error))
    raise ValueError(f'Unable to fetch reviewed source {name}: ' + '; '.join(errors))


def _safe_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe source archive path.')
    return path


def _is_notice(path: PurePosixPath) -> bool:
    name = path.name.upper()
    return (any(part.upper() == 'LICENSES' for part in path.parts) or
            any(token in name for token in ('LICENSE', 'LICENCE', 'COPYING', 'COPYRIGHT', 'NOTICE')) or
            name in ('QT_ATTRIBUTION.JSON', 'QT_ATTRIBUTIONS.JSON'))


def extract_notices(archive: Path, destination: Path) -> list[dict]:
    """Read a tar sequentially twice; never extract code or tar links."""
    selected, existing, declared = set(), set(), set()
    with tarfile.open(archive, mode='r|*') as source:
        for member in source:
            path = _safe_member_name(member.name)
            if not member.isfile():
                continue
            name = path.as_posix()
            existing.add(name)
            if _is_notice(path):
                selected.add(name)
            if path.name.lower() not in ('qt_attribution.json', 'qt_attributions.json'):
                continue
            data = source.extractfile(member).read(MAX_NOTICE_BYTES + 1)
            if len(data) > MAX_NOTICE_BYTES:
                raise ValueError('Oversized Qt attribution record.')
            try:
                value = json.loads(data, strict=False)
            except ValueError as error:
                raise ValueError(f'Invalid Qt attribution record: {name}: {error}') from error
            records = value if isinstance(value, list) else [value]
            for record in records:
                if not isinstance(record, dict):
                    continue
                filenames = record.get('LicenseFiles') or record.get('LicenseFile') or []
                if isinstance(filenames, str):
                    filenames = [filenames]
                for filename in filenames:
                    if PurePosixPath(filename).is_absolute() or '\\' in filename or ':' in filename:
                        raise ValueError('Unsafe Qt license-file reference.')
                    normalized = posixpath.normpath((path.parent / filename).as_posix())
                    declared.add(_safe_member_name(normalized).as_posix())
    selected.update(declared & existing)
    result = []
    with tarfile.open(archive, mode='r|*') as source:
        for member in source:
            if member.name not in selected or not member.isfile():
                continue
            if member.size > MAX_NOTICE_BYTES:
                raise ValueError('Oversized source license notice.')
            # Drop only the archive root, preserving independent notice paths.
            relative = PurePosixPath(member.name)
            relative = PurePosixPath(*relative.parts[1:]) if len(relative.parts) > 1 else relative
            target = destination.joinpath(*relative.parts)
            data = source.extractfile(member).read()
            if b'\0' in data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            result.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(data).hexdigest(),
                           'bytes': len(data)})
    if not result:
        raise ValueError(f'No source license notices found in {archive.name}.')
    return result


def _qt_modules(destination: Path) -> set[str]:
    modules = set()
    for path in destination.rglob('Qt6*.dll'):
        if 'client' not in path.relative_to(destination).parts:
            modules.add(path.stem[3:])
    return modules


def _verify_qt_coverage(modules: set[str], components: set[str]) -> None:
    base = {'Core', 'Gui', 'Widgets', 'Network', 'OpenGL', 'OpenGLWidgets', 'Xml',
            'Sql', 'PrintSupport', 'Concurrent', 'Test', 'DBus'}
    svg = {'Svg', 'SvgWidgets'}
    tools = {'Designer', 'DesignerComponents', 'UiTools', 'Help', 'UiPlugin'}
    for module in modules:
        needed = 'qtbase' if module in base else 'qtsvg' if module in svg else 'qttools' if module in tools else None
        if needed is None or needed not in components:
            raise ValueError(f'No reviewed corresponding source coverage for Qt6{module}.dll.')


def stage_gui_licenses(python_executable: Path, destination_root: Path, source_cache: Path,
                       *, download: bool = False, include_source_archives: bool = True) -> dict:
    destination = Path(destination_root).resolve()
    cache = Path(source_cache).resolve()
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    runtime = probe_runtime(Path(python_executable))
    _verify_versions(runtime, lock)
    components = lock['components']
    modules = _qt_modules(destination)
    _verify_qt_coverage(modules, {item['id'] for item in components})
    with ThreadPoolExecutor(max_workers=5) as executor:
        archives = list(executor.map(lambda item: _cached_source(item, cache, download), components))
    licenses = destination / 'licenses/gui'
    sources = destination / 'sources/gui'
    for target in (licenses, sources) if include_source_archives else (licenses,):
        if target.exists() and any(target.iterdir()):
            raise ValueError('Use empty GUI license/source staging directories.')
    licenses.mkdir(parents=True, exist_ok=True)
    if include_source_archives:
        sources.mkdir(parents=True, exist_ok=True)
    inventory = {'kind': 'proxmark-studio-gui-source-inventory-v1', 'runtime': runtime,
                 'qt_dll_modules': sorted(modules), 'components': [],
                 'source_archives_included': include_source_archives,
                 'license_documents_from': 'Unmodified matching upstream source archives',
                 'upstream_licensing': 'https://doc.qt.io/qt-6/licensing.html'}
    for component, archive in zip(components, archives):
        notices = extract_notices(archive, licenses / component['id'])
        if include_source_archives:
            shutil.copy2(archive, sources / archive.name)
        inventory['components'].append({**component,
            'source_path': 'sources/gui/' + archive.name if include_source_archives else None,
            'license_directory': 'licenses/gui/' + component['id'], 'notice_files': notices})
    # The open-source alternatives are intentionally supplied rather than
    # treating the commercial-only license file in a wheel as our license grant.
    for name in ('GPL-3.0-only.txt', 'LGPL-3.0-only.txt'):
        origin = licenses / 'pyside-setup/LICENSES' / name
        if not origin.is_file():
            raise ValueError('Matching PySide sources lack the required open-source license texts.')
        shutil.copy2(origin, licenses / name)
    (licenses / 'SOURCE_INVENTORY.json').write_text(json.dumps(inventory, indent=2) + '\n', encoding='utf-8')
    notice = '''GUI runtime sources and notices
===============================

Proxmark Studio is distributed under GPL-3.0-or-later. The included Qt 6,
PySide6 and Shiboken runtime uses upstream open-source licensing options;
GPL v3 and LGPL v3 texts are supplied here. No Qt commercial license grant
is asserted. Qt's source archives also document independent third-party terms.

The exact upstream source archives and published checksums are listed in
SOURCE_INVENTORY.json. If included, archives are at ../../sources/gui relative
to this directory. Archives are unmodified and contain their build instructions.
The onedir layout keeps shared libraries replaceable; no restriction is added
on modifying them or reverse engineering to debug modifications under LGPL.

Python is provided under its PSF license and documented third-party notices.
pySerial is BSD licensed. PyInstaller supplies its bootloader under its GPL
license with a distribution exception; its exact COPYING text is included.

The component notice folders conservatively include notices from the matching
source trees. Some refer to tools, tests, examples or platforms not used by this
Windows release; their inclusion does not claim those components are shipped.

Qt licensing: https://doc.qt.io/qt-6/licensing.html
Qt third-party notices: https://doc.qt.io/qt-6/licenses-used-in-qt.html
'''
    (licenses / 'README.txt').write_text(notice, encoding='utf-8')
    return {'passed': True, 'components': len(components), 'qt_dll_modules': sorted(modules),
            'archives_included': include_source_archives,
            'archive_bytes': sum(path.stat().st_size for path in archives),
            'license_files': sum(len(item['notice_files']) for item in inventory['components']),
            'inventory': 'licenses/gui/SOURCE_INVENTORY.json'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--source-cache', type=Path, required=True)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--omit-source-copies', action='store_true')
    args = parser.parse_args()
    try:
        result = stage_gui_licenses(args.python, args.destination, args.source_cache,
                                    download=args.download, include_source_archives=not args.omit_source_copies)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as error:
        parser.exit(1, f'GUI source/license staging failed: {error}\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
