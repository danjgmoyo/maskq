# -*- coding: utf-8 -*-
"""
histogram.py  —  Collapsible interactive histogram for MaskQ.

Pure Qt / QPainter.  No matplotlib.  No external dependencies.

Public API (used by panel.py only):
    section = HistogramSection(parent)
    section.range_changed  pyqtSignal(float, float)  — emitted on handle drag
    section.set_data(counts, dmin, dmax, n_samples)  — feed bin counts
    section.set_range(lo, hi)                         — sync from spinboxes
    section.set_threshold(op, val)                    — threshold mode
    section.set_range_mode()                          — back to range mode
    section.set_loading()                             — show spinner text
    section.is_expanded() -> bool
"""
import numpy as np

from qgis.PyQt.QtCore    import Qt, pyqtSignal, QRectF, QPointF
from qgis.PyQt.QtGui     import (QPainter, QColor, QPen, QBrush,
                                  QLinearGradient, QFont, QPainterPath,
                                  QFontMetrics, QCursor, QPalette)
from qgis.PyQt.QtWidgets import (QWidget, QVBoxLayout, QPushButton,
                                  QSizePolicy, QFrame, QLabel)

# ── colour palette (matches panel.py) ────────────────────────────────────────
_G  = '#589632'; _GM = '#73A843'; _GD = '#374E11'

_COL_BAR      = QColor(100, 150, 200, 160)
_COL_KEEP     = QColor(88,  150,  50,  55)
_COL_KEEP_BDR = QColor(88,  150,  50, 190)
_COL_THR      = QColor(88,  150,  50, 190)
_COL_HANDLE   = QColor(88,  150,  50, 255)
_COL_HANDLE_H = QColor(115, 168,  67, 255)
_COL_GRID     = QColor(200, 210, 200, 110)

_MIN_H     = 110
_HANDLE_HIT = 12


def _fmt(v):
    """Compact axis tick label."""
    if v == 0:
        return '0'
    if abs(v) >= 1e5 or (abs(v) < 0.001 and v != 0):
        return f'{v:.2e}'
    if abs(v) >= 100:
        return f'{v:.0f}'
    if abs(v) >= 10:
        return f'{v:.1f}'
    return f'{v:.3g}'


# ── drawing canvas ────────────────────────────────────────────────────────────

class _HistCanvas(QWidget):
    """
    Draws the histogram with interactive handles (range mode) or a threshold
    line (threshold mode).  Emits range_changed(lo, hi) while dragging.
    """
    range_changed = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(_MIN_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

        # Data
        self._counts   = np.array([], dtype=np.float64)
        self._dmin     = 0.0
        self._dmax     = 1.0

        # Range handles
        self._lo       = 0.0
        self._hi       = 1.0

        # Threshold visualisation
        self._vis_mode = 'range'   # 'range' | 'threshold'
        self._thr_op   = '>'
        self._thr_val  = 0.0

        # Interaction
        self._dragging = None   # 'lo' | 'hi' | None
        self._hover    = None   # 'lo' | 'hi' | None

        # Drawing margins
        self._ml, self._mr, self._mt, self._mb = 8, 8, 8, 20

    # ── public API ────────────────────────────────────────────────────────────

    def set_data(self, counts, dmin, dmax):
        self._counts = np.asarray(counts, dtype=np.float64)
        self._dmin   = float(dmin)
        self._dmax   = float(dmax)
        self.repaint()

    def set_range(self, lo, hi):
        self._lo = max(self._dmin, min(float(lo), self._dmax))
        self._hi = max(self._dmin, min(float(hi), self._dmax))
        self.repaint()

    def set_range_mode(self):
        self._vis_mode = 'range'
        self.repaint()

    def set_threshold(self, op, val):
        self._vis_mode = 'threshold'
        self._thr_op   = op
        self._thr_val  = float(val)
        self.repaint()

    # ── coordinate helpers ────────────────────────────────────────────────────

    def _plot_rect(self):
        return QRectF(self._ml, self._mt,
                      self.width()  - self._ml - self._mr,
                      self.height() - self._mt - self._mb)

    def _d2x(self, val):
        r    = self._plot_rect()
        span = self._dmax - self._dmin
        if span == 0:
            return r.left()
        return r.left() + (val - self._dmin) / span * r.width()

    def _x2d(self, px):
        r    = self._plot_rect()
        span = self._dmax - self._dmin
        if r.width() == 0:
            return self._dmin
        raw = self._dmin + (px - r.left()) / r.width() * span
        return max(self._dmin, min(raw, self._dmax))

    def _handle_x(self, side):
        return self._d2x(self._lo if side == 'lo' else self._hi)

    def _hit_side(self, mx):
        lx = self._handle_x('lo')
        hx = self._handle_x('hi')
        dl = abs(mx - lx)
        dh = abs(mx - hx)
        if dl <= _HANDLE_HIT and dl <= dh:
            return 'lo'
        if dh <= _HANDLE_HIT:
            return 'hi'
        return None

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p   = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r   = self._plot_rect()
        pw  = int(r.width())
        ph  = int(r.height())
        ox  = r.left()
        oy  = r.top()
        bot = r.bottom()

        # Resolve palette once per paint — adapts to dark/light theme
        pal = self.palette()
        # Backgrounds
        p.fillRect(self.rect(), pal.color(QPalette.Window))
        p.fillRect(int(ox), int(oy), pw, ph, pal.color(QPalette.Base))

        if len(self._counts) == 0 or self._dmax == self._dmin:
            p.setPen(pal.color(QPalette.PlaceholderText))
            p.drawText(self.rect(), Qt.AlignCenter, 'Loading…')
            p.end()
            return

        max_c = self._counts.max() if self._counts.max() > 0 else 1

        # Horizontal grid lines
        p.setPen(QPen(_COL_GRID, 0.8))
        for frac in (0.25, 0.5, 0.75, 1.0):
            y = bot - frac * ph
            p.drawLine(QPointF(ox, y), QPointF(ox + pw, y))

        # Histogram bars
        nb = len(self._counts)
        bw = pw / nb
        p.setPen(Qt.NoPen)
        for i, cnt in enumerate(self._counts):
            bh = (cnt / max_c) * ph
            p.fillRect(QRectF(ox + i * bw, bot - bh, bw + 0.5, bh), _COL_BAR)

        # ── mode-specific overlay ─────────────────────────────────────────────
        if self._vis_mode == 'range':
            lx = self._d2x(self._lo)
            hx = self._d2x(self._hi)
            if hx > lx:
                keep_r = QRectF(lx, oy, hx - lx, ph)
                p.fillRect(keep_r, _COL_KEEP)
                p.setPen(QPen(_COL_KEEP_BDR, 1.2))
                p.drawRect(keep_r)
        else:
            # Threshold — shade kept half, draw dashed vertical line + label
            tx  = self._d2x(self._thr_val)
            op  = self._thr_op
            shade = None
            if op in ('>', '≥'):
                shade = QRectF(tx, oy, ox + pw - tx, ph)
            elif op in ('<', '≤'):
                shade = QRectF(ox, oy, tx - ox, ph)
            elif op == '=':
                shade = QRectF(tx - 2, oy, 4, ph)
            # '≠' shades everything — skip (would be confusing)
            if shade is not None and shade.width() > 0:
                p.fillRect(shade, _COL_KEEP)
            # Dashed vertical line
            p.setPen(QPen(_COL_THR, 1.8, Qt.DashLine))
            p.drawLine(QPointF(tx, oy), QPointF(tx, bot))
            # Value label just above the line
            font = QFont(); font.setPointSizeF(7.5)
            p.setFont(font)
            p.setPen(QPen(_COL_HANDLE, 1))
            lbl = _fmt(self._thr_val)
            lw  = QFontMetrics(font).horizontalAdvance(lbl)
            p.drawText(QPointF(max(ox + 2, min(tx - lw / 2, ox + pw - lw - 2)),
                               oy + 12), lbl)

        # ── axis tick labels ──────────────────────────────────────────────────
        p.setPen(pal.color(QPalette.Text))
        font = QFont(); font.setPointSizeF(7.5)
        p.setFont(font)
        span = self._dmax - self._dmin
        for tick in range(5):
            frac = tick / 4.0
            val  = self._dmin + frac * span
            tx   = ox + frac * pw
            p.drawLine(QPointF(tx, bot), QPointF(tx, bot + 3))
            lbl  = _fmt(val)
            lw   = QFontMetrics(font).horizontalAdvance(lbl)
            p.drawText(QPointF(max(ox, min(tx - lw / 2, ox + pw - lw)), bot + 13), lbl)

        # ── drag handles (range mode only) ────────────────────────────────────
        if self._vis_mode == 'range':
            for side in ('lo', 'hi'):
                hx2 = self._handle_x(side)
                col = (_COL_HANDLE_H
                       if (self._hover == side or self._dragging == side)
                       else _COL_HANDLE)
                p.setPen(QPen(col, 2.0))
                p.drawLine(QPointF(hx2, oy), QPointF(hx2, bot))
                kw   = 7
                path = QPainterPath()
                if side == 'lo':
                    path.moveTo(hx2, oy)
                    path.lineTo(hx2 + kw, oy - kw)
                    path.lineTo(hx2 + kw, oy)
                else:
                    path.moveTo(hx2, oy)
                    path.lineTo(hx2 - kw, oy - kw)
                    path.lineTo(hx2 - kw, oy)
                path.closeSubpath()
                p.fillPath(path, QBrush(col))

        # ── border ────────────────────────────────────────────────────────────
        p.setPen(QPen(pal.color(QPalette.Mid), 1))
        p.drawRect(QRectF(ox, oy, pw, ph))
        p.end()

    # ── mouse interaction ─────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self._vis_mode == 'range':
            side = self._hit_side(e.x())
            if side:
                self._dragging = side
                self.setCursor(QCursor(Qt.SizeHorCursor))
        elif self._vis_mode == 'threshold':
            # Click anywhere to move the threshold line
            self._dragging = 'thr'
            self.setCursor(QCursor(Qt.SizeHorCursor))
            self._thr_val = self._x2d(e.x())
            self.range_changed.emit(self._thr_val, self._thr_val)
            self.repaint()

    def mouseMoveEvent(self, e):
        if self._dragging == 'thr':
            # Threshold drag — move the vertical line
            self._thr_val = self._x2d(e.x())
            self.range_changed.emit(self._thr_val, self._thr_val)
            self.repaint()
        elif self._dragging in ('lo', 'hi'):
            val = self._x2d(e.x())
            if self._dragging == 'lo':
                self._lo = min(val, self._hi - 1e-12)
            else:
                self._hi = max(val, self._lo + 1e-12)
            self.range_changed.emit(self._lo, self._hi)
            self.repaint()
        elif self._vis_mode == 'range':
            side = self._hit_side(e.x())
            if side != self._hover:
                self._hover = side
                self.setCursor(QCursor(
                    Qt.SizeHorCursor if side else Qt.ArrowCursor))
                self.repaint()
        elif self._vis_mode == 'threshold':
            # Show resize cursor over the whole canvas in threshold mode
            self.setCursor(QCursor(Qt.SizeHorCursor))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._dragging:
            self._dragging = None
            self.setCursor(QCursor(
                Qt.SizeHorCursor if self._vis_mode == 'threshold'
                else Qt.ArrowCursor))


# ── collapsible section ───────────────────────────────────────────────────────

class HistogramSection(QWidget):
    """
    Collapsible histogram panel — click ▶ Histogram to expand, ▼ to collapse.
    Toggle style mirrors QGIS "Advanced Parameters" behaviour.

    Signals:
        range_changed(lo, hi)  — emitted when user drags handles
    """
    range_changed = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = True   # start expanded — user can collapse if unwanted

        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 4, 0, 0)
        vl.setSpacing(0)

        # Toggle button — QPushButton with unicode arrow (always visible,
        # unlike QToolButton arrows which go white on some Windows themes)
        self._btn = QPushButton('\u25bc  Histogram')   # ▼ expanded by default
        self._btn.setCheckable(True)
        self._btn.setChecked(True)   # checked = expanded
        self._btn.setFlat(True)
        self._btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setStyleSheet(
            'QPushButton{border:none;background:transparent;'
            'font-size:8.5pt;color:palette(shadow);padding:2px 0;text-align:left;}'
            'QPushButton:hover{color:palette(windowText);}'
        )
        self._btn.clicked.connect(self._on_toggle)
        vl.addWidget(self._btn)

        # Collapsible body — visible by default
        self._body = QFrame()
        self._body.setFrameShape(QFrame.NoFrame)
        # body starts visible (histogram auto-expands)
        body_l = QVBoxLayout(self._body)
        body_l.setContentsMargins(0, 4, 0, 4)
        body_l.setSpacing(4)

        self._canvas = _HistCanvas()
        self._canvas.range_changed.connect(self.range_changed)
        body_l.addWidget(self._canvas)

        self._lbl = QLabel('')
        self._lbl.setStyleSheet(
            'font-size:8pt;color:palette(shadow);background:transparent;')
        self._lbl.setAlignment(Qt.AlignCenter)
        body_l.addWidget(self._lbl)

        vl.addWidget(self._body)

    def _on_toggle(self, checked):
        self._expanded = checked
        self._btn.setText(
            '\u25bc  Histogram' if checked else '\u25b6  Histogram')  # ▼ / ▶
        self._body.setVisible(checked)
        if checked:
            self._canvas.repaint()

    # ── public API ────────────────────────────────────────────────────────────

    def set_loading(self):
        self._canvas.set_data(np.array([]), 0.0, 1.0)
        self._lbl.setText('Computing histogram…')

    def set_data(self, counts, dmin, dmax, n_samples):
        self._canvas.set_data(counts, dmin, dmax)
        self._lbl.setText(
            f'{n_samples:,} pixels sampled  ·  {len(counts)} bins')

    def set_range(self, lo, hi):
        """Sync handles from spinboxes — does NOT emit range_changed."""
        try:
            self._canvas.range_changed.disconnect(self.range_changed)
        except TypeError:
            pass
        self._canvas.set_range(lo, hi)
        try:
            self._canvas.range_changed.connect(self.range_changed)
        except Exception:
            pass

    def set_range_mode(self):
        """Switch canvas to range (two handles) mode."""
        self._canvas.set_range_mode()

    def set_threshold(self, op, val):
        """Switch canvas to threshold (vertical line) mode."""
        self._canvas.set_threshold(op, val)

    def is_expanded(self):
        return self._expanded
