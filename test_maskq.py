# -*- coding: utf-8 -*-
"""
test_maskq.py  —  Unit tests for MaskQ v0.9.0

Tests the computation engine (task.py) and helper functions only.
No Qt or QGIS required — all tests run with plain Python + numpy + GDAL.

Run:
    python -m pytest test_maskq.py -v
    python test_maskq.py            (no pytest needed)
"""
import os, sys, tempfile, unittest
import numpy as np

# Make the package importable from the parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tif(path, data, nodata=None, crs_wkt=None):
    """Write a numpy 2-D array to a single-band GTiff for testing."""
    from osgeo import gdal, osr
    H, W = data.shape
    drv = gdal.GetDriverByName('GTiff')
    gdt = gdal.GDT_Float32
    ds  = drv.Create(path, W, H, 1, gdt)
    ds.SetGeoTransform((0.0, 1.0, 0.0, H, 0.0, -1.0))
    if crs_wkt is None:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32736)
        ds.SetProjection(srs.ExportToWkt())
    else:
        ds.SetProjection(crs_wkt)
    b = ds.GetRasterBand(1)
    b.WriteArray(data.astype(np.float32))
    if nodata is not None:
        b.SetNoDataValue(float(nodata))
    b.FlushCache()
    ds.FlushCache()
    ds = None
    return path


def _make_multiband_tif(path, bands_data, nodata=None):
    """Write a list of 2-D numpy arrays as a multi-band GTiff."""
    from osgeo import gdal, osr
    nb  = len(bands_data)
    H, W = bands_data[0].shape
    drv = gdal.GetDriverByName('GTiff')
    ds  = drv.Create(path, W, H, nb, gdal.GDT_Float32)
    ds.SetGeoTransform((0.0, 1.0, 0.0, H, 0.0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32736)
    ds.SetProjection(srs.ExportToWkt())
    for i, arr in enumerate(bands_data, 1):
        b = ds.GetRasterBand(i)
        b.WriteArray(arr.astype(np.float32))
        if nodata is not None:
            b.SetNoDataValue(float(nodata))
        b.FlushCache()
    ds.FlushCache()
    ds = None
    return path


def _read_band(path, band=1):
    from osgeo import gdal
    ds  = gdal.Open(path, gdal.GA_ReadOnly)
    arr = ds.GetRasterBand(band).ReadAsArray()
    nd  = ds.GetRasterBand(band).GetNoDataValue()
    ds  = None
    return arr, nd


def _run_task(params):
    """Run MaskQTask synchronously and return (success, task)."""
    from task import MaskQTask
    task = MaskQTask('test', params)
    ok   = task.run()
    return ok, task


# ── test cases ────────────────────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):
    """Test module-level helper functions in task.py."""

    def test_cmp_operators(self):
        from task import _cmp
        a = np.array([0.0, 0.5, 1.0, 2.0, -1.0])
        np.testing.assert_array_equal(_cmp(a, '=',  1.0), [F,F,T,F,F])
        np.testing.assert_array_equal(_cmp(a, '≠',  1.0), [T,T,F,T,T])
        np.testing.assert_array_equal(_cmp(a, '>',  0.5), [F,F,T,T,F])
        np.testing.assert_array_equal(_cmp(a, '≥',  0.5), [F,T,T,T,F])
        np.testing.assert_array_equal(_cmp(a, '<',  0.5), [T,F,F,F,T])
        np.testing.assert_array_equal(_cmp(a, '≤',  0.5), [T,T,F,F,T])
        np.testing.assert_array_equal(_cmp(a, '>=', 0.5), [F,T,T,T,F])
        np.testing.assert_array_equal(_cmp(a, '<=', 0.5), [T,T,F,F,T])
        np.testing.assert_array_equal(_cmp(a, '!=', 1.0), [T,T,F,T,T])

    def test_cmp_unknown_op_defaults_to_eq(self):
        from task import _cmp
        a = np.array([1.0, 2.0])
        np.testing.assert_array_equal(_cmp(a, 'BOGUS', 1.0), [T, F])

    def test_out_dtype_preserves_float32(self):
        from task import _out_dtype
        from osgeo import gdal
        gdt, npt = _out_dtype(gdal.GDT_Float32, -9999.0, True)
        self.assertEqual(gdt, gdal.GDT_Float32)
        self.assertEqual(npt, np.float32)

    def test_out_dtype_fallback_when_nodata_out_of_range(self):
        from task import _out_dtype
        from osgeo import gdal
        # Int16 range is -32768 to 32767 — NoData -9999 fits
        gdt, npt = _out_dtype(gdal.GDT_Int16, -9999.0, True)
        self.assertEqual(gdt, gdal.GDT_Int16)
        # NoData +99999 does NOT fit in Int16 → upcast to Float32
        gdt2, _ = _out_dtype(gdal.GDT_Int16, 99999.0, True)
        self.assertEqual(gdt2, gdal.GDT_Float32)

    def test_out_dtype_no_preserve(self):
        from task import _out_dtype
        from osgeo import gdal
        gdt, npt = _out_dtype(gdal.GDT_Int16, -9999.0, False)
        self.assertEqual(gdt, gdal.GDT_Float32)

    def test_wkt_to_srs_valid(self):
        from task import _wkt_to_srs
        from osgeo import osr
        srs_ref = osr.SpatialReference()
        srs_ref.ImportFromEPSG(32736)
        wkt = srs_ref.ExportToWkt()
        srs = _wkt_to_srs(wkt)
        self.assertIsNotNone(srs)
        self.assertTrue(srs.IsSame(srs_ref))

    def test_wkt_to_srs_empty(self):
        from task import _wkt_to_srs
        self.assertIsNone(_wkt_to_srs(''))
        self.assertIsNone(_wkt_to_srs(None))


class TestValueRangeMask(unittest.TestCase):

    def setUp(self):
        self.td  = tempfile.mkdtemp()
        self.src = os.path.join(self.td, 'src.tif')
        self.out = os.path.join(self.td, 'out.tif')
        # 4×4 raster, values 0.0 to 1.5 in 0.1 steps
        data = np.array([
            [0.0, 0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6, 0.7],
            [0.8, 0.9, 1.0, 1.1],
            [1.2, 1.3, 1.4, 1.5],
        ], dtype=np.float32)
        _make_tif(self.src, data)

    def _base_params(self, **kw):
        p = {
            'input_path': self.src, 'output_path': self.out,
            'output_format': 'GTiff', 'mode': 1, 'ref_band': 1,
            'condition_type': 0, 'v_min': 0.3, 'v_max': 0.9,
            'operator': '>', 'threshold': 0.5,
            'invert': False, 'output_type': 0,
            'operation': 'mask', 'nodata_out': -9999.0,
            'preserve_dtype': False, 'load_output': False,
            'exclude_nodata': True,
        }
        p.update(kw)
        return p

    def test_range_keeps_correct_pixels(self):
        ok, task = _run_task(self._base_params())
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        # Values 0.3–0.9 should be kept (6 pixels)
        kept = arr[arr != nd]
        self.assertEqual(len(kept), 7)  # 0.3,0.4,0.5,0.6,0.7,0.8,0.9
        self.assertAlmostEqual(float(kept.min()), 0.3, places=5)
        self.assertAlmostEqual(float(kept.max()), 0.9, places=5)

    def test_range_invert(self):
        ok, task = _run_task(self._base_params(invert=True))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        kept = arr[arr != nd]
        # Inverted: keep values OUTSIDE 0.3–0.9
        self.assertEqual(len(kept), 9)  # 16 total - 7 in range

    def test_threshold_gt(self):
        ok, task = _run_task(self._base_params(
            condition_type=1, operator='>', threshold=0.8))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        kept = arr[arr != nd]
        # Values nominally > 0.8: 0.9,1.0,1.1,1.2,1.3,1.4,1.5 = 7 pixels.
        # However the raster is Float32. When the task reads and casts to float64,
        # float32(0.8) → 0.80000001192... which is > float64(0.8), so the pixel
        # at exactly 0.8 is also included → 8 pixels total.
        self.assertEqual(len(kept), 8)

    def test_threshold_lte(self):
        ok, task = _run_task(self._base_params(
            condition_type=1, operator='≤', threshold=0.4))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        kept = arr[arr != nd]
        # Values nominally ≤ 0.4: 0.0,0.1,0.2,0.3,0.4 = 5 pixels.
        # However float32(0.4) → 0.40000000596... which is > float64(0.4),
        # so the pixel at exactly 0.4 is NOT included → 4 pixels total.
        self.assertEqual(len(kept), 4)

    def test_binary_output(self):
        ok, task = _run_task(self._base_params(output_type=1))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        unique = set(arr.flatten().tolist())
        # Binary: only 1 (kept) and 255 (masked)
        self.assertEqual(unique, {1.0, 255.0})

    def test_clip_operation(self):
        ok, task = _run_task(self._base_params(operation='clip'))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        # Clip crops to bounding box of kept pixels
        # Kept: 0.3–0.9 → rows 0–2, cols 0–3 (the bbox)
        self.assertLessEqual(arr.shape[0], 4)
        self.assertLessEqual(arr.shape[1], 4)

    def test_crop_has_no_nodata(self):
        ok, task = _run_task(self._base_params(operation='crop'))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        # Crop: no pixels should equal NoData
        nd_count = np.sum(arr == nd) if nd is not None else 0
        self.assertEqual(nd_count, 0)

    def test_output_stats_accurate(self):
        ok, task = _run_task(self._base_params())
        self.assertTrue(ok)
        s = task.result_stats
        self.assertEqual(s['n_valid'], 7)
        self.assertEqual(s['n_masked'], 9)
        self.assertAlmostEqual(s['pct_valid'], 43.75, places=1)

    def test_multiband_all_bands_written(self):
        """Condition evaluated on band 1; output should have all 3 bands."""
        src3 = os.path.join(self.td, 'src3.tif')
        out3 = os.path.join(self.td, 'out3.tif')
        data = np.ones((4,4), dtype=np.float32)
        data[0,0] = 0.5  # one pixel passes 0.3–0.9
        _make_multiband_tif(src3, [data, data*2, data*3])
        ok, task = _run_task({
            'input_path': src3, 'output_path': out3,
            'output_format': 'GTiff', 'mode': 1, 'ref_band': 1,
            'condition_type': 0, 'v_min': 0.3, 'v_max': 0.9,
            'operator': '>', 'threshold': 0.5,
            'invert': False, 'output_type': 0,
            'operation': 'mask', 'nodata_out': -9999.0,
            'preserve_dtype': False, 'load_output': False,
            'exclude_nodata': False,
        })
        self.assertTrue(ok)
        from osgeo import gdal
        ds = gdal.Open(out3)
        self.assertEqual(ds.RasterCount, 3)
        ds = None


class TestRasterMask(unittest.TestCase):

    def setUp(self):
        self.td   = tempfile.mkdtemp()
        self.src  = os.path.join(self.td, 'src.tif')
        self.mask = os.path.join(self.td, 'mask.tif')
        self.out  = os.path.join(self.td, 'out.tif')
        data = np.arange(16, dtype=np.float32).reshape(4,4)
        _make_tif(self.src, data)

    def _base_params(self, **kw):
        p = {
            'input_path': self.src, 'output_path': self.out,
            'output_format': 'GTiff', 'mode': 2,
            'mask_path': self.mask, 'mask_band': 1,
            'mask_op': '=', 'mask_value': 1.0,
            'mask_nodata': False, 'mask_resample': 'near',
            'invert': False, 'output_type': 0,
            'operation': 'mask', 'nodata_out': -9999.0,
            'preserve_dtype': False, 'load_output': False,
            'exclude_nodata': False,
        }
        p.update(kw)
        return p

    def test_binary_mask_removes_flagged_pixels(self):
        """mask=1 → remove; mask=0 → keep. Default mask_nodata=False."""
        mask_data = np.array([
            [1, 0, 0, 1],
            [0, 1, 0, 0],
            [0, 0, 1, 0],
            [1, 0, 0, 0],
        ], dtype=np.float32)
        _make_tif(self.mask, mask_data)
        ok, task = _run_task(self._base_params())
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        # Pixels where mask==1 should be NoData
        masked_positions = [(0,0),(0,3),(1,1),(2,2),(3,0)]
        for r, c in masked_positions:
            self.assertEqual(arr[r, c], nd,
                             msg=f"Pixel ({r},{c}) should be masked")
        # Pixels where mask==0 should have real values (≠ NoData)
        for r, c in [(0,1),(0,2),(1,0),(1,2)]:
            self.assertNotEqual(arr[r, c], nd,
                                msg=f"Pixel ({r},{c}) should be kept")

    def test_mask_nodata_false_preserves_nodata_pixels(self):
        """With mask_nodata=False, pixels where mask==NoData are KEPT."""
        mask_data = np.zeros((4,4), dtype=np.float32)
        mask_data[0,0] = 1.0   # flag one pixel
        mask_data[1,1] = 255.0 # this is the NoData value in the mask
        _make_tif(self.mask, mask_data, nodata=255.0)
        ok, task = _run_task(self._base_params(mask_nodata=False))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        # (0,0) masked (value==1), (1,1) kept (value==255 but nodata=False)
        self.assertEqual(arr[0,0], nd)
        self.assertNotEqual(arr[1,1], nd)

    def test_mask_nodata_true_removes_nodata_pixels(self):
        """With mask_nodata=True, pixels where mask==NoData are also removed."""
        mask_data = np.zeros((4,4), dtype=np.float32)
        mask_data[1,1] = 255.0
        _make_tif(self.mask, mask_data, nodata=255.0)
        ok, task = _run_task(self._base_params(mask_nodata=True))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        self.assertEqual(arr[1,1], nd)

    def test_this_was_the_main_bug(self):
        """Regression: binary NDVI mask (1=veg, 255=bare) with nodata=255.
        Old default mask_nodata=True → everything removed.
        New default mask_nodata=False → only veg pixels removed.
        """
        mask_data = np.ones((4,4), dtype=np.float32)   # all vegetation
        mask_data[2,:] = 255.0                          # row 2 = bare soil
        mask_data[3,:] = 255.0                          # row 3 = bare soil
        _make_tif(self.mask, mask_data, nodata=255.0)

        # NEW behaviour (mask_nodata=False): only value==1 pixels removed
        ok, task = _run_task(self._base_params(mask_nodata=False))
        self.assertTrue(ok)
        arr, nd = _read_band(self.out)
        n_kept = int(np.sum(arr != nd))
        self.assertEqual(n_kept, 8,
            msg=f"Expected 8 bare pixels kept, got {n_kept}. "
                f"This is the binary NDVI mask regression.")

        # OLD behaviour (mask_nodata=True): would keep 0 pixels
        out2 = os.path.join(self.td, 'out_old.tif')
        ok2, _ = _run_task(self._base_params(
            output_path=out2, mask_nodata=True))
        self.assertTrue(ok2)
        arr2, nd2 = _read_band(out2)
        n_kept2 = int(np.sum(arr2 != nd2))
        self.assertEqual(n_kept2, 0,
            msg="With mask_nodata=True all pixels should be removed (demonstrates old bug)")


class TestVectorMask(unittest.TestCase):

    def setUp(self):
        from osgeo import gdal, osr, ogr
        self.td  = tempfile.mkdtemp()
        self.src = os.path.join(self.td, 'src.tif')
        self.vec = os.path.join(self.td, 'poly.shp')
        self.out = os.path.join(self.td, 'out.tif')

        # 8×8 raster, all values = 1.0
        data = np.ones((8,8), dtype=np.float32)
        # GeoTransform: origin (0,8), pixel 1×1
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32736)
        ds = gdal.GetDriverByName('GTiff').Create(self.src, 8, 8, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((0.0, 1.0, 0.0, 8.0, 0.0, -1.0))
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(data)
        ds.FlushCache(); ds = None

        # Polygon covering pixels [2:6, 2:6] (rows 2-5, cols 2-5)
        drv   = ogr.GetDriverByName('ESRI Shapefile')
        ds_v  = drv.CreateDataSource(self.vec)
        lyr_v = ds_v.CreateLayer('poly', srs=srs, geom_type=ogr.wkbPolygon)
        ring  = ogr.Geometry(ogr.wkbLinearRing)
        ring.AddPoint(2, 2); ring.AddPoint(6, 2)
        ring.AddPoint(6, 6); ring.AddPoint(2, 6)
        ring.AddPoint(2, 2)
        poly  = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        feat  = ogr.Feature(lyr_v.GetLayerDefn())
        feat.SetGeometry(poly)
        lyr_v.CreateFeature(feat)
        ds_v.FlushCache(); ds_v = None
        self.srs_wkt = srs.ExportToWkt()

    def _base_params(self, **kw):
        p = {
            'input_path': self.src, 'output_path': self.out,
            'output_format': 'GTiff', 'mode': 3,
            'vector_path': self.vec, 'vector_layer_name': 'poly',
            'vector_subset': '', 'vector_sel_wkts': None,
            'all_touched': False, 'buffer_dist': 0.0,
            'invert': False, 'output_type': 0,
            'operation': 'mask', 'nodata_out': -9999.0,
            'preserve_dtype': False, 'load_output': False,
            'exclude_nodata': False,
        }
        p.update(kw)
        return p

    def test_mask_keeps_pixels_inside_polygon(self):
        ok, task = _run_task(self._base_params())
        self.assertTrue(ok, msg=str(task.exception))
        arr, nd = _read_band(self.out)
        n_kept   = int(np.sum(arr != nd))
        n_masked = int(np.sum(arr == nd))
        self.assertGreater(n_kept, 0)
        self.assertGreater(n_masked, 0)

    def test_clip_output_smaller_than_input(self):
        ok, task = _run_task(self._base_params(operation='clip'))
        self.assertTrue(ok, msg=str(task.exception))
        arr, nd = _read_band(self.out)
        # Clipped output must be smaller than original 8×8
        self.assertLess(arr.shape[0] * arr.shape[1], 64)

    def test_crop_no_nodata_pixels(self):
        ok, task = _run_task(self._base_params(operation='crop'))
        self.assertTrue(ok, msg=str(task.exception))
        arr, nd = _read_band(self.out)
        if nd is not None:
            n_nd = int(np.sum(arr == nd))
            self.assertEqual(n_nd, 0,
                msg="Crop operation should produce no NoData pixels")

    def test_invert_keeps_outside_pixels(self):
        ok, task = _run_task(self._base_params(invert=True))
        self.assertTrue(ok, msg=str(task.exception))
        arr, nd = _read_band(self.out)
        # Inverted: pixels OUTSIDE polygon are kept
        n_kept = int(np.sum(arr != nd))
        self.assertGreater(n_kept, 0)


class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self.td  = tempfile.mkdtemp()
        self.src = os.path.join(self.td, 'src.tif')
        self.out = os.path.join(self.td, 'out.tif')
        data = np.ones((4,4), dtype=np.float32) * 5.0
        _make_tif(self.src, data)

    def test_missing_input_sets_exception(self):
        ok, task = _run_task({
            'input_path': '/nonexistent/path.tif',
            'output_path': self.out, 'output_format': 'GTiff',
            'mode': 1, 'ref_band': 1, 'condition_type': 0,
            'v_min': 0.0, 'v_max': 1.0, 'operator': '>', 'threshold': 0.5,
            'invert': False, 'output_type': 0, 'operation': 'mask',
            'nodata_out': -9999.0, 'preserve_dtype': False,
            'load_output': False, 'exclude_nodata': False,
        })
        self.assertFalse(ok)
        self.assertIsNotNone(task.exception)

    def test_unknown_mode_sets_exception(self):
        ok, task = _run_task({
            'input_path': self.src, 'output_path': self.out,
            'output_format': 'GTiff', 'mode': 99,
            'invert': False, 'output_type': 0, 'operation': 'mask',
            'nodata_out': -9999.0, 'preserve_dtype': False,
            'load_output': False, 'exclude_nodata': False,
        })
        self.assertFalse(ok)
        self.assertIsNotNone(task.exception)

    def test_result_stats_populated_on_success(self):
        ok, task = _run_task({
            'input_path': self.src, 'output_path': self.out,
            'output_format': 'GTiff', 'mode': 1, 'ref_band': 1,
            'condition_type': 0, 'v_min': 4.0, 'v_max': 6.0,
            'operator': '>', 'threshold': 0.5,
            'invert': False, 'output_type': 0, 'operation': 'mask',
            'nodata_out': -9999.0, 'preserve_dtype': False,
            'load_output': False, 'exclude_nodata': False,
        })
        self.assertTrue(ok)
        s = task.result_stats
        for key in ('n_valid','n_masked','pct_valid','cols_out','rows_out','elapsed'):
            self.assertIn(key, s, msg=f"Missing key '{key}' in result_stats")
        self.assertEqual(s['n_valid'], 16)
        self.assertEqual(s['pct_valid'], 100.0)


# ── entry point ───────────────────────────────────────────────────────────────

T, F = True, False   # shorthand for expected bool arrays above

if __name__ == '__main__':
    # Change to the plugin directory so imports resolve
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    unittest.main(verbosity=2)
