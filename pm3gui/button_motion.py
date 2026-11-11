"""Animated native button surfaces; activation remains a normal QPushButton."""
from PySide6.QtCore import QEvent, QEasingCurve, Qt, QVariantAnimation
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (QApplication, QPushButton, QStyle,
                              QStyleOptionButton, QStyleOptionFocusRect,
                              QStylePainter)


class SoftButton(QPushButton):
    """Blend actual styled backgrounds while drawing the current label once.

    The animation does not own input, change geometry or delay clicked. Keeping
    the label outside the cached surfaces also makes live captions and keyboard
    focus immediate, rather than leaving a faded copy of yesterday's text.
    """
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._from_surface = self._to_surface = None
        self._surface_size = None
        self._surface_dpr = None
        self._surface_signature = None
        self._progress = 1.
        self._motion = QVariantAnimation(self)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._motion.valueChanged.connect(self._frame_changed)
        self.pressed.connect(self._animate_surface)
        self.released.connect(self._animate_surface)
        self.toggled.connect(self._animate_surface)
        self.objectNameChanged.connect(self._reset_surface)

    def _option(self):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.state &= ~QStyle.StateFlag.State_MouseOver
        if self._hovered and self.isEnabled():
            option.state |= QStyle.StateFlag.State_MouseOver
        return option

    def _image(self):
        ratio = self.devicePixelRatioF()
        image = QImage(max(1, round(self.width()*ratio)),
                       max(1, round(self.height()*ratio)),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        return image

    def _signature(self):
        option = self._option()
        return (option.state.value, option.features.value)

    def _render_surface(self):
        image = self._image()
        painter = QPainter(image)
        self.style().drawControl(QStyle.ControlElement.CE_PushButtonBevel,
                                 self._option(), painter, self)
        painter.end()
        return image

    def _mixed_surface(self):
        if self._from_surface is None or self._progress >= 1.:
            return self._to_surface
        image = self._image()
        painter = QPainter(image)
        # Add weighted premultiplied pixels, including transparent nav surfaces.
        # Painting one opaque frame over the other would darken the midpoint.
        painter.setOpacity(1.-self._progress)
        painter.drawImage(0, 0, self._from_surface)
        painter.end()
        target = self._image()
        weight = QPainter(target)
        weight.setOpacity(self._progress)
        weight.drawImage(0, 0, self._to_surface)
        weight.end()
        painter = QPainter(image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        # Keep opacity at 1 for Plus: Qt's constant-opacity Plus fast path
        # otherwise applies SourceOver alpha and produces a bright midpoint.
        painter.drawImage(0, 0, target)
        painter.end()
        return image

    def _reduced_motion(self):
        app = QApplication.instance()
        return app is not None and bool(app.property('reducedMotion'))

    def _reset_surface(self, *_):
        if not hasattr(self, '_motion'):
            return
        self._motion.stop()
        self._progress = 1.
        self._from_surface = None
        self._to_surface = None
        self._surface_size = None
        self._surface_signature = None
        self.update()

    def _frame_changed(self, progress):
        self._progress = float(progress)
        self.update()

    def _animate_surface(self, *_):
        if not hasattr(self, '_motion'):
            return
        current = self._mixed_surface()
        self._motion.stop()
        target = self._render_surface()
        self._surface_size = self.size()
        self._surface_dpr = self.devicePixelRatioF()
        self._surface_signature = self._signature()
        self._to_surface = target
        if (not self.isEnabled() or not self.isVisible() or self._reduced_motion()
                or current is None or current == target):
            self._from_surface = None
            self._progress = 1.
            self.update()
            return
        self._from_surface = current
        self._progress = 0.
        self._motion.setDuration(80 if self.isDown() else 160)
        self._motion.setStartValue(0.)
        self._motion.setEndValue(1.)
        self._motion.start()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._hovered = True
        self._animate_surface()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hovered = False
        self._animate_surface()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._animate_surface()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._animate_surface()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.EnabledChange, QEvent.Type.StyleChange,
                            QEvent.Type.PaletteChange, QEvent.Type.FontChange,
                            QEvent.Type.LayoutDirectionChange}:
            self._reset_surface()

    def event(self, event):
        result = super().event(event)
        if event.type() in {QEvent.Type.DynamicPropertyChange,
                            QEvent.Type.DevicePixelRatioChange}:
            self._reset_surface()
        return result

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reset_surface()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._hovered = False
        self._reset_surface()

    def showEvent(self, event):
        super().showEvent(event)
        self._hovered = self.underMouse()
        self._reset_surface()

    def paintEvent(self, event):
        if (self._to_surface is None or self._surface_size != self.size()
                or self._surface_dpr != self.devicePixelRatioF()
                or self._reduced_motion()):
            self._motion.stop()
            self._from_surface = None
            self._progress = 1.
            self._surface_size = self.size()
            self._surface_dpr = self.devicePixelRatioF()
            self._surface_signature = self._signature()
            self._to_surface = self._render_surface()
        elif self._surface_signature != self._signature():
            # setDown/default/flat and window activation can alter native
            # appearance without pressed/toggled or a stylesheet event.
            self._animate_surface()
        option = self._option()
        painter = QStylePainter(self)
        painter.drawImage(0, 0, self._mixed_surface())
        painter.drawControl(QStyle.ControlElement.CE_PushButtonLabel, option)
        if option.state & QStyle.StateFlag.State_HasFocus:
            focus = QStyleOptionFocusRect()
            focus.initFrom(self)
            focus.rect = self.style().subElementRect(
                QStyle.SubElement.SE_PushButtonFocusRect, option, self)
            focus.backgroundColor = self.palette().button().color()
            painter.drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, focus)
