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
PROP_STRING = "App::PropertyString"

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
PROP_RANDOM_DIRECTION = "RandomDirection"

#: Which algorithm produced the layout. Read by
#: `NestingController._load_params_from_layout` and, before this was written,
#: read by nothing else -- so a Physics layout reopened as Minkowski and showed
#: the wrong algorithm's entire settings section. See issues.md NEST-001.
#:
#: It exists because `NestingDirection` is ambiguous on its own: the same dial
#: reading means a different search direction under each algorithm, because each
#: has its own dial. Recording the direction without recording which dial it came
#: from records a number that cannot be interpreted.
PROP_ALGORITHM = "Algorithm"

#: The Physics algorithm's own direction dial, as a **preference** key.
#:
#: Deliberately not `PROP_NESTING_DIRECTION`-with-a-different-name on the layout
#: side: a layout records one direction, because it records one algorithm, and the
#: algorithm says which dial the number came from. Preferences have no such
#: constraint -- both controls exist on the panel at once, and a user may have
#: configured either -- so both are remembered separately, matching the
#: `PhysicsRandomDirection` / `PhysicsRotationSteps` pair that already worked this
#: way. See issues.md NEST-001.
PROP_PHYSICS_DIRECTION = "PhysicsDirection"

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

#: The algorithm a layout is assumed to have been produced by when it does not
#: say. Matches the panel's own default (`ui_nesting.algorithm_dropdown`,
#: index 0), which is what an old layout -- written before `PROP_ALGORITHM
#: existed -- was in fact run with.
DEFAULT_ALGORITHM = "Minkowski"

# Degrees the dial moves per click. 15 gives 24 positions, which is finer than
# the four cardinals the dial used to snap to and finer than most parts'
# symmetry warrants, while still being coarse enough to land on a repeatable
# angle by hand. Persisted readings are dial values, so a stored 15 is 15 here
# and not 15 degrees of bearing.
DIRECTION_STEP_DEGREES = 15


def dial_to_bearing(dial_value):
    """Convert a QDial reading to the compass bearing the search actually uses.

    A quarter turn plus a flip, so the two are NOT the same number and the dial
    is not pointing where the name says. Verified against the controller's own
    conversion: reading 0 becomes bearing 270, which resolves to ( 0, -1) Down.

    Lives here because three places need it and two of them are in the GUI: the
    dial's readout, and both of the controller's per-algorithm conversions.
    Written out by hand in the controller twice already, which is twice the
    chance of the two drifting apart.
    """
    return (270 - int(dial_value)) % 360


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
