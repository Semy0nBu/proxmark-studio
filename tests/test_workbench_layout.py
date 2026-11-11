"""Native Workbench sizing uses synthetic cards and never opens a device."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pm3gui.app import MainWindow
from pm3gui.core import DumpData
from pm3gui.i18n import set_language
from pm3gui.theme import apply_theme


class WorkbenchLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pm3-workbench-layout-')
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.run_client = self.enterContext(patch('pm3gui.app.ClientRunner.run', side_effect=AssertionError('No device I/O')))
        self.run_operation = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation', side_effect=AssertionError('No device I/O')))
        self.window = MainWindow(Path(self.temp.name)/'profile', language='en')
        data = bytearray(1024)
        for sector in range(16):
            data[(sector*4+3)*16:(sector*4+4)*16] = bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')
        self.source = bytes(data)
        card = self.window.library.create_card('Synthetic Classic', DumpData(self.source, 'bin', {}, 16),
                                               protocol='MIFARE Classic 1K')
        self.window.load_card(card)
        self.window.navigate(1)
        self.scroll = self.window.detail_stack.widget(1)

    def tearDown(self):
        self.run_client.assert_not_called()
        self.run_operation.assert_not_called()
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()
        set_language('ru')

    def show(self, width, height):
        self.window.resize(width, height)
        self.window.show()
        QTest.qWait(70)

    def assert_no_editor_action_overlap(self):
        layout = self.window.workbench_editor_actions
        rects = [layout.itemAt(index).geometry() for index in range(layout.count())]
        for index, first in enumerate(rects):
            self.assertGreater(first.width(), 0)
            self.assertGreater(first.height(), 0)
            for second in rects[index+1:]:
                self.assertFalse(first.intersects(second))

    def test_desktop_editor_and_actions_fit_the_detail_viewport(self):
        self.show(1440, 960)
        self.assertEqual(self.window.card_tabs.currentIndex(), 2)
        self.assertFalse(self.window.card_tabs.hasHeightForWidth())
        self.assertEqual(self.scroll.verticalScrollBar().maximum(), 0)
        self.assertGreater(self.window.hex_edit.height(), 250)
        self.assertGreater(self.window.hex_edit.width(), 600)
        last = self.window.workbench_editor_actions.itemAt(3).widget()
        point = last.mapTo(self.scroll.viewport(), QPoint(0, last.height()))
        self.assertLessEqual(point.y(), self.scroll.viewport().height())
        self.assert_no_editor_action_overlap()

    def test_short_window_keeps_a_usable_editor_and_scrolls_remaining_actions(self):
        self.show(920, 640)
        self.assertGreaterEqual(self.window.workbench_splitter.height(), 190)
        self.assertGreaterEqual(self.window.hex_edit.height(), 120)
        self.assertLess(self.window.hex_edit.height(), 300)
        bar = self.scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        self.app.processEvents()
        last = self.window.workbench_editor_actions.itemAt(3).widget()
        top = last.mapTo(self.scroll.viewport(), QPoint(0, 0))
        bottom = last.mapTo(self.scroll.viewport(), QPoint(0, last.height()))
        self.assertGreaterEqual(top.y(), 0)
        self.assertLessEqual(bottom.y(), self.scroll.viewport().height())
        self.assert_no_editor_action_overlap()

    def test_resizing_back_reclaims_space_without_changing_the_working_copy(self):
        self.show(920, 640)
        small_height = self.window.hex_edit.height()
        self.show(1440, 960)
        self.assertGreater(self.window.hex_edit.height(), small_height+100)
        self.assertEqual(self.scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(self.window._working_dump().data, self.source)
        self.assertFalse(self.window.dirty)

    def test_hidden_tab_preferred_heights_do_not_inflate_the_editor(self):
        self.show(1440, 960)
        for index in (0, 1, 3, 2):
            self.window.card_tabs.setCurrentIndex(index)
            QTest.qWait(30)
            self.assertEqual(self.scroll.verticalScrollBar().maximum(), 0)
        self.assertLess(self.window.card_tabs.height(), self.scroll.viewport().height())
        self.assertFalse(self.window.dirty)

    def test_wrapped_action_rows_raise_the_minimum_instead_of_overlapping(self):
        self.show(920, 640)
        self.window.library_splitter.setSizes([340, 560])
        QTest.qWait(50)
        self.assert_no_editor_action_overlap()
        splitter = self.window.workbench_splitter
        last = self.window.workbench_editor_actions.itemAt(0).widget()
        editor_bottom = splitter.mapTo(self.window.card_tabs.currentWidget(), QPoint(0, splitter.height())).y()
        actions_top = last.mapTo(self.window.card_tabs.currentWidget(), QPoint(0, 0)).y()
        self.assertGreaterEqual(actions_top, editor_bottom)
        self.assertGreaterEqual(splitter.height(), 190)


if __name__ == '__main__':
    unittest.main()
