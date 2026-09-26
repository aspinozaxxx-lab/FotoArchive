"""A report drawer that requests its content height and scrolls only if needed."""
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QSizePolicy


class ProcessingDrawer(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }')

    def setWidget(self, widget):
        super().setWidget(widget)
        widget.installEventFilter(self)

    def sizeHint(self):
        hint = super().sizeHint()
        content = self.widget()
        if content is None:
            return hint
        layout = content.layout()
        width = max(1, self.width() - 2 * self.frameWidth())
        height = layout.totalHeightForWidth(width) if layout and layout.hasHeightForWidth() else -1
        if height < 0:
            height = content.sizeHint().height()
        return QSize(hint.width(), height + 2 * self.frameWidth())

    def eventFilter(self, watched, event):
        if watched is self.widget() and event.type() == QEvent.LayoutRequest:
            # Telemetry, warnings and theme/font changes can alter the report.
            self.updateGeometry()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()
