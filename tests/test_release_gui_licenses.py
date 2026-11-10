"""GUI release notices are derived from verified source tarballs, without network."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import release_gui_licenses as legal


def archive(path, files, links=None):
    with tarfile.open(path, 'w:gz') as target:
        for name, data in files.items():
            data = data.encode() if isinstance(data, str) else data
            member = tarfile.TarInfo(name)
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
        for name, destination in (links or {}).items():
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = destination
            target.addfile(member)


class GuiLicenseReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache, self.release = self.root/'cache', self.root/'release'
        self.cache.mkdir();self.release.mkdir()
        self.runtime = {'python':'3.12.14','pyside':'6.12.0','qt':'6.12.0','packages':{
            key:{'version':version,'license':'fixture'} for key,version in (
                ('PySide6-Essentials','6.12.0'),('shiboken6','6.12.0'),
                ('pyserial','3.5'),('PyInstaller','6.22.3'),('pycryptodomex','3.24.2'))}}
        self.lock = {key:self.runtime[key] for key in ('python','pyside','qt')}
        self.lock.update(pyserial='3.5',pyinstaller='6.22.3',pycryptodomex='3.24.2',components=[])
        for name in ('qtbase','pyside-setup'):
            path = self.cache/(name+'.tar.gz')
            archive(path, {name+'/LICENSES/GPL-3.0-only.txt':'GPL fixture',
                           name+'/LICENSES/LGPL-3.0-only.txt':'LGPL fixture',
                           name+'/src/module.cpp':'Code stays inside its source archive.'})
            self.lock['components'].append({'id':name,'file':path.name,'version':'6.12.0',
                'sha256':legal.digest(path),'bytes':path.stat().st_size,
                'url':'https://example.invalid/'+path.name,'checksum_source':'https://example.invalid/checksum'})
        self.lock_path = self.root/'lock.json'
        self.lock_path.write_text(json.dumps(self.lock))
        self.enterContext(patch.object(legal,'LOCK',self.lock_path))
        self.enterContext(patch.object(legal,'probe_runtime',return_value=self.runtime))

    def stage(self):
        return legal.stage_gui_licenses(Path('release-python.exe'), self.release, self.cache)

    def test_stages_license_texts_and_exact_archives_without_private_paths_or_network(self):
        (self.release/'_internal').mkdir()
        (self.release/'_internal/Qt6Core.dll').write_bytes(b'MZ fixture')
        with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            result = self.stage()
        self.assertTrue(result['passed'])
        self.assertEqual(result['qt_dll_modules'], ['Core'])
        self.assertEqual((self.release/'licenses/gui/GPL-3.0-only.txt').read_text(),'GPL fixture')
        self.assertEqual((self.release/'licenses/gui/LGPL-3.0-only.txt').read_text(),'LGPL fixture')
        inventory = (self.release/'licenses/gui/SOURCE_INVENTORY.json').read_text()
        self.assertNotIn(str(self.root),inventory)
        self.assertNotIn('module.cpp',[p.name for p in (self.release/'licenses').rglob('*')])
        for component in self.lock['components']:
            self.assertEqual((self.release/'sources/gui'/component['file']).read_bytes(),
                             (self.cache/component['file']).read_bytes())

    def test_missing_or_changed_cached_source_fails_before_writing_release_notices(self):
        path = self.cache/self.lock['components'][0]['file']
        path.write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            self.stage()
        self.assertFalse((self.release/'licenses').exists())
        path.unlink()
        with self.assertRaisesRegex(ValueError,'Missing reviewed source archive'):
            self.stage()
        self.assertFalse((self.release/'licenses').exists())

    def test_version_or_module_without_reviewed_source_is_rejected(self):
        self.runtime['qt']='6.99.0'
        with self.assertRaisesRegex(ValueError,'qt version differs'):
            self.stage()
        self.runtime['qt']='6.12.0'
        (self.release/'Qt6WebEngineCore.dll').write_bytes(b'MZ extra module')
        with self.assertRaisesRegex(ValueError,'No reviewed corresponding source coverage'):
            self.stage()
        self.assertFalse((self.release/'licenses').exists())

    def test_attribution_referenced_notice_is_copied_but_code_and_links_are_not(self):
        path = self.cache/'attributions.tar.gz'
        archive(path, {'source/LICENSE':'Root notice',
            'source/src/lib/qt_attribution.json':json.dumps({'LicenseFile':'../legal/FTL.TXT'}),
            'source/src/legal/FTL.TXT':'Referenced third-party notice',
            'source/src/code.py':'print("not extracted")'},
            links={'source/LICENSE-link':'../../outside.txt'})
        target = self.root/'notices'
        legal.extract_notices(path,target)
        self.assertEqual((target/'src/legal/FTL.TXT').read_text(),'Referenced third-party notice')
        self.assertFalse((target/'src/code.py').exists())
        self.assertFalse((target/'LICENSE-link').exists())

    def test_archive_traversal_never_writes_outside_notice_directory(self):
        path = self.cache/'malicious.tar.gz'
        archive(path, {'source/../../outside/LICENSE':'escape'})
        with self.assertRaisesRegex(ValueError,'Unsafe source archive path'):
            legal.extract_notices(path,self.root/'notices')
        self.assertFalse((self.root/'outside').exists())

    def test_existing_legal_payload_is_never_overwritten(self):
        target=self.release/'licenses/gui';target.mkdir(parents=True)
        sentinel=target/'existing.txt';sentinel.write_text('preserve')
        with self.assertRaisesRegex(ValueError,'empty GUI license/source'):
            self.stage()
        self.assertEqual(sentinel.read_text(),'preserve')

    def test_wrong_network_content_cannot_enter_verified_source_cache(self):
        component = dict(self.lock['components'][0],file='new-source.tar.gz')
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'not the pinned source')):
            with self.assertRaisesRegex(ValueError,'Downloaded source checksum mismatch'):
                legal._cached_source(component,self.cache,True)
        self.assertFalse((self.cache/component['file']).exists())


if __name__ == '__main__':
    unittest.main()
