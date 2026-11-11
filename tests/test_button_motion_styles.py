"""Real rendered frames must interpolate the existing QSS button appearances."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest

from PySide6.QtCore import QAbstractAnimation, QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QEnterEvent, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QLineEdit, QStyle, QStyleOptionButton, QWidget)
from pm3gui.theme import stylesheet
from pm3gui.ui import SoftButton


class ButtonStyleMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        old_sheet = self.app.styleSheet()
        old_motion = self.app.property('reducedMotion')
        self.addCleanup(self.app.setStyleSheet, old_sheet)
        self.addCleanup(self.app.setProperty, 'reducedMotion', old_motion)
        self.app.setStyleSheet(stylesheet())
        self.app.setProperty('reducedMotion', False)

    def button(self, name='', caption='', local_style=''):
        parent = QWidget()
        parent.resize(210, 80)
        button = SoftButton(caption, parent)
        button.setObjectName(name)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setGeometry(20, 12, 160, 42)
        if local_style:
            button.setStyleSheet(local_style)
        parent.show()
        self.app.processEvents()
        self.addCleanup(parent.close)
        self.app.sendEvent(button, QEvent(QEvent.Type.Leave))
        button._motion.setCurrentTime(button._motion.duration())
        self.app.processEvents()
        # Keep the Python parent wrapper alive until its cleanup as well.
        self.addCleanup(lambda parent=parent: None)
        return button

    def pixel(self, button, x=20, y=20):
        return button.parentWidget().grab().toImage().pixelColor(
            button.x()+x, button.y()+y)

    def enter(self, button):
        self.app.sendEvent(button, QEnterEvent(QPointF(20, 20), QPointF(20, 20), QPointF(40, 32)))
        self.assertEqual(button._motion.state(), QAbstractAnimation.State.Running)
        button._motion.pause()

    def finish(self, button):
        button._motion.setCurrentTime(button._motion.duration())
        self.app.processEvents()

    def assert_between(self, start, middle, end):
        self.assertNotEqual(middle, start)
        self.assertNotEqual(middle, end)
        for a, value, b in zip(start.getRgb()[:3], middle.getRgb()[:3], end.getRgb()[:3]):
            self.assertGreaterEqual(value, min(a, b)-1)
            self.assertLessEqual(value, max(a, b)+1)

    def test_hover_renders_real_normal_primary_nav_attack_and_custom_colors(self):
        cases = [('', '', '', '#343b47'),
                 ('primary', '', '', '#a0ccff'),
                 ('nav', '', '', '#2b2b2b'),
                 ('', 'attack', '', '#3b4d69'),
                 ('', '', 'QPushButton {background:#233c31;} QPushButton:hover {background:#42745b;}', '#42745b')]
        for name, role, local, expected in cases:
            with self.subTest(name=name, role=role, custom=bool(local)):
                button = self.button(name, local_style=local)
                if role:
                    button.setProperty('classicAction', role)
                    button.style().unpolish(button)
                    button.style().polish(button)
                    self.app.processEvents()
                idle = self.pixel(button)
                self.enter(button)
                button._motion.setCurrentTime(0)
                self.assertEqual(self.pixel(button), idle)
                frames = []
                for time in (20, 60, 110):
                    button._motion.setCurrentTime(time)
                    frames.append(self.pixel(button))
                self.finish(button)
                end = self.pixel(button)
                self.assertEqual(end.name(), expected)
                self.assert_between(idle, frames[1], end)
                self.assertGreater(len(set(color.rgba() for color in frames)), 1)

    def test_press_uses_native_pressed_style_and_release_click_is_immediate(self):
        for name, expected in [('', '#23272e'), ('primary', '#65a4e6')]:
            with self.subTest(name=name):
                button = self.button(name)
                self.enter(button)
                self.finish(button)
                clicks = []
                button.clicked.connect(lambda checked=False: clicks.append(checked))
                before = button.geometry()
                hover = self.pixel(button)
                QTest.mousePress(button, Qt.MouseButton.LeftButton)
                button._motion.pause()
                button._motion.setCurrentTime(30)
                middle = self.pixel(button)
                self.finish(button)
                pressed = self.pixel(button)
                self.assertEqual(pressed.name(), expected)
                self.assert_between(hover, middle, pressed)
                QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
                self.assertEqual(clicks, [False])
                self.assertEqual(button.geometry(), before)
                self.assertIsNone(button.graphicsEffect())

    def test_interrupted_hover_reverses_from_the_current_visible_frame(self):
        button = self.button()
        idle = self.pixel(button)
        self.enter(button)
        button._motion.setCurrentTime(60)
        before = self.pixel(button)
        self.app.sendEvent(button, QEvent(QEvent.Type.Leave))
        button._motion.pause()
        button._motion.setCurrentTime(0)
        self.assertEqual(self.pixel(button), before)
        self.finish(button)
        self.assertEqual(self.pixel(button), idle)

    def test_checked_nav_keeps_accent_underline_and_existing_checked_fill(self):
        button = self.button('nav')
        button.setCheckable(True)
        idle = self.pixel(button)
        button.setChecked(True)
        button._motion.pause()
        button._motion.setCurrentTime(60)
        mid = self.pixel(button)
        self.finish(button)
        checked = self.pixel(button)
        self.assertEqual(checked.name(), '#2b3039')
        self.assertEqual(self.pixel(button, 80, 41).name(), '#9abcf8')
        self.assert_between(idle, mid, checked)

    def test_style_and_size_changes_discard_stale_cached_surfaces(self):
        button = self.button()
        self.enter(button)
        button._motion.setCurrentTime(60)
        button.setStyleSheet('QPushButton {background:#404020;} QPushButton:hover {background:#6a6a30;}')
        self.app.processEvents()
        self.assertEqual(self.pixel(button).name(), '#6a6a30')
        self.assertEqual(button._motion.state(), QAbstractAnimation.State.Stopped)
        button.resize(180, 48)
        self.app.processEvents()
        self.assertEqual(self.pixel(button, 160, 35).name(), '#6a6a30')
        self.assertEqual(button.grab().size(), button.size())

    def native_frame(self, button, caption):
        image = QImage(button.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor('#191b1f'))
        option = QStyleOptionButton()
        button.initStyleOption(option)
        option.state |= QStyle.StateFlag.State_MouseOver
        option.text = caption
        painter = QPainter(image)
        button.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, button)
        painter.end()
        return image

    def bright_text_pixels(self, image):
        text_color = QColor('#e4e7ee').rgba()
        return {(x, y) for x in range(9, image.width()-9) for y in range(8, image.height()-8)
                if image.pixel(x, y) == text_color}

    def test_live_caption_is_drawn_once_without_faded_previous_letters(self):
        button = self.button(caption='BEFORE')
        self.enter(button)
        button._motion.setCurrentTime(60)
        button.setText('NOW')
        actual = self.bright_text_pixels(button.grab().toImage())
        expected = self.bright_text_pixels(self.native_frame(button, 'NOW'))
        previous = self.bright_text_pixels(self.native_frame(button, 'BEFORE'))
        self.assertTrue(expected)
        self.assertNotEqual(expected, previous)
        self.assertEqual(actual, expected)

    def test_keyboard_activation_and_disabled_appearance_stay_immediate(self):
        button = self.button('primary', caption='Stop')
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        clicks = []
        button.clicked.connect(lambda checked=False: clicks.append(checked))
        QTest.keyPress(button, Qt.Key.Key_Space)
        QTest.keyRelease(button, Qt.Key.Key_Space)
        self.assertEqual(clicks, [False])
        button.setEnabled(False)
        self.app.processEvents()
        self.assertEqual(self.pixel(button).name(), '#3a3a3a')
        self.assertEqual(button._motion.state(), QAbstractAnimation.State.Stopped)
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual(clicks, [False])

    def test_first_mouse_press_from_another_focus_control_keeps_visible_transition(self):
        button = self.button(caption='Stop')
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        parent = button.parentWidget()
        parent.resize(410, 80)
        field = QLineEdit(parent)
        field.setGeometry(208, 12, 180, 42)
        field.show()
        field.setFocus()
        self.app.processEvents()
        self.assertTrue(field.hasFocus())
        self.assertFalse(button.hasFocus())
        self.enter(button)
        self.finish(button)
        hover = self.pixel(button)
        geometry = button.geometry()
        clicks = []
        button.clicked.connect(lambda checked=False: clicks.append(checked))
        QTest.mousePress(button, Qt.MouseButton.LeftButton)
        self.assertTrue(button.hasFocus())
        self.assertTrue(button.isDown())
        self.assertEqual(button._motion.state(), QAbstractAnimation.State.Running)
        button._motion.pause()
        button._motion.setCurrentTime(0)
        self.assertEqual(self.pixel(button), hover)
        button._motion.setCurrentTime(30)
        middle = self.pixel(button)
        self.finish(button)
        pressed = self.pixel(button)
        self.assertEqual(pressed.name(), '#23272e')
        self.assert_between(hover, middle, pressed)
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
        self.assertEqual(clicks, [False])
        self.assertEqual(button.geometry(), geometry)
        self.assertTrue(button.hasFocus())

    def test_reduced_motion_and_hide_stop_active_frames(self):
        button = self.button('primary')
        self.enter(button)
        button._motion.setCurrentTime(20)
        self.app.setProperty('reducedMotion', True)
        self.assertEqual(self.pixel(button).name(), '#a0ccff')
        self.assertEqual(button._motion.state(), QAbstractAnimation.State.Stopped)
        self.app.setProperty('reducedMotion', False)
        self.app.sendEvent(button, QEvent(QEvent.Type.Leave))
        button.hide()
        self.assertEqual(button._motion.state(), QAbstractAnimation.State.Stopped)
        button.show()
        self.app.processEvents()
        self.assertEqual(button._motion.state(), QAbstractAnimation.State.Stopped)
        self.assertIsNone(button.graphicsEffect())


if __name__ == '__main__':
    unittest.main()
