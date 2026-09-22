"""Pixel scrolling for the photo canvas, including ordinary notched wheels."""
from math import exp
from time import perf_counter

from PySide6.QtCore import Qt, QTimer, QSignalBlocker
from PySide6.QtWidgets import QListView


class PhotoGallery(QListView):
    WHEEL_STEP = 64.0
    RESPONSE_SECONDS = 0.075

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self._position = self._target = 0.0
        self._last_frame = self._last_wheel = 0.0
        self._direction = 0
        self._boost = 1.0
        self._setting_scroll = False
        self._append_range_floor = None
        self._animation = QTimer(self)
        self._animation.setTimerType(Qt.PreciseTimer)
        self._animation.setInterval(16)
        self._animation.timeout.connect(self._advance)
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._external_scroll)
        bar.sliderPressed.connect(self.stop_scroll)
        bar.actionTriggered.connect(self.stop_scroll)
        bar.rangeChanged.connect(self._range_changed)

    def setModel(self, model):
        self.stop_scroll()
        previous = self.model()
        if previous is not None:
            previous.modelAboutToBeReset.disconnect(self._reset_scroll)
            previous.rowsAboutToBeInserted.disconnect(self._rows_appending)
        super().setModel(model)
        if model is not None:
            model.modelAboutToBeReset.connect(self._reset_scroll)
            model.rowsAboutToBeInserted.connect(self._rows_appending)

    def _reset_scroll(self):
        self._append_range_floor = None
        self.stop_scroll()

    def _rows_appending(self, parent, first, last):
        if not parent.isValid() and first == self.model().rowCount() and first:
            self._append_range_floor = self.verticalScrollBar().maximum()

    def updateGeometries(self):
        floor = getattr(self, '_append_range_floor', None)
        if floor is None:
            return super().updateGeometries()
        # QListView restarts its batched layout when rows are appended. Its
        # first batch temporarily shrinks the scroll range, clamping a deep
        # viewport to the beginning. Keep the previous range until the layout
        # catches up, without exposing the temporary clamp to the view/user.
        bar = self.verticalScrollBar()
        minimum, maximum, value = bar.minimum(), bar.maximum(), bar.value()
        with QSignalBlocker(bar):
            super().updateGeometries()
            new_minimum, new_maximum = bar.minimum(), bar.maximum()
            bar.setRange(minimum, maximum)
            bar.setValue(value)
        if new_maximum >= floor:
            self._append_range_floor = None
        bar.setRange(new_minimum, max(floor, new_maximum))

    def stop_scroll(self):
        self._animation.stop()
        self._position = self._target = float(self.verticalScrollBar().value())
        self._last_wheel = 0.0
        self._direction = 0
        self._boost = 1.0

    def _external_scroll(self, value):
        # A drag, keyboard action or programmatic scroll immediately takes over.
        if not self._setting_scroll:
            self.stop_scroll()

    def _range_changed(self, minimum, maximum):
        self._target = min(maximum, max(minimum, self._target))
        self._position = min(maximum, max(minimum, self._position))

    def _set_position(self, value):
        self._setting_scroll = True
        try:
            self.verticalScrollBar().setValue(round(value))
        finally:
            self._setting_scroll = False

    def wheelEvent(self, event):
        pixels, angle = event.pixelDelta(), event.angleDelta()
        if event.modifiers() & Qt.ShiftModifier or abs(angle.x()) > abs(angle.y()):
            self.stop_scroll()
            return super().wheelEvent(event)
        if not pixels.isNull():
            # Touchpads already supply continuous pixels and their own momentum.
            self.stop_scroll()
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - pixels.y())
            event.accept()
            return
        if not angle.y():
            return super().wheelEvent(event)

        now = perf_counter()
        direction = -1 if angle.y() > 0 else 1
        gap = now - self._last_wheel
        current = self.verticalScrollBar().value()
        if direction != self._direction or not self._animation.isActive():
            # Reversing must never wait for the previous destination to be reached.
            self._position = self._target = float(current)
            self._boost = 1.0
        elif gap < 0.16:
            self._boost = min(3.5, self._boost + 0.6 * (1.0 - gap / 0.16))
        else:
            self._boost = 1.0

        steps = abs(angle.y()) / 120.0
        boost = min(3.5, max(self._boost, 1.0 + max(0.0, steps - 1.0) * 0.3))
        distance = direction * self.WHEEL_STEP * steps * boost
        # Bound the tail even if wheel messages arrive faster than frames paint.
        tail = max(256, self.viewport().height() * 1.5)
        target = min(current + tail, max(current - tail, self._target + distance))
        bar = self.verticalScrollBar()
        self._target = min(bar.maximum(), max(bar.minimum(), target))
        self._last_wheel, self._direction = now, direction
        if not self._animation.isActive() and self._target != current:
            self._last_frame = now
            self._animation.start()
        event.accept()

    def _advance(self):
        now = perf_counter()
        elapsed = min(0.05, now - self._last_frame)
        self._last_frame = now
        self._position += (self._target - self._position) * (1.0 - exp(-elapsed / self.RESPONSE_SECONDS))
        if abs(self._target - self._position) < 0.5:
            self._position = self._target
            self._animation.stop()
        self._set_position(self._position)

    def hideEvent(self, event):
        self.stop_scroll()
        super().hideEvent(event)
