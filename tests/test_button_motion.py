"""Offscreen widget events verify immediate activation during button animation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from PySide6.QtCore import Qt, QAbstractAnimation
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from pm3gui.ui import SoftButton, SmoothScrollArea


class ButtonMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def test_press_and_release_activate_once_without_waiting_or_geometry_change(self):
        button=SoftButton('Test');button.resize(120,36);button.show();QTest.qWait(10)
        self.addCleanup(button.close)
        clicks=[];button.clicked.connect(lambda checked=False:clicks.append(checked));before=button.geometry()
        QTest.mousePress(button,Qt.MouseButton.LeftButton)
        self.assertTrue(button.isDown());self.assertEqual(clicks,[])
        self.assertEqual(button._motion.state(),QAbstractAnimation.State.Running)
        QTest.mouseRelease(button,Qt.MouseButton.LeftButton)
        self.assertEqual(len(clicks),1);self.assertEqual(button.geometry(),before)
        self.assertIsNone(button.graphicsEffect())

    def test_disable_during_animation_does_not_allow_another_activation(self):
        button=SoftButton('Test');button.show();self.addCleanup(button.close)
        clicks=[];button.clicked.connect(lambda checked=False:clicks.append(checked))
        QTest.mousePress(button,Qt.MouseButton.LeftButton);button.setEnabled(False)
        button.click()
        self.assertEqual(clicks,[])
        self.assertEqual(button._motion.state(),QAbstractAnimation.State.Stopped)
        disabled=button.grab().toImage()
        QTest.qWait(180)
        self.assertEqual(button.grab().toImage(),disabled)

    def test_reduced_motion_keeps_immediate_buttons(self):
        self.app.setProperty('reducedMotion',True);self.addCleanup(lambda:self.app.setProperty('reducedMotion',False))
        button=SoftButton('Test');button.show();self.addCleanup(button.close)
        clicks=[];button.clicked.connect(lambda checked=False:clicks.append(checked));button.click()
        self.assertEqual(len(clicks),1)
        self.assertEqual(button._motion.state(),QAbstractAnimation.State.Stopped)

    def test_manual_scroll_interrupts_smooth_scroll(self):
        area=SmoothScrollArea();body=QWidget();body.resize(300,2000);area.setWidget(body);area.resize(300,200);area.show()
        self.addCleanup(area.close)
        area._scroll.setStartValue(0);area._scroll.setEndValue(100);area._scroll.start()
        area.verticalScrollBar().sliderPressed.emit()
        self.assertEqual(area._scroll.state(),QAbstractAnimation.State.Stopped)
