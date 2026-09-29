# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Centralized FreeCAD property names and type strings for the Nesting Workbench.
Import these constants instead of using hardcoded strings.
"""

# -- FreeCAD Property Type Strings --
PROP_LENGTH = "App::PropertyLength"
PROP_FLOAT = "App::PropertyFloat"
PROP_BOOL = "App::PropertyBool"
PROP_INTEGER = "App::PropertyInteger"
PROP_FILE = "App::PropertyFile"

# -- Layout Property Names --
PROP_SHEET_WIDTH = "SheetWidth"
PROP_SHEET_HEIGHT = "SheetHeight"
PROP_PART_SPACING = "PartSpacing"
PROP_SHEET_THICKNESS = "SheetThickness"
PROP_DEFLECTION_ANGLE = "DeflectionAngle"
PROP_SIMPLIFICATION = "Simplification"
PROP_FONT_FILE = "FontFile"
PROP_SHOW_BOUNDS = "ShowBounds"
PROP_ADD_LABELS = "AddLabels"
PROP_LABEL_HEIGHT = "LabelHeight"
PROP_LABEL_SIZE = "LabelSize"
PROP_GLOBAL_ROTATION_STEPS = "GlobalRotationSteps"
PROP_GENERATIONS = "Generations"
PROP_POPULATION_SIZE = "PopulationSize"
PROP_NESTING_DIRECTION = "NestingDirection"

# -- FreeCAD Preferences Path --
PREFS_PATH = "User parameter:BaseApp/Preferences/NestingWorkbench"

# -- Algorithm Presets --
# Rotation angle presets (degrees). Index 0 = coarsest, last = finest.
PHYSICS_ROTATION_PRESETS = [360, 90, 45, 30, 15, 10, 5, 2, 1]
MINKOWSKI_ROTATION_PRESETS = [360, 180, 120, 90, 45, 30, 15, 10, 5, 1]

# -- Nesting Direction --
# Dial reading for each named direction, and the default.
#
# The dial is an absolute Qt angle, and nesting_controller converts it with
# `angle_deg = (270 - value) % 360` before taking cos/sin. The dial reading is
# therefore NOT the compass bearing, and the two differ by a 90-degree turn plus
# a flip. Verified by conversion:
#
#       0 -> 270 deg -> ( 0, -1)  Down      180 ->  90 deg -> (0,  1)  Up
#      90 -> 180 deg -> (-1,  0)  Left      270 ->   0 deg -> (1,  0)  Right
#
# Default is Left (90). Was Down (0).
#
# Lives in constants rather than in the panel because NestingJob needs the same
# value as a fallback, and a job object has no business importing a GUI module
# to read a number.
DIRECTION_LABELS = {0: "Down", 90: "Left", 180: "Up", 270: "Right"}
DEFAULT_DIRECTION_DIAL = 90


# -- Behaviour Defaults --
# Lives here rather than beside CandidateGeometryCache because ui_nesting needs it
# at module scope and must not import nesting_strategy: that pulls in Shapely,
# and the panel has to keep importing when the optional nesting dependency is
# missing (the same reason ga_coordinator imports the strategy lazily).
#
# Single source of truth on purpose. The UI checkbox, the preference read-back,
# the controller's two fallbacks, the coordinator's gate and the benchmark
# harness all consult this, because five hardcoded booleans is five chances to
# disagree about what the workbench actually does.
#
# Default-on is measured, not assumed. On the n70 workload, interleaved A/B,
# min of 3 (tests/freecad_harness/RESULTS-9.4.md):
#
#     wall                72.688s -> 62.715s   0.863x
#     candidate_geometry  10757ms ->  886ms   -91.8%
#     hit rate                            92.2%
#     peak RSS            580 MiB -> 580 MiB  +0 MiB
#     placed/sheets/density  122 / 2 / 0.418433, identical in all 6 runs
#
# Layout-neutrality is what made it safe to enable. The cache is an unbounded
# per-run dict with no eviction, so the two real risks were memory and returning
# the wrong polygon; +0 MiB measured, and 20 tests now cover the key algebra.
#
# Existing installs: the preference is written on save, so anyone who has
# already run this workbench has CandidateGeometryCache=False stored and keeps
# the old behaviour. Deliberate -- a saved setting is the user's choice and
# should not be silently overridden -- but it does mean this default reaches new
# users and anyone who resets preferences, not everyone.
CANDIDATE_GEOMETRY_CACHE_DEFAULT = True
