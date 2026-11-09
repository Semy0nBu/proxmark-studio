"""Read-only key projection: original buffers, card versions and keys survive."""
from copy import deepcopy
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication
from pm3gui.app import MainWindow
from pm3gui.core import DumpData,parse_hex
from pm3gui.encoding_view import without_classic_keys,classic_view_positions,readable_preview
from test_writing import classic_image,UID


class EncodingProjectionTests(unittest.TestCase):
    def test_all_classic_sizes_hide_only_keys_including_large_4k_sectors(self):
        for size,sectors in ((320,5),(1024,16),(2048,32),(4096,40)):
            with self.subTest(size=size):
                data=bytes(i%251 for i in range(size));expected=bytearray()
                for block in range(size//16):
                    raw=data[block*16:(block+1)*16]
                    trailer=block%4==3 if block<128 else (block-128)%16==15
                    expected.extend(raw[6:10] if trailer else raw)
                view,count=without_classic_keys(data)
                self.assertEqual(view,bytes(expected));self.assertEqual(count,sectors)
                self.assertEqual(len(view),size-12*sectors);self.assertEqual(view[:16],data[:16])

    def test_wrong_geometry_and_known_foreign_family_are_rejected(self):
        for size,hints in ((16,{}),(1023,{}),(1024,{'block_size':4}),(1024,{'kind':'mfu'}),(1024,{'filetype':'ndef'})):
            with self.subTest(size=size,hints=hints),self.assertRaises(ValueError):without_classic_keys(bytes(size),**hints)

    def test_block_view_has_no_control_characters_and_collapses_zero_rows(self):
        data=b'Hello\x00\r\n\t\x1b'+bytes(54)+b'World'
        output,count=readable_preview(data,'cp1251')
        self.assertIn('0000  Hello·····',output);self.assertIn('0040  World',output)
        self.assertIn('[нулевые блоки:',output)
        self.assertFalse(any(ord(char)<32 and char!='\n' for char in output));self.assertEqual(count,3)

    def test_utf8_and_utf16_strings_cross_block_boundaries_with_original_addresses(self):
        for codec,prefix,text in (('utf-8',bytes(15),'Привет'),('utf-16-le',bytes(14),'A😀BC'),('utf-16-be',bytes(14),'A😀BC')):
            with self.subTest(codec=codec):
                data=prefix+text.encode(codec)+bytes(8)
                output,count=readable_preview(data,codec,strings_only=True)
                self.assertEqual(output,f'{len(prefix):04X}  {text}');self.assertEqual(count,1)

    def test_hidden_keys_break_text_runs_and_do_not_shift_original_addresses(self):
        data=bytearray(1024);data[46:48]=b'AB';data[48:54]=b'SECRET';data[54:58]=b'DATA';data[58:64]=b'SECRET';data[64:69]=b'Hello'
        positions,_=classic_view_positions(1024)
        output,count=readable_preview(bytes(data),'ascii',positions=positions,strings_only=True)
        self.assertEqual(output,'0036  DATA\n0040  Hello');self.assertEqual(count,2)
        self.assertNotIn('SECRET',output);self.assertNotIn('ABDATA',output)

    def test_invalid_sequences_do_not_become_text_or_hide_following_valid_strings(self):
        for codec,data,start in (('utf-8',b'\xffValid text',1),('ascii',b'\xffValid text',1),
                                 ('utf-16-le',b'\x00\xd8'+'Valid text'.encode('utf-16-le'),2)):
            with self.subTest(codec=codec):
                output,count=readable_preview(data,codec,strings_only=True)
                self.assertEqual(output,f'{start:04X}  Valid text');self.assertEqual(count,1)


class EncodingViewAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        for name,value in (('available_ports',[]),('discover_client','')):
            self.enterContext(patch('pm3gui.app.'+name,return_value=value))
        self.run=self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.operations=self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        self.window=MainWindow(Path(self.temp.name))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.window.dirty=False;self.window.close();self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents();self.temp.cleanup()

    def source(self):
        dump=DumpData(classic_image(),'bin',{'read_validation':{'kind':'mfc','known_key_slots':[{'sector':0,'type':'A','key_hex':'FFFFFFFFFFFF'}]}},16)
        cid=self.window.library.create_card('Read-only fixture',dump,uid=UID,protocol='MIFARE Classic 1K')
        self.window.load_card(cid);self.window.encoding.setCurrentIndex(self.window.encoding.findData('latin-1'))
        self.window.copy_to_converter();return cid

    def test_view_and_codec_changes_restore_exact_buffers_without_mutating_card_or_keys(self):
        window=self.window;cid=self.source()
        window.key_library.record_verified_keys(UID,[{'sector':0,'type':'A','key_hex':'FFFFFFFFFFFF'}])
        keys=window.key_library.list_card_keys(UID);original=deepcopy(window.loaded_dump)
        main_hex=window.hex_edit.toPlainText();raw=window.converter_hex.toPlainText()
        window.converter_text.setPlainText('Unsaved converter text');codec=window.converter_encoding.currentIndex()
        window.converter_hide_keys.setChecked(True)
        self.assertEqual(len(parse_hex(window.converter_hex.toPlainText())),832)
        self.assertTrue(window.converter_hex.isReadOnly());self.assertTrue(window.converter_text.isReadOnly())
        self.assertFalse(window.converter_encode_button.isEnabled())
        filtered=window.converter_hex.toPlainText();window.convert_text();self.assertEqual(window.converter_hex.toPlainText(),filtered)
        window.converter_encoding.setCurrentIndex(window.converter_encoding.findData('cp866'))
        window.converter_hide_keys.setChecked(False)
        self.assertEqual(window.converter_hex.toPlainText(),raw);self.assertEqual(window.converter_text.toPlainText(),'Unsaved converter text')
        self.assertEqual(window.converter_encoding.currentIndex(),codec);self.assertFalse(window.converter_hex.isReadOnly())
        self.assertEqual(window.hex_edit.toPlainText(),main_hex);self.assertFalse(window.dirty)
        self.assertEqual(window.loaded_dump,original);self.assertEqual(window.library.get_dump(cid),original)
        self.assertEqual(len(window.library.list_revisions(cid)),1);self.assertEqual(window.key_library.list_card_keys(UID),keys)
        self.run.assert_not_called();self.operations.assert_not_called()

    def test_invalid_view_keeps_existing_editors_and_unchecks_the_option(self):
        window=self.window;window.converter_hex.setPlainText('DE AD BE EF');window.converter_text.setPlainText('Keep me')
        window.converter_hide_keys.setChecked(True)
        self.assertFalse(window.converter_hide_keys.isChecked());self.assertIsNone(window.converter_key_view)
        self.assertEqual(window.converter_hex.toPlainText(),'DE AD BE EF');self.assertEqual(window.converter_text.toPlainText(),'Keep me')
        self.assertFalse(window.converter_hex.isReadOnly());self.assertEqual(window.converter_info.property('tone'),'error')

    def test_failed_decode_does_not_leave_unfiltered_text_visible_and_reload_restores_full_data(self):
        window=self.window;self.source();raw=window.converter_hex.toPlainText()
        window.converter_encoding.setCurrentIndex(window.converter_encoding.findData('utf-8'))
        window.converter_text.setPlainText('Old unfiltered key text');window.converter_hide_keys.setChecked(True)
        self.assertNotIn('Old unfiltered key text',window.converter_text.toPlainText());self.assertIn('0000',window.converter_text.toPlainText())
        self.assertEqual(window.converter_text_mode.currentData(),'blocks');self.assertTrue(window.converter_hex.isReadOnly())
        window.copy_to_converter()
        self.assertFalse(window.converter_hide_keys.isChecked());self.assertEqual(window.converter_hex.toPlainText(),raw)
        self.assertEqual(len(parse_hex(window.converter_hex.toPlainText())),1024);self.assertFalse(window.converter_text.isReadOnly())

    def test_readable_modes_are_readonly_and_strict_restores_unsaved_text(self):
        window=self.window;self.source();raw=window.converter_hex.toPlainText()
        window.converter_text.setPlainText('Unsaved strict editor');codec=window.converter_encoding.currentIndex()
        window.converter_text_mode.setCurrentIndex(window.converter_text_mode.findData('blocks'))
        self.assertTrue(window.converter_text.isReadOnly());self.assertFalse(window.converter_encode_button.isEnabled())
        self.assertIn('нулевые блоки',window.converter_text.toPlainText());self.assertEqual(window.converter_hex.toPlainText(),raw)
        window.converter_text_mode.setCurrentIndex(window.converter_text_mode.findData('strings'))
        self.assertTrue(window.converter_text.isReadOnly());self.assertEqual(window.converter_hex.toPlainText(),raw)
        window.converter_text_mode.setCurrentIndex(window.converter_text_mode.findData('strict'))
        self.assertEqual(window.converter_text.toPlainText(),'Unsaved strict editor');self.assertFalse(window.converter_text.isReadOnly())
        self.assertEqual(window.converter_encoding.currentIndex(),codec);self.assertFalse(window.dirty)

    def test_new_hex_in_a_readable_view_cannot_restore_old_unrelated_text(self):
        window=self.window;window.converter_hex.setPlainText('41 42 43 44');window.converter_text.setPlainText('Old editor')
        window.converter_text_mode.setCurrentIndex(window.converter_text_mode.findData('strings'))
        window.converter_hex.setPlainText('4E 45 57 21')
        self.assertIn('NEW!',window.converter_text.toPlainText())
        window.converter_text_mode.setCurrentIndex(window.converter_text_mode.findData('strict'))
        self.assertEqual(window.converter_text.toPlainText(),'NEW!')


if __name__=='__main__':unittest.main()
