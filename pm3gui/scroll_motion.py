"""Wheel easing for read-only lists/tables; native touchpad input stays native."""
from PySide6.QtCore import QObject, QEvent, Qt, QEasingCurve, QPropertyAnimation
from PySide6.QtWidgets import QAbstractItemView
from .motion import reduced_motion


class SmoothViewScroll(QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.bar = view.verticalScrollBar()
        self.animation = QPropertyAnimation(self.bar, b'value', self)
        self.animation.setDuration(150)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.bar.sliderPressed.connect(self.animation.stop)
        self.bar.actionTriggered.connect(self.animation.stop)
        self.bar.rangeChanged.connect(self.animation.stop)
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress):
            self.animation.stop()
        if kind != QEvent.Type.Wheel:
            return False
        if (reduced_motion() or event.pixelDelta().y() or not event.angleDelta().y()
                or event.modifiers() != Qt.KeyboardModifier.NoModifier
                or event.buttons() != Qt.MouseButton.NoButton or self.bar.maximum() == 0):
            self.animation.stop()
            return False
        target = self.animation.endValue() if self.animation.state() == QPropertyAnimation.State.Running else self.bar.value()
        target = max(self.bar.minimum(), min(self.bar.maximum(), round(target-event.angleDelta().y()/120*90)))
        self.animation.stop()
        self.animation.setStartValue(self.bar.value())
        self.animation.setEndValue(target)
        self.animation.start()
        event.accept()
        return True
