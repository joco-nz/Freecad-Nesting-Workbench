"""Does the replay say anything when the layout leaves the tool no room?

`spacing` and the CAM tool are independent controls. `shape_processor.py:289`
buffers each part outline by `spacing / 2` and has no knowledge of a tool --
there is no reference to a tool, a diameter or an endmill anywhere under
`Tools/Nesting/` -- while the replay copies the user's real tool into the new
job (NEST-020). Nothing related the two, so a nest laid out tighter than the
tool is wide would be cut through without a word.

A flat cutter of radius `r` sweeps `r` beyond the outline it follows, so cut
paths around parts `g` apart overlap whenever `g < 2r`, which is the tool
diameter. That is arithmetic; what has to be measured is whether real nests
actually get close enough for it to matter, and whether the check finds it when
they do.

Measured on the committed fixture before the check existed: `PartSpacing = 4.0`,
a 1.2 mm plasma kerf, 1128 part pairs, tightest **3.6316 mm** -- 0.908 of the
spacing, and clear of the tool by 2.43 mm. So the reference nest is healthy and
the check is silent on it, which is what a warning should do.

The pytest tier covers the comparison and the bounding-box prune against
stand-ins. It cannot show that the footprints it prunes are the ones the replay
will cut, so that is what this file is for: real solids, real slicing, and the
prune checked against brute force on the same geometry.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_clearance`; 0 pass, 1 fail.
"""
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_clearance")

_FIXTURE = os.path.join(_REPO, "tests", "Test_Files",
                       "replay-fixture-CAM-Nested.FCStd")

import FreeCAD

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.freecad_helpers import get_sheet_groups

_failures = []
_checks = [0]


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        pass


def check(condition, detail):
    _checks[0] += 1
    if not condition:
        _failures.append(detail)
        emit("  FAIL: %s" % detail)
    return bool(condition)


class _FixedCache:
    """A `FootprintCache` stand-in over already-computed footprints.

    Lets a test choose the geometry without going near a document, while still
    going through the real `tightest_part_gap`.
    """

    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, part):
        return self._mapping[id(part)]


class _Part:
    def __init__(self, label, footprint):
        self.Label = label
        self.Shape = None
        self._footprint = footprint


def _job(*diameters):
    """A stand-in job carrying tool controllers of the given diameters."""

    class _Tool:
        def __init__(self, d):
            self.Diameter = type("Q", (), {"Value": d})()

    class _Controller:
        def __init__(self, label, d):
            self.Label = label
            self.Tool = _Tool(d)

    class _Tools:
        Group = [_Controller("tool_%.1f" % d, d) for d in diameters]

    class _Job:
        Tools = _Tools()

    return _Job()


def _boxes(spec):
    """`(label, polygon)` for each `(label, x0, y0, w, h)` in `spec`."""
    from shapely.geometry import box
    out = []
    for label, x0, y0, w, h in spec:
        out.append((label, box(x0, y0, x0 + w, y0 + h)))
    return out


# --------------------------------------------------------------------------
# 1. the comparison, on real polygons
# --------------------------------------------------------------------------

def check_threshold():
    emit("")
    emit("-- 1. silent when the tool fits, warns when it does not --")
    spec = [("a", 0.0, 0.0, 40.0, 25.0), ("b", 55.0, 0.0, 40.0, 25.0)]
    parts = [_Part(n, f) for n, f in _boxes(spec)]
    cache = _FixedCache({id(p): p._footprint for p in parts})

    gap = cam_replay.tightest_part_gap(parts, cache)
    emit("  two parts 15.000 mm apart -> gap %.4f" % gap[0])
    check(abs(gap[0] - 15.0) < 1e-9,
          "measured gap %.6f against an exact 15.0" % gap[0])
    check(gap[1] in ("a", "b") and gap[2] in ("a", "b"),
          "the closest pair is not the two parts: %r" % (gap,))

    # 5 mm tool, 15 mm of room: silent.
    check(cam_replay.check_tool_clearance(parts, _job(5.0), cache) is None,
          "warned with 15 mm of room and a 5 mm tool")
    # 20 mm tool, 15 mm of room: warns.
    warning = cam_replay.check_tool_clearance(parts, _job(20.0), cache)
    check(warning is not None,
          "stayed silent with 15 mm of room and a 20 mm tool")
    if warning:
        emit("  %s" % warning)
        check("5.0000" in warning, "the shortfall is not stated: %s" % warning)
        check("PartSpacing" in warning, "no remedy offered: %s" % warning)
        check("a" in warning and "b" in warning,
              "the parts are not named: %s" % warning)

    # Exactly one diameter: silent, because the cutter only grazes.
    spec = [("a", 0.0, 0.0, 40.0, 25.0), ("b", 45.0, 0.0, 40.0, 25.0)]
    parts = [_Part(n, f) for n, f in _boxes(spec)]
    cache = _FixedCache({id(p): p._footprint for p in parts})
    gap = cam_replay.tightest_part_gap(parts, cache)
    emit("  two parts 5.000 mm apart -> gap %.4f" % gap[0])
    check(abs(gap[0] - 5.0) < 1e-9,
          "measured gap %.6f against an exact 5.0" % gap[0])
    check(cam_replay.check_tool_clearance(parts, _job(5.0), cache) is None,
          "warned at exactly one tool diameter, where the cutter only grazes")


# --------------------------------------------------------------------------
# 2. the bounding-box prune must not change the answer
# --------------------------------------------------------------------------

def check_prune_agrees_with_brute_force():
    """The prune is what makes this affordable, and what could hide a pair.

    Checked against brute force on the same polygons, because a prune that
    returns the wrong minimum would make the warning quote a comfortable pair
    instead of the tight one -- the quietest possible way to be wrong.
    """
    emit("")
    emit("-- 2. the bounding-box prune agrees with brute force --")
    from shapely.geometry import box
    # Deliberately awkward: the tight pair is last, the rest are far apart or
    # overlapping, and one part is long and thin so its bounds lie about much.
    spec = [
        ("long_1", 0.0, 0.0, 400.0, 12.0),
        ("far_1", 900.0, 0.0, 30.0, 30.0),
        ("far_2", 200.0, 700.0, 30.0, 30.0),
        ("near_1", 130.0, 500.0, 40.0, 25.0),
        ("near_2", 172.0, 500.0, 40.0, 25.0),
        ("near_3", 172.0, 560.0, 40.0, 25.0),
    ]
    parts = [_Part(n, f) for n, f in _boxes(spec)]
    cache = _FixedCache({id(p): p._footprint for p in parts})

    brute = None
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            d = parts[i]._footprint.distance(parts[j]._footprint)
            if brute is None or d < brute[0]:
                brute = (d, parts[i].Label, parts[j].Label)
    got = cam_replay.tightest_part_gap(parts, cache)
    emit("  brute force %.6f (%s, %s)" % (brute[0], brute[1], brute[2]))
    emit("  pruned     %.6f (%s, %s)" % (got[0], got[1], got[2]))
    check(abs(got[0] - brute[0]) < 1e-9,
          "the prune changed the minimum: %.9f against %.9f"
          % (got[0], brute[0]))
    check({got[1], got[2]} == {brute[1], brute[2]},
          "the prune named a different pair: %r against %r" % (got, brute))

    # And the warning must quote the tight pair, not a comfortable one.
    warning = cam_replay.check_tool_clearance(parts, _job(10.0), cache)
    check(warning is not None, "stayed silent with a 2 mm gap and a 10 mm tool")
    if warning:
        emit("  %s" % warning)
        check("near_1" in warning or "near_2" in warning,
              "the warning does not name the tight pair: %s" % warning)


# --------------------------------------------------------------------------
# 3. on the real nest
# --------------------------------------------------------------------------

def check_real_nest():
    """The committed fixture, flattened the way the replay flattens it."""
    emit("")
    emit("-- 3. the committed nest, as the replay flattens it --")
    if not os.path.exists(_FIXTURE):
        check(False, "the fixture is missing at %s" % _FIXTURE)
        return

    doc = FreeCAD.openDocument(_FIXTURE)
    layout, _warnings = cam_replay.resolve_layout_group(doc)
    sheets = list(get_sheet_groups(layout))
    if not check(bool(sheets), "no sheet group under the layout"):
        FreeCAD.closeDocument(doc.Name)
        return
    sheet = sheets[0]
    thickness = float(getattr(layout, "SheetThickness", 2.0))
    spacing = float(getattr(layout, "PartSpacing", 0.0))
    source_job = [o for o in doc.Objects if cam_replay.is_cam_job(o)][0]

    flattened = cam_replay.flatten_sheet(doc, sheet, thickness)
    replay = cam_replay.create_replay_job(doc, layout, sheet, flattened.parts,
                                          source_job=source_job)
    if not check(replay is not None, "create_replay_job returned None"):
        FreeCAD.closeDocument(doc.Name)
        return
    clones = replay.clones

    # Slicing and sweeping timed apart, because they are not the same cost and
    # only the second one is this check's own. The replay's `FootprintCache` is
    # shared with `find_hole_nestings`, so in production the slicing is already
    # paid; here it is not, and pretending otherwise would misattribute it.
    cache = cam_replay.FootprintCache()
    t0 = time.perf_counter()
    for clone in clones:
        cache.get(clone)
    slicing = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    gap = cam_replay.tightest_part_gap(clones, cache)
    elapsed = (time.perf_counter() - t0) * 1000.0
    emit("  %d part(s), PartSpacing %s, tightest gap %.4f mm"
         % (len(clones), spacing, gap[0]))
    emit("  pair: %s / %s" % (gap[1], gap[2]))
    emit("  slicing %.1f ms (%d footprints), sweep %.1f ms"
         % (slicing, cache.misses, elapsed))

    check(gap[0] > 0.0, "parts are touching or overlapping: %.6f" % gap[0])
    check(0.8 * spacing <= gap[0] <= 1.05 * spacing,
          "the tightest gap is %.4f against a spacing of %s, which is not "
          "close to it" % (gap[0], spacing))
    emit("  tightest gap is %.4f x the spacing"
         % (gap[0] / spacing if spacing else 0.0))

    # Cost with the cache warm, since that is the state the replay is in.
    t0 = time.perf_counter()
    for _ in range(5):
        cam_replay.tightest_part_gap(clones, cache)
    warm = (time.perf_counter() - t0) / 5 * 1000.0
    emit("  sweep on a warm cache: %.1f ms over %d parts" % (warm, len(clones)))
    check(warm < 250.0,
          "the sweep costs %.1f ms per sheet, which is too much to run "
          "unconditionally" % warm)

    # **The SOURCE job's tool, deliberately.** At this point `replay.job` still
    # carries the 5 mm endmill `PathJob.Create` brings with it: the user's tool
    # is copied in by `replay_recipe`, which has not run. Judging against
    # `replay.job` here reported the reference nest as a warning against a tool
    # nobody will ever use -- which is exactly the sort of wrong-and-reported
    # this workbench tries not to produce. Section 4 checks the real ordering.
    diameters = cam_replay.tool_diameters(source_job)
    emit("  source job tools: %s" % diameters)
    emit("  replay job tools at this point: %s (defaults, not yet copied)"
         % cam_replay.tool_diameters(replay.job))
    check(bool(diameters), "the fixture's CAM job carries no readable tool")
    if diameters:
        warning = cam_replay.check_tool_clearance(clones, source_job, cache)
        if gap[0] >= diameters[0][1]:
            check(warning is None,
                  "warned on the reference nest: gap %.4f, tool %.4f"
                  % (gap[0], diameters[0][1]))
            emit("  verdict: silent, as it should be (%s)"
                 % diameters[0][0])
        else:
            check(warning is not None,
                  "stayed silent with a gap of %.4f and a %.4f tool"
                  % (gap[0], diameters[0][1]))
            emit("  verdict: %s" % warning)

    # And it must fire when the tool outgrows the room.
    wide = max(diameters)[1] if diameters else 1.0
    needed = wide * 10.0
    forced = cam_replay.check_tool_clearance(clones, _job(needed), cache)
    check(forced is not None,
          "stayed silent with a %.1f mm tool and a %.4f mm gap"
          % (needed, gap[0]))
    if forced:
        emit("  with a %.1f mm tool: %s" % (needed, forced))

    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# 4. through the whole replay
# --------------------------------------------------------------------------

def check_end_to_end():
    """The warning has to reach `ReplayResult.warnings`, not just be computed."""
    emit("")
    emit("-- 4. through replay_layout --")
    doc = FreeCAD.openDocument(_FIXTURE)
    layout, _warnings = cam_replay.resolve_layout_group(doc)
    source_job = [o for o in doc.Objects if cam_replay.is_cam_job(o)][0]

    diameters = cam_replay.tool_diameters(source_job)
    check(bool(diameters), "the fixture's CAM job carries no readable tool")
    if not diameters:
        FreeCAD.closeDocument(doc.Name)
        return
    widest = max(d for _l, d in diameters)

    outcomes = cam_replay.replay_layout(doc, layout, source_job)
    check(bool(outcomes), "replay_layout produced no outcomes")
    for outcome in outcomes:
        if outcome.replay_job is None:
            continue
        got = cam_replay.tool_diameters(outcome.replay_job.job)
        emit("  replay job tools: %s" % got)
        # The replay job must carry the SOURCE's tool, not the 5 mm endmill
        # `PathJob.Create` brings with it. Judged against the source, because
        # reading the wrong job here would quietly compare against a tool
        # nobody will use.
        check(any(abs(d - widest) < 1e-9 for _l, d in got),
              "the replay job's tools %s do not include the source's %.4f mm"
              % (got, widest))
        clearance = [w for w in outcome.result.warnings
                     if "PartSpacing" in w]
        emit("  clearance warnings: %d" % len(clearance))
        for w in clearance:
            emit("    %s" % w[:160])
        if widest <= 3.7:
            check(not clearance,
                  "the reference nest warned about clearance: %s" % clearance)
    FreeCAD.closeDocument(doc.Name)


def run():
    check_threshold()
    check_prune_agrees_with_brute_force()
    check_real_nest()
    check_end_to_end()


try:
    run()
    emit("")
    emit("tool clearance: %d checks, %d failure(s)" % (_checks[0], len(_failures)))
    status = 1 if _failures else 0
except Exception:
    traceback.print_exc()
    emit("tool clearance: CRASHED")
    status = 1

with open(_STATUS_FILE, "w") as fh:
    fh.write(str(status))