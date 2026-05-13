# -*- coding: utf-8 -*-
"""
task.py  —  Background computation task.

This file is the computation engine. It knows nothing about Qt widgets,
the QGIS panel, or the histogram. It only reads a params dict and writes
a GeoTIFF. All three mask modes live here as separate methods.

─────────────────────────────────────────────────────────────────────────────
HOW TO ADD A NEW MASK MODE
─────────────────────────────────────────────────────────────────────────────
A "mask mode" is any method that decides which pixels to keep.
It receives the raster dimensions and returns a bool numpy array
(True = keep that pixel, False = mask it to NoData).

Step 1 — Write the builder method in this file:

    def _mymode_mask(self, p, W, H, gt, prj):
        \"\"\"
        p   = params dict (everything the panel sent)
        W,H = raster width, height in pixels
        gt  = GDAL GeoTransform tuple  (origin, pixel size)
        prj = WKT projection string

        Return: np.ndarray of shape (H, W), dtype bool
                True  = pixel is kept
                False = pixel becomes NoData
                None  = something went wrong (set self.exception first)
        \"\"\"
        keep = np.ones((H, W), dtype=bool)   # example: keep everything
        # ... your logic here ...
        return keep

Step 2 — Register it in _execute() (one new elif, that's all):

    elif mode == 4:
        keep = self._mymode_mask(p, W, H, gt, prj)

Step 3 — Add a UI page in panel.py:
    • New tab button in _build_method_card()
    • New page widget in _build_mymode_page()
    • New params block in _on_run() under "if mode == 4:"

Step 4 — Add a Processing algorithm in provider.py:
    • New class AlgoMyMode(_MQAlgoBase)
    • Register it in MaskQProvider.loadAlgorithms()

─────────────────────────────────────────────────────────────────────────────
HOW TO ADD A NEW OUTPUT FORMAT
─────────────────────────────────────────────────────────────────────────────
Add one entry to EXT_DRIVER below. The panel's file filter picker and the
task's format-conversion path both read from this dict automatically.
No other changes needed.

─────────────────────────────────────────────────────────────────────────────
WHY gdal.UseExceptions() IS NOT CALLED HERE
─────────────────────────────────────────────────────────────────────────────
Inside QgsTask.run(), GDAL C++ exceptions bypass Python's try/except.
They go straight to the C++ QgsTask wrapper, which marks the task as
terminated and fires taskTerminated with exception=None — a silent failure
with no traceback. We avoid this by checking every GDAL return value
explicitly (if ds is None: ...) instead of relying on exceptions.
"""
import os, time, datetime, tempfile
import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import QgsTask, QgsMessageLog, Qgis

# ── module-level constants ────────────────────────────────────────────────────

# Maps file extension → GDAL driver name.
# Used by: panel.py (file filter), task.py (format conversion).
# To support a new format: add one line here.
EXT_DRIVER = {
    '.tif':  'GTiff',  '.tiff': 'GTiff',
    '.jp2':  'JP2OpenJPEG',
    '.img':  'HFA',
    '.bil':  'ENVI',   '.bip':  'ENVI',  '.bsq': 'ENVI',
    '.nc':   'netCDF',
    '.gpkg': 'GPKG',
    '.sdat': 'SAGA',
    '.asc':  'AAIGrid',
}

_CAT    = 'MaskQ'   # Log Messages tab name

_STRIP  = 512              # Rows read per loop iteration.
                           # Keeps memory use bounded for very large rasters.

_BIN_ND = 255              # NoData value for binary (Byte) output.
                           # 1 = kept pixel, 255 = masked pixel.

# GTiff creation options applied to every output file.
# LZW compression + tiling makes files smaller and faster to display in QGIS.
_GTIFF = [
    'COMPRESS=LZW',
    'BLOCKXSIZE=512', 'BLOCKYSIZE=512',
    'TILED=YES',
    'BIGTIFF=IF_SAFER',   # auto-switch to BigTIFF if file would exceed 4 GB
]

# Maps GDAL data type integer → (numpy dtype, min valid value, max valid value).
# Used to decide whether the user's chosen NoData value fits in the output dtype.
_DTYPES = {
    gdal.GDT_Byte:    (np.uint8,    0,            255),
    gdal.GDT_UInt16:  (np.uint16,   0,            65535),
    gdal.GDT_Int16:   (np.int16,   -32768,        32767),
    gdal.GDT_UInt32:  (np.uint32,   0,            4294967295),
    gdal.GDT_Int32:   (np.int32,   -2147483648,   2147483647),
    gdal.GDT_Float32: (np.float32, -3.4e38,       3.4e38),
    gdal.GDT_Float64: (np.float64, -1.8e308,      1.8e308),
}


# ── module-level helper functions ─────────────────────────────────────────────

def _cmp(array, op, value):
    """Apply a comparison operator to a numpy array.
    Returns a bool array: True where the condition holds.
    Supports both ASCII (>=) and unicode (≥) operators.
    """
    return {
        '=':  array == value,  '≠': array != value,  '!=': array != value,
        '>':  array >  value,  '≥': array >= value,  '>=': array >= value,
        '<':  array <  value,  '≤': array <= value,  '<=': array <= value,
    }.get(op, array == value)


def _out_dtype(input_gdt, nodata_value, preserve):
    """Choose the output GDAL data type and numpy dtype.

    If preserve=True and the NoData value fits in the input dtype,
    keep the same type (no unnecessary precision loss).
    Otherwise fall back to Float32 (safe for any value).
    """
    if not preserve:
        return gdal.GDT_Float32, np.float32
    info = _DTYPES.get(input_gdt)
    if not info:
        return gdal.GDT_Float32, np.float32
    npt, lo, hi = info
    try:
        nd = float(nodata_value)
        fits = lo <= nd <= hi
        # Integer types need the NoData to be a whole number too
        if input_gdt not in (gdal.GDT_Float32, gdal.GDT_Float64):
            fits = fits and (nd == int(nd))
        return (input_gdt, npt) if fits else (gdal.GDT_Float32, np.float32)
    except Exception:
        return gdal.GDT_Float32, np.float32


def _wkt_to_srs(wkt):
    """Parse a WKT string into an osr.SpatialReference.
    Returns None if the string is empty or invalid.
    Sets axis mapping to traditional GIS order (lon, lat) which is
    required for GDAL geometry operations on geographic CRS.
    """
    if not wkt:
        return None
    srs = osr.SpatialReference()
    if srs.ImportFromWkt(wkt) != 0:
        return None
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


# ── task class ────────────────────────────────────────────────────────────────

class MaskQTask(QgsTask):
    """Background task that reads a raster, builds a mask, and writes output.

    Public attributes set after run() completes:
        result_path  (str | None)  — absolute path to the output file
        result_stats (dict)        — pixel counts, timing, dimensions
        exception    (Exception | None) — set if the task failed
        _traceback   (str)         — full Python traceback on failure
    """

    def __init__(self, description, params):
        # CanCancel flag: shows Cancel button in QGIS task manager.
        # The flag attribute path changed in QGIS 3.22 — try both.
        try:
            flags = QgsTask.Flag.CanCancel
        except AttributeError:
            flags = QgsTask.CanCancel
        super().__init__(description, flags)

        self.params       = params  # dict from panel._on_run()
        self.result_path  = None
        self.result_stats = {}
        self.exception    = None
        self._traceback   = ''
        self._temps       = []      # temp files to delete in finally block

    # ── QgsTask interface ─────────────────────────────────────────────────────

    def run(self):
        """Entry point called by QgsTaskManager on a background thread.
        Must return True on success, False on failure.
        Never raises — all exceptions are caught and stored in self.exception.
        """
        p = self.params
        QgsMessageLog.logMessage(
            f'MaskQ run: mode={p.get("mode")} '
            f'op={p.get("operation")} '
            f'input={p.get("input_path", "?")[:80]}',
            _CAT, Qgis.Info)
        try:
            ok = self._execute()
            # If _execute returned False without setting self.exception,
            # something unexpected happened. Create a bug-report message.
            if not ok and self.exception is None and not self.isCanceled():
                import traceback as _tb
                self.exception  = RuntimeError(
                    'Task returned False without an exception. '
                    'This is a bug — please report it.')
                self._traceback = ''.join(_tb.format_stack())
                QgsMessageLog.logMessage(
                    f'Unexpected False return.\n{self._traceback}',
                    _CAT, Qgis.Critical)
            return ok
        except Exception as exc:
            import traceback as _tb
            self.exception  = exc
            self._traceback = _tb.format_exc()
            QgsMessageLog.logMessage(
                f'Exception:\n{self._traceback}', _CAT, Qgis.Critical)
            return False
        finally:
            self._cleanup()   # always delete temp files, even on crash

    def finished(self, result):
        """Called by QgsTask after run() completes (on the main thread).
        We don't use this — panel._on_done() handles completion via signals.
        """
        pass

    # ── temp file helpers ─────────────────────────────────────────────────────

    def _tmp(self, suffix):
        """Create a temp file, register it for cleanup, return its path.
        The file is created empty (so the name is guaranteed unique on disk).
        IMPORTANT: if you pass this path to a GDAL driver that refuses to
        overwrite an existing file (e.g. GPKG), delete it first:
            path = self._tmp('.gpkg')
            os.remove(path)   # remove the empty placeholder
            gdal.CreateCopy(path, ...)   # now GDAL can create it fresh
        """
        t = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        t.close()
        self._temps.append(t.name)
        return t.name

    def _cleanup(self):
        """Delete all temp files registered via _tmp().
        Called in the finally block of run() so this always executes.
        """
        for path in self._temps:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        self._temps.clear()

    # ── main execution pipeline ───────────────────────────────────────────────

    def _execute(self):
        """Full pipeline: open raster → build mask → write output.

        Returns True on success, False on any failure.
        On failure, self.exception is always set before returning.
        """
        p  = self.params
        t0 = time.time()

        # ── Step 1: Open input raster ─────────────────────────────────────────
        # Always resolve to absolute path. dataSourceUri() can return a path
        # relative to the QGIS project file, and background threads on Windows
        # have an undefined working directory.
        src = os.path.abspath(p['input_path'].strip())
        ds  = gdal.Open(src, gdal.GA_ReadOnly)
        if ds is None:
            self.exception = RuntimeError(
                f'Cannot open raster:\n{src}\n\n'
                'Only local file-based rasters are supported.\n'
                'XYZ tiles, WMS, and WCS service layers cannot be processed.\n'
                'Check the file path is correct and the file is not corrupted.')
            return False

        # Read raster metadata — used throughout the pipeline
        nb  = ds.RasterCount          # number of bands
        W   = ds.RasterXSize          # width in pixels
        H   = ds.RasterYSize          # height in pixels
        gt  = ds.GetGeoTransform()    # (originX, pixelW, 0, originY, 0, pixelH)
        prj = ds.GetProjection()      # WKT CRS string
        idt = ds.GetRasterBand(1).DataType                                 # GDAL type int
        nds = [ds.GetRasterBand(i).GetNoDataValue() for i in range(1, nb+1)]  # per-band NoData
        dsc = [ds.GetRasterBand(i).GetDescription() or '' for i in range(1, nb+1)]

        self.setProgress(5)
        if self.isCanceled():
            return False

        # ── Step 2: Build the keep-mask ───────────────────────────────────────
        # Each mask builder returns a (H × W) bool array.
        # True  = this pixel passes the condition → keep it
        # False = this pixel fails the condition → replace with NoData
        # None  = an error occurred (self.exception is already set)
        mode = p['mode']
        if   mode == 1: keep = self._range_mask(ds, p, W, H, nds)
        elif mode == 2: keep = self._raster_mask(p, W, H, gt, prj)
        elif mode == 3: keep = self._vector_mask(p, W, H, gt, prj)
        else:
            self.exception = ValueError(
                f'Unknown mode {mode}. '
                'Valid modes are 1 (value range), 2 (raster mask), 3 (vector mask).')
            return False

        if keep is None:
            # Mask builder already set self.exception
            if self.exception is None:
                self.exception = RuntimeError(
                    'Mask builder returned None without setting an exception.')
            return False

        self.setProgress(55)
        if self.isCanceled():
            return False

        # Invert: swap kept ↔ masked pixels
        if p.get('invert', False):
            keep = ~keep

        # Log pixel statistics for debugging
        n_keep = int(np.sum(keep))
        QgsMessageLog.logMessage(
            f'Mask built: {n_keep:,} / {keep.size:,} px kept '
            f'({100 * n_keep / keep.size:.1f}%)',
            _CAT, Qgis.Info)

        # ── Step 3: Determine output window ───────────────────────────────────
        # Mask  → output covers the full input extent  (rs, cs = slice(None))
        # Clip  → output is cropped to the tight bounding box of kept pixels.
        #         This is equivalent to QGIS "Clip raster by mask layer".
        #         The GeoTransform origin is shifted to the bbox corner.
        op      = p.get('operation', 'mask')
        do_clip = op in ('clip', 'mask_clip', 'crop')  # mask_clip kept for back-compat
        do_mask = op != 'crop'   # 'crop' keeps ALL pixel values inside bbox

        if do_clip:
            ri = np.where(np.any(keep, axis=1))[0]  # rows that have ≥1 True pixel
            ci = np.where(np.any(keep, axis=0))[0]  # cols that have ≥1 True pixel
            if len(ri) == 0:
                self.exception = RuntimeError(
                    'No valid pixels after masking — nothing to clip to.\n'
                    'Try Mask instead of Clip, or check your condition/polygon.')
                return False
            rm, rM = int(ri[0]), int(ri[-1])   # first and last kept row
            cm, cM = int(ci[0]), int(ci[-1])   # first and last kept col
            # Shift GeoTransform origin to top-left corner of the bounding box
            new_gt = (gt[0] + cm * gt[1], gt[1], gt[2],
                      gt[3] + rm * gt[5], gt[4], gt[5])
            oW, oH = cM - cm + 1, rM - rm + 1  # output pixel dimensions
            rs = slice(rm, rM + 1)              # row slice into keep / input
            cs = slice(cm, cM + 1)              # col slice into keep / input
        else:
            new_gt = gt
            oW, oH = W, H
            rs = cs = slice(None)   # full extent

        self.setProgress(60)
        if self.isCanceled():
            return False

        # ── Step 4: Choose output data type ──────────────────────────────────
        binary = (p.get('output_type') == 1)
        nd_out = float(p.get('nodata_out', -9999))
        if binary:
            # Binary output: always Byte, 1 = kept, 255 = masked
            o_gdt, o_np, nd_out, n_out = gdal.GDT_Byte, np.uint8, float(_BIN_ND), 1
        else:
            # Real-values output: keep original dtype if NoData fits in it
            o_gdt, o_np = _out_dtype(idt, nd_out, p.get('preserve_dtype', True))
            n_out = nb

        self.setProgress(65)

        # ── Step 5: Create output file ────────────────────────────────────────
        out_path = p['output_path']
        out_fmt  = p.get('output_format', 'GTiff')
        is_tiff  = (out_fmt == 'GTiff')
        # For non-GTiff formats, write to a temp GTiff first then convert
        write_to = out_path if is_tiff else self._tmp('_rf_work.tif')

        drv    = gdal.GetDriverByName('GTiff')
        ds_out = drv.Create(write_to, oW, oH, n_out, o_gdt, _GTIFF)
        if ds_out is None:
            self.exception = RuntimeError(
                f'Cannot create output file:\n{write_to}\n\n'
                'Check the directory exists and is writable.\n'
                'Leave the path blank to auto-save next to the input raster.')
            return False

        ds_out.SetGeoTransform(new_gt)
        ds_out.SetProjection(prj)

        # ── Step 6: Write pixel data ──────────────────────────────────────────
        # We read in horizontal strips of _STRIP rows to keep memory bounded.
        # For each strip:
        #   data = original pixel values from input
        #   msk  = True where pixel passes condition (clipped window)
        #   output = data where msk=True, NoData where msk=False
        if binary:
            b = ds_out.GetRasterBand(1)
            b.SetNoDataValue(float(_BIN_ND))
            b.SetDescription('binary_mask')
            for y0 in range(0, oH, _STRIP):
                if self.isCanceled(): break
                h  = min(_STRIP, oH - y0)
                sy = y0 + (rs.start or 0)   # row offset in the full input
                if do_mask:
                    row_bin = np.where(
                        keep[sy:sy + h, cs], 1, _BIN_ND).astype(np.uint8)
                else:
                    row_bin = np.ones((h, oW), dtype=np.uint8)  # crop: all=1
                b.WriteArray(row_bin, 0, y0)
            b.FlushCache()
        else:
            for bi in range(nb):
                b_in  = ds.GetRasterBand(bi + 1)
                b_out = ds_out.GetRasterBand(bi + 1)
                b_out.SetNoDataValue(nd_out)
                if dsc[bi]:
                    b_out.SetDescription(dsc[bi])  # preserve band name
                nd_b = nds[bi]                      # input NoData for this band
                for y0 in range(0, oH, _STRIP):
                    if self.isCanceled(): break
                    h    = min(_STRIP, oH - y0)
                    sy   = y0 + (rs.start or 0)
                    sx   = cs.start or 0
                    data = b_in.ReadAsArray(sx, sy, oW, h).astype(np.float64)
                    if do_mask:
                        # Mask or Clip: pixels outside condition → NoData
                        msk = keep[sy:sy + h, cs].copy()
                        if nd_b is not None and p.get('exclude_nodata', True):
                            msk &= (data != nd_b)
                        row_out = np.where(msk, data, nd_out).astype(o_np)
                    else:
                        # Crop: pure rectangular crop — write ALL pixel values as-is
                        row_out = data.astype(o_np)
                    b_out.WriteArray(row_out, 0, y0)
                b_out.FlushCache()
                self.setProgress(65 + int(25 * (bi + 1) / nb))

        ds_out.FlushCache()
        ds_out = None
        ds     = None

        # ── Step 7: Convert format if not GTiff ───────────────────────────────
        if not is_tiff:
            self.setProgress(92)
            fdrv = gdal.GetDriverByName(out_fmt)
            if fdrv is None:
                self.exception = RuntimeError(
                    f'GDAL driver "{out_fmt}" is not available in this build.\n'
                    'Install the matching GDAL plugin or choose a different format.')
                return False
            tmp_ds = gdal.Open(write_to, gdal.GA_ReadOnly)
            if tmp_ds is None:
                self.exception = RuntimeError(
                    f'Cannot re-open intermediate GTiff: {write_to}')
                return False
            res = fdrv.CreateCopy(out_path, tmp_ds)
            tmp_ds = None
            if res is None:
                self.exception = RuntimeError(
                    f'Format conversion to {out_fmt} failed.\nOutput: {out_path}')
                return False
            res = None

        # ── Step 8: Record result statistics ─────────────────────────────────
        nk = int(np.sum(keep))
        nt = keep.size
        self.result_path  = out_path
        self.result_stats = {
            'n_valid'    : nk,
            'n_masked'   : nt - nk,
            'pct_valid'  : round(nk / nt * 100, 2) if nt else 0.0,
            'cols_out'   : oW,
            'rows_out'   : oH,
            'elapsed'    : round(time.time() - t0, 2),
            'mode'       : mode,
            'operation'  : op,
            'output_type': p.get('output_type', 0),
            'timestamp'  : datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        self.setProgress(100)
        return True

    # ═════════════════════════════════════════════════════════════════════════
    # MASK BUILDERS
    # Each method returns a (H × W) bool array, or None on error.
    # On None: self.exception must be set before returning.
    # ═════════════════════════════════════════════════════════════════════════

    def _range_mask(self, ds, p, W, H, nds):
        """Mode 1 — Value range / threshold mask.

        Two sub-modes (set by params['condition_type']):
          0 = Range:     keep pixels where  v_min ≤ value ≤ v_max
          1 = Threshold: keep pixels where  value <op> threshold
              where <op> is one of  > ≥ < ≤ = ≠

        Reads the selected band in horizontal strips for memory efficiency.
        """
        ref   = max(0, min(p.get('ref_band', 1) - 1, ds.RasterCount - 1))
        band  = ds.GetRasterBand(ref + 1)
        nd    = nds[ref]                       # input NoData value for this band
        ctype = p.get('condition_type', 0)     # 0=range, 1=threshold
        vmin  = p.get('v_min',      0.0)
        vmax  = p.get('v_max',      1.0)
        op    = p.get('operator',   '>')
        thr   = p.get('threshold',  0.0)
        excl  = p.get('exclude_nodata', True)

        keep = np.zeros((H, W), dtype=bool)
        for y0 in range(0, H, _STRIP):
            if self.isCanceled():
                return None
            h   = min(_STRIP, H - y0)
            arr = band.ReadAsArray(0, y0, W, h).astype(np.float64)
            # Apply the condition
            m = ((arr >= vmin) & (arr <= vmax)) if ctype == 0 \
                else _cmp(arr, op, thr)
            # Exclude original NoData pixels (they are not real data values)
            if excl and nd is not None:
                m &= (arr != nd)
            keep[y0:y0 + h, :] = m
            self.setProgress(5 + int(45 * (y0 + h) / H))
        return keep

    def _raster_mask(self, p, W, H, gt, prj):
        """Mode 2 — Raster mask (e.g. QA band, cloud mask, water mask).

        Opens a second raster and removes pixels where its values satisfy
        a condition (e.g. "remove where mask = 1" removes cloudy pixels).
        If the mask raster has a different grid than the input, it is warped
        to match using gdal.Warp before pixel comparison.
        """
        mpath = p.get('mask_path', '')
        if mpath:
            mpath = os.path.abspath(mpath.strip())
        if not mpath or not os.path.exists(mpath):
            self.exception = RuntimeError(f'Mask raster not found:\n{mpath}')
            return None

        mds = gdal.Open(mpath, gdal.GA_ReadOnly)
        if mds is None:
            self.exception = RuntimeError(f'Cannot open mask raster:\n{mpath}')
            return None

        # Check if the mask grid matches the input grid
        # (same pixel count AND same geotransform to within floating-point tolerance)
        need_warp = (mds.RasterXSize != W or mds.RasterYSize != H)
        if not need_warp:
            mgt       = mds.GetGeoTransform()
            need_warp = any(abs(mgt[i] - gt[i]) > 1e-10 for i in range(6))

        if need_warp:
            # Warp the mask to exactly match the input raster's grid
            tmp  = self._tmp('_rf_mask.tif')
            ulx, uly = gt[0], gt[3]
            lrx, lry = gt[0] + W * gt[1], gt[3] + H * gt[5]
            mval = p.get('mask_value', 1.0)
            algo = (gdal.GRA_Bilinear
                    if p.get('mask_resample') == 'bilinear'
                    else gdal.GRA_NearestNeighbour)
            # mval+1e9 is a sentinel fill value that won't clash with real mask values
            gdal.Warp(tmp, mds, options=gdal.WarpOptions(
                width=W, height=H,
                outputBounds=(ulx, lry, lrx, uly),
                dstSRS=prj, resampleAlg=algo,
                dstNodata=mval + 1e9))
            mds = None
            mds = gdal.Open(tmp, gdal.GA_ReadOnly)
            if mds is None:
                self.exception = RuntimeError('Warp of mask raster failed.')
                return None

        mb  = max(1, min(p.get('mask_band', 1), mds.RasterCount))
        bnd = mds.GetRasterBand(mb)
        mnd = bnd.GetNoDataValue()

        # Start with all pixels kept; remove where condition holds
        keep = np.ones((H, W), dtype=bool)
        for y0 in range(0, H, _STRIP):
            if self.isCanceled():
                return None
            h   = min(_STRIP, H - y0)
            arr = bnd.ReadAsArray(0, y0, W, h).astype(np.float64)
            # Pixels to REMOVE (note: we remove, not keep, so logic is inverted)
            rem = _cmp(arr, p.get('mask_op', '='), p.get('mask_value', 1.0))
            # Also remove pixels that are NoData in the mask raster.
            # Default is False — only apply when the user explicitly ticks it.
            # If mask nodata=0 and we default True, all 0-valued (clear) pixels
            # get removed along with the flagged pixels — everything gets masked.
            if p.get('mask_nodata', False) and mnd is not None:
                rem |= (arr == mnd)
            keep[y0:y0 + h, :] &= ~rem   # keep = keep AND NOT remove
            self.setProgress(5 + int(45 * (y0 + h) / H))

        mds = None
        return keep

    def _vector_mask(self, p, W, H, gt, prj):
        """Mode 3 — Vector polygon mask.

        Burns polygon features onto the raster grid and returns a bool array
        where True = pixel centre is inside a polygon.

        Processing pipeline:
          1. Open the vector file with OGR.
          2. Apply the QGIS definition query (subsetString) if the layer was
             loaded with a filter (e.g. NAME_2 = 'TA Mwamlowe').
          3. Apply selected-features filter if the user ticked "Selected only".
          4. Apply buffer if requested.
          5. Reproject features to the raster CRS using osr (feature-by-feature,
             no temp files — avoids the GPKG driver "file already exists" error).
          6. Rasterise with gdal.RasterizeLayer.
          7. Log extents and pixel counts for debugging.
        """
        vpath = p.get('vector_path', '')
        if vpath:
            vpath = os.path.abspath(vpath.strip())
        if not vpath or not os.path.exists(vpath):
            self.exception = RuntimeError(f'Vector file not found:\n{vpath}')
            return None

        ds_v = ogr.Open(vpath, 0)   # 0 = read-only
        if ds_v is None:
            self.exception = RuntimeError(f'Cannot open vector:\n{vpath}')
            return None

        # Get the right layer — by OGR layer name if available, else layer 0.
        # For GPKG files this distinguishes between multiple layers in one file.
        lname = p.get('vector_layer_name', '')
        lyr   = (ds_v.GetLayerByName(lname) if lname else None) \
                or ds_v.GetLayer(0)
        if lyr is None:
            self.exception = RuntimeError('No usable layer in vector file.')
            return None

        src_srs = lyr.GetSpatialRef()

        # ── Apply QGIS definition query ───────────────────────────────────────
        # When a QGIS layer has a definition query (e.g. set via Layer Properties
        # or loaded with a |subset=... URI), that WHERE clause is stored in
        # vl.subsetString(). OGR ignores it when opening the file — we must
        # apply it manually via SetAttributeFilter() so only filtered features
        # are rasterised, not the full shapefile.
        subset = p.get('vector_subset', '').strip()
        if subset:
            if lyr.SetAttributeFilter(subset) != 0:
                self.exception = RuntimeError(
                    f'Could not apply definition query:\n{subset}\n\n'
                    'The expression may use QGIS syntax that OGR does not support.\n'
                    'Alternative: select the features manually in QGIS and tick '
                    '"Selected features only" in the plugin.')
                return None
            QgsMessageLog.logMessage(
                f'Applied definition query: {subset}', _CAT, Qgis.Info)

        # ── Apply selected features ───────────────────────────────────────────
        # Geometries are passed as WKT strings (not QGIS FIDs) because QGIS
        # feature IDs and OGR feature IDs are not guaranteed to match,
        # especially for filtered or reprojected layers.
        sel_wkts = p.get('vector_sel_wkts')
        if sel_wkts:
            mem  = ogr.GetDriverByName('Memory').CreateDataSource('sel')
            mlyr = mem.CreateLayer('sel', srs=src_srs, geom_type=ogr.wkbPolygon)
            for wkt in sel_wkts:
                g = ogr.CreateGeometryFromWkt(wkt)
                if g:
                    nf = ogr.Feature(mlyr.GetLayerDefn())
                    nf.SetGeometry(g)
                    mlyr.CreateFeature(nf)
            lyr     = mlyr
            ds_v    = mem
            src_srs = lyr.GetSpatialRef()
            QgsMessageLog.logMessage(
                f'Using {len(sel_wkts)} selected feature geometries',
                _CAT, Qgis.Info)

        # ── Apply buffer ──────────────────────────────────────────────────────
        buf = p.get('buffer_dist', 0.0)
        if buf and buf > 0:
            bmem = ogr.GetDriverByName('Memory').CreateDataSource('buf')
            blyr = bmem.CreateLayer('buf', srs=src_srs, geom_type=ogr.wkbPolygon)
            lyr.ResetReading()
            for feat in lyr:
                g = feat.GetGeometryRef()
                if g:
                    nf = ogr.Feature(blyr.GetLayerDefn())
                    nf.SetGeometry(g.Buffer(buf))   # OGR Buffer in CRS units
                    blyr.CreateFeature(nf)
            lyr     = blyr
            ds_v    = bmem
            src_srs = lyr.GetSpatialRef()

        # ── Reproject features to raster CRS ─────────────────────────────────
        # We always reproject — even if the CRS looks the same.
        # String-based WKT comparison is unreliable: two WKTs can describe
        # identical projections but differ in parameter order or authority name.
        # osr.IsSame() does a proper semantic comparison.
        # We use feature-by-feature transformation (not VectorTranslate or
        # a GPKG temp file) to avoid the "file already exists" crash.
        dst_srs = _wkt_to_srs(prj)
        if dst_srs is None:
            self.exception = RuntimeError(
                'Input raster has no valid CRS. '
                'Assign a CRS to it in QGIS before using vector mask.')
            return None

        needs_transform = True
        if src_srs is not None:
            src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            needs_transform = not bool(src_srs.IsSame(dst_srs))

        QgsMessageLog.logMessage(
            f'Vector: {lyr.GetFeatureCount()} features, '
            f'needs_reproject={needs_transform}',
            _CAT, Qgis.Info)

        # Write reprojected (or copied) features into an in-memory OGR layer
        out_mem = ogr.GetDriverByName('Memory').CreateDataSource('out')
        out_lyr = out_mem.CreateLayer('out', srs=dst_srs, geom_type=ogr.wkbPolygon)

        ct = osr.CoordinateTransformation(src_srs, dst_srs) \
             if (needs_transform and src_srs is not None) else None

        lyr.ResetReading()
        n_ok = 0
        for feat in lyr:
            g = feat.GetGeometryRef()
            if g is None:
                continue
            g2 = g.Clone()
            if ct is not None and g2.Transform(ct) != 0:
                continue    # skip features where reprojection fails
            nf = ogr.Feature(out_lyr.GetLayerDefn())
            nf.SetGeometry(g2)
            out_lyr.CreateFeature(nf)
            n_ok += 1

        if n_ok == 0:
            self.exception = RuntimeError(
                f'No features could be rasterised.\n'
                f'The layer has {lyr.GetFeatureCount()} feature(s) but none '
                f'produced valid geometries after reprojection.\n'
                f'Check that the vector layer has a CRS assigned.')
            return None

        # Log extents for debugging — these appear in the Log Messages panel
        env       = out_lyr.GetExtent()     # (minX, maxX, minY, maxY)
        rast_minX = gt[0]
        rast_maxX = gt[0] + W * gt[1]
        rast_minY = gt[3] + H * gt[5]
        rast_maxY = gt[3]
        QgsMessageLog.logMessage(
            f'Vector extent (in raster CRS): '
            f'X={env[0]:.1f}–{env[1]:.1f}  Y={env[2]:.1f}–{env[3]:.1f}\n'
            f'Raster extent:                 '
            f'X={rast_minX:.1f}–{rast_maxX:.1f}  Y={rast_minY:.1f}–{rast_maxY:.1f}\n'
            f'Features written: {n_ok}',
            _CAT, Qgis.Info)

        # Check overlap — if the extents don't intersect, result would be 0%
        overlap = (env[0] < rast_maxX and env[1] > rast_minX and
                   env[2] < rast_maxY and env[3] > rast_minY)
        if not overlap:
            self.exception = RuntimeError(
                'Vector polygon does not overlap the raster extent.\n'
                f'Vector:  X {env[0]:.1f}–{env[1]:.1f},  '
                f'Y {env[2]:.1f}–{env[3]:.1f}\n'
                f'Raster:  X {rast_minX:.1f}–{rast_maxX:.1f},  '
                f'Y {rast_minY:.1f}–{rast_maxY:.1f}\n\n'
                'Both layers must be in the same projected CRS.\n'
                'Check Layer Properties → CRS for both layers.')
            return None

        # ── Rasterise ─────────────────────────────────────────────────────────
        self.setProgress(20)
        if self.isCanceled():
            return None

        burn = self._tmp('_rf_burn.tif')
        bds  = gdal.GetDriverByName('GTiff').Create(
            burn, W, H, 1, gdal.GDT_Byte)
        if bds is None:
            self.exception = RuntimeError(
                'Cannot create rasterisation temp file.')
            return None
        bds.SetGeoTransform(gt)
        bds.SetProjection(prj)
        bb = bds.GetRasterBand(1)
        bb.Fill(0)                  # default = 0 (outside polygon)
        bb.SetNoDataValue(255)
        opts = ['ALL_TOUCHED=TRUE'] if p.get('all_touched') else []
        gdal.RasterizeLayer(bds, [1], out_lyr, burn_values=[1], options=opts)
        bds.FlushCache()
        bds     = None
        out_lyr = None
        out_mem = None

        rds  = gdal.Open(burn, gdal.GA_ReadOnly)
        if rds is None:
            self.exception = RuntimeError('Cannot read rasterisation result.')
            return None
        data = rds.GetRasterBand(1).ReadAsArray()
        rds  = None

        n_burned = int(np.sum(data == 1))
        QgsMessageLog.logMessage(
            f'Rasterised: {n_burned:,} / {W*H:,} px inside polygon '
            f'({100 * n_burned / (W*H):.1f}%)',
            _CAT, Qgis.Info)

        return data == 1    # True where pixel is inside a polygon
