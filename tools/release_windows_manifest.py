"""Post-link Windows UTF-8 manifest, preserving native code and GNU symbols.

This build tool changes only a staged copy. It does not flash hardware or modify
the native source checkout. pefile is needed only in the release-build environment.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
import zipfile


MANIFEST_KEY = '24/1/0'
UTF8_APPLICATION = ('<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>'
    '<activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>'
    '</windowsSettings></application>')
SOURCE_MEMBERS = ('PROXMARK_STUDIO_WINDOWS_MANIFEST.py', 'PROXMARK_STUDIO_UTF8.manifest',
                  'PROXMARK_STUDIO_WINDOWS_UTF8_TRANSFORM.json', 'PROXMARK_STUDIO_WINDOWS_UTF8_BUILD.md')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utf8_manifest(original):
    text=original.decode('utf-8-sig').rstrip('\0')
    root=ET.fromstring(text)
    if root.tag!='{urn:schemas-microsoft-com:asm.v1}assembly' or '</assembly>' not in text:
        raise ValueError('Unsupported original Windows assembly manifest.')
    if any(node.tag.endswith('}activeCodePage') for node in root.iter()):
        raise ValueError('The native client already declares an active code page.')
    result=text.replace('</assembly>',UTF8_APPLICATION+'</assembly>')
    ET.fromstring(result)
    return result.encode('utf-8')


def snapshot(data):
    import pefile
    pe=pefile.PE(data=data)
    try:
        directory=pe.OPTIONAL_HEADER.DATA_DIRECTORY
        if directory[4].VirtualAddress or directory[4].Size:
            raise ValueError('Signed executables must not be resource-patched.')
        resources={}
        for kind in getattr(getattr(pe,'DIRECTORY_ENTRY_RESOURCE',None),'entries',[]):
            for name in kind.directory.entries:
                for lang in name.directory.entries:
                    key='/'.join(str(item.id if item.name is None else item.name) for item in (kind,name,lang))
                    entry=lang.data.struct
                    resources[key]={'sha256':sha(pe.get_data(entry.OffsetToData,entry.Size)),
                                    'codepage':entry.CodePage}
        if MANIFEST_KEY not in resources:
            raise ValueError('Expected neutral-language RT_MANIFEST 24/1/0.')
        sections={}
        for section in pe.sections:
            name=section.Name.rstrip(b'\0').decode('ascii')
            if name in sections:
                raise ValueError('Duplicate PE section names are unsupported.')
            sections[name]={'sha256':sha(section.get_data()),'rva':section.VirtualAddress,
                            'virtual_size':section.Misc_VirtualSize,'characteristics':section.Characteristics}
        overlay_start=pe.get_overlay_data_start_offset()
        overlay=data[overlay_start:] if overlay_start is not None else b''
        return {'sections':sections,'resources':resources,
                'machine':pe.FILE_HEADER.Machine,'characteristics':pe.FILE_HEADER.Characteristics,
                'entrypoint':pe.OPTIONAL_HEADER.AddressOfEntryPoint,'image_base':pe.OPTIONAL_HEADER.ImageBase,
                'subsystem':pe.OPTIONAL_HEADER.Subsystem,'dll_characteristics':pe.OPTIONAL_HEADER.DllCharacteristics,
                'directories':[(entry.VirtualAddress,entry.Size) for index,entry in enumerate(directory) if index not in (2,4)],
                'overlay_sha256':sha(overlay),'overlay_bytes':len(overlay),
                'symbol_count':pe.FILE_HEADER.NumberOfSymbols,'overlay_start':overlay_start}, resources[MANIFEST_KEY]
    finally:
        pe.close()


def _manifest_payload(data):
    import pefile
    pe=pefile.PE(data=data)
    try:
        for kind in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if kind.id!=24:continue
            for name in kind.directory.entries:
                if name.id!=1:continue
                for lang in name.directory.entries:
                    if lang.id==0:
                        value=lang.data.struct
                        return pe.get_data(value.OffsetToData,value.Size)
    finally:
        pe.close()
    raise ValueError('Original Windows manifest was not found.')


def _update_resource(path, manifest):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    begin=kernel.BeginUpdateResourceW;begin.argtypes=[wintypes.LPCWSTR,wintypes.BOOL];begin.restype=wintypes.HANDLE
    update=kernel.UpdateResourceW
    update.argtypes=[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD,ctypes.c_void_p,wintypes.DWORD]
    update.restype=wintypes.BOOL
    end=kernel.EndUpdateResourceW;end.argtypes=[wintypes.HANDLE,wintypes.BOOL];end.restype=wintypes.BOOL
    handle=begin(str(path),False)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    buffer=ctypes.create_string_buffer(manifest)
    try:
        if not update(handle,ctypes.c_void_p(24),ctypes.c_void_p(1),0,buffer,len(manifest)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        end(handle,True);raise
    if not end(handle,False):raise ctypes.WinError(ctypes.get_last_error())


def _restore_overlay(original, updated):
    """UpdateResource discards GNU COFF symbols; retain them at the new file end."""
    import pefile
    before=pefile.PE(data=original);after=pefile.PE(data=updated)
    try:
        start=before.get_overlay_data_start_offset()
        if start is None:raise ValueError('Expected the original GNU symbol overlay.')
        overlay=original[start:]
        current_start=after.get_overlay_data_start_offset()
        if current_start is not None:
            if updated[current_start:]!=overlay:
                raise ValueError('Windows resource update produced an unexpected overlay.')
            append_at=current_start;result=bytearray(updated)
        else:
            append_at=len(updated);result=bytearray(updated+overlay)
        delta=before.FILE_HEADER.PointerToSymbolTable-start
        if delta<0 or delta+18*before.FILE_HEADER.NumberOfSymbols>len(overlay):
            raise ValueError('Invalid original GNU symbol-table bounds.')
        fields={'PointerToSymbolTable':append_at+delta,'NumberOfSymbols':before.FILE_HEADER.NumberOfSymbols,
                'TimeDateStamp':before.FILE_HEADER.TimeDateStamp}
        for name,value in fields.items():
            struct.pack_into('<I',result,after.FILE_HEADER.get_field_absolute_offset(name),value)
        rebuilt=pefile.PE(data=bytes(result))
        try:struct.pack_into('<I',result,rebuilt.OPTIONAL_HEADER.get_field_absolute_offset('CheckSum'),rebuilt.generate_checksum())
        finally:rebuilt.close()
        return bytes(result)
    finally:
        before.close();after.close()


def transform_client(path):
    """Apply UTF-8 and prove executable code/data and other resources unchanged."""
    path=Path(path).resolve();original=path.read_bytes();before,_=snapshot(original)
    manifest=utf8_manifest(_manifest_payload(original))
    try:
        _update_resource(path,manifest)
        result=_restore_overlay(original,path.read_bytes());after,_=snapshot(result)
        protected=('machine','characteristics','entrypoint','image_base','subsystem','dll_characteristics',
                   'directories','overlay_sha256','overlay_bytes','symbol_count')
        if any(before[key]!=after[key] for key in protected):
            raise ValueError('The resource update changed protected PE metadata or symbols.')
        code=lambda value:{k:v for k,v in value['sections'].items() if k!='.rsrc'}
        other=lambda value:{k:v for k,v in value['resources'].items() if k!=MANIFEST_KEY}
        if code(before)!=code(after) or other(before)!=other(after):
            raise ValueError('The resource update changed native code/data or other resources.')
        if _manifest_payload(result)!=manifest:
            raise ValueError('The UTF-8 manifest did not survive the resource update.')
        path.write_bytes(result)
        proof={'kind':'proxmark-studio-windows-utf8-resource-v1','original_client_sha256':sha(original),
               'client_sha256':sha(result),'resource':'RT_MANIFEST/1/0','windows_active_code_page':'UTF-8',
               'minimum_windows_utf8_support':'Windows 10 version 1903',
               'code_data_sections_unchanged':True,'other_resource_payloads_unchanged':True,
               'gnu_symbol_overlay_unchanged':True,'firmware_images_unchanged':True,
               'overlay_bytes':before['overlay_bytes'],'protected_sections':code(before),
               'original_manifest_sha256':before['resources'][MANIFEST_KEY]['sha256'],
               'manifest_sha256':sha(manifest)}
        return proof,manifest
    except BaseException:
        path.write_bytes(original);raise


def _write_json(path,value):
    path.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')


def apply_native_utf8_manifest(application):
    """Run after original firmware staging; bind its resource-only client copy."""
    application=Path(application).resolve();client=application/'client/proxmark3.exe'
    native_path=application/'native-manifest.json';runtime_path=application/'client/libs/.pm3gui-runtime-manifest.json'
    native=json.loads(native_path.read_text(encoding='utf-8'));runtime=json.loads(runtime_path.read_text(encoding='utf-8'))
    original_sha=sha(client.read_bytes())
    if native.get('client_sha256')!=original_sha or runtime.get('client_sha256')!=original_sha or native.get('resource_transformation'):
        raise ValueError('The staged native baseline is changed or already transformed.')
    manifests=[application/'firmware'/name for name in ('gui-cancel-build-manifest.json','gui-blueshark-build-manifest.json')]
    images={};values=[]
    for path in manifests:
        value=json.loads(path.read_text(encoding='utf-8'));entries=value['artifact_files']
        if entries['proxmark3.exe']['sha256']!=original_sha:
            raise ValueError('Stage firmware with the original verified client before transforming its manifest.')
        for name in ('fullimage.elf','default_version_pm3.c','Makefile.platform'):
            target=(path.parent/entries[name]['path']).resolve()
            if not target.is_relative_to(application) or sha(target.read_bytes())!=entries[name]['sha256']:
                raise ValueError('A staged firmware image or ARM metadata changed.')
            images[target.relative_to(application).as_posix()]=entries[name]['sha256']
        values.append(value)
    source=application/'sources/proxmark3-corresponding-source.zip'
    if source.is_file():
        with zipfile.ZipFile(source) as archive:
            if any('proxmark3/'+name in archive.namelist() for name in SOURCE_MEMBERS):
                raise ValueError('Preferred resource-transform source already exists in the archive.')
    paths=[client,native_path,runtime_path,*manifests]
    release_path=application/'release.json'
    if release_path.is_file():paths.append(release_path)
    if source.is_file():paths.append(source)
    backups={path:path.read_bytes() for path in paths}
    try:
        proof,manifest=transform_client(client);new_sha=proof['client_sha256'];proof['firmware_bindings']=images
        native['original_client_sha256']=original_sha;native['client_sha256']=new_sha;native['resource_transformation']=proof
        matching=[item for item in native['files'] if item['relative_target']=='client/proxmark3.exe']
        if len(matching)!=1 or matching[0]['sha256']!=original_sha:
            raise ValueError('The native file inventory does not bind its original client.')
        matching[0].update(sha256=new_sha,size=client.stat().st_size)
        runtime['original_client_sha256']=original_sha;runtime['client_sha256']=new_sha
        _write_json(native_path,native);_write_json(runtime_path,runtime)
        for path,value in zip(manifests,values):
            value['original_client_sha256']=original_sha
            value['artifact_files']['proxmark3.exe'].update(sha256=new_sha,bytes=client.stat().st_size)
            value['client_resource_transformation']={'kind':proof['kind'],'code_data_sections_unchanged':True}
            _write_json(path,value)
        if release_path.is_file():
            release=json.loads(release_path.read_text(encoding='utf-8'));release['native_client_sha256']=new_sha
            release['native_client_original_sha256']=original_sha;_write_json(release_path,release)
        if any(sha((application/name).read_bytes())!=expected for name,expected in images.items()):
            raise ValueError('Firmware changed during the client resource transformation.')
        if source.is_file():
            with zipfile.ZipFile(source,'a',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('proxmark3/'+SOURCE_MEMBERS[0],Path(__file__).read_bytes())
                archive.writestr('proxmark3/'+SOURCE_MEMBERS[1],manifest)
                archive.writestr('proxmark3/'+SOURCE_MEMBERS[2],json.dumps(proof,indent=2)+'\n')
                archive.writestr('proxmark3/'+SOURCE_MEMBERS[3],
                    '# Windows UTF-8 post-link resource\n\nCompile the accompanying native sources normally. '
                    'Then on Windows, with pefile 2024.8.26 installed, run:\n\n'
                    '```\npython PROXMARK_STUDIO_WINDOWS_MANIFEST.py --client client/proxmark3.exe --proof utf8-proof.json\n```\n\n'
                    'Only RT_MANIFEST is changed. PE layout/checksum/symbol-table pointers may change; '
                    'all compiled code/data, other resource payloads and the GNU symbol overlay are verified unchanged. '
                    'The packaged firmware images are unchanged. The adjacent XML is the preferred manifest input.\n')
        return proof
    except BaseException:
        for path,data in backups.items():path.write_bytes(data)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--client',type=Path);group.add_argument('--application',type=Path)
    parser.add_argument('--proof',type=Path,required=True);args=parser.parse_args()
    proof=apply_native_utf8_manifest(args.application) if args.application else transform_client(args.client)[0]
    _write_json(args.proof,proof);print(json.dumps(proof,indent=2))


if __name__=='__main__':main()
