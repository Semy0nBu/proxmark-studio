"""Stage only reviewed RDV4 firmware and portable manifests for a release.

This tool reads firmware bundles and client runtime files. It never opens a
device, creates a user profile, or copies hardware backups and session logs.
The destination client must already have been staged by the release builder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pm3gui.firmware import FirmwareCatalog, FirmwareError


PROFILES = (('usb', 'gui-cancel', 'rdv4-usb', []),
            ('blueshark', 'gui-blueshark', 'rdv4-bt', ['BTADDON']))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path, parent: Path) -> str:
    return Path(os.path.relpath(path, parent)).as_posix()


def _entry(path: Path, parent: Path) -> dict:
    return {'path': _relative(path, parent), 'bytes': path.stat().st_size,
            'sha256': digest(path)}


def stage_firmware(source_project: Path, destination_root: Path, client_path: Path) -> dict:
    """Create destination_root/firmware bound to its staged matching client.

    All input bundles are verified before the first destination write. Only
    fullimage.elf and generated version metadata are taken from their artifacts;
    build options are generated from the validated profile configuration.
    """
    source_project = Path(source_project).resolve()
    destination_root = Path(destination_root).resolve()
    client_path = Path(client_path).resolve()
    if source_project == destination_root or not client_path.is_relative_to(destination_root):
        raise ValueError('Stage firmware beside a client inside a separate release directory.')
    if client_path != destination_root / 'client/proxmark3.exe':
        raise ValueError('The staged matching client must be client/proxmark3.exe.')
    if not client_path.is_file():
        raise ValueError('Stage the matching client before its firmware.')
    catalog = FirmwareCatalog(source_project)
    prepared = []
    staged_client_sha = digest(client_path)
    for key, stem, folder, extras in PROFILES:
        plan = catalog.prepare(key)
        if staged_client_sha != plan.client_sha256:
            raise ValueError('The staged client does not match the reviewed firmware bundle.')
        # Rechecking the staged runtime ensures relocation will not borrow DLLs
        # from the build machine when this bundle is prepared by an end user.
        runtime = client_path.parent / 'libs'
        runtime_manifest = runtime / '.pm3gui-runtime-manifest.json'
        runtime_value = json.loads(runtime_manifest.read_text(encoding='utf-8'))
        if runtime_value.get('kind') != 'proxmark3-gui-local-ucrt64-runtime-v1':
            raise ValueError('The staged runtime manifest is missing or invalid.')
        entries = runtime_value.get('files')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 1024:
            raise ValueError('The staged runtime manifest has no reviewed files.')
        staged_runtime = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('The staged runtime manifest contains an invalid entry.')
            relative = entry.get('relative_target')
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise ValueError('The staged runtime manifest contains an invalid path.')
            target = (runtime / relative).resolve()
            if not target.is_relative_to(runtime) or not target.is_file() or digest(target) != entry.get('sha256'):
                raise ValueError('The staged matching client runtime is incomplete or changed.')
            relative = target.relative_to(runtime).as_posix()
            if relative in staged_runtime:
                raise ValueError('The staged runtime manifest contains duplicate files.')
            staged_runtime[relative] = entry['sha256']
        for path, expected in plan.runtime_bindings:
            if staged_runtime.get(path.relative_to(plan.runtime_dir).as_posix()) != expected:
                raise ValueError('The staged matching client runtime is incomplete or changed.')
        manifest = json.loads(plan.profile.manifest_path.read_text(encoding='utf-8'))
        metadata = manifest.get('artifact_files', {}).get('default_version_pm3.c', {})
        value = metadata.get('path') or metadata.get('source')
        if not isinstance(value, str) or not value:
            raise ValueError('The reviewed bundle must include generated version metadata.')
        metadata_path = Path(value)
        if not metadata_path.is_absolute():
            metadata_path = plan.profile.manifest_path.parent / metadata_path
        if not metadata_path.is_file() or digest(metadata_path) != metadata.get('sha256'):
            raise ValueError('The reviewed firmware version metadata is missing or changed.')
        prepared.append((key, stem, folder, extras, plan, metadata_path, manifest))
    license_path = source_project / 'docs/licenses/proxmark3-LICENSE.txt'
    if not license_path.is_file():
        raise ValueError('The Proxmark3 license must accompany the firmware.')
    firmware_root = destination_root / 'firmware'
    if firmware_root.exists() and any(firmware_root.iterdir()):
        raise ValueError('Use an empty firmware staging directory.')
    firmware_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(license_path, firmware_root / 'LICENSE.txt')
    result = {'kind': 'proxmark-studio-portable-firmware-v1', 'profiles': []}
    for key, stem, folder, extras, plan, metadata_path, original in prepared:
        target = firmware_root / folder
        target.mkdir()
        image = target / 'fullimage.elf'
        shutil.copy2(plan.image_path, image)
        version_file = target / 'default_version_pm3.c'
        shutil.copy2(metadata_path, version_file)
        config = target / 'Makefile.platform'
        config.write_text('PLATFORM=PM3RDV4\nPLATFORM_EXTRAS=' + ' '.join(extras) +
                          '\nUSART_BAUD_RATE=115200\n', encoding='ascii', newline='\n')
        value = {'format': 'proxmark-studio-portable-firmware-v1',
                 'platform': 'PM3RDV4', 'platform_extras': extras, 'flash_capacity': 524288,
                 'uart_baud': 115200, 'custom_version': plan.version,
                 'arm_sources_checksum': plan.arm_checksum,
                 'artifact_files': {'fullimage.elf': _entry(image, firmware_root),
                                    'default_version_pm3.c': _entry(version_file, firmware_root),
                                    'Makefile.platform': _entry(config, firmware_root),
                                    'proxmark3.exe': _entry(client_path, firmware_root)}}
        for field in ('source_tag', 'source_commit', 'build_time'):
            if field in original:
                value[field] = original[field]
        manifest_path = firmware_root / (stem + '-build-manifest.json')
        manifest_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        # An explicit external manifest exercises the same loader in a normal
        # build environment where sys.frozen is deliberately not set.
        portable = FirmwareCatalog(destination_root).prepare(key, manifest_path=manifest_path,
                                                            backup_root=destination_root / 'validation-backups')
        result['profiles'].append({'key': key, 'manifest': _relative(manifest_path, destination_root),
                                   'image_sha256': portable.image_sha256,
                                   'client_sha256': portable.client_sha256,
                                   'runtime_files': len(portable.runtime_bindings),
                                   'bootloader_write_disabled': True})
    result['passed'] = len(result['profiles']) == 2
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=ROOT)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--client', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = stage_firmware(args.project, args.destination, args.client)
    except (FirmwareError, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Firmware staging failed: {error}\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
