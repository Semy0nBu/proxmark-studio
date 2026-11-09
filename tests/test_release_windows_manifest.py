"""Bind a resource-only client copy without changing firmware or ARM metadata."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from tools.release_windows_manifest import SOURCE_MEMBERS, apply_native_utf8_manifest, utf8_manifest


def sha(data):return hashlib.sha256(data).hexdigest()
def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data if isinstance(data,bytes) else data.encode())
def store(path,data):write(path,json.dumps(data))


class WindowsManifestReleaseTests(unittest.TestCase):
    def test_utf8_addition_preserves_invoker_and_original_compatibility_xml(self):
        original=(b'<?xml version="1.0" encoding="UTF-8"?><assembly xmlns="urn:schemas-microsoft-com:asm.v1">'
                  b'<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>'
                  b'<requestedExecutionLevel level="asInvoker"/></requestedPrivileges></security></trustInfo>'
                  b'<compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application/></compatibility></assembly>')
        result=utf8_manifest(original);root=ET.fromstring(result)
        self.assertIn(b'<requestedExecutionLevel level="asInvoker"/>',result)
        self.assertIn(b'<compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application/></compatibility>',result)
        self.assertEqual(root.find('.//{http://schemas.microsoft.com/SMI/2019/WindowsSettings}activeCodePage').text,'UTF-8')
        with self.assertRaises(ValueError):utf8_manifest(result)

    def fixture(self,root):
        old=b'original-native-fixture';oldsha=sha(old);write(root/'client/proxmark3.exe',old)
        store(root/'native-manifest.json',{'client_sha256':oldsha,'files':[{'relative_target':'client/proxmark3.exe','sha256':oldsha,'size':len(old)}]})
        store(root/'client/libs/.pm3gui-runtime-manifest.json',{'client_sha256':oldsha,'files':[]})
        store(root/'release.json',{'native_client_sha256':oldsha})
        for name in ['gui-cancel','gui-blueshark']:
            entries={}
            for artifact in ['fullimage.elf','default_version_pm3.c','Makefile.platform']:
                body=(name+'/'+artifact).encode();relative=name+'/'+artifact;write(root/'firmware'/relative,body)
                entries[artifact]={'path':relative,'sha256':sha(body),'bytes':len(body)}
            entries['proxmark3.exe']={'path':'../client/proxmark3.exe','sha256':oldsha,'bytes':len(old)}
            store(root/'firmware'/(name+'-build-manifest.json'),{'artifact_files':entries})
        (root/'sources').mkdir()
        with zipfile.ZipFile(root/'sources/proxmark3-corresponding-source.zip','w') as archive:
            archive.writestr('proxmark3/client/src/comms.c','original exact code')
        return oldsha

    def test_staged_transform_updates_all_bindings_and_adds_preferred_source_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=self.fixture(root)
            before={p.relative_to(root).as_posix():p.read_bytes() for p in (root/'firmware').rglob('*') if p.is_file() and p.suffix!='.json'}
            def fake(path):
                body=b'resource-transformed-fixture';path.write_bytes(body)
                return {'kind':'fixture-resource-only','original_client_sha256':original,'client_sha256':sha(body)},b'<assembly/>'
            with patch('tools.release_windows_manifest.transform_client',side_effect=fake) as transform:
                result=apply_native_utf8_manifest(root)
                self.assertEqual(transform.call_count,1)
                for path in [root/'native-manifest.json',root/'client/libs/.pm3gui-runtime-manifest.json',root/'release.json']:
                    self.assertIn(result['client_sha256'],path.read_text())
                for path in (root/'firmware').glob('*-build-manifest.json'):
                    self.assertEqual(json.loads(path.read_text())['artifact_files']['proxmark3.exe']['sha256'],result['client_sha256'])
                self.assertEqual({p.relative_to(root).as_posix():p.read_bytes() for p in (root/'firmware').rglob('*') if p.is_file() and p.suffix!='.json'},before)
                with zipfile.ZipFile(root/'sources/proxmark3-corresponding-source.zip') as archive:
                    self.assertEqual(len(archive.namelist()),len(set(archive.namelist())))
                    self.assertTrue(all('proxmark3/'+name in archive.namelist() for name in SOURCE_MEMBERS))
                with self.assertRaises(ValueError):apply_native_utf8_manifest(root)
                self.assertEqual(transform.call_count,1)

    def test_wrong_original_hash_stops_before_resource_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture(root);write(root/'client/proxmark3.exe',b'unreviewed change')
            with patch('tools.release_windows_manifest.transform_client') as transform:
                with self.assertRaises(ValueError):apply_native_utf8_manifest(root)
                transform.assert_not_called()


if __name__=='__main__':unittest.main()
