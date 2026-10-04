# FreeCAD Nesting Workbench

A workbench for 2D nesting of shapes in FreeCAD, utilizing the Minkowski Sum algorithm for efficient packing.

[![Patreon](https://img.shields.io/badge/Support-Patreon-F96854?style=flat&logo=patreon)](https://www.patreon.com/cw/AngrySasquatch)

## Usage Guide

### 1. Preparing Parts
Select the 3D parts or 2D shapes you wish to nest from the Tree View or 3D View.

### 2. Running the Nester
Click the **Run Nesting** icon (or access via the Nesting menu). This opens the Nesting Task Panel.

### 3. Configuring Options

#### Sheet Settings
Every length in the panel is shown in, and accepts, the **active document's**
unit system — so an imperial document shows `23.62 in` where a metric one shows
`600.00 mm`. Fields also accept whatever FreeCAD's quantity parser does, so
`1/2 in`, `2ft 6in` and `1' 11"` all work in an imperial document. Internally
everything stays in millimetres regardless of what is displayed.

*   **Sheet Width/Height:** Dimensions of the material sheet.
*   **Sheet Thickness:** Thickness of the material (used for 3D visualization and CAM).
*   **Part Spacing:** Minimum distance between nested parts.

#### Bounds Resolution (Advanced)
*   **Curve Angle (Quality):** Controls how smooth curved edges are approximated. Lower angles (5-10°) give smoother curves but are slower. Higher angles (30°+) are faster but coarser.
*   **Simplify:** Tolerance for reducing determining points on a polygon. Higher values (1.0+) speed up nesting by removing tiny details.

#### Minkowski Nester Settings
These live in the panel's `Optimizations` section, except **Nesting Direction** and **Rotation Angle**, which are under `Nesting Settings` below. They are hidden entirely when the `Physics` algorithm is selected.

*   **Use Random Direction:** If checked, randomizes placement heuristics for potentially better (or worse) results.
*   **Clear NFP Cache:** Forces recalculation of No-Fit Polygons. Useful if you suspect caching issues, but slower.
*   **Candidate Step:** Spacing between candidate positions when sampling a No-Fit Polygon. The main control over run time.
*   **Rotation Threads:** How many candidate rotations are checked concurrently. `Auto` uses one per CPU core.
*   **Generations / Population Size:** Settings for the Genetic Algorithm optimizer. Both default to 1, which is a single pass with no search. Crossover only begins at a population of 3 — below that the loop carries the best layout forward plus one random layout, and never combines genes. Both values are remembered between sessions.
*   **Stop At Sheets:** Stop the run as soon as everything fits on this many sheets, with nothing left over. `0` (the default) turns it off and the run continues on its own rules. A run that reaches the target stops on that layout and is reported as a success, not a cancellation, so the fill phase and the completion message still run. A target that is never reached is reported as such when the run finishes, rather than being silently ignored. Deliberately not remembered between sessions, so a target set for one run cannot silently apply to the next.

#### Panel Sections
The panel is five collapsible sections. Click a title to expand or collapse it; three start collapsed and two start open.

| section | starts | holds |
|---|---|---|
| `Nesting Settings` | collapsed | the direction dial, **Use Random Direction**, **Rotation Angle** |
| `Optimizations` | open | **Generations**, **Population Size**, **Stop At Sheets**, **Candidate Step**, **Rotation Threads**, **Compactness**, and the candidate-geometry and NFP-cache switches |
| `Physics Nesting Settings` | open | the Physics nester's dials — gravity direction, step size, spawn and nesting-step limits, rotation and anneal curve settings, and its own random-direction and shake options |
| `Helpers` | collapsed | identifier labels, font, label size and height, simulate/show-bounds/sound switches |
| `Logging` | collapsed | the verbose and performance switches |

The first three follow the selected algorithm: switching to `Physics` hides `Nesting Settings` and `Optimizations` and shows `Physics Nesting Settings` instead, and switching back restores them. `Helpers` and `Logging` are always present, because their controls apply to whichever algorithm is running.

Collapsing a section hides its contents rather than disabling them, so a control inside a collapsed section still contributes to a run exactly as it would when open.

#### Helpers and Logging
Diagnostic controls, collapsed by default to keep the panel short. The `Helpers` group holds the identifier font, label size and height, and the simulate/show-bounds/sound switches; `Logging` holds the verbose and performance switches.

#### Nesting Settings
Collapsed by default, and hidden when the `Physics` algorithm is selected. Click a group title to expand it — see **Panel Sections** above for all five.

*   **Nesting Direction:** The direction the nester searches from. The dial steps in 15° increments; the readout below it gives the direction the run will actually use, which is not the same as the dial's angle. All four cardinals are reachable. Remembered between sessions, as is the **Use Random Direction** checkbox.
*   **Rotation Angle:** How many orientations of each part the nester may try.

#### Part Options (In the Table)
*   **Quantity:** How many copies of this part to nest.
*   **Rotations:** Global setting for rotation steps (e.g., 4 steps = 0°, 90°, 180°, 270°).
*   **Override:** Check this to set specific rotation behavior for individual parts.
*   **Up Dir:** Define which axis is "Up" (Z+, Y+, etc.) for projecting the 3D part to 2D.
*   **Fill:** Mark a part as "filler" to be placed in gaps after main parts are nested.

### 4. Generating the Layout
Click **Run Nesting** at the bottom of the panel.
*   The tool will process the shapes and generate a `Layout` group in the tree.
*   This group contains `Sheet` objects with the nested parts.

## Other Tools

*   **Stack Sheets:** Stacks the sheets at origin.
*   **Export Sheets:** Export the nested sheets to DXF or SVG files.
*   **Create CAM Job:** Generates a Path/CAM job from the nested layout, organizing parts, labels, and outlines for machining.
*   **Create Silhouette:** Generates a 2D projection (outline) of a 3D part which can be used in a cam job.
*   **Transform Parts:** (Experimental) A manual tool to move/rotate nested parts. **NOTE: This tool is currently under construction and may not function correctly.**

## License

Licensed under the GNU Lesser General Public License v2.1 or later. See [LICENSE](LICENSE) for the full text.
