"""Offset decoration must never become card bytes or an editing operation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QKeySequence, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from pm3gui.core import parse_hex
from pm3gui.hex_view import WorkbenchHexEdit


class HexViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = WorkbenchHexEdit();self.view.resize(640, 300);self.view.show()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.view.close();self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete);self.app.processEvents()

    def settle(self):
        deadline = time.monotonic() + 3
        while self.view.offset_status == 'pending' and time.monotonic() < deadline:
            self.app.processEvents();time.sleep(.005)
        self.assertNotEqual(self.view.offset_status, 'pending')

    def test_offsets_for_four_and_sixteen_byte_lines_do_not_enter_plaintext(self):
        for count in (4, 16):
            with self.subTest(block_size=count):
                self.view.set_bytes_per_block(count)
                value='\n'.join(' '.join(f'{i:02X}' for i in range(j, j+count)) for j in range(0,count*4,count))
                self.view.setPlainText(value);self.settle()
                self.assertEqual(self.view.toPlainText(),value)
                self.assertEqual([self.view.byte_offset_for_block(i) for i in range(4)],list(range(0,count*4,count)))
                self.assertEqual(parse_hex(self.view.toPlainText()),bytes(range(count*4)))
                self.assertEqual(self.view.lineWrapMode(),QPlainTextEdit.LineWrapMode.NoWrap)

    def test_ascii_whitespace_and_nibble_crossing_follow_existing_parser(self):
        value=' AA\tBB  \n\nC\nC\nDD\vEE'
        self.view.setPlainText(value);self.settle()
        self.assertEqual(parse_hex(self.view.toPlainText()),bytes.fromhex('AABBCCDDEE'))
        self.assertEqual([self.view.byte_offset_for_block(i) for i in range(5)],[0,None,2,None,3])

    def test_invalid_hex_suppresses_every_address_not_only_the_bad_line(self):
        for value in ('00 01\nGG\n02 03','00 01\nA','00\nAA:BB','00\n??','00\nAA\u00a0BB'):
            with self.subTest(value=value):
                self.view.setPlainText(value)
                self.assertEqual(self.view.offset_status,'pending')
                self.assertIsNone(self.view.byte_offset_for_block(0))
                self.settle();self.assertEqual(self.view.offset_status,'invalid')
                self.assertTrue(all(self.view.byte_offset_for_block(i) is None for i in range(self.view.blockCount())))
                self.assertEqual(self.view.offset_label(0),'—')

    def test_paste_copy_and_undo_have_only_user_text(self):
        self.view.setPlainText('AA BB');self.view.document().setModified(False)
        self.view.moveCursor(QTextCursor.MoveOperation.End)
        self.app.clipboard().setText('\nCC DD')
        self.view.paste();self.settle()
        self.assertEqual(self.view.toPlainText(),'AA BB\nCC DD')
        self.assertTrue(self.view.document().isModified())
        self.view.selectAll();self.view.copy()
        self.assertEqual(self.app.clipboard().text(),'AA BB\nCC DD')
        self.view.undo();self.settle()
        self.assertEqual(self.view.toPlainText(),'AA BB');self.assertFalse(self.view.document().isModified())
        self.view.redo();self.settle();self.assertEqual(self.view.byte_offset_for_block(1),2)

    def test_paint_navigation_and_block_metadata_do_not_modify_document_or_undo(self):
        self.view.setPlainText('AA BB\nCC DD');self.settle();self.view.document().setModified(False)
        undo=self.view.document().availableUndoSteps();generation=self.view._generation
        with patch.object(self.view,'_scan_offsets',wraps=self.view._scan_offsets) as scan:
            for _ in range(3):
                self.view.gutter.repaint();self.view.grab();self.app.processEvents()
            scan.assert_not_called()
        self.view.moveCursor(QTextCursor.MoveOperation.End);self.view.set_bytes_per_block(4)
        self.assertFalse(self.view.document().isModified());self.assertEqual(self.view.document().availableUndoSteps(),undo)
        self.assertEqual(self.view._generation,generation);self.assertEqual(self.view.toPlainText(),'AA BB\nCC DD')

    def test_dirty_cache_never_shows_previous_snapshot_addresses(self):
        self.view.setPlainText('AA BB\nCC DD');self.settle()
        self.assertEqual(self.view.byte_offset_for_block(1),2)
        self.view.setPlainText('00\n11\n22')
        self.assertIsNone(self.view.byte_offset_for_block(1))
        self.view.setPlainText('00 11 22\n33 44 55')
        self.settle();self.assertEqual(self.view.byte_offset_for_block(1),3)

    def test_block_size_validation_does_not_change_bytes(self):
        self.view.setPlainText('AA')
        for value in (0,-1,65537,True,1.5,'16'):
            with self.subTest(value=value),self.assertRaises(ValueError):self.view.set_bytes_per_block(value)
        self.assertEqual(self.view.toPlainText(),'AA')

    def test_large_text_rebuild_yields_and_uses_only_final_generation(self):
        self.view.setPlainText(('00 01 02 03\n')*12000)
        self.assertEqual(self.view.offset_status,'pending')
        self.view._offset_timer.stop()
        self.view._scan_offsets()
        self.assertEqual(self.view.offset_status,'pending')
        self.assertGreater(len(self.view._pending_offsets),0)
        self.assertLess(len(self.view._pending_offsets),self.view.document().blockCount())
        self.view.setPlainText('AB CD\nEF 00')
        self.settle();self.assertEqual(self.view.byte_offset_for_block(1),2)
        self.assertEqual(self.view.document().blockCount(),2)

    def test_font_change_reflows_the_gutter_without_changing_the_snapshot(self):
        self.view.setPlainText('00 01\n02 03');self.settle()
        self.view.document().setModified(False)
        before=self.view.offset_gutter_width()
        font=self.view.font();font.setPointSize(20);self.view.setFont(font);self.app.processEvents()
        self.assertGreater(self.view.offset_gutter_width(),before)
        self.assertEqual(self.view.gutter.width(),self.view.offset_gutter_width())
        self.assertFalse(self.view.document().isModified());self.assertEqual(self.view.toPlainText(),'00 01\n02 03')

    def test_external_shared_document_is_tracked_without_editing_it(self):
        other=QPlainTextEdit();self.addCleanup(other.deleteLater)
        other.setPlainText('00 01\n02 03');other.document().setModified(False)
        self.view.setDocument(other.document());self.settle()
        self.assertEqual(self.view.byte_offset_for_block(1),2)
        self.assertFalse(other.document().isModified())
        other.moveCursor(QTextCursor.MoveOperation.End);other.insertPlainText('\n04 05')
        self.settle();self.assertEqual(self.view.byte_offset_for_block(2),4)


if __name__=='__main__':unittest.main()
