from datetime import date
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class DateRangeSlider(QWidget):
    rangeChanged = Signal(int, int)
    rangeCommitted = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.minimum = date(2002, 1, 1).toordinal()
        self.maximum = date(2003, 12, 31).toordinal()
        self.lower, self.upper = self.minimum, self.maximum
        self.drag = None
        self.active = "lower"
        self.histogram = []
        self.setMinimumHeight(62)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip("Перетащите края периода или выделенную полосу целиком. Стрелки меняют границу на день; Shift + стрелки сдвигают весь период.")
        self.setAccessibleName("Диапазон дат съёмки")

    def sizeHint(self):
        return QSize(220, 66)

    def x_for(self, value):
        return 12 + (self.width() - 24) * (value - self.minimum) / max(1, self.maximum - self.minimum)

    def value_for(self, x):
        value = self.minimum + (x - 12) * (self.maximum - self.minimum) / max(1, self.width() - 24)
        return max(self.minimum, min(self.maximum, round(value)))

    def setBounds(self, minimum, maximum):
        full = self.lower == self.minimum and self.upper == self.maximum
        self.minimum, self.maximum = minimum, max(minimum, maximum)
        if full:
            self.setRange(minimum, maximum)
        else:
            self.setRange(self.lower, self.upper)

    def setRange(self, lower, upper):
        lower = max(self.minimum, min(self.maximum, lower))
        upper = max(lower, min(self.maximum, upper))
        changed = (lower, upper) != (self.lower, self.upper)
        self.lower, self.upper = lower, upper
        self.update()
        if changed:
            self.rangeChanged.emit(lower, upper)

    def setHistogram(self, rows):
        self.histogram = [(date.fromisoformat(row['month']+'-01').toordinal(),row['count']) for row in rows if row.get('month')]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        largest = max((count for _,count in self.histogram),default=1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(23,124,112,85))
        for day,count in self.histogram:
            x = self.x_for(day)
            width = max(1,self.x_for(min(day+30,self.maximum))-x)
            height = 18*count/largest
            painter.drawRect(QRectF(x,19-height,width,height))
        y = 24
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#dce5e5"))
        painter.drawRoundedRect(QRectF(12, y-4, self.width()-24, 8), 4, 4)
        left, right = self.x_for(self.lower), self.x_for(self.upper)
        painter.setBrush(QColor("#177c70" if self.isEnabled() else "#aababa"))
        painter.drawRoundedRect(QRectF(left, y-4, max(1, right-left), 8), 4, 4)
        for name, x in (("lower", left), ("upper", right)):
            painter.setPen(QPen(QColor("#177c70"), 2 if self.hasFocus() and self.active == name else 1.5))
            painter.setBrush(QColor("white"))
            painter.drawEllipse(QPointF(x, y), 8, 8)
        painter.setPen(QColor("#7b898f"))
        painter.drawText(QRectF(6, 41, self.width()/2, 20), Qt.AlignLeft, str(date.fromordinal(self.minimum).year))
        painter.drawText(QRectF(self.width()/2, 41, self.width()/2-6, 20), Qt.AlignRight, str(date.fromordinal(self.maximum).year))

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.setFocus()
        x = event.position().x()
        left, right = self.x_for(self.lower), self.x_for(self.upper)
        if abs(x-left) <= 11 or abs(x-right) <= 11:
            self.drag = "lower" if abs(x-left) <= abs(x-right) else "upper"
            self.active = self.drag
        elif left < x < right:
            self.drag = "range"
        else:
            self.drag = "lower" if x < left else "upper"
            self.active = self.drag
        self.start_x = self.value_for(x)
        self.start_range = self.lower, self.upper
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event):
        if not self.drag:
            return
        value = self.value_for(event.position().x())
        if self.drag == "lower":
            self.setRange(min(value, self.upper), self.upper)
        elif self.drag == "upper":
            self.setRange(self.lower, max(value, self.lower))
        else:
            delta = max(self.minimum-self.start_range[0], min(self.maximum-self.start_range[1], value-self.start_x))
            self.setRange(self.start_range[0]+delta, self.start_range[1]+delta)

    def mouseReleaseEvent(self, event):
        if self.drag:
            self.drag = None
            self.rangeCommitted.emit()

    def keyPressEvent(self, event):
        if event.key() not in (Qt.Key_Left, Qt.Key_Right):
            return super().keyPressEvent(event)
        delta = -1 if event.key() == Qt.Key_Left else 1
        if event.modifiers() & Qt.ShiftModifier:
            delta = max(self.minimum-self.lower, min(self.maximum-self.upper, delta))
            self.setRange(self.lower+delta, self.upper+delta)
        elif self.active == "lower":
            self.setRange(min(self.upper, self.lower+delta), self.upper)
        else:
            self.setRange(self.lower, max(self.lower, self.upper+delta))
        self.rangeCommitted.emit()
