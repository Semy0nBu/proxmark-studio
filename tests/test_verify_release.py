"""End-user package validation detects incomplete and privacy-unsafe releases."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from tools.verify_release import GUI_ASSETS, _elf, _inside, digest, verify_release


def write(path,data=b'fixture'):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data if isinstance(data,bytes) else data.encode())


def json_file(path,value):
    write(path,json.dumps(value))


def pe64():
    data=bytearray(154);data[:2]=b'MZ';struct.pack_into('<I',data,60,128)
    data[128:132]=b'PE\0\0';struct.pack_into('<H',data,132,0x8664);struct.pack_into('<H',data,152,0x20b)
    return bytes(data)


def gui_fixture(root):
    json_file(root/'release.json',{'application':'Proxmark Studio','version':'0.3.0-preview.1',
                                 'platform':'windows-x64','bundle_kind':'gui-only','native_client_sha256':None})
    write(root/'ProxmarkStudio.exe',pe64());write(root/'_internal/python312.dll')
    for name in GUI_ASSETS:write(root/'_internal/pm3gui'/name)
    for name in ('README.md','README.en.md','LICENSE','THIRD_PARTY_NOTICES.md','docs/USER_GUIDE_RU.md','docs/USER_GUIDE_EN.md'):
        write(root/name,'Public fixture documentation\n')
    for name in ('GPL-3.0-only.txt','LGPL-3.0-only.txt'):write(root/'licenses/gui'/name)
    source=root/'sources/gui/example-source.tar.xz';write(source)
    notice=root/'licenses/gui/example/LICENSE';write(notice)
    json_file(root/'licenses/gui/SOURCE_INVENTORY.json',{'components':[{'id':'example',
        'source_path':'sources/gui/example-source.tar.xz','sha256':digest(source),'license_directory':'licenses/gui/example',
        'notice_files':[{'path':'LICENSE','sha256':digest(notice)}]}]})
    with zipfile.ZipFile(root/'sources/proxmark-studio-source.zip','w') as archive:
        for name in ('LICENSE','README.md','README.en.md','run_gui.py','pm3gui/app.py',
                     'tools/build_release.py','tools/release_gui_sources.json','docs/licenses/proxmark3-LICENSE.txt'):
            archive.writestr('proxmark-studio/'+name,'public fixture\n')


def checksum_manifest(root):
    json_file(root/'files.sha256.json',{p.relative_to(root).as_posix():digest(p)
         for p in root.rglob('*') if p.is_file() and p.name!='files.sha256.json'})


class VerifyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()/'Application';self.root.mkdir();gui_fixture(self.root)

    def test_gui_only_release_with_sources_and_complete_checksums_passes(self):
        with zipfile.ZipFile(self.root/'sources/proxmark-studio-source.zip','a') as source:
            source.writestr('proxmark-studio/.github/ISSUE_TEMPLATE/bug_report.md','public issue template')
            source.writestr('proxmark-studio/SOURCE_SNAPSHOT.json','{}')
            source.writestr('proxmark-studio/tools/native-client-tests/production_usb_control_test.cpp','// public test fixture')
            source.writestr('proxmark-studio/tools/client-patches/USB_HARDNESTED.md','Public build recipe')
            source.writestr('proxmark-studio/tools/client-patches/usb-hardnested-sources.json','{}')
        checksum_manifest(self.root)
        result=verify_release(self.root)
        self.assertTrue(result['passed'],result['errors']);self.assertFalse(result['hardware_access'])
        self.assertEqual(result['warnings'],[])
        self.assertFalse((self.root/'settings.json').exists())

    def test_missing_sources_and_manifest_tampering_are_not_silent_success(self):
        checksum_manifest(self.root);write(self.root/'README.md','Changed after manifest')
        result=verify_release(self.root)
        self.assertFalse(result['passed']);self.assertTrue(any('changed file' in e for e in result['errors']))
        checksum_manifest(self.root);write(self.root/'undocumented-extra.txt')
        self.assertTrue(any('coverage differs' in e for e in verify_release(self.root)['errors']))
        (self.root/'undocumented-extra.txt').unlink();(self.root/'sources/gui/example-source.tar.xz').unlink()
        result=verify_release(self.root)
        self.assertFalse(result['passed']);self.assertTrue(any('gui-runtime-licenses-and-sources' in e for e in result['errors']))

    def test_rejects_private_state_build_machine_metadata_and_foreign_system_dll(self):
        write(self.root/'settings.json','{}')
        result=verify_release(self.root)
        self.assertTrue(any('Private/development artifact' in e for e in result['errors']))
        (self.root/'settings.json').unlink()
        value=json.loads((self.root/'release.json').read_text());value['build_root']=r'C:\Users\Alice\project'
        json_file(self.root/'release.json',value)
        write(self.root/'_internal/ucrtbase.dll')
        result=verify_release(self.root)
        self.assertTrue(any('build-machine path' in e for e in result['errors']))
        self.assertTrue(any('system DLL' in e for e in result['errors']))

    def test_source_snapshot_excludes_internal_tests_and_private_archives(self):
        with zipfile.ZipFile(self.root/'sources/proxmark-studio-source.zip','a') as archive:
            archive.writestr('proxmark-studio/docs/IPHONE_CHECKPOINT_RU.md','private checkpoint')
        result=verify_release(self.root)
        self.assertTrue(any('unreviewed path' in e for e in result['errors']))

    def test_complete_bundle_cannot_pass_as_gui_only_without_native_components(self):
        value=json.loads((self.root/'release.json').read_text());value['bundle_kind']='complete'
        json_file(self.root/'release.json',value)
        result=verify_release(self.root)
        self.assertFalse(result['passed'])
        self.assertTrue(any('native-runtime-resources-and-notices' in e for e in result['errors']))
        self.assertTrue(any('portable-firmware-profiles' in e for e in result['errors']))

    def test_reviewed_patch_metadata_does_not_allow_other_lab_json(self):
        with zipfile.ZipFile(self.root/'sources/proxmark-studio-source.zip','a') as source:
            source.writestr('proxmark-studio/tools/client-patches/private-lab.json','{}')
        self.assertTrue(any('unreviewed path' in e for e in verify_release(self.root)['errors']))

    def test_relative_firmware_paths_may_reach_sibling_client_but_cannot_escape_bundle(self):
        self.assertEqual(_inside(self.root,self.root/'firmware','../client/proxmark3.exe'),self.root/'client/proxmark3.exe')
        for relative in ('../../outside.bin','C:/Users/Alice/outside.bin','/tmp/outside.bin'):
            with self.subTest(relative=relative),self.assertRaises(ValueError):_inside(self.root,self.root/'firmware',relative)

    def test_elf_validator_rejects_bootloader_range_even_if_sha_would_match(self):
        data=bytearray(88);data[:7]=b'\x7fELF\x01\x01\x01'
        struct.pack_into('<HH',data,16,2,40);struct.pack_into('<II',data,24,0x102001,52)
        struct.pack_into('<HHH',data,40,52,32,1);struct.pack_into('<8I',data,52,1,84,0x102000,0x102000,4,4,5,4)
        image=self.root/'safe.elf';write(image,bytes(data));_elf(image)
        struct.pack_into('<I',data,64,0x100000);write(image,bytes(data))
        with self.assertRaisesRegex(ValueError,'bootloader'):_elf(image)


if __name__=='__main__':unittest.main()
