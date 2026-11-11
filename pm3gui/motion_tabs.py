"""Immediate native tab selection with a short, input-transparent transition."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QStackedWidget, QTabWidget
from shiboken6 import isValid

from .motion import PageTransition


class SmoothTabs(QTabWidget):
    """Keep Qt's selection/focus semantics; animate only the page viewport.

    Qt also changes tabs from C++ mouse and keyboard handlers. Listening to the
    public currentChanged signal, rather than only overriding its setter, covers
    those paths without delaying controller callbacks or moving live controls.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pages = self.findChild(QStackedWidget, 'qt_tabwidget_stackedwidget')
        if self._pages is None:
            raise RuntimeError('The native tab page stack is unavailable')
        self._transition = PageTransition(self._pages)
        self._previous_page = None
        self._previous_index = -1
        self._prepared = None
        self._capture_serial = 0
        self.currentChanged.connect(self._page_changed)
        self.installEventFilter(self)
        self.tabBar().installEventFilter(self)

    def _prepare_change(self):
        if self.currentWidget() is None:
            return
        self._capture_serial += 1
        serial = self._capture_serial
        self._prepared = (serial, self._transition.snapshot(self._pages))
        # Unrelated input (e.g. a key with no tab change) must not leave a stale
        # picture to be consumed by a later programmatic selection.
        QTimer.singleShot(0, lambda: self._discard_capture(serial))

    def _discard_capture(self, serial):
        if self._prepared is not None and self._prepared[0] == serial:
            self._prepared = None

    def setCurrentIndex(self, index):
        if 0 <= index < self.count() and index != self.currentIndex():
            self._prepare_change()
        super().setCurrentIndex(index)

    def setCurrentWidget(self, widget):
        index = self.indexOf(widget)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            super().setCurrentWidget(widget)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.Hide, QEvent.Type.Resize):
            self._prepared = None
        elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.Wheel):
            self._prepare_change()
        return super().eventFilter(watched, event)

    def _page_changed(self, index):
        previous, old_index = self._previous_page, self._previous_index
        current = self.currentWidget()
        self._previous_page, self._previous_index = current, index
        prepared, self._prepared = self._prepared, None
        if current is None or previous is current or previous is None or not isValid(previous):
            self._transition.clear()
            return
        # A direct tabBar().setCurrentIndex call bypasses our setters and input
        # filters. QWidget.grab can render the old, now-hidden page as a fallback.
        snapshot = prepared[1] if prepared is not None else self._transition.snapshot(previous)
        self._transition.start(snapshot, self._pages.rect(), direction=1 if index > old_index else -1)
