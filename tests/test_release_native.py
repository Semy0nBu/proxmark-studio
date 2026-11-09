"""Release boundaries keep local captures out of native binary/source payloads."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from tools.release_native import _relative, build_native_source_archive
from tools.release_sources import source_specs


class NativeReleaseTests(unittest.TestCase):
    def test_source_archive_keeps_exact_modified_code_but_excludes_local_and_staged_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'native';root.mkdir()
            subprocess.run(['git','init','-q',str(root)],check=True)
            (root/'LICENSE.txt').write_text('Public fixture license\n')
            (root/'client/src').mkdir(parents=True)
            source=root/'client/src/comms.c';source.write_text('original\n')
            subprocess.run(['git','-C',str(root),'add','LICENSE.txt','client/src/comms.c'],check=True)
            subprocess.run(['git','-C',str(root),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                            '-c','commit.gpgSign=false','commit','-qm','fixture'],check=True)
            source.write_text('exact modified build source\n')
            (root/'client/src/comms_handshake.h').write_text('new compiled helper\n')
            (root/'client/src/local_notes.h').write_text('private note\n')
            (root/'client/resources').mkdir();(root/'client/resources/private-card.bin').write_bytes(b'private capture')
            subprocess.run(['git','-C',str(root),'add','client/resources/private-card.bin'],check=True)
            archive=Path(directory)/'source.zip'
            result=build_native_source_archive(root,archive)
            self.assertEqual(result['source_files'],3)
            with zipfile.ZipFile(archive) as data:
                self.assertEqual(data.read('proxmark3/client/src/comms.c'),source.read_bytes())
                self.assertIn('proxmark3/client/src/comms_handshake.h',data.namelist())
                self.assertFalse(any('private-card' in name or 'local_notes' in name for name in data.namelist()))
            with self.assertRaises(ValueError):build_native_source_archive(root,archive)

    def test_package_relative_paths_reject_traversal_absolute_drives_and_user_data(self):
        for value in ('../personal.keys','/tmp/private','C:/Users/name/file','libs/../../data/library.sqlite3',
                      'client/.proxmark3/log.txt','outputs/session.bin','client/libs/evil:stream'):
            with self.subTest(value=value),self.assertRaises(ValueError):_relative(value)
        self.assertEqual(str(_relative('platforms/qoffscreen.dll')),'platforms/qoffscreen.dll')

    def test_copyleft_sources_include_custom_x264_but_respect_gcc_exception(self):
        packages=[{'name':'a','licenses':['GPL-3.0-or-later'],'package_base':['mingw-w64-readline'],'version':'8.3.003-1'},
                  {'name':'b','licenses':['custom'],'package_base':['mingw-w64-x264'],'version':'1:0.165-3'},
                  {'name':'c','licenses':['GPL-3.0 WITH GCC-exception-3.1'],'package_base':['mingw-w64-gcc'],'version':'16.2-1'},
                  {'name':'d','licenses':['MIT'],'package_base':['mingw-w64-zlib'],'version':'1.4-2'}]
        specs=source_specs({'packages':packages})
        self.assertEqual({item['filename'] for item in specs},{'mingw-w64-readline-8.3.003-1.src.tar.zst',
                                                               'mingw-w64-x264-0.165-3.src.tar.zst'})
        packages[0]['package_base']=['../../private']
        with self.assertRaises(ValueError):source_specs({'packages':packages})


if __name__=='__main__':unittest.main()
