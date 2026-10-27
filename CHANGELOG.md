# Changelog

All notable changes to MaskQ are documented here.

## [0.1.2]

### Added
- **Canvas Draw mode** — draw the mask shape directly on the map canvas, no vector layer needed:
  - **Freehand** — drag to trace, release to close
  - **Polygon** — click to place vertices; double-click, Enter or right-click closes; Backspace removes the last vertex
  - **Rectangle** — drag a box on the map
  - **Use Current Extent** — one click captures the visible map extent
  - Esc or right-click cancels any tool; tools arm with a single click
- Multiple shapes can be drawn and combined in a single run; each stays visible on the canvas until cleared
- Canvas Draw supports the same **Mask, Clip and Crop** operations as Vector Mask, plus an All-touched option
- Output names reflect the draw method and shape count (e.g. `dem_masked_canvasdraw_freehand.tif`)
- New Processing Toolbox algorithm **Mask / clip by canvas draw (WKT)** — `maskq:maskbycanvasdraw` — takes polygon WKT (multiple shapes separated by `;`) and an optional CRS; usable from Model Builder and the Python console

### Fixed
- The "output same as input" check now runs in the processing engine, so Processing and Model Builder runs are protected too; it compares files on disk, so relative paths and layer-name suffixes can't bypass it
- Errors that were silently swallowed (teardown and cosmetic code) are now written to the MaskQ tab in the Log Messages panel

### Changed
- New `canvas_draw.py` module holds the interactive map tools, keeping `panel.py` free of canvas-tool logic

## [0.1.1]

### Changed
- Range mode Min/Max boundaries can each be inclusive (`>=`, `<=`) or strict (`>`, `<`), matching Threshold mode
- Histogram handles show solid (inclusive) or dashed (strict) lines for both Range and Threshold modes
- Live preview toggle moved to the top of Output Options; highlight colour picker restored

### Fixed
- Histogram handles stuck disabled until a value was changed manually

### Added
- Confirmation prompt before overwriting an existing output file
- Placeholder text clarifying that a blank output path saves a `.tif` by default

## [0.1.0]

- First public release: mask or clip rasters by value range/threshold, a second raster mask, or a vector polygon boundary
