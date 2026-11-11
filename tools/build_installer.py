"""Build a single offline Windows installer from a verified complete release."""
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
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pm3gui.version import APP_VERSION, WINDOWS_VERSION
from tools.build_release import build_environment
from tools.verify_release import verify_release, _pe64


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_payload(application):
    application = Path(application).resolve()
    result = verify_release(application)
    if not result['passed']:
        raise ValueError('The installer requires a verified release: '+json.dumps(result['errors']))
    metadata = json.loads((application/'release.json').read_text(encoding='utf-8'))
    if metadata.get('bundle_kind') != 'complete':
        raise ValueError('The end-user installer requires the complete client and firmware bundle.')
    if metadata.get('version') != APP_VERSION:
        raise ValueError('Installer source and application versions differ.')
    return application


def compiler_lock(compiler):
    lock = json.loads((ROOT/'installer/toolchain.json').read_text(encoding='utf-8'))
    compiler = Path(compiler).resolve()
    if not compiler.is_file() or digest(compiler) != lock['compiler_sha256']:
        raise ValueError('Use the exact reviewed Inno Setup compiler in installer/toolchain.json.')
    probe = subprocess.run([str(compiler), '--version'], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=15,
                           env=build_environment())
    if lock['version'] not in probe.stdout+probe.stderr:
        raise ValueError('Inno Setup version does not match the toolchain lock.')
    return lock


def make_icon(destination):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    application = QGuiApplication.instance() or QGuiApplication([])
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(str(ROOT/'pm3gui/app-icon.svg')).render(painter)
    painter.end()
    if not pixmap.save(str(destination), 'ICO'):
        raise RuntimeError('Could not create the installer icon.')
    return application


def _publish_without_replace(source, target):
    # Windows rename refuses an existing target; POSIX rename would replace it.
    if os.name == 'nt':
        os.rename(source, target)
    else:
        os.link(source, target)
        Path(source).unlink()


def publish_installer(produced, target, *, replace=False, stamp=None):
    """Publish a verified same-volume copy, retaining the compiled original."""
    produced, target = Path(produced).resolve(), Path(target).absolute()
    if produced == target:
        raise ValueError('The compiled installer and published target must differ.')
    if os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
        raise ValueError('Existing installer target must be a regular file.')
    if os.path.lexists(target) and not replace:
        raise FileExistsError('Installer already exists; use --replace to retain it in installer-history.')
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_hash, expected_size = digest(produced), produced.stat().st_size
    descriptor, temporary_name = tempfile.mkstemp(prefix='.'+target.name+'.', suffix='.partial', dir=target.parent)
    temporary = Path(temporary_name)
    history_file = None
    try:
        with os.fdopen(descriptor, 'wb') as destination, produced.open('rb') as source:
            shutil.copyfileobj(source, destination, length=8*1024*1024)
            destination.flush()
            os.fsync(destination.fileno())
        if (temporary.stat().st_size != expected_size or digest(temporary) != expected_hash
                or digest(produced) != expected_hash):
            raise RuntimeError('Installer copy verification failed; the compiled original was retained.')
        _pe64(temporary)
        if os.path.lexists(target):
            if not replace:
                raise FileExistsError('Installer appeared during publication; existing file was not replaced.')
            if target.is_symlink() or not target.is_file():
                raise ValueError('Existing installer target must be a regular file.')
            stamp = stamp or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            if Path(stamp).name != stamp:
                raise ValueError('Installer history stamp must be a single directory name.')
            history = target.parent/'installer-history'/stamp
            history.mkdir(parents=True)
            history_file = history/target.name
            _publish_without_replace(target, history_file)
        try:
            _publish_without_replace(temporary, target)
        except OSError:
            if history_file is not None:
                try:
                    _publish_without_replace(history_file, target)
                except OSError as recovery_error:
                    raise RuntimeError('Installer publication failed; the old installer remains in '
                                       +str(history_file)+' and the compiled original remains in '
                                       +str(produced)+'.') from recovery_error
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return {'sha256': expected_hash, 'bytes': expected_size}


def _write_atomic_text(path, text, encoding):
    descriptor, name = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding=encoding, newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finalize_installer(application, output, compiler, produced, *, replace=False):
    """Validate and publish an already compiled installer without compiling again."""
    application = validate_payload(application)
    output, compiler, produced = Path(output).resolve(), Path(compiler).resolve(), Path(produced).resolve()
    if output == application or output.is_relative_to(application):
        raise ValueError('Installer output folder must be outside the application payload.')
    lock = compiler_lock(compiler)
    filename = f'ProxmarkStudio-{APP_VERSION}-Setup-x64.exe'
    if produced.name != filename:
        raise ValueError('Compiled installer filename does not match the current application version.')
    _pe64(produced)
    # The complete payload is embedded; sidecar .bin parts are forbidden.
    if sorted(p.name for p in produced.parent.iterdir()) != [filename]:
        raise RuntimeError('Installer must be a single self-contained EXE.')
    target = output/filename
    published = publish_installer(produced, target, replace=replace)
    script = ROOT/'installer/proxmark-studio.iss'
    record = {
        'version': APP_VERSION, 'filename': filename, **published,
        'built_utc': datetime.now(timezone.utc).isoformat(),
        'signed': False, 'platform': 'windows-x64', 'installation_scope': 'current-user',
        'requires_python_installation': False, 'requires_network_during_install': False,
        'desktop_shortcut_default': True, 'uninstall_preserves_user_data': True,
        'payload_manifest_sha256': digest(application/'files.sha256.json'),
        'installer_script_sha256': digest(script),
        'compiler': {'version': lock['version'], 'sha256': lock['compiler_sha256'],
                     'download_url': lock['download_url'], 'download_sha256': lock['download_sha256']},
    }
    _write_atomic_text(output/'installer-manifest.json', json.dumps(record, indent=2)+'\n', 'utf-8')
    sums = output/'SHA256SUMS.txt'
    lines = sums.read_text(encoding='ascii').splitlines() if sums.exists() else []
    lines = [line for line in lines if not line.endswith('  '+filename)]
    lines.append(record['sha256']+'  '+filename)
    _write_atomic_text(sums, '\n'.join(lines)+'\n', 'ascii')
    return record


def build_installer(application, output, compiler, work, *, replace=False):
    application = validate_payload(application)
    output, work, compiler = Path(output).resolve(), Path(work).resolve(), Path(compiler).resolve()
    if output == application or output.is_relative_to(application) or work.is_relative_to(application):
        raise ValueError('Installer output and work folders must be outside the application payload.')
    compiler_lock(compiler)
    output.mkdir(parents=True, exist_ok=True)
    filename = f'ProxmarkStudio-{APP_VERSION}-Setup-x64.exe'
    target = output/filename
    if target.exists() and not replace:
        raise FileExistsError('Installer already exists; use --replace to retain it in installer-history.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    build = work/stamp
    build.mkdir(parents=True)
    compiled = build/'output'
    compiled.mkdir()
    icon = build/'app.ico'
    gui_application = make_icon(icon)
    script = ROOT/'installer/proxmark-studio.iss'
    command = [str(compiler), '--quiet-progress', '--no-ide-signtools', '--define=BundleDir='+str(application),
               '--define=SetupOutputDir='+str(compiled), '--define=AppVersion='+APP_VERSION,
               '--define=NumericVersion='+'.'.join(map(str, WINDOWS_VERSION)),
               '--define=SetupIcon='+str(icon), str(script)]
    with (build/'compile.log').open('w', encoding='utf-8') as stream:
        subprocess.run(command, cwd=ROOT, env=build_environment(), stdout=stream,
                       stderr=subprocess.STDOUT, check=True)
    produced = compiled/filename
    return finalize_installer(application, output, compiler, produced, replace=replace)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, default=ROOT/'dist/ProxmarkStudio')
    parser.add_argument('--output-root', type=Path, default=ROOT/'dist')
    parser.add_argument('--work-root', type=Path, default=ROOT/'build/installer')
    parser.add_argument('--compiler', type=Path, required=True)
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    result = build_installer(args.application, args.output_root, args.compiler,
                             args.work_root, replace=args.replace)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
