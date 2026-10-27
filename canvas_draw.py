# -*- coding: utf-8 -*-
"""
canvas_draw.py  —  Interactive map tools for Mode 4 (Canvas Draw).

Small QgsMapTool subclasses that capture a polygon on the map canvas
and emit it as a QgsGeometry. Kept in their own file (like histogram.py)
so panel.py stays a single class with no Qt-canvas-tool logic mixed in.

  MQFreehandCaptureTool     — click-drag-release traces a freehand polygon
  MQLineSegmentCaptureTool  — "Polygon": click to place vertices,
                               double-click / Enter / right-click to close
  MQRectangleCaptureTool    — click-drag a rectangle

"Use current extent" needs no tool at all — the panel reads
iface.mapCanvas().extent() directly and builds a rectangle geometry.

Both tools:
  • show a live QgsRubberBand while drawing
  • emit `captured(QgsGeometry)` on success (geometry in canvas CRS)
  • emit `cancelled()` on Esc / right-click-with-no-vertices / a
    degenerate (zero-area) result
  • clear their rubber band on deactivate() so nothing is left behind
    if the user switches tools or closes the panel mid-draw
"""
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import QgsWkbTypes, QgsGeometry, QgsRectangle

_FILL   = QColor(220, 80, 20, 60)
_STROKE = QColor(220, 80, 20)


class MQFreehandCaptureTool(QgsMapTool):
    """Left-drag traces a freehand polygon; release closes it."""

    captured  = pyqtSignal(object)   # QgsGeometry
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self._band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._band.setColor(_FILL)
        self._band.setStrokeColor(_STROKE)
        self._band.setWidth(2)
        self._drawing = False
        self.setCursor(Qt.CrossCursor)

    def canvasPressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drawing = True
            self._band.reset(QgsWkbTypes.PolygonGeometry)
            self._band.addPoint(self.toMapCoordinates(e.pos()), True)
        elif e.button() == Qt.RightButton:
            self._abort()

    def canvasMoveEvent(self, e):
        if self._drawing:
            self._band.addPoint(self.toMapCoordinates(e.pos()), True)

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self._drawing:
            return
        self._drawing = False
        geom = self._band.asGeometry()
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        if geom and not geom.isEmpty() and geom.area() > 0:
            self.captured.emit(geom)
        else:
            self.cancelled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._abort()

    def _abort(self):
        self._drawing = False
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        self.cancelled.emit()

    def deactivate(self):
        self._drawing = False
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()


class MQLineSegmentCaptureTool(QgsMapTool):
    """Click to place each vertex; double-click / Enter / right-click closes
    the polygon. Backspace removes the last vertex."""

    captured  = pyqtSignal(object)   # QgsGeometry
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self._band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._band.setColor(_FILL)
        self._band.setStrokeColor(_STROKE)
        self._band.setWidth(2)
        self._points = []
        self.setCursor(Qt.CrossCursor)

    def canvasPressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._points.append(self.toMapCoordinates(e.pos()))
            self._band.addPoint(self._points[-1], True)
        elif e.button() == Qt.RightButton:
            self._finish()

    def canvasDoubleClickEvent(self, e):
        self._finish()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._abort()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish()
        elif e.key() == Qt.Key_Backspace and self._points:
            self._points.pop()
            self._band.reset(QgsWkbTypes.PolygonGeometry)
            for pt in self._points:
                self._band.addPoint(pt, True)

    def _finish(self):
        if len(self._points) < 3:
            self._abort()
            return
        geom = self._band.asGeometry()
        self._points = []
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        if geom and not geom.isEmpty() and geom.area() > 0:
            self.captured.emit(geom)
        else:
            self.cancelled.emit()

    def _abort(self):
        self._points = []
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        self.cancelled.emit()

    def deactivate(self):
        self._points = []
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()


class MQRectangleCaptureTool(QgsMapTool):
    """Click-drag a rectangle; release closes it. Esc / right-click cancels."""

    captured  = pyqtSignal(object)   # QgsGeometry
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self._band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._band.setColor(_FILL)
        self._band.setStrokeColor(_STROKE)
        self._band.setWidth(2)
        self._start = None
        self.setCursor(Qt.CrossCursor)

    def _rect(self, end):
        return QgsRectangle(self._start, end)

    def canvasPressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = self.toMapCoordinates(e.pos())
            self._band.reset(QgsWkbTypes.PolygonGeometry)
        elif e.button() == Qt.RightButton:
            self._abort()

    def canvasMoveEvent(self, e):
        if self._start is None:
            return
        rect = self._rect(self.toMapCoordinates(e.pos()))
        self._band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._start is None:
            return
        rect = self._rect(self.toMapCoordinates(e.pos()))
        self._start = None
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        if rect.width() > 0 and rect.height() > 0:
            self.captured.emit(QgsGeometry.fromRect(rect))
        else:
            self.cancelled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._abort()

    def _abort(self):
        self._start = None
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        self.cancelled.emit()

    def deactivate(self):
        self._start = None
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()
