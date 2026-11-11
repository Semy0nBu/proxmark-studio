import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from PySide6.QtCore import QPoint, QPointF, Qt, QAbstractAnimation
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidget
from pm3gui.scroll_motion import SmoothViewScroll


class ViewScrollMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.app.setProperty('reducedMotion', False)
        self.view = QListWidget()
        self.view.addItems([str(i) for i in range(100)])
        self.view.resize(250, 180)
        self.motion = SmoothViewScroll(self.view)
        self.view.show()
        QTest.qWait(10)
        self.addCleanup(self.view.close)
        self.addCleanup(lambda: self.app.setProperty('reducedMotion', False))

    def wheel(self, *, pixel=0, modifiers=Qt.KeyboardModifier.NoModifier):
        event = QWheelEvent(QPointF(30, 30), QPointF(30, 30), QPoint(0, pixel), QPoint(0, -120),
                            Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.view.viewport(), event)

    def test_wheel_has_intermediate_position_and_native_selection_is_immediate(self):
        self.wheel()
        self.assertEqual(self.motion.animation.state(), QAbstractAnimation.State.Running)
        self.motion.animation.setCurrentTime(60)
        value = self.motion.bar.value()
        self.assertGreater(value, 0)
        self.assertLess(value, 90)
        self.view.setCurrentRow(1)
        self.assertEqual(self.view.currentRow(), 1)
        self.motion.animation.setCurrentTime(150)
        self.assertEqual(self.motion.bar.value(), 90)

    def test_repeated_wheel_accumulates_and_scrollbar_interrupts(self):
        self.wheel(); self.motion.animation.setCurrentTime(50); self.wheel()
        self.assertEqual(self.motion.animation.endValue(), 180)
        self.motion.bar.sliderPressed.emit()
        self.assertEqual(self.motion.animation.state(), QAbstractAnimation.State.Stopped)

    def test_touchpad_reduced_motion_modified_wheel_and_keyboard_stop_the_tween(self):
        self.wheel(); self.wheel(pixel=-4)
        self.assertEqual(self.motion.animation.state(), QAbstractAnimation.State.Stopped)
        self.wheel(); self.wheel(modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.motion.animation.state(), QAbstractAnimation.State.Stopped)
        self.wheel(); QTest.keyClick(self.view, Qt.Key.Key_Down)
        self.assertEqual(self.motion.animation.state(), QAbstractAnimation.State.Stopped)
        self.app.setProperty('reducedMotion', True); self.wheel()
        self.assertEqual(self.motion.animation.state(), QAbstractAnimation.State.Stopped)


if __name__ == '__main__':
    unittest.main()
