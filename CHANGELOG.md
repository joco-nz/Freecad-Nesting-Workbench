# Changelog

All notable changes to the FreeCAD Nesting Workbench will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The panel's length fields follow the document's unit system. Sheet size,
  thickness, part spacing, label size and height, simplify tolerance, candidate
  step, physics step and anneal amplitudes, and the Manual Nester influence
  radius are now shown in, and accept, whatever units the document uses --
  including fractional and compound inch entry (`1/2 in`, `1' 11"`). Values
  still reach the nester as plain millimetres; nothing below the UI changed.
- Console and dialog messages that quote a dimension -- packing yield, CAM
  stock size, the sheet-thickness mismatch warning, the manual nester's physics
  log -- report it in the document's units instead of a hardcoded `mm`.
- `Helpers` and `Logging` collapse, and start collapsed, taking 167px off the
  panel's height. Their contents are diagnostic controls and were taller than
  the four sheet fields at the top of the panel. The collapse state is not
  remembered, so every open is the same shape.
  Note that a checkable `QGroupBox` alone does not do this: unchecking it only
  *disables* its children, leaving them visible and the panel just as tall.
  FreeCAD's own BIM workbench has that behaviour in its "Sun Position" group.
  Hiding the contents is what actually reclaims the space.
- Sheet size, part spacing, candidate step and rotation threads are laid out as
  two-column grids rather than single-field rows.
- The nesting direction dial steps in 15° increments, and the four cardinal
  buttons around it are gone -- all four directions are still on the 15° grid,
  so nothing was lost but three rows of height. The readout now shows the
  compass *bearing* rather than the dial reading, which differ by a quarter
  turn and a flip: it used to display 90 where the run searched 180, which was
  survivable while only the four button values were reachable and wrong at 20 of
  the 24 positions now reachable.
- The nesting direction and both "Use Random Direction" checkboxes are
  remembered between sessions. A restored random-direction run correctly leaves
  its dial disabled, so the direction cannot be set and then ignored.
- `Nesting Settings` collapses, and starts collapsed, like `Helpers` and
  `Logging`. Its dial is the tallest single control in the panel and is a
  set-and-forget choice.
- The three controls in `Nesting Settings` are laid out in two columns, with the
  dial spanning both rows on the right and the checkbox and rotation angle
  stacked to its left.

### Fixed
- A document switch while the panel is open no longer leaves it resolving
  against the previous document, which could nest one document's shapes into
  another. The fields also re-render for the newly active document, and a
  change to a document's unit system is picked up before the next run even
  though FreeCAD raises no event for it.
- The Manual Nester's influence radius can now display values above the field's
  maximum when set from Ctrl+scroll, instead of showing a number that disagreed
  with the radius actually in use.

### Notes
- `Population Size` deliberately still defaults to 1. Measured on the heavy
  corpus at a contested sheet size and on the n70 customer part, populations of
  1, 3, 4 and 10 all returned the same sheet count, placed count and density,
  while wall clock went 18s -> 86s and 49s -> 197s. Crossover does run across
  those populations (offspring 0 -> 2 -> 16), so this is not the degenerate
  small-population case; `create_ga_population` leaves the first layout
  unshuffled and unrotated, and it wins every generation and seed measured, so
  best-of-N converges on it. A higher default would multiply the cost of every
  first run for no measured gain. Set it as high as you like -- it now sticks.

## [1.0.1] - 2026-08-29

### Changed
- All toolbar icons converted from PNG to SVG for crisp rendering at any size.
- Icons renamed with a `Nesting_` prefix so they cannot collide with other
  addons in FreeCAD's global icon search path.
- `FreeCADGui.addIconPath()` moved out of module scope into `Initialize()`, so
  the workbench no longer affects icon lookup unless it is activated.

### Added
- Explicit `__init__.py` in the six packages that previously relied on implicit
  namespace packages.

## [1.0.0] - 2026-08-06

### Added
- 2D bin-packing nesting of 3D parts onto flat material sheets.
- Minkowski-Sum / No-Fit Polygon (NFP) placement engine with GA optimizer.
- Interactive Manual Nester tool with drag-and-drop and proximity physics repulsion.
- Direct FreeCAD CAM job creation from nested layouts.
- Multi-sheet DXF export utility.
- 2D projection silhouette generator for complex 3D geometry.
- Addon manifest `package.xml` and LGPL-2.1 license.
