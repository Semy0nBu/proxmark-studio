"""Read-only release validation. Never opens a device or a user database."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import struct
import zipfile


GUI_ASSETS = ('locales/en.json', 'client_commands.json', 'hf_commands.json',
              'lf_commands.json', 'app-icon.svg', 'check.svg', 'chevron.svg')
PRIVATE_NAMES = {'library.sqlite3', 'settings.json', 'startup-error.log', 'client-log.txt',
                 'device-flash-before.bin', 'device-flash-after.bin', 'mf_classic_dict_user.nfc'}
PRIVATE_PARTS = {'.git', '.venv', '.proxmark3', '__pycache__', '.pytest_cache',
                 'sessions', 'flipper-imports', 'firmware-backups', 'reference'}
PUBLIC_ROOT = {'README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'START_HERE.txt',
               'CHANGELOG.md', '.gitattributes',
               'requirements.txt', 'requirements-build.txt', 'run_gui.py',
               'start.ps1', 'launch-gui.vbs', 'auto-write.ps1', '.gitignore', 'SOURCE_SNAPSHOT.json'}
PUBLIC_DOCS = {'USER_GUIDE_RU.md', 'USER_GUIDE_EN.md', 'BUILDING.md', 'RELEASING.md'}
PUBLIC_TOOLS = {'build_release.py', 'release_native.py', 'release_firmware.py',
                'release_gui_licenses.py', 'release_gui_sources.json', 'release_sources.py',
                'verify_release.py', 'audit_english_catalog.py', 'release_windows_manifest.py',
                'build_installer.py', 'verify_installer_install.py'}
SHA = re.compile(r'[0-9a-f]{64}\Z')
DRIVE_PATH = re.compile(r'(?<![A-Za-z])[A-Za-z]:[\\/]')
PERSONAL_PATH = re.compile(r'(?i)(?:[A-Z]:[\\/]Users[\\/](?!Public\b|Default\b)[^\\/\s<>]+|/home/[^/\s<>]+|/Users/[^/\s<>]+)')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024*1024), b''):
            result.update(data)
    return result.hexdigest()


def _json(path):
    if path.stat().st_size > 8*1024*1024:
        raise ValueError('Oversized release metadata: '+path.name)
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('JSON object required: '+path.name)
    return value


def _inside(root, base, relative):
    if not isinstance(relative, str) or not relative or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ValueError('An artifact path is missing or absolute.')
    value = PurePosixPath(relative.replace('\\', '/'))
    if value.is_absolute():
        raise ValueError('An artifact path is absolute.')
    path = (base/str(value)).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise ValueError('An artifact path escapes the application directory.')
    return path


def _hash(root, base, relative, expected):
    path = _inside(root, base, relative)
    if not isinstance(expected, str) or not SHA.fullmatch(expected.lower()):
        raise ValueError('Missing or invalid SHA256 for '+str(relative))
    if not path.is_file() or digest(path) != expected.lower():
        raise ValueError('Missing or changed file: '+str(relative))
    return path


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _elf(path):
    data = path.read_bytes()
    if len(data)<84 or data[:7] != b'\x7fELF\x01\x01\x01':
        raise ValueError('Firmware is not little-endian ELF32.')
    kind, machine = struct.unpack_from('<HH', data, 16)
    entry, offset = struct.unpack_from('<II', data, 24)
    header, width, count = struct.unpack_from('<HHH', data, 40)
    if kind!=2 or machine!=40 or header!=52 or width!=32 or not 1<=count<=64 or offset<52 or offset+width*count>len(data):
        raise ValueError('Firmware ELF header is invalid.')
    segments, executable = [], []
    for index in range(count):
        typ, start, virtual, physical, size, memory, flags, _ = struct.unpack_from('<8I', data, offset+width*index)
        if typ!=1 or not size:
            continue
        if size>memory or start+size>len(data) or not 0x102000<=physical<0x180000 or physical+size>0x180000:
            raise ValueError('Firmware LOAD segment touches the bootloader or escapes flash.')
        segments.append((physical, physical+size))
        if flags&1:
            executable.append((virtual, virtual+size))
    segments.sort()
    if not segments or segments[0][0]!=0x102000 or any(a[1]>b[0] for a,b in zip(segments,segments[1:])):
        raise ValueError('Firmware LOAD segments are missing or overlap.')
    if not entry&1 or not any(a<=(entry&~1)<b for a,b in executable):
        raise ValueError('Firmware Thumb entry point is invalid.')


def _pe64(path):
    with path.open('rb') as stream:
        header=stream.read(64)
        if len(header)!=64 or header[:2]!=b'MZ':
            raise ValueError('Not a Windows PE executable: '+path.name)
        offset=struct.unpack_from('<I',header,60)[0]
        if offset>4*1024*1024:
            raise ValueError('Invalid Windows PE header offset.')
        stream.seek(offset);pe=stream.read(26)
        if len(pe)!=26 or pe[:4]!=b'PE\0\0' or struct.unpack_from('<H',pe,4)[0]!=0x8664 or struct.unpack_from('<H',pe,24)[0]!=0x20b:
            raise ValueError('Expected a Windows x64 executable: '+path.name)


def _source_export(path):
    with zipfile.ZipFile(path) as archive:
        files = [PurePosixPath(name.replace('\\','/')) for name in archive.namelist() if not name.endswith('/')]
        if not files:
            raise ValueError('The GUI source archive is empty.')
        roots={item.parts[0] for item in files}
        if len(roots)!=1:
            raise ValueError('GUI source archive needs one root folder.')
        names=set()
        for item in files:
            if item.is_absolute() or '..' in item.parts or any(':' in part or part.casefold() in PRIVATE_PARTS for part in item.parts):
                raise ValueError('Private or escaping entry in GUI source archive.')
            relative=PurePosixPath(*item.parts[1:]);names.add(str(relative))
            if str(relative) in {'tests/test_connection_lab.py','tests/test_live_functional_harness.py'}:
                raise ValueError('An internal hardware/prototype test reached the GUI source snapshot.')
            if relative.name.casefold() in PRIVATE_NAMES or relative.suffix.lower() in {'.sqlite','.sqlite3','.db','.keys','.nfc','.trace','.wav'}:
                raise ValueError('A user-data format reached the GUI source snapshot.')
            parts=relative.parts
            allowed=(len(parts)==1 and parts[0] in PUBLIC_ROOT or
                     len(parts)>1 and parts[0] in {'pm3gui','tests'} and relative.suffix in {'.py','.json','.svg'} or
                     len(parts)==2 and parts[0]=='docs' and parts[1] in PUBLIC_DOCS or
                     len(parts)==3 and parts[:2]==('docs','assets') and parts[2] in {'banner.svg','interface-en.png','interface-ru.png'} or
                     len(parts)>2 and parts[:2]==('docs','licenses') or
                     len(parts)==2 and parts[0]=='tools' and parts[1] in PUBLIC_TOOLS or
                     len(parts)==2 and parts[0]=='installer' and parts[1] in {'proxmark-studio.iss','toolchain.json'} or
                     len(parts)>2 and parts[:2]==('tools','native-client-tests') and relative.suffix in {'.py','.c','.md'} or
                     len(parts)>2 and parts[:2]==('tools','client-patches') and relative.suffix=='.patch' or
                     len(parts)>1 and parts[0]=='.github' and relative.suffix in {'.yml','.yaml'} or
                     str(relative)=='.github/ISSUE_TEMPLATE/bug_report.md')
            if not allowed:
                raise ValueError('GUI source archive contains an unreviewed path: '+str(relative))
        required={'LICENSE','README.md','README.en.md','run_gui.py','pm3gui/app.py',
                  'tools/build_release.py','tools/release_gui_sources.json','docs/licenses/proxmark3-LICENSE.txt'}
        if required-names:
            raise ValueError('GUI source archive lacks required build inputs: '+', '.join(sorted(required-names)))
        return len(files)


def verify_release(app_dir, *, require_sources=True):
    root=Path(app_dir).resolve()
    report={'passed':False,'generated_utc':datetime.now(timezone.utc).isoformat(),'application_directory':str(root),
            'hardware_access':False,'require_sources':require_sources,'checks':[],'errors':[],'warnings':[]}
    def check(name, function):
        try:
            details=function()
            report['checks'].append({'name':name,'passed':True,'details':details})
            return details
        except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError,struct.error,zipfile.BadZipFile) as error:
            message=str(error)
            report['checks'].append({'name':name,'passed':False,'error':message})
            report['errors'].append(name+': '+message)
            return None
    if not root.is_dir():
        report['errors'].append('Application directory does not exist.')
        return report
    release=check('release-metadata',lambda:_json(root/'release.json')) or {}
    kind=release.get('bundle_kind')
    if (release.get('application')!='Proxmark Studio' or release.get('platform')!='windows-x64' or
            not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?',str(release.get('version','')))):
        report['errors'].append('Application name, Windows x64 platform or release version is invalid.')
    if kind not in {'complete','gui-only'}:
        report['errors'].append('Unknown bundle_kind; expected complete or gui-only.')
    def gui():
        if not (root/'ProxmarkStudio.exe').is_file() or (root/'ProxmarkStudio.exe').stat().st_size<2:
            raise ValueError('The Windows application executable is missing.')
        _pe64(root/'ProxmarkStudio.exe')
        if not list((root/'_internal').glob('python3*.dll')):
            raise ValueError('The frozen Python runtime is missing.')
        forbidden = {'ucrtbase.dll', 'icuuc.dll'}
        foreign = [p.name for p in (root/'_internal').rglob('*.dll')
                   if p.name.casefold() in forbidden or p.name.casefold().startswith('api-ms-win-')]
        if foreign:
            raise ValueError('Host Windows system DLLs reached the GUI payload: '+', '.join(sorted(foreign)))
        for name in GUI_ASSETS:
            if not (root/'_internal/pm3gui'/name).is_file():
                raise ValueError('Missing GUI resource: '+name)
        for name in ('README.md','README.en.md','LICENSE','THIRD_PARTY_NOTICES.md','docs/USER_GUIDE_RU.md','docs/USER_GUIDE_EN.md'):
            if not (root/name).is_file():
                raise ValueError('Missing end-user document: '+name)
        return {'assets':len(GUI_ASSETS)}
    check('frozen-gui-and-guides',gui)
    def privacy():
        count=0
        for path in root.rglob('*'):
            relative=path.relative_to(root)
            if path.is_symlink():
                raise ValueError('Symlink in the release: '+relative.as_posix())
            if any(part.casefold() in PRIVATE_PARTS for part in relative.parts) or path.name.casefold() in PRIVATE_NAMES:
                raise ValueError('Private/development artifact: '+relative.as_posix())
            if path.is_file():
                count+=1
                if path.suffix.lower() in {'.sqlite','.sqlite3','.db','.keys','.nfc','.trace','.wav'}:
                    raise ValueError('Unexpected user-data format: '+relative.as_posix())
        return {'files_checked':count}
    check('no-personal-data-or-development-caches',privacy)
    def file_manifest():
        manifest=root/'files.sha256.json'
        if not manifest.is_file():
            report['warnings'].append('files.sha256.json is not present yet; whole-release coverage was not checked.')
            return {'present':False}
        entries=_json(manifest);actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p!=manifest}
        if set(entries)!=actual:
            raise ValueError('File manifest coverage differs: missing='+str(sorted(actual-set(entries))[:8])+
                             '; stale='+str(sorted(set(entries)-actual)[:8]))
        for name, expected in entries.items():
            _hash(root,root,name,expected)
        return {'present':True,'files':len(entries)}
    check('complete-file-checksums',file_manifest)
    native=None
    if kind=='complete':
        def runtime():
            value=_json(root/'native-manifest.json')
            client=root/'client/proxmark3.exe'
            _pe64(client)
            if digest(client)!=release.get('native_client_sha256') or digest(client)!=value.get('client_sha256'):
                raise ValueError('Native client does not match release metadata.')
            runtime_root=root/'client/libs';manifest=_json(runtime_root/'.pm3gui-runtime-manifest.json')
            if manifest.get('kind')!='proxmark3-gui-local-ucrt64-runtime-v1' or len(manifest.get('files',[]))!=63:
                raise ValueError('The native runtime manifest must contain the reviewed 63 DLLs.')
            if manifest.get('client_sha256')!=value['client_sha256']:
                raise ValueError('Native DLL manifest refers to a different client.')
            proof=value.get('resource_transformation',{})
            if (proof.get('kind')!='proxmark-studio-windows-utf8-resource-v1' or
                    proof.get('client_sha256')!=value['client_sha256'] or
                    proof.get('original_client_sha256')!=value.get('original_client_sha256') or
                    proof.get('original_client_sha256')!=manifest.get('original_client_sha256') or
                    proof.get('original_client_sha256')!=release.get('native_client_original_sha256') or
                    proof.get('windows_active_code_page')!='UTF-8' or
                    not all(proof.get(key) is True for key in ('code_data_sections_unchanged',
                        'other_resource_payloads_unchanged','gnu_symbol_overlay_unchanged','firmware_images_unchanged'))):
                raise ValueError('The portable UTF-8 client resource transformation is missing or unbound.')
            names=set()
            for item in manifest['files']:
                name=item['relative_target']
                if name in names:
                    raise ValueError('Duplicate native DLL record.')
                names.add(name);_hash(root,runtime_root,name,item['sha256'])
            if not {'platforms/qoffscreen.dll','platforms/qwindows.dll','libpython3.14.dll'}.issubset(names):
                raise ValueError('Native Qt platform plugins are missing.')
            for item in value.get('files',[]):
                _hash(root,root,item['relative_target'],item['sha256'])
            declared={item['relative_target'] for item in value.get('files',[]) if item['relative_target'].startswith('client/')}
            declared.add('client/libs/.pm3gui-runtime-manifest.json')
            actual={(p.relative_to(root)).as_posix() for p in (root/'client').rglob('*') if p.is_file()}
            if actual!=declared:
                raise ValueError('Undeclared or missing files in the native client payload.')
            for directory in ('cmdscripts','lualibs','luascripts','pyscripts','resources','dictionaries'):
                if not any((root/'client'/directory).rglob('*')):
                    raise ValueError('Missing native resources: '+directory)
            if not (root/'client/python/lib/python3.14/encodings/__init__.py').is_file() or not (root/'client/python/bin/python.exe').is_file():
                raise ValueError('Matching private native Python 3.14 is missing.')
            packages=value.get('packages',[])
            if not packages:
                raise ValueError('Native runtime package/license inventory is missing.')
            for package in packages:
                if not package.get('notice_files'):
                    raise ValueError('Missing native package notice: '+package.get('name','?'))
                for name in package['notice_files']:
                    if not _inside(root,root/'licenses/native-runtime',name).is_file():
                        raise ValueError('Missing native package notice file: '+name)
            return value
        native=check('native-runtime-resources-and-notices',runtime)
        def firmware():
            result=[]
            for stem, extras in [('gui-cancel',[]),('gui-blueshark',['BTADDON'])]:
                base=root/'firmware';value=_json(base/(stem+'-build-manifest.json'))
                if value.get('platform')!='PM3RDV4' or value.get('platform_extras')!=extras or value.get('flash_capacity')!=524288:
                    raise ValueError('Incorrect portable RDV4 firmware profile: '+stem)
                entries=value['artifact_files']
                for item in entries.values():
                    path=_hash(root,base,item['path'],item['sha256'])
                    if 'bytes' in item and path.stat().st_size!=item['bytes']:
                        raise ValueError('Firmware artifact size mismatch.')
                if entries['proxmark3.exe']['sha256']!=release.get('native_client_sha256'):
                    raise ValueError('Firmware references a different native client.')
                _elf(_inside(root,base,entries['fullimage.elf']['path']))
                result.append(stem)
            if not (root/'firmware/LICENSE.txt').is_file():
                raise ValueError('Firmware license is missing.')
            return result
        check('portable-firmware-profiles',firmware)
    elif kind=='gui-only':
        if (root/'client').exists() or (root/'firmware').exists() or release.get('native_client_sha256') is not None:
            report['errors'].append('A gui-only release contains undeclared native components.')
    def gui_licenses():
        value=_json(root/'licenses/gui/SOURCE_INVENTORY.json')
        components=value.get('components',[])
        if not components:
            raise ValueError('GUI runtime source/license inventory is empty.')
        for name in ('GPL-3.0-only.txt','LGPL-3.0-only.txt'):
            if not (root/'licenses/gui'/name).is_file():
                raise ValueError('Missing Qt/PySide open-source license: '+name)
        for component in components:
            notices=component.get('notice_files',[])
            if not notices:
                raise ValueError('No license documents for GUI component '+component.get('id','?'))
            for item in notices:
                _hash(root,_inside(root,root,component['license_directory']),item['path'],item['sha256'])
            if require_sources:
                _hash(root,root,component.get('source_path'),component['sha256'])
        return {'components':len(components)}
    check('gui-runtime-licenses-and-sources',gui_licenses)
    if require_sources:
        check('public-gui-corresponding-source',lambda:_source_export(root/'sources/proxmark-studio-source.zip'))
        if kind=='complete':
            def native_sources():
                archive=root/'sources/proxmark3-corresponding-source.zip'
                with zipfile.ZipFile(archive) as source:
                    if 'proxmark3/PROXMARK_STUDIO_SOURCE_MANIFEST.json' not in source.namelist():
                        raise ValueError('Native source manifest is missing from its archive.')
                    if native:
                        prefix='proxmark3/PROXMARK_STUDIO_'
                        preferred=(prefix+'WINDOWS_MANIFEST.py',prefix+'UTF8.manifest',prefix+'WINDOWS_UTF8_TRANSFORM.json',
                                   prefix+'WINDOWS_UTF8_BUILD.md')
                        if not all(name in source.namelist() for name in preferred):
                            raise ValueError('The UTF-8 post-link transform lacks its preferred source inputs.')
                        proof=native['resource_transformation']
                        if hashlib.sha256(source.read(preferred[1])).hexdigest()!=proof['manifest_sha256']:
                            raise ValueError('Preferred UTF-8 manifest differs from the installed client proof.')
                        if json.loads(source.read(preferred[2]))!=proof:
                            raise ValueError('Native source transformation proof differs from the binary inventory.')
                base=root/'sources/native-runtime';index=_json(base/'NATIVE_SOURCE_INDEX.json')
                if not index.get('passed') or not index.get('sources') or index.get('failures'):
                    raise ValueError('Native runtime sources are incomplete.')
                for item in index['sources']:
                    _hash(root,base,item['filename'],item['sha256'])
                    if not item.get('source_version_matches_installed_package'):
                        raise ValueError('A native source archive was not matched to its installed version.')
                if native:
                    expected=set()
                    for package in native.get('packages',[]):
                        licenses=' '.join(package['licenses']);base=(package.get('package_base') or [''])[0]
                        if 'GCC-exception' not in licenses and ('GPL' in licenses or base=='mingw-w64-x264'):
                            expected.add((base,package['version'].split(':',1)[-1]))
                    provided={(item['package_base'],item['version']) for item in index['sources']}
                    if expected-provided:
                        raise ValueError('Missing corresponding source packages: '+str(sorted(expected-provided)))
                return {'runtime_source_packages':len(index['sources'])}
            check('native-corresponding-source-and-runtime-sources',native_sources)
    def metadata_privacy():
        paths=[*root.glob('*.json'),
               root/'client/libs/.pm3gui-runtime-manifest.json',root/'licenses/gui/SOURCE_INVENTORY.json',
               root/'sources/native-runtime/NATIVE_SOURCE_INDEX.json',*(root/'firmware').glob('*-build-manifest.json')]
        count=0
        for path in paths:
            if not path.is_file():
                continue
            for value in _strings(_json(path)):
                if DRIVE_PATH.search(value) or PERSONAL_PATH.search(value):
                    raise ValueError('A build-machine path leaked into '+path.relative_to(root).as_posix())
            count+=1
        for path in [*root.glob('*.md'),*(root/'docs').glob('*.md')]:
            if PERSONAL_PATH.search(path.read_text(encoding='utf-8')):
                raise ValueError('Personal home path in end-user documentation: '+path.name)
        return {'generated_metadata_files':count}
    check('portable-generated-metadata',metadata_privacy)
    # Large native inventories are validated above; the report need not repeat
    # every file hash already stored in the release's own manifests.
    for item in report['checks']:
        if item['name']=='native-runtime-resources-and-notices' and isinstance(item.get('details'),dict):
            item['details']={key:item['details'].get(key) for key in ('runtime_files','python_version','client_sha256')}
    report['passed']=not report['errors']
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--omit-source-checks',action='store_true')
    args=parser.parse_args()
    if args.output.resolve().is_relative_to(args.application.resolve()):
        parser.error('Write verification reports outside the application directory.')
    report=verify_release(args.application,require_sources=not args.omit_source_checks)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'errors':report['errors'],'warnings':report['warnings']},indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
