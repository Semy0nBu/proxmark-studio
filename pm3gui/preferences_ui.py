"""Keyboard navigation for a compact preferences form hosted by a QMenu.

QMenu normally moves between actions; a QWidgetAction containing a form is one
action. Keep a local form focus chain so Tab reaches every control instead of
returning to the menu, without changing ordinary combo-box or button behavior.
"""
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMenu


class PreferencesMenu(QMenu):
    def __init__(self, button):
        super().__init__(button)
        self._button = button
        self._controls = ()
        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.timeout.connect(self._focus_first)
        self.aboutToShow.connect(lambda:self._focus_timer.start(0))
        self.aboutToHide.connect(self._focus_timer.stop)

    def set_focus_controls(self, controls):
        for control in self._controls:control.removeEventFilter(self)
        self._controls = tuple(controls)
        for control in self._controls:
            control.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            control.installEventFilter(self)

    def _available_controls(self):
        return [control for control in self._controls
                if control.isEnabled() and control.isVisibleTo(self)]

    def _focus_first(self):
        if self.isVisible():self._move_focus(False, initial=True)

    def _move_focus(self, backwards, *, initial=False):
        controls = self._available_controls()
        if not controls:return
        current = QApplication.focusWidget()
        if initial or current not in controls:
            index = len(controls)-1 if backwards else 0
        else:
            index = (controls.index(current) + (-1 if backwards else 1)) % len(controls)
        controls[index].setFocus(Qt.FocusReason.BacktabFocusReason if backwards else Qt.FocusReason.TabFocusReason)

    def _handle_key(self, event):
        if event.type() != QEvent.Type.KeyPress:return False
        # A combo's own open popup keeps its normal Escape and Tab handling.
        popup = QApplication.activePopupWidget()
        if popup is not None and popup is not self:return False
        key = event.key()
        if key in {Qt.Key.Key_Tab, Qt.Key.Key_Backtab}:
            backwards = key == Qt.Key.Key_Backtab or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._move_focus(backwards)
        elif key == Qt.Key.Key_Escape:
            self.hide()
            self._button.setFocus(Qt.FocusReason.PopupFocusReason)
        else:return False
        event.accept()
        return True

    def event(self, event):
        if self._handle_key(event):return True
        return super().event(event)

    def eventFilter(self, watched, event):
        if watched in self._controls and self._handle_key(event):return True
        return super().eventFilter(watched, event)
