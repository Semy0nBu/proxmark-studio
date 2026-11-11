"""Small native Qt presentation helpers; no device access."""
from PySide6.QtCore import Qt, QEasingCurve, QPropertyAnimation
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QScrollArea, QToolButton, QVBoxLayout, QWidget, QApplication
from .button_motion import SoftButton
from .motion import SmoothStack


class SmoothScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll = QPropertyAnimation(self.verticalScrollBar(), b'value', self)
        self._scroll.setDuration(150)
        self._scroll.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.verticalScrollBar().sliderPressed.connect(self._scroll.stop)
        self.verticalScrollBar().actionTriggered.connect(self._scroll.stop)

    def hideEvent(self, event):
        self._scroll.stop()
        super().hideEvent(event)

    def wheelEvent(self, event):
        bar = self.verticalScrollBar()
        if (QApplication.instance().property('reducedMotion') or event.pixelDelta().y()
                or not event.angleDelta().y() or bar.maximum() == 0):
            # A native touchpad gesture must not fight a pending wheel tween.
            self._scroll.stop()
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
