"""Build a single offline Windows installer from a verified complete release."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

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


def build_installer(application, output, compiler, work, *, replace=False):
    application = validate_payload(application)
    output, work, compiler = Path(output).resolve(), Path(work).resolve(), Path(compiler).resolve()
    if output == application or output.is_relative_to(application) or work.is_relative_to(application):
        raise ValueError('Installer output and work folders must be outside the application payload.')
    lock = compiler_lock(compiler)
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
    _pe64(produced)
    # The complete payload is embedded; sidecar .bin parts are forbidden.
    if sorted(p.name for p in compiled.iterdir()) != [filename]:
        raise RuntimeError('Installer must be a single self-contained EXE.')
    if target.exists():
        history = output/'installer-history'/stamp
        history.mkdir(parents=True)
        target.rename(history/filename)
    produced.rename(target)
    record = {
        'version': APP_VERSION, 'filename': filename, 'sha256': digest(target),
        'bytes': target.stat().st_size, 'built_utc': datetime.now(timezone.utc).isoformat(),
        'signed': False, 'platform': 'windows-x64', 'installation_scope': 'current-user',
        'requires_python_installation': False, 'requires_network_during_install': False,
        'desktop_shortcut_default': True, 'uninstall_preserves_user_data': True,
        'payload_manifest_sha256': digest(application/'files.sha256.json'),
        'installer_script_sha256': digest(script),
        'compiler': {'version': lock['version'], 'sha256': lock['compiler_sha256'],
                     'download_url': lock['download_url'], 'download_sha256': lock['download_sha256']},
    }
    (output/'installer-manifest.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    sums = output/'SHA256SUMS.txt'
    lines = sums.read_text(encoding='ascii').splitlines() if sums.exists() else []
    lines = [line for line in lines if not line.endswith('  '+filename)]
    lines.append(record['sha256']+'  '+filename)
    sums.write_text('\n'.join(lines)+'\n', encoding='ascii')
    return record


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
