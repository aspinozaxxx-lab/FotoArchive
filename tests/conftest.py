import os

# Render real Qt widgets without relying on interactive desktop focus or fonts.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QCoreApplication,Qt
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
