# -*- coding: utf-8 -*-
"""
provider.py  —  QGIS Processing Toolbox integration.

Registers three algorithms so MaskQ modes are available from:
  • Processing Toolbox panel
  • Model Builder (graphical modeller)
  • Python console: processing.run('maskq:maskbyvaluerange', {...})

─────────────────────────────────────────────────────────────────────────────
HOW TO ADD A NEW ALGORITHM (mirrors adding a new mask mode in task.py)
─────────────────────────────────────────────────────────────────────────────
1. Create a class AlgoMyMode(_MQAlgoBase)
2. Implement: name(), displayName(), shortHelpString(), initAlgorithm(), processAlgorithm()
3. Register it in MaskQProvider.loadAlgorithms()
─────────────────────────────────────────────────────────────────────────────
"""
from qgis.core import (
    QgsProcessingProvider,
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterBand,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterRasterDestination,
    QgsProcessing,
    QgsApplication,
)
from .task import MaskQTask, EXT_DRIVER


# ── Provider ──────────────────────────────────────────────────────────────────

class MaskQProvider(QgsProcessingProvider):
    """Registers MaskQ algorithms with the QGIS Processing registry."""

    def id(self):       return 'maskq'
    def name(self):     return 'MaskQ'
    def longName(self): return 'MaskQ'
    def icon(self):     return QgsApplication.getThemeIcon('processingAlgorithm.svg')

    def loadAlgorithms(self):
        """Add all algorithms. To add a new one: instantiate it here."""
        for cls in (AlgoValueRange, AlgoRasterMask, AlgoVectorMask):
            self.addAlgorithm(cls())


# ── Shared base class ─────────────────────────────────────────────────────────

class _MQAlgoBase(QgsProcessingAlgorithm):
    """Base class shared by all three algorithms.

    Contains the output parameters that are identical across all modes
    (operation, output type, NoData, invert, preserve dtype, exclude NoData).
    Each subclass only defines its own input parameters.
    """

    # Operation options — order must match OP_OPTS below
    OP_LABELS = ['Mask (keep full extent, outside → NoData)',
                 'Clip (crop to extent, outside polygon → NoData)',
                 'Crop (crop to extent, keep ALL pixel values)']
    OP_OPTS   = ['mask', 'clip', 'crop']

    # Output type options
    OUT_LABELS = ['Real values', 'Binary (1=kept, 255=masked)']

    def group(self):   return 'MaskQ'
    def groupId(self): return 'maskq'

    def flags(self):
        """Run synchronously — avoids Qt threading issues in Processing."""
        try:
            return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading
        except AttributeError:
            return super().flags()

    def _add_common_params(self, config):
        """Add the output-side parameters shared by all three algorithms."""
        config.addParameter(QgsProcessingParameterEnum(
            'OPERATION', 'Operation',
            options=self.OP_LABELS, defaultValue=0))
        config.addParameter(QgsProcessingParameterEnum(
            'OUTPUT_TYPE', 'Output type',
            options=self.OUT_LABELS, defaultValue=0))
        config.addParameter(QgsProcessingParameterNumber(
            'NODATA_OUT', 'NoData output value',
            type=QgsProcessingParameterNumber.Double, defaultValue=-9999.0))
        config.addParameter(QgsProcessingParameterBoolean(
            'INVERT', 'Invert (swap kept ↔ removed)', defaultValue=False))
        config.addParameter(QgsProcessingParameterBoolean(
            'PRESERVE_DTYPE', 'Preserve input data type', defaultValue=True))
        config.addParameter(QgsProcessingParameterBoolean(
            'EXCLUDE_NODATA', 'Exclude existing NoData pixels', defaultValue=True))
        config.addParameter(QgsProcessingParameterRasterDestination(
            'OUTPUT', 'Output raster'))

    def _read_common_params(self, parameters, context):
        """Read the shared output parameters into a dict for the task."""
        return {
            'operation'     : self.OP_OPTS[
                self.parameterAsInt(parameters, 'OPERATION', context)],
            'output_type'   : self.parameterAsInt(
                parameters, 'OUTPUT_TYPE', context),
            'nodata_out'    : self.parameterAsDouble(
                parameters, 'NODATA_OUT', context),
            'invert'        : self.parameterAsBool(
                parameters, 'INVERT', context),
            'preserve_dtype': self.parameterAsBool(
                parameters, 'PRESERVE_DTYPE', context),
            'exclude_nodata': self.parameterAsBool(
                parameters, 'EXCLUDE_NODATA', context),
        }

    def _run_task(self, params, parameters, context, feedback):
        """Build and run the task synchronously, return the output path."""
        import os
        out_path = self.parameterAsOutputLayer(parameters, 'OUTPUT', context)
        ext      = os.path.splitext(out_path)[1].lower()
        params['output_path']   = out_path
        params['output_format'] = EXT_DRIVER.get(ext, 'GTiff')
        params['load_output']   = False   # Processing handles loading

        task = MaskQTask(self.displayName(), params)
        ok   = task.run()
        if not ok or task.result_path is None:
            msg = str(task.exception) if task.exception else 'Task failed.'
            feedback.reportError(msg, fatalError=True)
            return {}
        return {'OUTPUT': task.result_path}

    def createInstance(self):
        return self.__class__()


# ── Algorithm 1: Value Range ──────────────────────────────────────────────────

class AlgoValueRange(_MQAlgoBase):
    """Keep pixels within a value range or satisfying a threshold comparison."""

    def name(self):        return 'maskbyvaluerange'
    def displayName(self): return 'Mask / clip by value range'
    def shortHelpString(self):
        return (
            'Keep pixels whose value falls within [Min, Max] (Range mode) '
            'or satisfies a threshold comparison (Threshold mode). '
            'All other pixels become NoData or are cropped away.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            'INPUT', 'Input raster'))
        self.addParameter(QgsProcessingParameterBand(
            'BAND', 'Reference band',
            parentLayerParameterName='INPUT', defaultValue=1))
        self.addParameter(QgsProcessingParameterEnum(
            'CONDITION_TYPE', 'Condition type',
            options=['Range (min ≤ value ≤ max)', 'Threshold (value op threshold)'],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            'V_MIN', 'Min value (Range mode)',
            type=QgsProcessingParameterNumber.Double, defaultValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            'V_MAX', 'Max value (Range mode)',
            type=QgsProcessingParameterNumber.Double, defaultValue=1.0))
        self.addParameter(QgsProcessingParameterEnum(
            'OPERATOR', 'Operator (Threshold mode)',
            options=['>', '≥', '<', '≤', '=', '≠'],
            defaultValue=0, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            'THRESHOLD', 'Threshold value',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, optional=True))
        self._add_common_params(self)

    def processAlgorithm(self, parameters, context, feedback):
        lyr    = self.parameterAsRasterLayer(parameters, 'INPUT', context)
        params = self._read_common_params(parameters, context)
        params.update({
            'input_path'    : lyr.source(),
            'input_crs_wkt' : lyr.crs().toWkt() if lyr.crs().isValid() else '',
            'mode'          : 1,
            'ref_band'      : self.parameterAsInt(parameters, 'BAND', context),
            'condition_type': self.parameterAsInt(
                parameters, 'CONDITION_TYPE', context),
            'v_min'         : self.parameterAsDouble(parameters, 'V_MIN', context),
            'v_max'         : self.parameterAsDouble(parameters, 'V_MAX', context),
            'operator'      : ['>', '≥', '<', '≤', '=', '≠'][
                self.parameterAsInt(parameters, 'OPERATOR', context)],
            'threshold'     : self.parameterAsDouble(
                parameters, 'THRESHOLD', context),
        })
        return self._run_task(params, parameters, context, feedback)


# ── Algorithm 2: Raster Mask ──────────────────────────────────────────────────

class AlgoRasterMask(_MQAlgoBase):
    """Mask using a second raster (QA band, cloud mask, water mask, etc.)."""

    def name(self):        return 'maskbyraster'
    def displayName(self): return 'Mask / clip by raster mask'
    def shortHelpString(self):
        return (
            'Use a second raster (e.g. a QA band or cloud mask) to decide which '
            'pixels to keep. Pixels where the mask satisfies the condition are '
            'removed (set to NoData). The mask is warped to match the input grid '
            'if their extents or CRS differ.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            'INPUT', 'Input raster'))
        self.addParameter(QgsProcessingParameterRasterLayer(
            'MASK',  'Mask raster'))
        self.addParameter(QgsProcessingParameterBand(
            'MASK_BAND', 'Mask band',
            parentLayerParameterName='MASK', defaultValue=1))
        self.addParameter(QgsProcessingParameterEnum(
            'MASK_OP', 'Remove where mask value is',
            options=['=', '≠', '>', '≥', '<', '≤'], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            'MASK_VALUE', 'Comparison value',
            type=QgsProcessingParameterNumber.Double, defaultValue=1.0))
        self.addParameter(QgsProcessingParameterBoolean(
            'MASK_NODATA', 'Also remove mask NoData pixels', defaultValue=False))
        self.addParameter(QgsProcessingParameterEnum(
            'RESAMPLE', 'Resample method (for grid alignment)',
            options=['Nearest neighbour', 'Bilinear'], defaultValue=0))
        self._add_common_params(self)

    def processAlgorithm(self, parameters, context, feedback):
        lyr  = self.parameterAsRasterLayer(parameters, 'INPUT', context)
        mlyr = self.parameterAsRasterLayer(parameters, 'MASK',  context)
        params = self._read_common_params(parameters, context)
        params.update({
            'input_path'    : lyr.source(),
            'input_crs_wkt' : lyr.crs().toWkt() if lyr.crs().isValid() else '',
            'mode'          : 2,
            'mask_path'     : mlyr.source() if mlyr else '',
            'mask_band'     : self.parameterAsInt(parameters, 'MASK_BAND', context),
            'mask_op'       : ['=', '≠', '>', '≥', '<', '≤'][
                self.parameterAsInt(parameters, 'MASK_OP', context)],
            'mask_value'    : self.parameterAsDouble(
                parameters, 'MASK_VALUE', context),
            'mask_nodata'   : self.parameterAsBool(
                parameters, 'MASK_NODATA', context),
            'mask_resample' : ['near', 'bilinear'][
                self.parameterAsInt(parameters, 'RESAMPLE', context)],
        })
        return self._run_task(params, parameters, context, feedback)


# ── Algorithm 3: Vector Mask ──────────────────────────────────────────────────

class AlgoVectorMask(_MQAlgoBase):
    """Keep pixels inside polygon features from a vector layer."""

    def name(self):        return 'maskbyvector'
    def displayName(self): return 'Mask / clip by vector polygon'
    def shortHelpString(self):
        return (
            'Keep pixels whose centres fall inside polygon features. '
            'The vector is automatically reprojected to match the raster CRS. '
            'Use All-touched to include pixels that the polygon edge crosses.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            'INPUT', 'Input raster'))
        self.addParameter(QgsProcessingParameterVectorLayer(
            'VECTOR', 'Polygon layer',
            types=[QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterBoolean(
            'ALL_TOUCHED', 'All touched pixels (include edge pixels)',
            defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            'BUFFER', 'Buffer distance (map units)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0))
        self._add_common_params(self)

    def processAlgorithm(self, parameters, context, feedback):
        lyr  = self.parameterAsRasterLayer(parameters, 'INPUT', context)
        vlyr = self.parameterAsVectorLayer(parameters, 'VECTOR', context)
        params = self._read_common_params(parameters, context)
        params.update({
            'input_path'        : lyr.source(),
            'input_crs_wkt'     : lyr.crs().toWkt() if lyr.crs().isValid() else '',
            'mode'              : 3,
            'vector_path'       : vlyr.source() if vlyr else '',
            'vector_layer_name' : vlyr.name()   if vlyr else '',
            'vector_subset'     : vlyr.subsetString() if vlyr else '',
            'vector_sel_wkts'   : None,   # no selection support from Processing
            'all_touched'       : self.parameterAsBool(
                parameters, 'ALL_TOUCHED', context),
            'buffer_dist'       : self.parameterAsDouble(
                parameters, 'BUFFER', context),
        })
        return self._run_task(params, parameters, context, feedback)
