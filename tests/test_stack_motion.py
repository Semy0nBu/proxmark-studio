"""Rendered transition frames and native page/input semantics, without hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QGraphicsOpacityEffect
from pm3gui.motion import SmoothStack


class StackMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.app.setProperty('reducedMotion', False)
        self.stack = SmoothStack()
        self.stack.resize(300, 180)
        self.pages = []
        for colour in ('#c82828', '#2848c8', '#28b850'):
            page = QWidget()
            page.setStyleSheet('background: '+colour+';')
            self.stack.addWidget(page)
            self.pages.append(page)
        self.stack.show()
        QTest.qWait(10)
        self.addCleanup(self.stack.close)
        self.addCleanup(lambda: self.app.setProperty('reducedMotion', False))

    def pixel(self):
        return self.stack.grab().toImage().pixelColor(100, 80)

    def test_real_frames_blend_and_final_pixels_are_exact_target(self):
        before = self.pixel()
        self.stack.setCurrentIndex(1)
        transition = self.stack._transition
        self.assertTrue(transition.running)
        self.assertEqual(self.stack.currentWidget(), self.pages[1])
        transition.animation.setCurrentTime(0)
        self.assertEqual(self.pixel(), before)
        transition.animation.setCurrentTime(80)
        middle = self.pixel()
        transition.animation.setCurrentTime(transition.DURATION_MS)
        after = self.pixel()
        self.assertNotEqual(middle, before)
        self.assertNotEqual(middle, after)
        self.assertGreater(middle.red(), after.red())
        self.assertGreater(middle.blue(), before.blue())
        self.assertEqual(after.name(), '#2848c8')
        self.assertFalse(transition.running)
        self.assertTrue(transition.overlay.pixmap.isNull())
        self.assertFalse(transition.overlay.isVisible())

    def test_target_signal_input_and_focus_are_immediate_during_animation(self):
        button = QPushButton('Action', self.pages[1])
        button.setGeometry(10, 10, 100, 30)
        changes, clicks = [], []
        self.stack.currentChanged.connect(changes.append)
        button.clicked.connect(lambda: clicks.append(True))
        self.stack.setCurrentIndex(1)
        self.assertEqual(changes, [1])
        self.assertTrue(self.stack._transition.running)
        self.assertTrue(self.stack._transition.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertEqual(self.stack._transition.overlay.focusPolicy(), Qt.FocusPolicy.NoFocus)
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual(clicks, [True])
        self.assertTrue(button.hasFocus())

    def test_rapid_retarget_uses_current_visible_composite(self):
        self.stack.setCurrentIndex(1)
        self.stack._transition.animation.setCurrentTime(60)
        before = self.pixel()
        self.stack.setCurrentWidget(self.pages[2])
        self.stack._transition.animation.setCurrentTime(0)
        self.assertEqual(self.stack.currentIndex(), 2)
        self.assertEqual(self.pixel(), before)
        self.assertEqual(len(self.stack.findChildren(QWidget, 'pageTransitionOverlay')), 1)
        self.stack._transition.animation.setCurrentTime(220)
        self.assertEqual(self.pixel().name(), '#28b850')

    def test_resize_hide_and_reduced_motion_remove_old_pixels(self):
        self.stack.setCurrentIndex(1)
        self.stack.resize(310, 190)
        self.assertFalse(self.stack._transition.running)
        self.assertTrue(self.stack._transition.overlay.pixmap.isNull())
        self.stack.setCurrentIndex(2)
        self.stack.hide()
        self.assertFalse(self.stack._transition.running)
        self.stack.show()
        self.stack.setCurrentIndex(0)
        self.app.setProperty('reducedMotion', True)
        self.assertFalse(self.stack._transition.running)
        self.assertEqual(self.stack.currentIndex(), 0)
        self.stack.setCurrentIndex(1)
        self.assertFalse(self.stack._transition.running)
        self.assertEqual(self.pixel().name(), '#2848c8')

    def test_existing_page_effect_and_geometry_are_preserved(self):
        effect = QGraphicsOpacityEffect(self.pages[1])
        effect.setOpacity(.9)
        self.pages[1].setGraphicsEffect(effect)
        self.stack.setCurrentIndex(1)
        geometry = self.pages[1].geometry()
        self.stack._transition.animation.setCurrentTime(100)
        self.assertIs(self.pages[1].graphicsEffect(), effect)
        self.assertEqual(self.pages[1].geometry(), geometry)
        self.stack._transition.animation.setCurrentTime(220)
        self.assertIs(self.pages[1].graphicsEffect(), effect)

    def test_hidden_navigation_noop_and_invalid_index_do_not_allocate_pixels(self):
        self.stack.hide()
        self.stack.setCurrentIndex(1)
        self.assertFalse(self.stack._transition.running)
        self.assertTrue(self.stack._transition.overlay.pixmap.isNull())
        self.stack.show()
        self.stack.setCurrentIndex(1)
        self.stack.setCurrentIndex(-3)
        self.assertEqual(self.stack.currentIndex(), 1)
        self.assertFalse(self.stack._transition.running)

    def test_removing_current_page_keeps_native_stack_semantics(self):
        self.stack.setCurrentIndex(1)
        self.stack.removeWidget(self.pages[1])
        self.assertEqual(self.stack.currentWidget(), self.pages[2])
        self.assertEqual(self.stack.count(), 2)
        self.stack._transition.clear()


if __name__ == '__main__':
    unittest.main()
