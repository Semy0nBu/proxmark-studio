"""Build an isolated Windows portable release and a clean publication snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pm3gui.version import APP_NAME, APP_VERSION, WINDOWS_VERSION

PUBLIC_ROOT_FILES = ('README.md', 'README.en.md', 'README.ru.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
                     'START_HERE.txt', 'CHANGELOG.md', '.gitattributes',
                     'requirements.txt', 'requirements-build.txt', 'run_gui.py',
                     'start.ps1', 'launch-gui.vbs', 'auto-write.ps1', '.gitignore')
PUBLIC_DOCS = ('USER_GUIDE_RU.md', 'USER_GUIDE_EN.md', 'BUILDING.md', 'RELEASING.md',
               'PRIVACY_RU.md', 'PRIVACY_EN.md', 'CARD_WORKSPACE.md', 'STABILITY_AND_MOTION.md', 'LIBRARY_BACKUPS.md', 'CARD_TRANSFER.md')
PUBLIC_TOOLS = ('build_release.py', 'release_native.py', 'release_firmware.py',
                'release_gui_licenses.py', 'release_gui_sources.json', 'release_sources.py',
                'release_windows_manifest.py', 'verify_release.py',
                'audit_english_catalog.py', 'build_installer.py', 'verify_installer_install.py')
PUBLIC_INSTALLER = ('proxmark-studio.iss', 'toolchain.json')
PUBLIC_ASSETS = ('banner.svg', 'interface-en.png', 'interface-ru.png')
PUBLIC_TEST_EXCLUDES = {'test_connection_lab.py', 'test_live_functional_harness.py'}
FORBIDDEN_NAMES = {'library.sqlite3', 'settings.json', 'startup-error.log',
                   'device-flash-before.bin', 'device-flash-after.bin', 'client-log.txt'}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def build_environment():
    environment = os.environ.copy()
    windows = Path(environment.get('SYSTEMROOT') or environment.get('SystemRoot', r'C:\Windows'))
    environment['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), str(Path(sys.base_prefix)),
                                         str(windows/'System32'), str(windows)))
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH',
                'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_PLATFORM'):
        environment.pop(key, None)
    return environment


def freeze(output, work):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QPainter
    app = QGuiApplication.instance() or QGuiApplication([])
    work.mkdir(parents=True, exist_ok=True)
    pixmap = QPixmap(256, 256)
    from PySide6.QtCore import Qt
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(str(ROOT/'pm3gui/app-icon.svg')).render(painter)
    painter.end()
    icon = work/'app.ico'
    if not pixmap.save(str(icon), 'ICO'):
        raise RuntimeError('Could not create Windows icon')
    version = work/'windows-version.txt'
    version.write_text(
        'VSVersionInfo(ffi=FixedFileInfo(filevers='+repr(WINDOWS_VERSION)+',prodvers='+repr(WINDOWS_VERSION)+
        ',mask=0x3f,flags=0x2,OS=0x40004,fileType=0x1,subtype=0x0,date=(0,0)), '
        'kids=[StringFileInfo([StringTable("040904B0",['
        'StringStruct("FileDescription", "Proxmark Studio"),'
        'StringStruct("ProductName", "Proxmark Studio"),'
        'StringStruct("FileVersion", '+repr(APP_VERSION)+'),'
        'StringStruct("ProductVersion", '+repr(APP_VERSION)+'),'
        'StringStruct("OriginalFilename", "ProxmarkStudio.exe")])]),'
        'VarFileInfo([VarStruct("Translation",[1033,1200])])])', encoding='utf-8')
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--windowed', '--noupx', '--name', 'ProxmarkStudio', '--paths', str(ROOT),
               '--distpath', str(output), '--workpath', str(work/'pyinstaller'),
               '--specpath', str(work), '--icon', str(icon), '--version-file', str(version),
               '--hidden-import', 'PySide6.QtSvg']
    for path in sorted((ROOT/'pm3gui').rglob('*')):
        if path.is_file() and path.suffix in {'.json', '.svg'}:
            command += ['--add-data', str(path)+':'+str(path.parent.relative_to(ROOT))]
    command.append(str(ROOT/'run_gui.py'))
    log = work/'pyinstaller.log'
    with log.open('w', encoding='utf-8') as stream:
        subprocess.run(command, cwd=ROOT, env=build_environment(), stdout=stream, stderr=subprocess.STDOUT, check=True)
    if not (output/'ProxmarkStudio/ProxmarkStudio.exe').is_file():
        raise RuntimeError('PyInstaller did not produce the application')


def export_source(destination):
    """A deliberate allowlist, independent of git status and local work files."""
    if destination.exists():
        raise FileExistsError('Source destination must be new: '+str(destination))
    destination.mkdir(parents=True)
    for name in PUBLIC_INSTALLER:
        source = ROOT/'installer'/name
        if source.is_file():
            target = destination/'installer'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in PUBLIC_ROOT_FILES:
        source = ROOT/name
        if source.is_file():
            shutil.copy2(source, destination/name)
    for folder in ('pm3gui', 'tests'):
        for path in sorted((ROOT/folder).rglob('*')):
            if (path.is_file() and '__pycache__' not in path.parts and
                    path.name not in PUBLIC_TEST_EXCLUDES and path.suffix in {'.py', '.json', '.svg'}):
                target = destination/path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    for name in PUBLIC_DOCS:
        source = ROOT/'docs'/name
        if source.is_file():
            target = destination/'docs'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in PUBLIC_ASSETS:
        source = ROOT/'docs/assets'/name
        if source.is_file():
            target = destination/'docs/assets'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for source in (ROOT/'docs/licenses').rglob('*'):
        if source.is_file():
            target = destination/source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in PUBLIC_TOOLS:
        source = ROOT/'tools'/name
        if source.is_file():
            target = destination/'tools'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for folder in ('tools/native-client-tests', '.github'):
        if (ROOT/folder).is_dir():
            for source in sorted((ROOT/folder).rglob('*')):
                if source.is_file() and source.suffix in {'.py', '.c', '.md', '.yml'}:
                    target = destination/source.relative_to(ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    for source in (ROOT/'tools/client-patches').glob('*.patch'):
        target = destination/source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    write_json(destination/'SOURCE_SNAPSHOT.json', {'version': APP_VERSION,
               'excluded_lab_tests': sorted(PUBLIC_TEST_EXCLUDES),
               'reason': 'Operator hardware harness and deferred iPhone prototype are outside the desktop product.'})
    return destination


def zip_tree(source, target):
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent).as_posix())


def reuse_native_bundle(source, destination):
    """Reuse reviewed public backend files for a GUI update, never user data."""
    from tools.verify_release import verify_release
    source = source.resolve()
    result = verify_release(source)
    if not result['passed'] or json.loads((source/'release.json').read_text())['bundle_kind'] != 'complete':
        raise ValueError('The base must be a complete verified release: '+json.dumps(result['errors']))
    for name in ('client', 'firmware', 'licenses/proxmark3', 'licenses/native-runtime', 'sources/native-runtime'):
        shutil.copytree(source/name, destination/name)
    for name in ('native-manifest.json', 'sources/proxmark3-corresponding-source.zip'):
        (destination/name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source/name, destination/name)


def stage_public_documents(app):
    for name in ('README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'START_HERE.txt'):
        shutil.copy2(ROOT/name, app/name)
    # Older release source trees did not have a separate Russian README.
    if (ROOT/'README.ru.md').is_file():
        shutil.copy2(ROOT/'README.ru.md', app/'README.ru.md')
    (app/'docs').mkdir(exist_ok=True)
    for name in ('USER_GUIDE_RU.md', 'USER_GUIDE_EN.md', 'PRIVACY_RU.md', 'PRIVACY_EN.md', 'CARD_WORKSPACE.md', 'STABILITY_AND_MOTION.md', 'LIBRARY_BACKUPS.md', 'CARD_TRANSFER.md'):
        shutil.copy2(ROOT/'docs'/name, app/'docs'/name)
    for name in PUBLIC_ASSETS:
        source = ROOT/'docs/assets'/name
        if source.is_file():
            target = app/'docs/assets'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    notice = ROOT/'docs/licenses/inno-setup/LICENSE.txt'
    if notice.is_file():
        target = app/'licenses/installer'
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(notice, target/'INNO_SETUP_LICENSE.txt')


def stage(args, app):
    if not (app/'ProxmarkStudio.exe').is_file():
        raise FileNotFoundError('Freeze the application first')
    native = None
    if not args.gui_only:
        if args.base_bundle is not None:
            reuse_native_bundle(args.base_bundle, app)
            native = True
        else:
            stage_native_inputs(args, app)
            native = True
    from tools.release_gui_licenses import stage_gui_licenses
    stage_gui_licenses(sys.executable, app, ROOT/'outputs/release-sources/gui', download=True)
    stage_public_documents(app)
    write_json(app/'release.json', {'application': APP_NAME, 'version': APP_VERSION,
               'platform': 'windows-x64', 'bundle_kind': 'gui-only' if args.gui_only else 'complete',
               'built_utc': datetime.now(timezone.utc).isoformat(), 'signed': False,
               'native_client_sha256': digest(app/'client/proxmark3.exe') if native else None,
               'native_client_original_sha256': json.loads((app/'native-manifest.json').read_text(encoding='utf-8')).get(
                   'original_client_sha256') if native else None})


def stage_native_inputs(args, app):
    if args.native_source is None:
        raise ValueError('--native-source or --base-bundle is required for a complete release')
    from tools.release_native import stage_native, build_native_source_archive, apply_native_utf8_manifest
    from tools.release_firmware import stage_firmware
    from tools.release_sources import fetch_sources
    stage_native(args.native_source.resolve(), app, python_prefix=args.native_python_prefix)
    stage_firmware(ROOT, app, app/'client/proxmark3.exe')
    source_dir = app/'sources'
    source_dir.mkdir(exist_ok=True)
    build_native_source_archive(args.native_source.resolve(), source_dir/'proxmark3-corresponding-source.zip')
    apply_native_utf8_manifest(app)
    cache = ROOT/'outputs/release-sources/native-runtime'
    index = fetch_sources(app/'native-manifest.json', cache)
    if not index['passed']:
        raise RuntimeError('Native runtime corresponding sources are incomplete')
    target = source_dir/'native-runtime'
    target.mkdir()
    for record in index['sources']:
        shutil.copy2(cache/record['filename'], target/record['filename'])
    shutil.copy2(cache/'NATIVE_SOURCE_INDEX.json', target/'NATIVE_SOURCE_INDEX.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, default=ROOT/'dist')
    parser.add_argument('--work-root', type=Path, default=ROOT/'build/release')
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--native-source', type=Path)
    inputs.add_argument('--base-bundle', type=Path)
    parser.add_argument('--native-python-prefix', type=Path)
    parser.add_argument('--gui-only', action='store_true')
    parser.add_argument('--phase', choices=('all', 'freeze', 'stage', 'archive'), default='all')
    parser.add_argument('--offscreen-checks', action='store_true', help='Run frozen GUI diagnostics without showing desktop windows')
    args = parser.parse_args()
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = output/'ProxmarkStudio'
    if args.phase in ('all', 'freeze'):
        if app.exists():
            parser.error('Use a new --output-root; existing bundles are never deleted.')
        freeze(output, args.work_root.resolve())
    if args.phase in ('all', 'stage'):
        stage(args, app)
    if args.phase in ('all', 'archive'):
        stage_public_documents(app)
        publication = output/'source/proxmark-studio'
        if publication.exists():
            marker = publication/'SOURCE_SNAPSHOT.json'
            if not marker.is_file() or not publication.resolve().is_relative_to(output/'source'):
                raise RuntimeError('Existing source directory is not a generated snapshot')
            history = output/'source-history'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            history.parent.mkdir(exist_ok=True)
            publication.rename(history)
        export_source(publication)
        sources = app/'sources'
        sources.mkdir(exist_ok=True)
        zip_tree(publication, sources/'proxmark-studio-source.zip')
        from tools.verify_release import verify_release
        previous_manifest = app/'files.sha256.json'
        if previous_manifest.exists():
            previous_manifest.replace(output/'previous-files.sha256.json')
        validation = verify_release(app)
        write_json(output/'release-verification.json', validation)
        if not validation['passed']:
            raise RuntimeError('Release validation failed: '+json.dumps(validation['errors']))
        before = {p.relative_to(app).as_posix(): digest(p) for p in app.rglob('*') if p.is_file()}
        for language in ('en', 'ru'):
            report = output/('frozen-check-'+language+'.json')
            check_environment = build_environment()
            if args.offscreen_checks:check_environment['QT_QPA_PLATFORM'] = 'offscreen'
            subprocess.run([str(app/'ProxmarkStudio.exe'), '--self-test', str(report), '--language', language],
                           cwd=output, env=check_environment, timeout=90, check=True)
            result = json.loads(report.read_text(encoding='utf-8'))
            if not result.get('passed') or result.get('hardware_commands') != 0:
                raise RuntimeError('Offline frozen application checks failed')
        after = {p.relative_to(app).as_posix(): digest(p) for p in app.rglob('*') if p.is_file()}
        if before != after:
            raise RuntimeError('Offline launch changed the application directory')
        files = sorted(path for path in app.rglob('*') if path.is_file())
        for path in files:
            if path.name.lower() in FORBIDDEN_NAMES:
                raise RuntimeError('A private user file reached the bundle: '+str(path))
        write_json(app/'files.sha256.json', {path.relative_to(app).as_posix(): digest(path) for path in files})
        validation = verify_release(app)
        write_json(output/'release-verification.json', validation)
        if not validation['passed']:
            raise RuntimeError('Final file manifest validation failed: '+json.dumps(validation['errors']))
        suffix = '-gui-only' if args.gui_only else ''
        target = output/f'ProxmarkStudio-{APP_VERSION}-windows-x64{suffix}.zip'
        zip_tree(app, target)
        (output/'SHA256SUMS.txt').write_text(digest(target)+'  '+target.name+'\n', encoding='ascii')
        print(str(target), flush=True)


if __name__ == '__main__':
    main()
