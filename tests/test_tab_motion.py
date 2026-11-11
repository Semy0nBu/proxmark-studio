"""Rendered native Qt tabs and real Workbench coverage, without device access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from pm3gui.motion_tabs import SmoothTabs


class TabMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.app.setProperty('reducedMotion', False)
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.app.setProperty('reducedMotion', False)

    def tabs(self, show=True):
        tabs = SmoothTabs()
        self.widgets.append(tabs)
        for color in ('#e02020', '#2040e0', '#20d040'):
            page = QWidget()
            page.setStyleSheet('background:'+color+';')
            tabs.addTab(page, color)
        tabs.resize(480, 300)
        if show:
            tabs.show()
            QTest.qWait(20)
        return tabs

    @staticmethod
    def pixel(tabs):
        image = tabs._pages.grab().toImage()
        return image.pixelColor(image.width()//2, image.height()//2)

    def test_actual_intermediate_frame_and_immediate_selection_signal(self):
        tabs = self.tabs()
        signals = []
        tabs.currentChanged.connect(signals.append)
        before = self.pixel(tabs)
        tabs.setCurrentIndex(1)
        self.assertEqual(tabs.currentIndex(), 1)
        self.assertEqual(signals, [1])
        self.assertTrue(tabs._transition.running)
        self.assertEqual(self.pixel(tabs), before)
        tabs._transition.animation.setCurrentTime(90)
        middle = self.pixel(tabs)
        self.assertNotEqual(middle, before)
        self.assertNotEqual(middle, QColor('#2040e0'))
        tabs._transition.animation.setCurrentTime(220)
        self.assertEqual(self.pixel(tabs), QColor('#2040e0'))
        self.assertFalse(tabs._transition.running)
        self.assertTrue(tabs._transition.overlay.pixmap.isNull())
        self.assertIsNone(tabs.currentWidget().graphicsEffect())

    def test_mouse_keyboard_and_direct_tabbar_selection_animate(self):
        tabs = self.tabs()
        QTest.mouseClick(tabs.tabBar(), Qt.MouseButton.LeftButton,
                         pos=tabs.tabBar().tabRect(1).center())
        self.assertEqual(tabs.currentIndex(), 1)
        self.assertTrue(tabs._transition.running)
        tabs._transition.clear()
        QTest.keyClick(tabs, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(tabs.currentIndex(), 2)
        self.assertTrue(tabs._transition.running)
        tabs._transition.clear()
        tabs.tabBar().setCurrentIndex(0)
        self.assertEqual(tabs.currentIndex(), 0)
        self.assertTrue(tabs._transition.running)

    def test_rapid_switch_uses_the_visible_composite_and_same_widget_is_noop(self):
        tabs = self.tabs()
        tabs.setCurrentIndex(1)
        tabs._transition.animation.setCurrentTime(75)
        visible = self.pixel(tabs)
        tabs.setCurrentWidget(tabs.widget(2))
        self.assertEqual(tabs.currentIndex(), 2)
        self.assertEqual(self.pixel(tabs), visible)
        animation = tabs._transition.animation
        animation.setCurrentTime(80)
        same_frame = self.pixel(tabs)
        tabs.setCurrentWidget(tabs.currentWidget())
        self.assertEqual(self.pixel(tabs), same_frame)
        self.assertEqual(animation.currentTime(), 80)

    def test_live_controls_receive_input_while_overlay_has_no_focus_or_effects(self):
        tabs = self.tabs()
        layout = QVBoxLayout(tabs.widget(1))
        button = QPushButton('Immediate')
        layout.addWidget(button)
        clicks = []
        button.clicked.connect(lambda: clicks.append(True))
        tabs.setCurrentIndex(1)
        button.setFocus()
        self.app.processEvents()
        self.assertTrue(tabs._transition.running)
        self.assertIs(tabs._pages.childAt(button.mapTo(tabs._pages, button.rect().center())), button)
        self.assertEqual(tabs._transition.overlay.focusPolicy(), Qt.FocusPolicy.NoFocus)
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual(clicks, [True])
        self.assertIsNone(button.graphicsEffect())

    def test_hidden_reduced_motion_resize_and_hide_never_leave_a_frozen_overlay(self):
        tabs = self.tabs(show=False)
        tabs.setCurrentIndex(1)
        self.assertFalse(tabs._transition.running)
        tabs.show()
        QTest.qWait(10)
        self.app.setProperty('reducedMotion', True)
        tabs.setCurrentIndex(2)
        self.assertFalse(tabs._transition.running)
        self.assertEqual(self.pixel(tabs), QColor('#20d040'))
        self.app.setProperty('reducedMotion', False)
        tabs.setCurrentIndex(0)
        self.assertTrue(tabs._transition.running)
        tabs.resize(520, 340)
        self.app.processEvents()
        self.assertFalse(tabs._transition.running)
        tabs.setCurrentIndex(1)
        self.assertTrue(tabs._transition.running)
        tabs.hide()
        self.assertFalse(tabs._transition.running)
        self.assertTrue(tabs._transition.overlay.pixmap.isNull())

    def test_removed_page_and_reentrant_selection_keep_the_final_native_target(self):
        tabs = self.tabs()
        changed = []
        def redirect(index):
            changed.append(index)
            if index == 1:
                tabs.setCurrentIndex(2)
        tabs.currentChanged.connect(redirect)
        tabs.setCurrentIndex(1)
        self.assertEqual(changed, [1, 2])
        self.assertEqual(tabs.currentIndex(), 2)
        self.assertTrue(tabs._transition.running)
        removed = tabs.currentWidget()
        tabs.removeTab(2)
        removed.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertIsNot(tabs.currentWidget(), removed)
        self.assertEqual(tabs.currentIndex(), 1)
        self.assertEqual(tabs.count(), 2)
        tabs._transition.animation.setCurrentTime(220)
        self.assertEqual(self.pixel(tabs), QColor('#2040e0'))
        self.assertFalse(tabs._transition.running)


class WorkbenchMotionCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_actual_workspace_card_and_studio_tabs_use_the_transition_without_io(self):
        from pm3gui.app import MainWindow
        from pm3gui.core import DumpData
        from pm3gui.motion import SmoothStack
        from pm3gui.i18n import set_language
        with tempfile.TemporaryDirectory(prefix='pm3-motion-coverage-') as directory, \
             patch('pm3gui.app.available_ports', return_value=[]), \
             patch('pm3gui.app.discover_client', return_value=''), \
             patch('pm3gui.app.ClientRunner.run', side_effect=AssertionError('No device command')) as run, \
             patch('pm3gui.app.ClientRunner.run_operation', side_effect=AssertionError('No device operation')) as operation:
            self.app.setProperty('reducedMotion', False)
            w = MainWindow(Path(directory)/'profile', language='en')
            try:
                self.assertIsInstance(w.workspace_bands, SmoothTabs)
                self.assertIsInstance(w.card_tabs, SmoothTabs)
                self.assertIsInstance(w.studio.classic_tabs, SmoothTabs)
                self.assertIsInstance(w.studio.pages, SmoothStack)
                w.resize(1440, 960)
                w.show()
                QTest.qWait(25)
                w.workspace_bands.setCurrentIndex(1)
                self.assertTrue(w.workspace_bands._transition.running)
                self.assertEqual(w.workspace_bands.currentIndex(), 1)
                card = w.library.create_card('Synthetic motion card',
                    DumpData(bytes(1024), 'bin', {}, 16), protocol='MIFARE Classic 1K')
                w.load_card(card)
                w.navigate(1)
                QTest.qWait(230)
                original = w.loaded_dump.data
                w.card_tabs.setCurrentIndex(0)
                self.assertTrue(w.card_tabs._transition.running)
                w.card_tabs._transition.animation.setCurrentTime(85)
                intermediate = w.card_tabs._pages.grab().toImage()
                w.card_tabs._transition.animation.setCurrentTime(220)
                settled = w.card_tabs._pages.grab().toImage()
                self.assertNotEqual(intermediate, settled)
                self.assertEqual(w.loaded_dump.data, original)
                w.navigate(5)
                QTest.qWait(230)
                w.studio.classic_tabs.setCurrentIndex(1)
                self.assertTrue(w.studio.classic_tabs._transition.running)
                self.assertEqual(w.studio.classic_tabs.currentIndex(), 1)
                run.assert_not_called()
                operation.assert_not_called()
            finally:
                w.dirty = False
                w.close()
                w.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
                set_language('ru')
