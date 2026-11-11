"""Short, interruptible presentation transitions. No device/controller work.

The real target page changes immediately. Only an input-transparent picture of
the outgoing view fades away; editors keep their native painting and focus.
"""
import weakref
from PySide6.QtCore import QObject, QEvent, QRect, Qt, QEasingCurve, QVariantAnimation
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget


def reduced_motion():
    app = QApplication.instance()
    return bool(app is not None and app.property('reducedMotion'))


class _MotionPolicy(QObject):
    """One app filter; closed pages must not leave global event filters behind."""
    def __init__(self, app):
        super().__init__(app)
        self.transitions = weakref.WeakSet()
        app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.DynamicPropertyChange
                and event.propertyName() == b'reducedMotion' and reduced_motion()):
            for transition in tuple(self.transitions):
                try:
                    transition.clear()
                except RuntimeError:
                    self.transitions.discard(transition)
        return False


class _PageOverlay(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('pageTransitionOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet('background: transparent; border: 0;')
        self.pixmap = QPixmap()
        self.progress = 0.
        self.direction = 0
        self.hide()

    def paintEvent(self, event):
        if self.pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setOpacity(1. - self.progress)
        painter.translate(-self.direction * 12 * self.progress, 0)
        painter.drawPixmap(self.rect(), self.pixmap)


class PageTransition(QObject):
    """Own at most one short-lived overlay; release its pixels on completion."""
    DURATION_MS = 220

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.overlay = _PageOverlay(host)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(self.DURATION_MS)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.clear)
        host.installEventFilter(self)
        app = QApplication.instance()
        if app is not None:
            policy = getattr(app, '_studioMotionPolicy', None)
            if policy is None:
                policy = app._studioMotionPolicy = _MotionPolicy(app)
            policy.transitions.add(self)

    @property
    def running(self):
        return self.animation.state() == QVariantAnimation.State.Running

    def snapshot(self, widget):
        if widget is None or not self.host.isVisible() or reduced_motion():
            return QPixmap()
        try:
            if widget.width() <= 0 or widget.height() <= 0:
                return QPixmap()
            return widget.grab()
        except RuntimeError:
            # A tab can be removed/deleted by the owner during currentChanged.
            return QPixmap()

    def start(self, snapshot, rect, *, direction=0):
        self.clear()
        if (snapshot is None or snapshot.isNull() or rect.isEmpty()
                or not self.host.isVisible() or reduced_motion()):
            return False
        self.overlay.setGeometry(QRect(rect))
        self.overlay.pixmap = snapshot
        self.overlay.progress = 0.
        self.overlay.direction = max(-1, min(1, direction))
        self.overlay.show()
        self.overlay.raise_()
        self.animation.setStartValue(0.)
        self.animation.setEndValue(1.)
        self.animation.start()
        return True

    def _frame(self, progress):
        self.overlay.progress = float(progress)
        self.overlay.update()

    def clear(self):
        self.animation.stop()
        self.overlay.hide()
        self.overlay.pixmap = QPixmap()
        self.host.update()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is getattr(self, 'host', None) and kind in (QEvent.Type.Resize, QEvent.Type.Hide, QEvent.Type.Close):
            self.clear()
        return False


class SmoothStack(QStackedWidget):
    """Preserve QStackedWidget semantics while transitioning its visible pixels."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._transition = PageTransition(self)
        self._previous_page = None
        self._previous_index = -1
        self._prepared = None
        self.currentChanged.connect(self._page_changed)

    def setCurrentIndex(self, index):
        if 0 <= index < self.count() and index != self.currentIndex():
            # Capture the actual composite during rapid successive navigation.
            view = self if self._transition.running else self.currentWidget()
            self._prepared = self._transition.snapshot(view)
        super().setCurrentIndex(index)

    def setCurrentWidget(self, widget):
        index = self.indexOf(widget)
        if index >= 0:
            self.setCurrentIndex(index)

    def _page_changed(self, index):
        page = self.currentWidget()
        previous = self._previous_page
        direction = 1 if index > self._previous_index else -1
        snapshot = self._prepared
        self._prepared = None
        self._previous_page, self._previous_index = page, index
        if snapshot is None:
            snapshot = self._transition.snapshot(previous)
        if page is None:
            self._transition.clear()
        else:
            self._transition.start(snapshot, page.geometry(), direction=direction)
