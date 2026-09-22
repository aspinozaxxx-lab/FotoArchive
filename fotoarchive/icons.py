"""Small vector controls, painted sharply at the current screen scale."""
from PySide6.QtCore import QRectF, QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPen, QPixmap, QPalette
from PySide6.QtWidgets import QApplication


def draw_symbol(painter,name,rect,color):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate(rect.x(),rect.y())
    painter.scale(rect.width()/24,rect.height()/24)
    painter.setPen(QPen(color,1.7,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    def line(x1,y1,x2,y2):
        painter.drawLine(QPointF(x1,y1),QPointF(x2,y2))
    if name in ('previous','next'):
        direction=-1 if name=='previous' else 1
        line(5,12,19,12)
        line(12+direction*7,12,12+direction*1,6)
        line(12+direction*7,12,12+direction*1,18)
    elif name=='fit':
        for x,y,sx,sy in ((3,3,1,1),(21,3,-1,1),(3,21,1,-1),(21,21,-1,-1)):
            line(x,y,x+sx*5,y);line(x,y,x,y+sy*5)
        painter.drawRect(QRectF(8,8,8,8))
    elif name=='actual':
        painter.drawEllipse(QRectF(2,2,16,16));line(16,16,22,22)
        font=painter.font();font.setPixelSize(8);font.setBold(True);painter.setFont(font)
        painter.drawText(QRectF(2,2,16,16),Qt.AlignCenter,'1:1')
    elif name=='original':
        line(13,3,21,3);line(21,3,21,11);line(21,3,11,13)
        line(8,5,3,5);line(3,5,3,21);line(3,21,19,21);line(19,21,19,16)
    elif name=='stack':
        painter.drawRoundedRect(QRectF(3,9,16,12),1.5,1.5)
        line(6,5,21,5);line(21,5,21,17);line(9,2,21,2)
    elif name=='collapse':
        line(5,9,12,16);line(12,16,19,9)
    painter.restore()


class SymbolEngine(QIconEngine):
    def __init__(self,name):
        super().__init__()
        self.name=name

    def clone(self):
        return SymbolEngine(self.name)

    def paint(self,painter,rect,mode,state):
        palette=QApplication.palette()
        color=palette.color(QPalette.Disabled if mode==QIcon.Disabled else QPalette.Active,QPalette.ButtonText)
        draw_symbol(painter,self.name,QRectF(rect).adjusted(1,1,-1,-1),color)

    def pixmap(self,size,mode,state):
        pixmap=QPixmap(size);pixmap.fill(Qt.transparent)
        painter=QPainter(pixmap)
        self.paint(painter,pixmap.rect(),mode,state)
        painter.end()
        return pixmap


def symbol_icon(name):
    return QIcon(SymbolEngine(name))
