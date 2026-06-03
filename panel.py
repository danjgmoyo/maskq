# -*- coding: utf-8 -*-
"""
panel.py  —  MaskQ dock panel.

One class. Inherits only QDockWidget.
Builds its own UI, runs its own stats thread, launches the task.
No base classes. No signal routing between files.
"""
import os, re

from qgis.PyQt.QtCore    import Qt, QThread, QObject, pyqtSignal
from qgis.PyQt.QtGui     import QColor
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLabel, QPushButton, QCheckBox, QDoubleSpinBox, QComboBox,
    QProgressBar, QStackedWidget, QScrollArea, QFrame, QGroupBox,
    QSizePolicy, QToolButton, QButtonGroup,
)
from qgis.gui import (
    QgsMapLayerComboBox, QgsRasterBandComboBox,
    QgsFileWidget, QgsColorButton,
)
from qgis.core import (
    QgsMapLayerProxyModel, QgsSettings, QgsApplication,
    QgsMessageLog, Qgis, QgsProject, QgsRasterLayer,
    QgsRasterShader, QgsColorRampShader,
    QgsSingleBandPseudoColorRenderer,
    QgsPalettedRasterRenderer, QgsProcessingUtils,
)
from .task import MaskQTask, EXT_DRIVER
from .histogram import HistogramSection

# Brand green — only hardcoded colour. Everything else is native Qt.
_GREEN       = '#589632'
_GREEN_LIGHT = '#73A843'
_GREEN_DARK  = '#374E11'

# Minimal stylesheet — native widgets render themselves using the system palette.
# We only override the specific elements that carry the brand identity:
#   - checked tab / mode buttons  → green fill
#   - Run button                  → green fill
#   - QCheckBox indicator         → green tick
#   - QProgressBar chunk          → green fill
#   - selection highlight         → green
# Everything else (borders, backgrounds, text, hover) is left to Qt / the OS.
_STYLE = (
    # Run button — always green, white text
    f"#MQPanel QPushButton#run{{background:{_GREEN};color:white;border:none;"
    "font-weight:bold;padding:4px 18px;min-height:26px;border-radius:3px;}"
    f"#MQPanel QPushButton#run:hover{{background:{_GREEN_LIGHT};}}"
    f"#MQPanel QPushButton#run:pressed{{background:{_GREEN_DARK};}}"
    "#MQPanel QPushButton#run:disabled{"
    "background:palette(mid);color:palette(shadow);border:none;}"
    # Mode / condition tab buttons — checked state green
    "#MQPanel QToolButton{"
    "border:1px solid palette(mid);padding:3px 8px;font-size:8.5pt;}"
    f"#MQPanel QToolButton:checked{{background:{_GREEN};color:white;"
    f"border-color:{_GREEN_DARK};}}"
    f"#MQPanel QToolButton:hover{{border-color:{_GREEN_LIGHT};}}"
    # Check box tick — green
    f"#MQPanel QCheckBox::indicator:checked{{background:{_GREEN};"
    f"border:1px solid {_GREEN_DARK};border-radius:2px;}}"
    # Progress bar chunk — green
    f"#MQPanel QProgressBar::chunk{{background:{_GREEN};border-radius:2px;}}"
    "#MQPanel QProgressBar{border:1px solid palette(mid);border-radius:3px;"
    "background:palette(window);font-size:8pt;text-align:center;}"
    # Combo box selection — green
    f"#MQPanel QComboBox QAbstractItemView{{"
    f"selection-background-color:{_GREEN};selection-color:white;}}"
)

def _card(title=''):
    """Native QGroupBox — renders with OS border and title, adapts to all themes."""
    gb = QGroupBox(title)
    vl = QVBoxLayout(gb)
    vl.setContentsMargins(8, 8, 8, 8)
    vl.setSpacing(6)
    return gb, vl

def _lbl(text, role=None):
    l = QLabel(text)
    if role:
        l.setObjectName(role)
    return l

def _hbox(*items, spacing=6):
    h = QHBoxLayout()
    h.setSpacing(spacing)
    h.setContentsMargins(0, 0, 0, 0)
    for it in items:
        if it is None:
            h.addStretch()
        elif isinstance(it, int):
            h.addSpacing(it)
        else:
            h.addWidget(it)
    return h

# ── background stats worker ───────────────────────────────────────────────────

class _StatsWorker(QObject):
    # min, max, dtype_name, hist_counts(list), n_samples
    done = pyqtSignal(float, float, str, object, int)

    def __init__(self, path, band, n_bins=128):
        super().__init__()
        self._path   = path
        self._band   = band
        self._n_bins = n_bins

    def run(self):
        lo, hi, dt, counts, n_samp = 0.0, 1.0, '?', [], 0
        try:
            from osgeo import gdal
            import numpy as np
            # UseExceptions intentionally NOT called — see task.py
            ds = gdal.Open(self._path, gdal.GA_ReadOnly)
            if ds:
                nb  = ds.RasterCount
                bi  = max(1, min(self._band, nb))
                b   = ds.GetRasterBand(bi)
                s   = b.GetStatistics(False, True)
                if s and len(s) >= 2 and s[1] > s[0]:
                    lo, hi = float(s[0]), float(s[1])
                dt_map = {1:'Byte',2:'UInt16',3:'Int16',4:'UInt32',
                          5:'Int32',6:'Float32',7:'Float64'}
                dt = dt_map.get(b.DataType, '?')
                # Compute histogram via GDAL (fast, uses overviews if available)
                if lo < hi:
                    gdal_hist = b.GetHistogram(
                        lo, hi, self._n_bins,
                        include_out_of_range=0, approx_ok=True)
                    if gdal_hist:
                        arr    = np.array(gdal_hist, dtype=np.float64)
                        # Smooth slightly to reduce salt-and-pepper noise
                        counts = arr.tolist()
                        n_samp = int(arr.sum())
                ds = None
        except Exception:
            pass
        self.done.emit(lo, hi, dt, counts, n_samp)

# ── main panel ────────────────────────────────────────────────────────────────

class MaskQPanel(QDockWidget):

    def __init__(self, iface):
        super().__init__('MaskQ', iface.mainWindow())
        self.iface = iface
        self.setObjectName('MaskQDock')
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # Instance state — all None/empty until first use
        self._stats_thread  = None
        self._stats_worker  = None
        self._task          = None
        self._prev_layer    = None   # QgsRasterLayer for live preview
        self._prev_fnc      = None   # QgsColorRampShader for preview
        self._data_min      = 0.0
        self._data_max      = 1.0
        self._histogram     = None   # HistogramSection, created lazily
        self._unloaded_sources = []   # (name, source) saved before overwrite unload

        self._build_ui()

    # =========================================================================
    # UI CONSTRUCTION  (called once from __init__)
    # =========================================================================

    def _build_ui(self):
        # Outer wrapper — scroll area so the panel works in a narrow dock
        outer = QWidget()
        self.setWidget(outer)
        outer_vl = QVBoxLayout(outer)
        outer_vl.setContentsMargins(0, 0, 0, 0)
        outer_vl.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        outer_vl.addWidget(scroll)

        # Inner panel — stylesheet applied here only (scoped to #MQPanel)
        inner = QWidget()
        inner.setObjectName('MQPanel')
        inner.setStyleSheet(_STYLE)
        inner.setMinimumWidth(280)
        scroll.setWidget(inner)

        root = QVBoxLayout(inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        root.addWidget(self._build_input_card())
        root.addWidget(self._build_method_card())
        root.addWidget(self._build_options_card())
        root.addLayout(self._build_run_row())
        root.addWidget(self._build_progress())
        root.addStretch()

        self._connect_signals()
        self._set_tab_order()
        self._switch_mode(0)
        self._switch_cond(0)
        self._refresh_layer()

    # ── input card ────────────────────────────────────────────────────────────

    def _build_input_card(self):
        card, vl = _card('Input / Output')

        # Layer combo
        self.cmb_layer = QgsMapLayerComboBox()
        try:
            self.cmb_layer.setFilters(Qgis.LayerFilter.RasterLayer)
        except AttributeError:
            self.cmb_layer.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.cmb_layer.setToolTip(
            'Local file-based raster only.\n'
            'XYZ/WMS/WCS tile services cannot be processed.')

        # Band combo
        self.cmb_band = QgsRasterBandComboBox()
        self.cmb_band.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.cmb_band.setToolTip(
            'Which band to evaluate the condition on.\n'
            'The OUTPUT always contains ALL bands of the raster.\n\n'
            'Example: a 4-band Sentinel composite with condition\n'
            '"Band 4 >= 0.3" will still produce a 4-band output.\n'
            'Every band is masked wherever Band 4 fails the condition.')
        # Layer picker spans full width — putting QgsMapLayerComboBox
        # inside a QFormLayout label+field row collapses it to just the
        # dropdown arrow because the label column eats most of the width.
        # Standard QGIS convention: layer picker is always full-width.
        vl.addWidget(QLabel('Layer'))
        vl.addWidget(self.cmb_layer)

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form.addRow('Condition band', self.cmb_band)
        vl.addLayout(form)

        # Shows raster dtype, CRS, and value range after statistics load.
        self.lbl_info = _lbl('', 'dim')
        self.lbl_info.setWordWrap(True)
        vl.addWidget(self.lbl_info)

        # Output path
        self.file_out = QgsFileWidget()
        self.file_out.setStorageMode(QgsFileWidget.SaveFile)
        self.file_out.setToolTip(
            'Output file path.\n'
            'Leave blank to save to a temporary file automatically.\n'
            'Use the Browse (…) button to pick a folder — '
            'the path must be absolute.')
        # Use QGIS native raster format filter — includes all formats
        # that the installed GDAL build actually supports.
        # This is QGIS's job, not the plugin's.
        try:
            from qgis.core import QgsProviderRegistry
            filt = QgsProviderRegistry.instance().fileRasterFilters()
            # Append a plain .tif catch-all at the start for quick typing
            if filt:
                self.file_out.setFilter(filt)
        except Exception:
            pass   # leave QgsFileWidget with its default filter
        last_dir = QgsSettings().value('MaskQ/last_dir', '')
        if last_dir:
            self.file_out.setDefaultRoot(last_dir)

        form.addRow('Output', self.file_out)

        return card

    # ── method card ───────────────────────────────────────────────────────────

    def _build_method_card(self):
        card, vl = _card('Mask Method')
        tab_row          = QHBoxLayout()
        tab_row.setSpacing(2)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_btns  = []
        for i, label in enumerate(['Value Range', 'Raster Mask', 'Vector Mask']):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._mode_group.addButton(b, i)
            self._mode_btns.append(b)
            tab_row.addWidget(b)
        self._mode_group.buttonClicked[int].connect(self._switch_mode)
        vl.addLayout(tab_row)
        self._mode_stack = QStackedWidget()
        self._mode_stack.addWidget(self._build_value_range_page())
        self._mode_stack.addWidget(self._build_raster_mask_page())
        self._mode_stack.addWidget(self._build_vector_mask_page())
        vl.addWidget(self._mode_stack)
        return card

    def _build_value_range_page(self):
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 4, 0, 0)
        vl.setSpacing(6)

        # Condition sub-tabs — QToolButton + QButtonGroup (native, theme-aware)
        ct_row           = QHBoxLayout()
        ct_row.setSpacing(2)
        self._cond_group = QButtonGroup(self)
        self._cond_group.setExclusive(True)
        self._cond_btns  = []
        for i, label in enumerate(['Range', 'Threshold']):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._cond_group.addButton(b, i)
            self._cond_btns.append(b)
            ct_row.addWidget(b)
        self._cond_group.buttonClicked[int].connect(self._switch_cond)
        vl.addLayout(ct_row)

        # Condition stack
        self._cond_stack = QStackedWidget()

        # Page 0: range
        rp = QWidget()
        rl = QHBoxLayout(rp)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        self.spn_min = QDoubleSpinBox()
        self.spn_min.setDecimals(6)
        self.spn_min.setRange(-1e15, 1e15)
        self.spn_min.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.spn_max = QDoubleSpinBox()
        self.spn_max.setDecimals(6)
        self.spn_max.setRange(-1e15, 1e15)
        self.spn_max.setValue(1.0)
        self.spn_max.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_reset = QPushButton('↺  Reset')
        self.btn_reset.setObjectName('reset_btn')
        self.btn_reset.setToolTip('Reset Min and Max to the full data range')
        self.btn_reset.setFixedHeight(26)
        rl.addWidget(_lbl('Min'))
        rl.addWidget(self.spn_min)
        rl.addWidget(_lbl('Max'))
        rl.addWidget(self.spn_max)
        rl.addWidget(self.btn_reset)
        self._cond_stack.addWidget(rp)

        # Page 1: threshold
        tp = QWidget()
        tl = QHBoxLayout(tp)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        self.cmb_op = QComboBox()
        for sym in ['>', '≥', '<', '≤', '=', '≠']:
            self.cmb_op.addItem(sym)
        self.spn_thr = QDoubleSpinBox()
        self.spn_thr.setDecimals(6)
        self.spn_thr.setRange(-1e15, 1e15)
        self.spn_thr.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.spn_thr.setToolTip('Threshold value — clamped to the data range of the selected raster')
        tl.addWidget(_lbl('value'))
        tl.addWidget(self.cmb_op)
        tl.addWidget(self.spn_thr)
        tl.addStretch()
        self._cond_stack.addWidget(tp)

        vl.addWidget(self._cond_stack)

        # Condition summary label
        self.lbl_cond = _lbl('', 'ok')
        self.lbl_cond.setWordWrap(True)
        vl.addWidget(self.lbl_cond)

        # Collapsible histogram section
        self._histogram = HistogramSection()
        self._histogram.range_changed.connect(self._on_histogram_range)
        vl.addWidget(self._histogram)

        return w

    def _build_raster_mask_page(self):
        w  = QWidget()
        gl = QGridLayout(w)
        gl.setContentsMargins(0, 4, 0, 0)
        gl.setSpacing(6)
        gl.setColumnStretch(1, 3)
        gl.setColumnStretch(3, 2)

        try:
            rf = Qgis.LayerFilter.RasterLayer
        except AttributeError:
            rf = QgsMapLayerProxyModel.RasterLayer
        self.cmb_mask_lyr  = QgsMapLayerComboBox()
        self.cmb_mask_lyr.setFilters(rf)
        self.cmb_mask_band = QgsRasterBandComboBox()
        self.cmb_mask_band.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.cmb_mask_op = QComboBox()
        for sym in ['=', '≠', '>', '≥', '<', '≤']:
            self.cmb_mask_op.addItem(sym)
        self.spn_mask_val = QDoubleSpinBox()
        self.spn_mask_val.setDecimals(4)
        self.spn_mask_val.setRange(-1e9, 1e9)
        self.spn_mask_val.setValue(1.0)
        self.spn_mask_val.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.chk_mask_nd = QCheckBox('Also remove mask NoData pixels')
        self.chk_mask_nd.setChecked(False)   # default OFF — see tooltip
        self.chk_mask_nd.setToolTip(
            'Tick this if pixels where the mask raster is NoData\n'
            'should also be removed from the output.\n\n'
            'Leave unticked (default) for binary masks where 0 means\n'
            '"clear" and 1 means "masked" — otherwise 0 gets treated\n'
            'as NoData and removes all clear pixels too.')
        self.cmb_resample = QComboBox()
        self.cmb_resample.addItem('Nearest neighbour', 'near')
        self.cmb_resample.addItem('Bilinear', 'bilinear')

        gl.addWidget(_lbl('Mask raster'), 0, 0, Qt.AlignRight)
        gl.addWidget(self.cmb_mask_lyr, 0, 1)
        lbl_mband = _lbl('Mask band', 'dim')
        lbl_mband.setToolTip(
            'Which band of the mask raster contains the flag values.\n'
            'A mask raster should be single-band (e.g. a QA band,\n'
            'cloud mask, or water mask). If your mask raster is\n'
            'multi-band, pick the one band that holds the flags.')
        gl.addWidget(lbl_mband, 0, 2, Qt.AlignRight)
        gl.addWidget(self.cmb_mask_band, 0, 3)

        val_row = QHBoxLayout()
        val_row.setSpacing(4)
        val_row.addWidget(_lbl('mask'))
        val_row.addWidget(self.cmb_mask_op)
        val_row.addWidget(self.spn_mask_val)
        val_row.addStretch()
        gl.addWidget(_lbl('Remove where'), 1, 0, Qt.AlignRight)
        gl.addLayout(val_row, 1, 1, 1, 3)
        # Info bar: shows mask raster value range and NoData
        # so the user knows what value to put in 'Remove where mask = ?'
        self.lbl_mask_info = _lbl('', 'dim')
        self.lbl_mask_info.setWordWrap(True)
        gl.addWidget(self.lbl_mask_info, 2, 0, 1, 4)
        gl.addWidget(self.chk_mask_nd, 3, 0, 1, 4)
        gl.addWidget(_lbl('Resample'), 4, 0, Qt.AlignRight)
        gl.addWidget(self.cmb_resample, 4, 1)

        return w

    def _build_vector_mask_page(self):
        w  = QWidget()
        gl = QGridLayout(w)
        gl.setContentsMargins(0, 4, 0, 0)
        gl.setSpacing(6)
        gl.setColumnStretch(1, 3)

        try:
            # Accept all vector layers — GPKG/SHP/GeoJSON/KML etc.
            # Polygon geometry is preferred but not enforced here;
            # rasterisation works on any closed geometry.
            pf = Qgis.LayerFilter.VectorLayer
        except AttributeError:
            pf = QgsMapLayerProxyModel.VectorLayer
        self.cmb_vec = QgsMapLayerComboBox()
        self.cmb_vec.setFilters(pf)
        self.chk_sel  = QCheckBox('Selected features only')
        self.lbl_feat = _lbl('', 'dim')
        self.chk_all_touched = QCheckBox('All touched pixels')
        self.spn_buf  = QDoubleSpinBox()
        self.spn_buf.setDecimals(1)
        self.spn_buf.setRange(0, 1e6)
        self.spn_buf.setValue(0.0)
        self.spn_buf.setSuffix(' m')
        self.spn_buf.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        gl.addWidget(_lbl('Layer'), 0, 0, Qt.AlignRight)
        gl.addWidget(self.cmb_vec, 0, 1)
        gl.addWidget(self.chk_sel, 1, 0, 1, 2)
        gl.addWidget(self.lbl_feat, 2, 0, 1, 2)
        gl.addWidget(self.chk_all_touched, 3, 0, 1, 2)
        gl.addWidget(_lbl('Buffer'), 4, 0, Qt.AlignRight)
        gl.addWidget(self.spn_buf, 4, 1)

        return w

    # ── options card ──────────────────────────────────────────────────────────

    def _build_options_card(self):
        card, vl = _card('Output Options')

        # Operation
        self.cmb_operation = QComboBox()
        # Output type
        self.cmb_out_type = QComboBox()
        self.cmb_out_type.addItem('Real values  (preserve pixel values)', 0)
        self.cmb_out_type.addItem('Binary  (1 = kept,  255 = masked)',    1)

        # NoData value + colour
        self.spn_nodata = QDoubleSpinBox()
        self.spn_nodata.setDecimals(4)
        self.spn_nodata.setRange(-1e15, 1e15)   # no artificial limit
        self.spn_nodata.setSingleStep(1.0)
        self.spn_nodata.setStepType(
            QDoubleSpinBox.AdaptiveDecimalStepType)  # smarter scrolling
        self.spn_nodata.setKeyboardTracking(False)  # apply only on Enter/focus-out
        self.spn_nodata.setToolTip(
            'Value written to masked pixels in the output.\n'
            'Type any value directly, or use the arrow buttons.\n'
            'Common values: -9999 (float), -32768 (Int16 DEM), 0, 255.\n'
            'The ↺ button sets this to match the input raster NoData.')
        self.spn_nodata.setValue(
            float(QgsSettings().value('MaskQ/nodata', -9999)))
        self.spn_nodata.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Button to copy the input raster's own NoData value
        self.btn_nd_match = QPushButton('↺')
        self.btn_nd_match.setFixedWidth(26)
        self.btn_nd_match.setFixedHeight(26)
        self.btn_nd_match.setToolTip(
            'Set NoData to match the input raster\'s own NoData value.\n'
            'Shown in the info bar above (e.g. NoData=-32768).')

        self.btn_color = QgsColorButton()
        self.btn_color.setColor(
            QColor(QgsSettings().value('MaskQ/color', '#DC5014')))
        self.btn_color.setToolTip('Preview / binary output highlight colour')
        self.btn_color.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self.lbl_nd_note = _lbl('Binary mode: masked pixels = 255', 'dim')
        self.lbl_nd_note.hide()

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        nd_row = QHBoxLayout()
        nd_row.setSpacing(4)
        nd_row.addWidget(self.spn_nodata, 3)
        nd_row.addWidget(self.btn_nd_match)
        nd_row.addWidget(QLabel('Colour'))
        nd_row.addWidget(self.btn_color, 1)

        form.addRow('Operation', self.cmb_operation)
        form.addRow('Output type', self.cmb_out_type)
        form.addRow('NoData out', nd_row)
        form.addRow(self.lbl_nd_note)
        vl.addLayout(form)

        # Checkboxes — each on its own row for clarity
        self.chk_invert   = QCheckBox('Invert  (swap kept ↔ removed pixels)')
        self.chk_preserve = QCheckBox('Preserve input dtype')
        self.chk_preserve.setChecked(True)
        self.chk_excl_nd  = QCheckBox('Exclude existing NoData pixels')
        self.chk_excl_nd.setChecked(True)
        self.chk_load     = QCheckBox('Load result into QGIS')
        self.chk_load.setChecked(True)
        self.chk_preview  = QCheckBox('Live preview on map')
        self.chk_preview.setEnabled(False)
        self.chk_preview.setToolTip(
            'Highlights kept pixels on the map canvas.\n'
            'Updates as you change the value range.\n'
            'Available for Value Range mode after statistics load.')

        for chk in (self.chk_invert, self.chk_preserve, self.chk_excl_nd,
                    self.chk_load, self.chk_preview):
            vl.addWidget(chk)

        return card

    # ── run row + progress ────────────────────────────────────────────────────

    def _build_run_row(self):
        self.btn_run    = QPushButton('Run')
        self.btn_run.setObjectName('run')
        self.btn_run.setFixedHeight(30)
        self.btn_run.setToolTip('Run the mask/clip operation  (Enter)')

        self.btn_cancel = QPushButton('Cancel')
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setFixedHeight(30)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_run)
        return row

    def _build_progress(self):
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(16)
        self.progress.setTextVisible(True)
        self.progress.hide()
        return self.progress

    # =========================================================================
    # SIGNAL WIRING  (called once after all widgets exist)
    # =========================================================================

    def _set_tab_order(self):
        """Set logical keyboard tab order through the main controls."""
        pairs = [
            (self.cmb_layer,   self.cmb_band),
            (self.cmb_band,    self.file_out),
            (self.file_out,    self.spn_min),
            (self.spn_min,     self.spn_max),
            (self.spn_max,     self.spn_nodata),
            (self.spn_nodata,  self.btn_run),
        ]
        for a, b in pairs:
            try:
                self.setTabOrder(a, b)
            except Exception:
                pass

    def _connect_signals(self):
        self.cmb_layer.layerChanged.connect(self._refresh_layer)
        self.cmb_band.bandChanged.connect(self._refresh_band)
        self.cmb_mask_lyr.layerChanged.connect(
            lambda lyr: self.cmb_mask_band.setLayer(lyr))
        self.cmb_mask_lyr.layerChanged.connect(
            lambda _: self._refresh_mask_info())
        self.cmb_mask_band.bandChanged.connect(
            lambda _: self._refresh_mask_info())

        self.cmb_vec.layerChanged.connect(self._refresh_vec)
        self.chk_sel.stateChanged.connect(lambda _: self._refresh_vec())

        self.spn_min.valueChanged.connect(self._on_range_change)
        self.spn_max.valueChanged.connect(self._on_range_change)
        self.cmb_op.currentIndexChanged.connect(self._on_range_change)
        self.spn_thr.valueChanged.connect(self._on_range_change)
        self.chk_invert.stateChanged.connect(self._on_range_change)
        self.btn_reset.clicked.connect(self._reset_range)
        self.btn_nd_match.clicked.connect(self._match_nodata)

        self.cmb_out_type.currentIndexChanged.connect(self._on_out_type_change)
        self.btn_color.colorChanged.connect(lambda _: self._update_preview())

        self.chk_preview.stateChanged.connect(self._on_preview_toggle)

        self.btn_run.clicked.connect(self._on_run)
        self.btn_cancel.clicked.connect(self._on_cancel)

        QgsApplication.taskManager().allTasksFinished.connect(
            self._fallback_reenable)

    # =========================================================================
    # MODE / CONDITION SWITCHING
    # =========================================================================

    def _switch_mode(self, idx):
        if not hasattr(self, '_mode_stack') or not hasattr(self, '_mode_group'):
            return
        self._mode_stack.setCurrentIndex(idx)
        b = self._mode_group.button(idx)
        if b and not b.isChecked(): b.setChecked(True)
        self._update_preview_enabled()
        self._rebuild_operation_combo(idx)
        self._on_range_change()   # refresh condition label for the active mode

    def _rebuild_operation_combo(self, mode_idx):
        """Rebuild the Operation dropdown based on the active mode.

        Clip and Crop only make sense for Vector mode — they produce a
        physically smaller file by cropping to the polygon bounding box.
        For Value Range and Raster Mask, kept pixels are scattered across
        the whole raster so cropping never reduces the file size.
        Rather than showing unclickable greyed items (confusing),
        we simply remove them from the list when they don't apply.
        """
        if not hasattr(self, 'cmb_operation'):
            return
        is_vector = (mode_idx == 2)   # 0=ValueRange 1=RasterMask 2=VectorMask

        # Remember what was selected so we can restore it if still valid
        prev = self.cmb_operation.currentData()

        self.cmb_operation.blockSignals(True)
        self.cmb_operation.clear()

        self.cmb_operation.addItem(
            'Mask  — keep extent, outside condition → NoData', 'mask')

        if is_vector:
            # Clip and Crop only appear for vector mode
            self.cmb_operation.addItem(
                'Clip  — crop to extent, outside polygon → NoData', 'clip')
            self.cmb_operation.addItem(
                'Crop  — crop to extent, keep ALL pixel values', 'crop')

        # Restore previous selection if it still exists, else default to Mask
        for i in range(self.cmb_operation.count()):
            if self.cmb_operation.itemData(i) == prev:
                self.cmb_operation.setCurrentIndex(i)
                break

        self.cmb_operation.blockSignals(False)

    def _update_preview_enabled(self):
        """Single source of truth for chk_preview enabled state.
        Enabled iff: Value Range mode is active AND stats have been loaded.
        Stats are considered loaded when _data_min != _data_max OR
        lbl_info has content (i.e. _on_stats_done has fired at least once).
        """
        if not hasattr(self, 'chk_preview'):
            return
        in_vr        = (self._mode_stack.currentIndex() == 0)
        stats_loaded = (self._data_min != self._data_max or
                        bool(self.lbl_info.text()) and
                        'Loading' not in self.lbl_info.text())
        can = in_vr and stats_loaded
        self.chk_preview.setEnabled(can)
        if not can:
            self.chk_preview.blockSignals(True)
            self.chk_preview.setChecked(False)
            self.chk_preview.blockSignals(False)
            self._destroy_preview()

    def _switch_cond(self, idx):
        if not hasattr(self, '_cond_stack') or not hasattr(self, '_cond_group'):
            return
        self._cond_stack.setCurrentIndex(idx)
        b = self._cond_group.button(idx)
        if b and not b.isChecked():
            b.setChecked(True)
        if hasattr(self, 'spn_min'):
            self._on_range_change()

    # =========================================================================
    # LAYER / BAND / STATS
    # =========================================================================

    def _refresh_layer(self):
        self._stop_stats()
        self._destroy_preview()
        self._update_preview_enabled()
        lyr = self.cmb_layer.currentLayer()
        if lyr is None or not lyr.isValid():
            self.lbl_info.setText('')
            return
        self.cmb_band.setLayer(lyr)
        self._start_stats(lyr)

    def _refresh_band(self):
        lyr = self.cmb_layer.currentLayer()
        if lyr and lyr.isValid():
            self._stop_stats()
            # Keep current spinbox range until new stats arrive
            self._start_stats(lyr)

    def _start_stats(self, lyr):
        band = self.cmb_band.currentBand() or 1
        self._stats_worker = _StatsWorker(
            (lyr.dataProvider().dataSourceUri().split('|')[0].strip()
             if lyr.dataProvider() else lyr.source()),
            band)
        self._stats_thread = QThread(self)
        self._stats_worker.moveToThread(self._stats_thread)
        self._stats_thread.started.connect(self._stats_worker.run)
        self._stats_worker.done.connect(self._on_stats_done)
        self._stats_worker.done.connect(self._stats_thread.quit)
        self.lbl_info.setText('Loading statistics…')
        if self._histogram is not None:
            self._histogram.set_loading()
        self._stats_thread.start()

    def _stop_stats(self):
        worker = self._stats_worker
        thread = self._stats_thread
        self._stats_worker = None
        self._stats_thread = None
        if worker is not None:
            try:
                from PyQt5 import sip
                if not sip.isdeleted(worker):
                    worker.done.disconnect()
            except Exception:
                pass
        if thread is None:
            return
        try:
            from PyQt5 import sip
            if sip.isdeleted(thread):
                return
        except Exception:
            pass
        try:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(1500):
                    thread.terminate()
                    thread.wait(500)
        except RuntimeError:
            pass

    def _on_stats_done(self, lo, hi, dtype, hist_counts, n_samp):
        self._stats_thread = None
        self._stats_worker = None
        self._data_min = lo
        self._data_max = hi

        # Clamp spinboxes to the raster data range so histogram handles
        # always map on-screen and extreme conditions are visible to the user.
        self.spn_min.blockSignals(True)
        self.spn_max.blockSignals(True)
        self.spn_min.setRange(lo, hi)
        self.spn_max.setRange(lo, hi)
        self.spn_min.setValue(lo)
        self.spn_max.setValue(hi)
        self.spn_min.blockSignals(False)
        self.spn_max.blockSignals(False)
        # Clamp threshold spinbox to data range too
        self.spn_thr.setRange(lo, hi)
        cur_thr = self.spn_thr.value()
        if not (lo <= cur_thr <= hi):
            self.spn_thr.setValue((lo + hi) / 2.0)

        # Info bar (also show native nodata so user knows what to avoid)
        lyr = self.cmb_layer.currentLayer()
        crs = '?'
        try:
            from PyQt5 import sip
            if lyr is not None and not sip.isdeleted(lyr) and lyr.isValid():
                crs = lyr.crs().authid()
        except Exception:
            pass
        nd_txt = ''
        try:
            from osgeo import gdal
            from PyQt5 import sip as _sip
            if lyr is not None and not _sip.isdeleted(lyr) and lyr.isValid():
                _src = os.path.abspath(
                    (lyr.dataProvider().dataSourceUri().split('|')[0].strip()
                     if lyr.dataProvider() else lyr.source()))
                _ds = gdal.Open(_src, gdal.GA_ReadOnly)
                if _ds is not None:
                    _nd = _ds.GetRasterBand(
                        max(1, self.cmb_band.currentBand() or 1)).GetNoDataValue()
                    _ds = None
                    if _nd is not None:
                        nd_txt = f'  ·  NoData={_nd:.4g}'
        except Exception:
            pass
        self.lbl_info.setText(f'{dtype}  ·  {crs}  ·  {lo:.4g} – {hi:.4g}{nd_txt}')

        # Re-evaluate preview enabled state now that stats are loaded
        self._update_preview_enabled()

        self._on_range_change()

        # Feed histogram (set_range not needed here — _on_range_change handles it)
        if self._histogram is not None and hist_counts:
            self._histogram.set_data(hist_counts, lo, hi, n_samp)

        # If preview was already on, refresh it for the new layer
        if self.chk_preview.isChecked():
            self._destroy_preview()
            self._create_preview()

    def _match_nodata(self):
        """Set the output NoData spinbox to match the input raster's NoData.
        The value is already shown in the info bar — this just copies it
        into the spinbox so the user doesn't have to type it manually.
        """
        lyr = self.cmb_layer.currentLayer()
        if lyr is None or not lyr.isValid():
            return
        try:
            from osgeo import gdal
            src_path = os.path.abspath(
                (lyr.dataProvider().dataSourceUri().split('|')[0].strip()
                 if lyr.dataProvider() else lyr.source()))
            ds = gdal.Open(src_path, gdal.GA_ReadOnly)
            if ds is None:
                return
            band = max(1, self.cmb_band.currentBand() or 1)
            nd   = ds.GetRasterBand(band).GetNoDataValue()
            ds   = None
            if nd is not None:
                self.spn_nodata.setValue(float(nd))
            else:
                self._bar(
                    'Input raster has no NoData value set — '
                    'assign one in Layer Properties first.',
                    Qgis.Warning, dur=6)
        except Exception as e:
            self._bar(f'Could not read NoData: {e}', Qgis.Warning, dur=5)

    def _reset_range(self):
        self.spn_min.setValue(self._data_min)
        self.spn_max.setValue(self._data_max)

    # =========================================================================
    # VECTOR
    # =========================================================================

    def _refresh_mask_info(self):
        """Update the mask raster info bar: dtype, value range, NoData.

        This tells the user exactly what values exist in the mask raster
        so they know what to enter in the 'Remove where mask = ?' field.
        E.g. 'Float32 · min=0 · max=1 · NoData=None' tells them to use 0 or 1.
        E.g. 'Int16 · min=0 · max=10 · NoData=-9999' tells them SCL values.
        """
        ml = self.cmb_mask_lyr.currentLayer()
        if not hasattr(self, 'lbl_mask_info'):
            return
        if ml is None or not ml.isValid():
            self.lbl_mask_info.setText('')
            return
        try:
            from osgeo import gdal
            mpath = os.path.abspath(
                ml.dataProvider().dataSourceUri().split('|')[0].strip()
                if ml.dataProvider() else ml.source())
            ds = gdal.Open(mpath, gdal.GA_ReadOnly)
            if ds is None:
                self.lbl_mask_info.setText('Cannot read mask raster')
                return
            mb  = max(1, min(self.cmb_mask_band.currentBand() or 1,
                             ds.RasterCount))
            b   = ds.GetRasterBand(mb)
            nd  = b.GetNoDataValue()
            st  = b.GetStatistics(False, True)
            dt_map = {1:'Byte',2:'UInt16',3:'Int16',4:'UInt32',
                      5:'Int32',6:'Float32',7:'Float64'}
            dt  = dt_map.get(b.DataType, '?')
            ds  = None
            lo, hi = st[0], st[1]
            nd_str = f'{nd:.4g}' if nd is not None else 'none'
            self.lbl_mask_info.setText(
                f'{dt}  ·  values: {lo:.4g} – {hi:.4g}  ·  NoData: {nd_str}')
        except Exception as e:
            self.lbl_mask_info.setText(f'Could not read mask stats: {e}')

    def _refresh_vec(self):
        vl = self.cmb_vec.currentLayer()
        if vl is None or not vl.isValid():
            self.lbl_feat.setText('')
            return
        try:
            # featureCount() ignores subsetString — use a filtered count
            tot = vl.featureCount()
            # If a definition query is set, show the filtered count
            if vl.subsetString():
                try:
                    vl.updateExtents()
                    # Use dataProvider for accurate filtered count
                    tot = sum(1 for _ in vl.getFeatures())
                except Exception:
                    pass
            sel = vl.selectedFeatureCount()
            if self.chk_sel.isChecked():
                if sel == 0:
                    self.lbl_feat.setObjectName('err')
                    self.lbl_feat.setStyleSheet('')
                    self.lbl_feat.setText('⚠  0 features selected — uncheck or select features')
                else:
                    self.lbl_feat.setObjectName('dim')
                    self.lbl_feat.setStyleSheet('')
                    self.lbl_feat.setText(f'{sel:,} selected')
            else:
                self.lbl_feat.setObjectName('dim')
                self.lbl_feat.setStyleSheet('')
                self.lbl_feat.setText(f'{tot:,} feature{"s" if tot != 1 else ""}')
        except Exception:
            self.lbl_feat.setText('')
        # Update buffer distance suffix to reflect the vector layer's CRS units
        try:
            from qgis.core import QgsUnitTypes
            unit = vl.crs().mapUnits()
            unit_str = QgsUnitTypes.encodeUnit(unit)
            short = {'degrees': '°', 'degree': '°', 'meters': 'm', 'metres': 'm',
                     'feet': 'ft', 'foot': 'ft', 'kilometers': 'km',
                     'kilometres': 'km'}.get(unit_str.lower(), unit_str)
            self.spn_buf.setSuffix(f' {short}')
        except Exception:
            self.spn_buf.setSuffix('')

    # =========================================================================
    # RANGE / CONDITION LABEL
    # =========================================================================

    def _on_range_change(self):
        if not hasattr(self, 'spn_min') or not hasattr(self, 'lbl_cond'): return
        inv = self.chk_invert.isChecked()
        act = 'Remove' if inv else 'Keep'
        ct  = self._cond_stack.currentIndex()
        if ct == 0:
            lo, hi = self.spn_min.value(), self.spn_max.value()
            if lo >= hi:
                self.lbl_cond.setObjectName('err')
                msg = ('⚠  Min = Max — all pixels will be masked'
                       if lo == hi else
                       '⚠  Min > Max — all pixels will be masked')
                self.lbl_cond.setText(msg)
            else:
                self.lbl_cond.setObjectName('ok')
                self.lbl_cond.setText(
                    f'{act}:  {lo:.6g}  ≤  value  ≤  {hi:.6g}')
        else:
            op  = self.cmb_op.currentText()
            thr = self.spn_thr.value()
            self.lbl_cond.setObjectName('ok')
            self.lbl_cond.setText(f'{act}:  value  {op}  {thr:.6g}')
        # Force style refresh after objectName change
        self.lbl_cond.style().unpolish(self.lbl_cond)
        self.lbl_cond.style().polish(self.lbl_cond)
        # Keep histogram in sync — range mode shows handles, threshold shows line
        if self._histogram is not None:
            if ct == 0:
                self._histogram.set_range_mode()
                self._histogram.set_range(self.spn_min.value(), self.spn_max.value())
            else:
                self._histogram.set_threshold(
                    self.cmb_op.currentText(), self.spn_thr.value())
        self._update_preview()

    def _on_out_type_change(self):
        binary = (self.cmb_out_type.currentData() == 1)
        self.spn_nodata.setEnabled(not binary)
        self.lbl_nd_note.setVisible(binary)
        # Binary output always writes Byte — preserve_dtype is meaningless
        self.chk_preserve.setEnabled(not binary)
        if binary:
            self.chk_preserve.setToolTip(
                'Not applicable for Binary output — output is always Byte (uint8).')
        else:
            self.chk_preserve.setToolTip('')

    # =========================================================================
    # LIVE PREVIEW
    # =========================================================================

    def _on_preview_toggle(self):
        if self.chk_preview.isChecked():
            self._create_preview()
        else:
            self._destroy_preview()

    def _create_preview(self):
        lyr = self.cmb_layer.currentLayer()
        if lyr is None or not lyr.isValid():
            return
        self._destroy_preview()

        self._prev_layer = QgsRasterLayer(lyr.source(), 'MaskQ — preview')
        if not self._prev_layer.isValid():
            self._prev_layer = None
            return

        # Insert above source layer in layer tree
        QgsProject.instance().addMapLayer(self._prev_layer, False)
        root = QgsProject.instance().layerTreeRoot()
        src_node = root.findLayer(lyr.id())
        if src_node:
            idx = list(src_node.parent().children()).index(src_node)
            src_node.parent().insertLayer(idx, self._prev_layer)
        else:
            root.insertLayer(0, self._prev_layer)

        # Build colour ramp shader (updated in _update_preview)
        self._prev_fnc = QgsColorRampShader()
        self._prev_fnc.setColorRampType(QgsColorRampShader.Discrete)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(self._prev_fnc)
        band = self.cmb_band.currentBand() or 1
        self._prev_layer.setRenderer(
            QgsSingleBandPseudoColorRenderer(
                self._prev_layer.dataProvider(), band, shader))

        self._update_preview()

    def _update_preview(self):
        if (self._prev_layer is None or
                not self._prev_layer.isValid() or
                not self.chk_preview.isChecked()):
            return
        try:
            ct  = self._cond_stack.currentIndex()
            inv = self.chk_invert.isChecked()
            col = self.btn_color.color()
            t   = QColor(0, 0, 0, 0)   # transparent
            INF = float('inf')

            if ct == 0:
                lo, hi = self.spn_min.value(), self.spn_max.value()
            else:
                op  = self.cmb_op.currentText()
                thr = self.spn_thr.value()
                lo, hi = {
                    '>':  (thr, INF), '≥': (thr, INF),
                    '<':  (-INF, thr), '≤': (-INF, thr),
                }.get(op, (thr, thr))

            keep_c  = t   if inv else col
            mask_c  = col if inv else t

            dmin, dmax = self._data_min, self._data_max

            if lo <= dmin and hi >= dmax:
                items = [QgsColorRampShader.ColorRampItem(INF, keep_c)]
            elif lo <= dmin:
                items = [QgsColorRampShader.ColorRampItem(hi,  keep_c),
                         QgsColorRampShader.ColorRampItem(INF, mask_c)]
            elif hi >= dmax:
                items = [QgsColorRampShader.ColorRampItem(lo - 1e-10, mask_c),
                         QgsColorRampShader.ColorRampItem(INF,        keep_c)]
            else:
                items = [QgsColorRampShader.ColorRampItem(lo - 1e-10, mask_c),
                         QgsColorRampShader.ColorRampItem(hi,          keep_c),
                         QgsColorRampShader.ColorRampItem(INF,         mask_c)]

            self._prev_fnc.setColorRampItemList(items)
            self._prev_layer.triggerRepaint()
        except Exception:
            pass

    def _destroy_preview(self):
        if self._prev_layer is not None:
            try:
                if self._prev_layer.isValid():
                    QgsProject.instance().removeMapLayer(
                        self._prev_layer.id())
                    self.iface.mapCanvas().refresh()
            except Exception:
                pass
            self._prev_layer = None
            self._prev_fnc   = None

    # =========================================================================
    # RUN / CANCEL / TASK CALLBACKS
    # =========================================================================

    def _on_run(self):
        # Guard: don't start a new task while one is already running
        if self._task is not None:
            self._bar('Already running — wait for the current task to finish '
                      'or click Cancel.', Qgis.Warning, dur=4)
            return
        # 1. Validate input layer
        lyr = self.cmb_layer.currentLayer()
        if lyr is None or not lyr.isValid():
            self._bar('Select a valid raster layer.', Qgis.Warning)
            return
        dp = lyr.dataProvider()
        if dp is None or dp.name() != 'gdal':
            pname = dp.name() if dp else 'unknown'
            self._bar(
                f'"{lyr.name()}" is a {pname} layer — '
                'only local file-based rasters are supported.\n'
                'XYZ tiles, WMS, and WCS services cannot be processed.',
                Qgis.Critical, dur=10)
            return

        # 2. Validate output path (unless temp)
        # QGIS native: empty path = save to temp file
        use_temp = not bool(self.file_out.filePath())
        out_path, out_fmt = '', 'GTiff'

        if not use_temp:
            path = self.file_out.filePath()
            ok, err = self._validate_path(path)
            if not ok:
                self._bar(err, Qgis.Warning, dur=8)
                return
            out_path = path
            ext      = os.path.splitext(path)[1].lower()
            out_fmt  = EXT_DRIVER.get(ext, 'GTiff')
            QgsSettings().setValue(
                'MaskQ/last_dir', os.path.dirname(path))
            # Guard: never overwrite the input raster
            lyr_check = self.cmb_layer.currentLayer()
            if lyr_check:
                src_norm = os.path.normcase(os.path.normpath(lyr_check.source()))
                out_norm = os.path.normcase(os.path.normpath(path))
                if src_norm == out_norm:
                    self._bar(
                        'Output path is the same as the input raster. '
                        'Choose a different file name to avoid destroying your data.',
                        Qgis.Critical, dur=10)
                    return
            # Windows locks open files — unload any QGIS layer using
            # this path before GDAL tries to delete/overwrite it.
            self._unload_layers_at(path)

        # 3. Validate mode-specific params
        mode = self._mode_stack.currentIndex() + 1
        if mode == 1:
            if (self._cond_stack.currentIndex() == 0 and
                    self.spn_min.value() > self.spn_max.value()):
                self._bar('Min must be ≤ Max — no pixels would be kept.',
                          Qgis.Warning)
                return
            if (self._cond_stack.currentIndex() == 0 and
                    self.spn_min.value() == self.spn_max.value()):
                self._bar(
                    'Min equals Max — only pixels with exactly that value '
                    'would be kept. Is that intentional?',
                    Qgis.Warning, dur=6)
                # Don't block — just warn. User may want exactly one value.
        elif mode == 2:
            ml = self.cmb_mask_lyr.currentLayer()
            if ml is None or not ml.isValid():
                self._bar('Select a valid mask raster.', Qgis.Warning)
                return
            # Warn if mask raster is same file as input
            if (ml.source() == lyr.source()):
                self._bar(
                    'Mask raster is the same file as the input — '
                    'the result will be all NoData.',
                    Qgis.Warning, dur=8)
                return
            # Warn on CRS mismatch (task reprojects, but user should know)
            if (lyr.crs().isValid() and ml.crs().isValid()
                    and lyr.crs().authid() != ml.crs().authid()):
                self._bar(
                    f'Mask raster CRS ({ml.crs().authid()}) differs from '
                    f'input ({lyr.crs().authid()}). '
                    'The mask will be reprojected automatically. '
                    'Check the result if edges look wrong.',
                    Qgis.Warning, dur=8)
                # Don't block — reprojection is handled correctly by the task
        elif mode == 3:
            vl = self.cmb_vec.currentLayer()
            if vl is None or not vl.isValid():
                self._bar('Select a valid polygon layer.', Qgis.Warning)
                return

        # Warn if NoData value falls inside the data range (real values mode)
        if self.cmb_out_type.currentData() == 0:
            nd = self.spn_nodata.value()
            if self._data_min < nd < self._data_max:
                self._bar(
                    f'NoData value {nd:.4g} falls inside the data range '
                    f'({self._data_min:.4g}–{self._data_max:.4g}). '
                    'Masked pixels may be confused with real data. '
                    'Consider using a value outside the data range.',
                    Qgis.Warning, dur=10)
                # Don't block — advanced users may have a reason

        # 4. Build output path if none given
        if use_temp:
            out_path = self._build_output_name(lyr, mode)
            out_fmt  = 'GTiff'

        # 5. Build params dict
        params = {
            # Always resolve to absolute path.
            # dataSourceUri() can return relative paths when a QGIS project
            # is open, and background threads have undefined CWD on Windows.
            'input_path'    : os.path.abspath(
                (lyr.dataProvider().dataSourceUri().split('|')[0]
                 if lyr.dataProvider() else lyr.source()).strip()),
            'output_path'   : out_path,
            'output_format' : out_fmt,
            'mode'          : mode,
            'ref_band'      : max(1, self.cmb_band.currentBand() or 1),
            'invert'        : self.chk_invert.isChecked(),
            'output_type'   : self.cmb_out_type.currentData(),
            'operation'     : self.cmb_operation.currentData() or 'mask',
            'nodata_out'    : self.spn_nodata.value(),
            'input_crs_wkt' : (lyr.crs().toWkt()
                               if lyr.crs().isValid() else ''),
            'preserve_dtype': self.chk_preserve.isChecked(),
            'load_output'   : (True if use_temp
                               else self.chk_load.isChecked()),
            'exclude_nodata': self.chk_excl_nd.isChecked(),
            'condition_type': self._cond_stack.currentIndex(),
            'v_min'         : self.spn_min.value(),
            'v_max'         : self.spn_max.value(),
            'operator'      : self.cmb_op.currentText(),
            'threshold'     : self.spn_thr.value(),
        }

        if mode == 2:
            ml = self.cmb_mask_lyr.currentLayer()
            params.update({
                'mask_path'    : (os.path.abspath(
                                    ml.dataProvider().dataSourceUri()
                                    .split('|')[0].strip())
                                  if ml and ml.dataProvider() else ''),
                'mask_band'    : max(1, self.cmb_mask_band.currentBand() or 1),
                'mask_op'      : self.cmb_mask_op.currentText(),
                'mask_value'   : self.spn_mask_val.value(),
                'mask_nodata'  : self.chk_mask_nd.isChecked(),
                'mask_resample': self.cmb_resample.currentData(),
            })

        if mode == 3:
            vl = self.cmb_vec.currentLayer()
            # Get the clean file path (strip QGIS URI params after '|')
            raw_uri  = (vl.dataProvider().dataSourceUri()
                        if vl and vl.dataProvider() else '')
            vec_path = (os.path.abspath(raw_uri.split('|')[0].strip())
                        if raw_uri else '')
            # subsetString is the SQL WHERE clause QGIS applies to the layer.
            # It must be passed to the task so OGR applies the same filter.
            # Without this, ALL features are rasterised even if the layer
            # shows only a subset (e.g. loaded with a definition query).
            subset   = vl.subsetString() if vl else ''
            # For GPKG and other multi-layer formats, get the OGR layer name
            # from the URI (|layername=...) so OGR opens the right layer.
            ogr_lname = ''
            for part in raw_uri.split('|')[1:]:
                if part.lower().startswith('layername='):
                    ogr_lname = part.split('=', 1)[1].strip()
                    break
            # Selected features: export their geometries as WKT to avoid
            # QGIS-FID / OGR-FID mismatch. Task uses these directly.
            sel_wkts = None
            if self.chk_sel.isChecked() and vl and vl.selectedFeatureCount() > 0:
                sel_wkts = [
                    f.geometry().asWkt()
                    for f in vl.selectedFeatures()
                    if f.geometry() and not f.geometry().isNull()
                ]
            params.update({
                'vector_path'       : vec_path,
                'vector_layer_name' : ogr_lname or (vl.name() if vl else ''),
                'vector_subset'     : subset,
                'vector_sel_wkts'   : sel_wkts,
                'all_touched'       : self.chk_all_touched.isChecked(),
                'buffer_dist'       : self.spn_buf.value(),
            })

        mode_lbl = {1: 'value range', 2: 'raster mask', 3: 'vector mask'}
        op       = params['operation']
        desc     = (f"MaskQ {op.replace('_','+')} "
                    f"({mode_lbl[mode]})")

        task = MaskQTask(desc, params)
        self._task = task
        # Pass task explicitly so the lambda holds a strong reference.
        # If we only captured self._task, it could be None by the time
        # the signal fires (e.g. if the user clicks Run again).
        task.taskCompleted.connect(lambda t=task: self._on_done(t, True))
        task.taskTerminated.connect(lambda t=task: self._on_done(t, False))
        task.progressChanged.connect(self._on_progress)

        QgsMessageLog.logMessage(
            f'Starting: {os.path.basename(lyr.source())} '
            f'→ {os.path.basename(out_path)}',
            'MaskQ', Qgis.Info)

        self._set_busy(True)
        QgsApplication.taskManager().addTask(self._task)

    def _on_histogram_range(self, lo, hi):
        """Called when histogram handles/line are dragged — update spinboxes.

        Range mode emits (lo, hi) with lo < hi.
        Threshold mode emits (val, val) with lo == hi.
        We route to the correct spinbox(es) based on which condition tab is active.
        """
        if self._cond_stack.currentIndex() == 1:
            # Threshold mode — lo==hi==threshold value
            self.spn_thr.blockSignals(True)
            self.spn_thr.setValue(lo)
            self.spn_thr.blockSignals(False)
        else:
            # Range mode
            self.spn_min.blockSignals(True)
            self.spn_max.blockSignals(True)
            self.spn_min.setValue(lo)
            self.spn_max.setValue(hi)
            self.spn_min.blockSignals(False)
            self.spn_max.blockSignals(False)
        self._on_range_change()  # sync condition label and preview

    def _unload_layers_at(self, path):
        """Remove layers at *path* from QGIS (Windows file-lock workaround).

        Saves (name, source) of every removed layer into self._unloaded_sources
        so _on_done() can restore them if the task fails.
        """
        norm = os.path.normcase(os.path.normpath(path))
        self._unloaded_sources = []
        to_remove = []
        for lid, layer in QgsProject.instance().mapLayers().items():
            if not hasattr(layer, 'source'):
                continue
            try:
                lsrc = os.path.normcase(os.path.normpath(layer.source()))
            except Exception:
                continue
            if lsrc == norm:
                self._unloaded_sources.append((layer.name(), layer.source()))
                to_remove.append(lid)
        if to_remove:
            QgsProject.instance().removeMapLayers(to_remove)
    def _validate_path(self, path):
        if not path:
            return False, ('No output path set.\n'
                           'Use Browse or tick "Save to temporary file".')
        if not os.path.isabs(path):
            return False, (f'Path must be absolute — got:\n{path}\n\n'
                           'Use the Browse button to pick a folder.')
        ext = os.path.splitext(path)[1].lower()
        if not ext:
            return False, (
                'No file extension — add an extension like .tif\n'
                'GDAL uses the extension to determine the output format.')
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            return False, f'Directory does not exist:\n{d}'
        if not os.access(d, os.W_OK):
            return False, f'Directory is not writable:\n{d}'
        return True, ''

    def _build_output_name(self, lyr, mode):
        """
        Build a descriptive output filename when no output path is specified.
        Saves next to the input raster, named after the operation AND values used.
        Uses QgsProcessingUtils.generateTempFilename() as fallback so the
        directory is always guaranteed to exist (QGIS native temp system).

        Naming convention:
          {input_stem}_{operation}_{mode}_{condition_values}.tif

        Examples:
          NDVI_masked_range_0.3to0.8.tif       (range mode, min=0.3 max=0.8)
          DEM_clipped_threshold_gt500.tif       (threshold mode, value > 500)
          Sentinel_cropped_vector_boundary.tif  (vector mask, layer name)
          NDVI_masked_range_0.3to0.8_binary.tif (binary output)
        """
        def _clean(s):
            """Remove characters that are illegal in filenames."""
            return re.sub(r'[^\w\-.]', '_', str(s)).strip('_')

        def _fmt_val(v):
            """Format a numeric value compactly for a filename.
            1000.0 → '1000', 0.3 → '0.3', -9999.0 → 'n9999'
            """
            # No decimal point if whole number
            s = f'{v:.6g}'
            # Replace minus with 'n' (minus is illegal on Windows)
            s = s.replace('-', 'n')
            # Replace decimal point with 'p' for clarity
            s = s.replace('.', 'p')
            return s

        # ── Input stem ────────────────────────────────────────────────────────
        src_path = (lyr.dataProvider().dataSourceUri().split('|')[0].strip()
                    if lyr.dataProvider() else lyr.source())
        stem = _clean(os.path.splitext(os.path.basename(src_path))[0])

        # ── Operation ─────────────────────────────────────────────────────────
        op     = self.cmb_operation.currentData() or 'mask'
        op_str = {'mask': 'masked', 'clip': 'clipped',
                  'crop': 'cropped', 'mask_clip': 'clipped'}.get(op, op)

        # ── Mode + condition values ───────────────────────────────────────────
        if mode == 1:
            # Value Range or Threshold — include the actual values in the name
            if self._cond_stack.currentIndex() == 0:
                # Range mode: NDVI_masked_range_0p3to0p8
                lo = self.spn_min.value()
                hi = self.spn_max.value()
                mode_str = f'range_{_fmt_val(lo)}to{_fmt_val(hi)}'
            else:
                # Threshold mode: DEM_masked_threshold_gt500
                op_sym = self.cmb_op.currentText()
                thr    = self.spn_thr.value()
                # Convert operator symbol to a filename-safe word
                op_word = {
                    '>': 'gt', '≥': 'gte', '>=': 'gte',
                    '<': 'lt', '≤': 'lte', '<=': 'lte',
                    '=': 'eq', '≠': 'neq', '!=': 'neq',
                }.get(op_sym, 'op')
                mode_str = f'threshold_{op_word}{_fmt_val(thr)}'

        elif mode == 2:
            # Raster mask — include the mask layer name
            ml = self.cmb_mask_lyr.currentLayer()
            if ml:
                mask_stem = _clean(os.path.splitext(
                    os.path.basename(ml.source().split('|')[0]))[0])[:20]
                mode_str = f'raster_{mask_stem}'
            else:
                mode_str = 'raster'

        elif mode == 3:
            # Vector mask — include the vector layer name
            vl = self.cmb_vec.currentLayer()
            if vl:
                vec_stem = _clean(vl.name())[:20]
                mode_str = f'vector_{vec_stem}'
            else:
                mode_str = 'vector'
        else:
            mode_str = 'filter'

        # ── Optional suffixes ─────────────────────────────────────────────────
        inv_str = '_inv'    if self.chk_invert.isChecked()          else ''
        bin_str = '_binary' if self.cmb_out_type.currentData() == 1 else ''

        filename = f'{stem}_{op_str}_{mode_str}{inv_str}{bin_str}.tif'

        # Try to save next to the input raster (like SGTOOLS)
        src_dir = os.path.dirname(os.path.abspath(src_path))
        candidate = os.path.join(src_dir, filename)

        # Check write permission on that directory
        if os.path.isdir(src_dir) and os.access(src_dir, os.W_OK):
            # Avoid overwriting — append _2, _3 etc. if needed
            base, ext = os.path.splitext(candidate)
            counter = 1
            while os.path.exists(candidate):
                counter += 1
                candidate = f'{base}_{counter}{ext}'
            return candidate

        # Fallback: QGIS native temp system — directory always exists
        return QgsProcessingUtils.generateTempFilename(filename)

    def _on_cancel(self):
        if self._task:
            self._task.cancel()

    def _fallback_reenable(self):
        # Safety net: if taskTerminated never fires, re-enable the UI.
        # allTasksFinished fires for ALL tasks — only act if OUR task
        # is still pending (signals never fired for some reason).
        # QgsTask status: 3=Complete, 4=Terminated (task finished either way)
        DONE_STATUSES = (3, 4)
        if self._task is not None and self._task.status() not in DONE_STATUSES:
            self._on_done(self._task, False)

    def _on_progress(self, val):
        self.progress.setValue(int(val))
        self.progress.setFormat(f'{int(val)} %')

    def _on_done(self, task, success):
        self._set_busy(False)
        # Clear self._task (may already be None if user ran again)
        if self._task is task:
            self._task = None

        if not success or task.result_path is None:
            tb  = getattr(task, '_traceback', '')
            exc = task.exception
            if exc is not None:
                # Grab last meaningful line from traceback for the bar
                lines = [l.strip() for l in tb.splitlines()
                         if l.strip()
                         and not l.strip().startswith(
                             ('Traceback', 'File ', '  File '))]
                detail = lines[-1] if lines else str(exc)
                msg    = f'{type(exc).__name__}: {detail}'
            else:
                # No Python exception — task was cancelled or had a silent failure
                if 'isCanceled=True' in tb:
                    msg = 'Task was cancelled before completing.'
                elif tb:
                    msg = ('Task returned False with no error details.\n'
                           'Check Log Messages › MaskQ — the full '
                           'stack trace has been logged there.')
                else:
                    msg = ('Task failed — no exception captured.\n'
                           'See Log Messages › MaskQ for details.')
            if tb:
                QgsMessageLog.logMessage(
                    f'Full traceback:\n{tb}', 'MaskQ', Qgis.Critical)
                # Open the Log Messages panel so the user can see the full trace
                try:
                    self.iface.openMessageLog()
                except Exception:
                    pass
            self._bar(msg, Qgis.Critical, dur=12)
            # Restore layers that were unloaded before task launch (Windows lock)
            for lname, lsource in getattr(self, '_unloaded_sources', []):
                try:
                    rl = QgsRasterLayer(lsource, lname)
                    if rl.isValid():
                        QgsProject.instance().addMapLayer(rl)
                except Exception:
                    pass
            self._unloaded_sources = []
            return

        # ── success ───────────────────────────────────────────────────────────
        s   = task.result_stats
        p   = task.params
        out = task.result_path

        if p.get('load_output', True):
            name = os.path.splitext(os.path.basename(out))[0]
            ol   = QgsRasterLayer(out, name)
            if ol.isValid():
                if s.get('output_type') == 1:
                    col = self.btn_color.color()
                    transparent = QColor(0, 0, 0, 0)
                    ol.setRenderer(QgsPalettedRasterRenderer(
                        ol.dataProvider(), 1, [
                            QgsPalettedRasterRenderer.Class(
                                1, col, '1 — kept'),
                            QgsPalettedRasterRenderer.Class(
                                255, transparent, '255 — masked'),
                        ]))
                QgsProject.instance().addMapLayer(ol)

        # Persist colour + nodata
        QgsSettings().setValue('MaskQ/nodata',
                               self.spn_nodata.value())
        QgsSettings().setValue('MaskQ/color',
                               self.btn_color.color().name())

        pct = s.get('pct_valid', 0)
        el  = s.get('elapsed', 0)
        self._bar(
            f"Done in {el:.1f}s  ·  "
            f"{s.get('n_valid', 0):,} px kept ({pct:.1f}%)  ·  "
            f"{s.get('cols_out')}×{s.get('rows_out')} px",
            Qgis.Success, dur=8)

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _bar(self, text, level=Qgis.Info, dur=5):
        self.iface.messageBar().pushMessage(
            'MaskQ', text, level=level, duration=dur)

    def _set_busy(self, on):
        self.btn_run.setText('Processing…' if on else 'Run')
        self.btn_run.setEnabled(not on)
        self.btn_cancel.setEnabled(on)
        self.progress.setVisible(on)
        if not on:
            self.progress.setValue(0)

    def has_active_task(self):
        return self._task is not None

    def cleanup(self):
        """Called by plugin.unload() — stop threads, disconnect signals, remove preview.

        allTasksFinished is a global QGIS signal. Disconnecting it here prevents
        Qt from calling _fallback_reenable on a destroyed panel after reload.
        """
        # Disconnect global signal first — before any partial destruction
        try:
            QgsApplication.taskManager().allTasksFinished.disconnect(
                self._fallback_reenable)
        except (TypeError, RuntimeError):
            pass
        # Cancel any running task
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        self._stop_stats()
        self._destroy_preview()

    def changeEvent(self, event):
        """Re-apply the stylesheet when the system palette changes.
        Qt evaluates palette() expressions in QSS only at application time,
        not dynamically. Re-applying on PaletteChange ensures the panel
        updates correctly when the user switches QGIS between light and dark.
        """
        from qgis.PyQt.QtCore import QEvent
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange:
            inner = self.findChild(QWidget, 'MQPanel')
            if inner:
                inner.setStyleSheet(_STYLE)

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)
