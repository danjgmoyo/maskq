# MaskQ

**A QGIS plugin for masking and clipping rasters by condition, raster mask, polygon boundary, or a shape drawn on the canvas.**

[![QGIS](https://img.shields.io/badge/QGIS-3.16%2B-green)](https://qgis.org)
[![License](https://img.shields.io/badge/License-GPL--2.0%2B-blue)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Experimental-yellow)]()

---

## What it does

MaskQ lets you keep only the pixels that meet a condition and write everything else as NoData — or crop the raster entirely to the boundary of the kept pixels.

Four masking modes:

| Mode | Description |
|---|---|
| **Value Range** | Keep pixels within a min–max range or meeting a threshold (`> 500`, `≤ 0.3`, etc.) |
| **Raster Mask** | Use a second raster (QA band, cloud mask, water mask) to flag pixels for removal |
| **Vector Mask** | Keep pixels inside a polygon layer — respects definition queries and selected features |
| **Canvas Draw** | Draw the shape directly on the map — Freehand, Polygon (click to place vertices), Rectangle (drag a box), or the current map extent. Multiple shapes per run |

Three output operations (Clip and Crop are available for Vector Mask and Canvas Draw):

| Operation | Result |
|---|---|
| **Mask** | Full raster extent, pixels outside condition → NoData |
| **Clip** | Crop to bounding box of kept pixels, outside polygon → NoData |
| **Crop** | Crop to bounding box, all pixel values kept (no masking) |

---

## Features

- **Interactive histogram** with draggable handles — set your range visually
- **Live canvas preview** — highlights kept pixels on the map as you adjust the range
- **All bands preserved** — condition evaluated on one band, output contains every band
- **Smart output naming** — embeds condition values in the filename (`NDVI_masked_range_0p3to0p8.tif`)
- **QGIS Processing Toolbox** integration — use from the Toolbox, Model Builder, or Python console
- **Respects layer filters** — honours QGIS definition queries on vector layers
- Saves next to the input raster by default, or to any path you choose

---

## Installation

### From the QGIS Plugin Repository (recommended)

1. Open QGIS → **Plugins → Manage and Install Plugins**
2. Tick **Show experimental plugins** in Settings
3. Search for **MaskQ**
4. Click **Install**

### Manual install from ZIP

1. Download `maskq.zip` from [Releases](https://github.com/danjgmoyo/maskq/releases)
2. QGIS → **Plugins → Manage and Install Plugins → Install from ZIP**
3. Browse to the downloaded ZIP → **Install Plugin**

---

## Usage

1. Open the panel from **Raster → MaskQ** or the toolbar icon
2. Select your input raster
3. Choose a mask method (Value Range / Raster Mask / Vector Mask / Canvas Draw)
4. Set the condition — or, for Canvas Draw, click Freehand, Polygon or Rectangle and draw on the map (Esc or right-click cancels; Polygon closes on double-click, Enter or right-click, Backspace removes the last vertex), or press Use Current Extent
5. Choose an operation (Mask / Clip / Crop)
6. Click **Run**

The output is saved next to the input raster with a descriptive name, or to the path you specify.

---

## Processing Toolbox

MaskQ registers four algorithms under the **MaskQ** group (`maskbyvaluerange`, `maskbyraster`, `maskbyvector`, `maskbycanvasdraw`):

```python
# Value range mask
processing.run("maskq:maskbyvaluerange", {
    'INPUT': 'path/to/raster.tif',
    'BAND': 1,
    'CONDITION_TYPE': 0,      # 0=Range, 1=Threshold
    'V_MIN': 0.3,
    'V_MAX': 0.8,
    'OPERATION': 0,           # 0=Mask, 1=Clip, 2=Crop
    'OUTPUT': 'path/to/output.tif'
})

# Canvas draw mask — polygon(s) supplied as WKT, separate multiple with ';'
processing.run("maskq:maskbycanvasdraw", {
    'INPUT': 'path/to/raster.tif',
    'GEOMETRY': 'POLYGON((500000 9000000, 501000 9000000, 501000 9001000, 500000 9000000))',
    'GEOMETRY_CRS': 'EPSG:32736',   # optional — defaults to the raster CRS
    'OPERATION': 0,                 # 0=Mask, 1=Clip, 2=Crop
    'OUTPUT': 'path/to/output.tif'
})
```

---

## Requirements

- QGIS 3.16 or later
- GDAL (bundled with QGIS)

---

## Known limitations

- Local file-based rasters only — XYZ tiles, WMS, and WCS layers are not supported
- Live preview available for Value Range mode only
- Canvas Draw shapes are not saved between QGIS sessions
- Raster mask must use a single band as the flag layer

---


## License

GPL-2.0-or-later — see [LICENSE](LICENSE)

---

## Author

Daniel Moyo — [moyodanj@gmail.com](mailto:moyodanj@gmail.com)
