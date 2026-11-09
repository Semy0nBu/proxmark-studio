"""Fetch exact MSYS2 source packages for the staged copyleft native runtime.

MSYS2 source packages contain the upstream source payload, packaging recipe and
patches. Downloaded filenames and hashes are bound into a local release index.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request


SOURCE_ORIGIN = 'https://repo.msys2.org/mingw/sources/'


def source_specs(manifest):
    specs = {}
    for package in manifest['packages']:
        licenses = ' '.join(package['licenses'])
        base = (package.get('package_base') or [None])[0]
        if 'GCC-exception' in licenses:
            continue
        if 'GPL' not in licenses and base != 'mingw-w64-x264':
            continue
        version = package['version'].split(':', 1)[-1]
        if not isinstance(base, str) or not re.fullmatch(r'[A-Za-z0-9+_.~-]+', base) or not re.fullmatch(r'[A-Za-z0-9+_.~-]+', version):
            raise ValueError('Unsafe or unknown source package identity.')
        filename = f'{base}-{version}.src.tar.zst'
        specs[filename] = {'filename': filename, 'url': SOURCE_ORIGIN+filename,
                           'package_base': base, 'version': version,
                           'licenses': package['licenses']}
    return list(specs.values())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch_one(spec, output, tar):
    target = output/spec['filename']
    if not target.exists():
        temporary = target.with_suffix(target.suffix+'.download')
        request = urllib.request.Request(spec['url'], headers={'User-Agent': 'Proxmark-Studio-release-source-builder'})
        with urllib.request.urlopen(request, timeout=60) as source, temporary.open('wb') as destination:
            while data := source.read(1024*1024):
                destination.write(data)
        temporary.replace(target)
    environment = os.environ.copy()
    environment['PATH'] = str(tar.parent)+os.pathsep+environment.get('PATH', '')
    result = subprocess.run([str(tar), '--force-local', '-tf', str(target)], env=environment,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('Unreadable source archive: '+target.name+': '+result.stderr[-300:])
    names = result.stdout.splitlines()
    recipe = spec['package_base']+'/PKGBUILD'
    srcinfo = spec['package_base']+'/.SRCINFO'
    if recipe not in names or srcinfo not in names or len(names) < 3:
        raise ValueError('Source archive lacks its recipe/metadata/source payload: '+target.name)
    info = subprocess.run([str(tar), '--force-local', '-xOf', str(target), srcinfo],
                          env=environment, capture_output=True, text=True, timeout=30)
    pkgver = re.search(r'^\s*pkgver\s*=\s*(\S+)', info.stdout, re.M)
    pkgrel = re.search(r'^\s*pkgrel\s*=\s*(\S+)', info.stdout, re.M)
    if info.returncode or not pkgver or not pkgrel or pkgver[1]+'-'+pkgrel[1] != spec['version']:
        raise ValueError('Source version does not match the installed package: '+target.name)
    listing = target.with_suffix(target.suffix+'.files.txt')
    listing.write_text('\n'.join(names)+'\n', encoding='utf-8')
    return {**spec, 'sha256': digest(target), 'bytes': target.stat().st_size,
            'archive_members': len(names), 'recipe_and_source_payload_present': True,
            'source_version_matches_installed_package': True,
            'pgp_signature_verified': False}


def fetch_sources(manifest_path, output, *, tar=None, workers=4):
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    tar = Path(tar or r'D:/Tools/ProxSpace-PM3/msys2/usr/bin/tar.exe').resolve()
    if not tar.is_file():
        raise ValueError('GNU tar with zstd support is required for source verification.')
    specs = source_specs(manifest)
    records, failures = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, spec, output, tar): spec for spec in specs}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                record = future.result()
                records.append(record)
                print(json.dumps({'source': record['filename'], 'bytes': record['bytes'], 'passed': True}), flush=True)
            except Exception as error:
                failures.append({'filename': spec['filename'], 'error': str(error)})
                print(json.dumps(failures[-1]), flush=True)
    report = {'generated_utc': datetime.now(timezone.utc).isoformat(), 'passed': not failures and len(records)==len(specs),
              'kind': 'proxmark-studio-native-copyleft-source-index-v1', 'sources': sorted(records, key=lambda item:item['filename']),
              'failures': failures, 'source_package_count': len(specs),
              'excluded_runtime_exception': 'GCC runtime DLLs use the GCC Runtime Library Exception 3.1.',
              'download_transport': 'HTTPS; local SHA256 recorded; MSYS2 PGP signatures were not checked.'}
    (output/'NATIVE_SOURCE_INDEX.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tar', type=Path)
    args = parser.parse_args()
    result = fetch_sources(args.native_manifest, args.output, tar=args.tar)
    print(json.dumps({'passed': result['passed'], 'source_packages': result['source_package_count'], 'failures': result['failures']}))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
