"""Small native Qt presentation helpers; no device access."""
from PySide6.QtCore import Qt, QEasingCurve, QPropertyAnimation, QVariantAnimation, QEvent
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QPushButton, QScrollArea,
                              QStackedWidget, QToolButton, QVBoxLayout, QWidget,
                              QStyle, QStyleOptionButton, QStylePainter, QApplication)


class SoftButton(QPushButton):
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tint=QColor(255,255,255,0)
        self._motion=QVariantAnimation(self)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._motion.valueChanged.connect(self._tint_changed)
        self.pressed.connect(lambda:self._animate_tint(True))
        self.released.connect(lambda:self._animate_tint(False))

    def _tint_changed(self,value):
        self._tint=value;self.update()

    def _animate_tint(self,pressed=False):
        target=(QColor(0,0,0,34) if pressed else QColor(255,255,255,20) if self.underMouse() else QColor(255,255,255,0))
        self._motion.stop()
        if not self.isEnabled() or not self.isVisible():
            self._tint_changed(QColor(255,255,255,0));return
        app=QApplication.instance()
        if app is not None and app.property('reducedMotion'):
            self._tint_changed(target);return
        self._motion.setDuration(65 if pressed else 120)
        self._motion.setStartValue(self._tint);self._motion.setEndValue(target);self._motion.start()

    def enterEvent(self,event):
        super().enterEvent(event);self._animate_tint(self.isDown())

    def leaveEvent(self,event):
        super().leaveEvent(event);self._animate_tint(self.isDown())

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.Type.EnabledChange and hasattr(self,'_motion'):
            self._animate_tint(self.isDown())

    def paintEvent(self,event):
        option=QStyleOptionButton();self.initStyleOption(option)
        option.state &= ~(QStyle.StateFlag.State_MouseOver|QStyle.StateFlag.State_Sunken)
        painter=QStylePainter(self);painter.drawControl(QStyle.ControlElement.CE_PushButton,option)
        if self.isEnabled() and self._tint.alpha():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(self._tint)
            painter.drawRoundedRect(self.rect().adjusted(1,1,-1,-1),4,4)


class SmoothStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._fade = None
        self._fading_page = None

    def _clear_fade(self):
        if self._fade is not None:
            self._fade.stop()
            self._fade.deleteLater()
            self._fade = None
        if self._fading_page is not None:
            self._fading_page.setGraphicsEffect(None)
            self._fading_page = None

    def setCurrentIndex(self, index):
        changed = index != self.currentIndex()
        self._clear_fade()
        super().setCurrentIndex(index)
        page = self.currentWidget()
        if not changed or not self.isVisible() or page is None or QApplication.instance().property('reducedMotion'):
            return
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        self._fading_page = page
        self._fade = QPropertyAnimation(effect, b'opacity', self)
        self._fade.setDuration(140)
        self._fade.setStartValue(.45)
        self._fade.setEndValue(1.)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._clear_fade)
        self._fade.start()


class SmoothScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll = QPropertyAnimation(self.verticalScrollBar(), b'value', self)
        self._scroll.setDuration(150)
        self._scroll.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.verticalScrollBar().sliderPressed.connect(self._scroll.stop)
        self.verticalScrollBar().actionTriggered.connect(self._scroll.stop)

    def wheelEvent(self, event):
        bar = self.verticalScrollBar()
        if (QApplication.instance().property('reducedMotion') or event.pixelDelta().y()
                or not event.angleDelta().y() or bar.maximum() == 0):
            super().wheelEvent(event)
            return
        target = self._scroll.endValue() if self._scroll.state() == QPropertyAnimation.State.Running else bar.value()
        target = max(bar.minimum(), min(bar.maximum(), int(target - event.angleDelta().y() / 120 * 90)))
        self._scroll.stop()
        self._scroll.setStartValue(bar.value())
        self._scroll.setEndValue(target)
        self._scroll.start()
        event.accept()


class Disclosure(QWidget):
    def __init__(self, title, parent=None, *, expanded=False):
        super().__init__(parent)
        self._expanded = expanded
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self.header = QToolButton()
        self.header.setObjectName('disclosure')
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        outer.addWidget(self.header)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 4, 0, 0)
        self.body.setSpacing(10)
        outer.addWidget(self.content)
        self.content.setVisible(expanded)
        self.content.setMaximumHeight(16777215 if expanded else 0)
        self._animation = QPropertyAnimation(self.content, b'maximumHeight', self)
        self._animation.setDuration(170)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._settled)
        self.header.toggled.connect(self.setExpanded)

    def setExpanded(self, expanded):
        expanded = bool(expanded)
        self._animation.stop()
        self._expanded = expanded
        blocked = self.header.blockSignals(True)
        self.header.setChecked(expanded)
        self.header.blockSignals(blocked)
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        if not self.isVisible() or QApplication.instance().property('reducedMotion'):
            self.content.setVisible(expanded)
            self.content.setMaximumHeight(16777215 if expanded else 0)
            return
        start = self.content.height() if self.content.isVisible() else 0
        if expanded:
            self.content.show()
        self._animation.setStartValue(start)
        self._animation.setEndValue(self.body.sizeHint().height() if expanded else 0)
        self._animation.start()

    def _settled(self):
        self.content.setVisible(self._expanded)
        self.content.setMaximumHeight(16777215 if self._expanded else 0)


def line_icon(name, color='#91a9c1'):
    """A consistent tiny icon set drawn as vectors at device pixel ratio2."""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.scale(2, 2)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if name == 'overview':
        for x, y in [(3,3),(11,3),(3,11),(11,11)]:
            painter.drawRoundedRect(x, y, 6, 6, 1, 1)
    elif name == 'check':
        painter.drawLine(4, 10, 8, 14); painter.drawLine(8, 14, 16, 5)
    elif name == 'cards':
        painter.drawRoundedRect(2, 4, 16, 12, 2, 2)
        painter.drawLine(3, 8, 17, 8)
        painter.drawLine(5, 12, 9, 12)
    elif name == 'encoding':
        painter.drawLine(7, 5, 3, 10); painter.drawLine(3, 10, 7, 15)
        painter.drawLine(13, 5, 17, 10); painter.drawLine(17, 10, 13, 15)
        painter.drawLine(11, 4, 9, 16)
    elif name == 'device':
        painter.drawRoundedRect(4, 2, 12, 16, 2, 2)
        painter.drawLine(7, 6, 13, 6); painter.drawEllipse(8, 11, 4, 4)
    elif name == 'keys':
        painter.drawEllipse(2, 3, 8, 8)
        painter.drawLine(9, 10, 17, 17); painter.drawLine(13, 13, 16, 10)
    else:
        painter.drawEllipse(8, 8, 4, 4)
        painter.drawArc(4, 4, 12, 12, -45*16, 90*16)
        painter.drawArc(0, 0, 20, 20, -45*16, 90*16)
        painter.drawLine(10, 12, 10, 18)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)
