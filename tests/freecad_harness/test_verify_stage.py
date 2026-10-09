#!/usr/bin/env freecadcmd
"""Regression test for the verify-on-accept stage and its repair-push.

Run with:
    /home/james/freecad_env/usr/bin/freecadcmd \
        tests/freecad_harness/test_verify_stage.py

What this covers
----------------
`nesting_strategy._verify_stage` (NEST-034) and `_repair_candidate` (NEST-036),
the last two things to change in the accept path and the only two with no test
in this repository until now.

Both sit between the mask stage and the scorer. The mask decides on SIMPLIFIED
polygons, whose stacked DP error can exceed the spacing budget, so a placement
the mask accepts can still draw its part closer than `spacing` allows. The
verify stage re-checks accepted rows on the un-simplified buffered profile.
NEST-036 then showed the obvious failure of that fix: the mask's simplification
optimism is not a real dead end, so a reject-only gate dropped ~99% of
candidates and starved the search (8 of 54 parts placed). The repair pushes a
rejected candidate to a nearby legal coordinate and re-proves BOTH predicates
there.

Scope
-----
Synthetic polygons and stand-in sheets, deliberately. What this guards is
arithmetic that a real nest cannot localise: with 54 parts and a real GA run,
a registration or tolerance regression does not fail here, it just quietly
returns fewer parts, which is how the 1.9979 mm defect in
`test_tool_clearance` sat behind a green gate. The predicate is small, the
inputs are constructible, and every number below is exact, so the checks can
say what the tolerance *is* rather than merely that something overlapped.

This needs the `freecadcmd` binary rather than pytest because
`nesting_strategy` imports FreeCAD at module scope -- but it never builds a
document, opens a fixture, or takes more than a second. That makes it the
cheapest gate in the harness and the one most likely to be run.

The one thing this cannot show is whether the stage is reached in production at
all. A gate that never runs is the failure `run.sh` refuses to tolerate
elsewhere ("a gate that quietly skips a check is the same failure as having no
gate at all"), and here the wiring lives in `_evaluate_rotation`, which is why
`test_rotation_determinism.py` is the suite that would notice a stage that
stopped being called.
"""
import os
import sys
import traceback

# Pinned before `nesting_strategy` is imported, because the module reads this
# once at import time into `ns.VERIFY_REPAIR`. The NEST-034 checks below assert
# reject-only semantics (an overlap rejects), and the NEST-036 block then flips
# `ns.VERIFY_REPAIR` at runtime to exercise the repair explicitly -- so the
# cases can cover both modes from one process, and neither depends on what the
# caller's environment happened to say.
os.environ["NESTING_VERIFY_REPAIR"] = "0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import FreeCAD  # noqa: E402  (after the env pin, before anything reads it)

_STATUS_FILE = os.path.join(_HERE, ".last_status_verifystage")
_FAILURES = []
_CHECKS = [0]

# The area tolerance the stage is called with throughout. Not zero: a real run
# passes a measured per-part figure, and zero would make "touching" and
# "overlapping by a rounding error" the same case.
TOL = 1e-7


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def check(label, condition, detail=""):
    _CHECKS[0] += 1
    if condition:
        emit(f"  PASS  {label}")
    else:
        emit(f"  FAIL  {label}  {detail}")
        _FAILURES.append(label)


# --------------------------------------------------------------------------
# Stand-ins. A sheet is only ever read for .width/.height, and the stage takes
# `bin_polygon` separately, so neither needs to be a real FreeCAD object.
# --------------------------------------------------------------------------
class Sheet(object):
    def __init__(self, width, height):
        self.width = width
        self.height = height


_ns = None
_stage = None
_Polygon = None
_sh_rotate = None
_sh_translate = None


def rect(x0, y0, x1, y1):
    return _Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def stage(valid, points, rotated_verify, rotated_centroid, sheet,
          existing_verify, probe=None, rotated_poly=None,
          existing_polygons=None, bin_polygon=None):
    """Call the stage under test and hand back the mutated `valid` array."""
    _stage(valid, points, rotated_verify, rotated_centroid, sheet,
           existing_verify, TOL, probe,
           rotated_poly=rotated_poly,
           existing_polygons=existing_polygons,
           bin_polygon=bin_polygon)
    return valid


# --------------------------------------------------------------------------
# Case 1: the gate is off
# --------------------------------------------------------------------------
def case_gate_off_is_a_no_op():
    """`rotated_verify is None` is how the caller switches the gate off.

    Every accepted row must survive, or the A/B that NESTING_VERIFY_ON_ACCEPT=0
    exists to enable would not be an A/B at all -- it would be an outage.
    """
    import numpy as np

    emit("")
    emit("--- gate off: a None verify polygon changes nothing ---")

    valid = np.array([True, True])
    stage(valid, np.array([[5.0, 5.0], [99.0, 5.0]]), None, None,
          Sheet(100, 100), [])
    check("none-verify no-op", bool(valid.all()), repr(valid))


# --------------------------------------------------------------------------
# Case 2: the bounds leg on an empty sheet
# --------------------------------------------------------------------------
def case_empty_sheet_bounds():
    """With nothing placed, only the bounds leg can reject.

    And it must, because the un-simplified polygon's bbox is not the mask's
    bbox: a candidate the mask placed flush against a corner overhangs here by
    the simplification error, and accepting it would put drawn geometry off the
    sheet. This is the case NEST-036's bounds repair exists for.
    """
    import numpy as np

    emit("")
    emit("--- empty sheet: only the bounds leg can reject ---")

    verify = rect(0, 0, 10, 10)
    centroid = verify.centroid
    valid = np.array([True, True, True])
    points = np.array([[5.0, 5.0], [99.0, 5.0], [5.0, 99.0]])
    stage(valid, points, verify, centroid, Sheet(100, 100), [])
    check("empty-sheet: inside kept", bool(valid[0]), repr(valid))
    check("empty-sheet: right overhang rejected", not valid[1], repr(valid))
    check("empty-sheet: top overhang rejected", not valid[2], repr(valid))


# --------------------------------------------------------------------------
# Case 3: registration against the MASK centroid
# --------------------------------------------------------------------------
def case_registration():
    """The candidate's verify polygon must be translated like the mask candidate.

    `points` are MASK centroids, so the correct translation is
    `points[i] - rotated_centroid`. Translating `rotated_verify` about *its own*
    centroid instead is off by the centroid offset between the two polygons --
    which is zero when the verify polygon is centred and non-zero exactly when
    it is asymmetric, so an unregistration of this kind passes unnoticed on
    every symmetric part in a nest.

    The mask and verify polygons below are deliberately asymmetric about
    different centres, so the two registrations land in measurably different
    places, and the "test is meaningful" check fails loudly if that ever stops
    being true.
    """
    import numpy as np

    emit("")
    emit("--- registration: rotate about the MASK polygon's centroid ---")

    mask_poly = rect(0, 0, 10, 10)
    verify_poly = _Polygon([(-1, -1), (11, -1), (11, 8), (-1, 8)])  # centroid (5, 3.5)
    mask_r = _sh_rotate(mask_poly, 30, origin=(5, 5))
    verify_r = _sh_rotate(verify_poly, 30, origin=(5, 5))
    mask_centroid = mask_r.centroid

    target = np.array([[50.0, 50.0]])
    dx = target[0, 0] - mask_centroid.x
    dy = target[0, 1] - mask_centroid.y
    expected = _sh_translate(verify_r, xoff=dx, yoff=dy)

    valid = np.array([True])
    stage(valid, target, verify_r, mask_centroid, Sheet(200, 200), [expected])
    check("registration: identical overlap rejected", not valid[0], repr(valid))

    far = _sh_translate(expected, xoff=100, yoff=0)
    valid = np.array([True])
    stage(valid, target, verify_r, mask_centroid, Sheet(200, 200), [far])
    check("registration: disjoint accepted", bool(valid[0]), repr(valid))

    manual = _sh_translate(_sh_rotate(verify_poly, 30, origin=(5, 5)),
                           xoff=dx, yoff=dy)
    check("registration: manual rigid motion matches",
          abs(manual.bounds[0] - expected.bounds[0]) < 1e-9
          and abs(manual.bounds[1] - expected.bounds[1]) < 1e-9,
          f"{manual.bounds} vs {expected.bounds}")

    # A self-centroid translation puts the verify polygon somewhere else
    # entirely. Guard that the fixture can still tell the two apart, so the
    # overlap checks above cannot silently degrade into "anything rejects".
    self_centroid = verify_r.centroid
    self_placed = _sh_translate(verify_r,
                                xoff=target[0, 0] - self_centroid.x,
                                yoff=target[0, 1] - self_centroid.y)
    check("registration: mask-centroid and self-centroid placements differ "
          "(test is meaningful)",
          abs(self_placed.bounds[0] - expected.bounds[0]) > 1e-6,
          f"{self_placed.bounds} vs {expected.bounds}")

    # The offset placement is rejected too. That alone does not discriminate
    # between the two registrations -- they overlap each other, so both a
    # correct and an incorrect translation would reject -- but it does prove
    # the path runs, and the decisive evidence is the exact-position check
    # above plus the manual match.
    valid = np.array([True])
    stage(valid, target, verify_r, mask_centroid, Sheet(200, 200), [self_placed])
    check("registration: offset placement rejected (path exercised)",
          not valid[0], repr(valid))

    # The decisive check, and the reason the two above are not enough.
    #
    # Everything above compares polygons this case builds itself, so it would
    # pass unchanged even if the stage translated the candidate about the wrong
    # origin -- the bug they were written for. The reason is geometric: this
    # fixture's two centroids are only 1.5 mm apart, so a correctly-registered
    # candidate and a self-registered one still overlap each other, and both
    # reject. No verdict can tell them apart.
    #
    # So: a 10x1 bar as the verify polygon on a 10x10 mask plate. The centroids
    # are 4.5 mm apart, which is more than the bar is tall, so the two
    # registrations land in disjoint places and give opposite verdicts.
    bar = rect(0, 0, 10, 1)        # centroid (5, 0.5)
    plate = rect(0, 0, 10, 10)      # centroid (5, 5)
    plate_centroid = plate.centroid
    at_mask_origin = _sh_translate(bar, xoff=target[0, 0] - plate_centroid.x,
                                   yoff=target[0, 1] - plate_centroid.y)
    at_self_origin = _sh_translate(bar, xoff=target[0, 0] - bar.centroid.x,
                                   yoff=target[0, 1] - bar.centroid.y)
    check("registration: the two origins are discriminable",
          not at_mask_origin.intersects(at_self_origin),
          f"{at_mask_origin.bounds} vs {at_self_origin.bounds}")

    valid = np.array([True])
    stage(valid, target, bar, plate_centroid, Sheet(200, 200), [at_mask_origin])
    check("registration: mask-origin candidate rejected (the de-registration "
          "guard)", not valid[0], repr(valid))

    valid = np.array([True])
    stage(valid, target, bar, plate_centroid, Sheet(200, 200), [at_self_origin])
    check("registration: self-origin placement accepted",
          bool(valid[0]), repr(valid))


# --------------------------------------------------------------------------
# Case 4: touch is not overlap
# --------------------------------------------------------------------------
def case_touch_is_not_overlap():
    """Two parts that share an edge are legal. Overlap is an area, not a test.

    `intersects` is true for parts that merely touch, so an implementation that
    gated on it would reject every perfectly-packed nest -- and it would do so
    as a *density* regression, which is exactly the sort of thing that gets
    tuned away rather than debugged.
    """
    import numpy as np

    emit("")
    emit("--- touch vs overlap: the tolerance is an area ---")

    a = rect(0, 0, 10, 10)
    centroid = a.centroid
    touching = rect(10, 0, 20, 0 + 10)
    valid = np.array([True])
    stage(valid, np.array([[centroid.x, centroid.y]]), a, centroid,
          Sheet(100, 100), [touching])
    check("exact touch accepted (area 0 <= tol)", bool(valid[0]), repr(valid))

    overlapping = _Polygon([(9.5, 0), (20, 0), (20, 10), (9.5, 10)])
    valid = np.array([True])
    stage(valid, np.array([[centroid.x, centroid.y]]), a, centroid,
          Sheet(100, 100), [overlapping])
    check("0.5mm2 overlap rejected", not valid[0], repr(valid))

    # The tolerance itself, which the two checks above do NOT pin.
    #
    # An exactly-touching pair is settled one stage earlier than the tolerance:
    # the screen's strict inequalities drop it, so the area comparison never
    # runs. That is the correct answer for the right reason -- sharing an edge
    # is legal -- but it means a gate built on `intersects` would also pass the
    # check above while rejecting every legal nest in production.
    #
    # This sliver is 1e-9 mm wide, so the overlap area is 1e-8 against a
    # tolerance of 1e-7, but it is wide enough that the screen admits the pair
    # and `intersects` is true. A gate on `intersects` rejects it; a gate on the
    # area accepts it. Only one of those two is the documented predicate.
    sliver = rect(10 - 1e-9, 0, 20, 10)
    check("tolerance: the sliver reaches the exact check (test is meaningful)",
          10.0 > sliver.bounds[0] and a.intersects(sliver),
          f"bounds {sliver.bounds}")
    valid = np.array([True])
    stage(valid, np.array([[centroid.x, centroid.y]]), a, centroid,
          Sheet(100, 100), [sliver])
    check("sub-tolerance sliver accepted (area 1e-8 <= tol)",
          bool(valid[0]), repr(valid))


# --------------------------------------------------------------------------
# Case 5: the bbox screen cannot be the whole test
# --------------------------------------------------------------------------
def case_bbox_screen_then_exact():
    """A bbox pre-filter is an optimisation; it must not decide anything.

    If the screen rejected on its own, two parts whose boxes overlap but whose
    polygons do not would be dropped. This case puts them in exactly that
    arrangement and requires the accepted answer.
    """
    import numpy as np

    emit("")
    emit("--- bbox screen, then the exact check ---")

    a = rect(0, 0, 10, 10)
    centroid = a.centroid
    # Boxes overlap (6..14 vs 0..10), polygons do not.
    screen_only = rect(6, 6, 14, 14)
    valid = np.array([True])
    stage(valid, np.array([[centroid.x, centroid.y]]), a, centroid,
          Sheet(100, 100), [screen_only])
    check("bbox+real overlap rejected", not valid[0], repr(valid))


# --------------------------------------------------------------------------
# Case 6: the probe counters
# --------------------------------------------------------------------------
def case_probe_counters():
    """The probe is measurement-only, so a wrong counter corrupts every number.

    It is also the only way an operator can tell a starving verify gate from a
    working one -- and a starving gate was exactly what NEST-036 was. If a
    counter stops being incremented the failure is silent: the nest still runs,
    it just quietly places fewer parts.
    """
    import numpy as np

    emit("")
    emit("--- probe counters ---")

    probe = {}
    verify = rect(0, 0, 10, 10)
    centroid = verify.centroid
    valid = np.array([True, True])
    points = np.array([[centroid.x, centroid.y], [99.0, centroid.y]])
    stage(valid, points, verify, centroid, Sheet(100, 100), [verify], probe=probe)
    check("probe: candidates", probe.get("verify_candidates") == 2, str(probe))
    check("probe: bounds rejections",
          probe.get("verify_bounds_rejections") == 1, str(probe))
    check("probe: rejections >= 1", probe.get("verify_rejections", 0) >= 1,
          str(probe))
    check("probe: exact checks ran",
          probe.get("verify_exact_checks", 0) >= 1, str(probe))
    check("probe: ms recorded", probe.get("verify_ms", 0.0) >= 0.0, str(probe))


# --------------------------------------------------------------------------
# Case 7: NEST-036 repair-push
# --------------------------------------------------------------------------
def case_repair_pulls_a_penetration_clear():
    """The case the repair was written for.

    The mask proposes a position where the SIMPLIFIED polygons graze; at
    simplification 0.3 that grazing is 0.05-1.0 mm of true penetration. The
    repair measures the sliver and steps the candidate out of it by its depth.
    The mask geometry here is the slightly-smaller twin of the verify polygon,
    as in production.
    """
    import numpy as np

    emit("")
    emit("--- repair: a penetrating candidate is pushed clear ---")

    _ns.VERIFY_REPAIR = True
    sheet = Sheet(100, 100)
    bin_polygon = rect(0, 0, 100, 100)

    verify = rect(20, 20, 30, 30)
    mask = rect(20.1, 20, 29.9, 30)
    centroid = verify.centroid
    partner = rect(29.5, 20, 40, 30)

    probe = {}
    valid = np.array([True])
    stage(valid, np.array([[25.0, 25.0]]), verify, centroid, sheet, [partner],
          probe=probe, rotated_poly=mask, existing_polygons=[partner],
          bin_polygon=bin_polygon)
    check("repair: overlapping candidate kept", bool(valid[0]), str(probe))
    check("repair: repairs counted", probe.get("verify_repairs") == 1, str(probe))
    check("repair: no rejection left", probe.get("verify_rejections") == 0,
          str(probe))
    check("repair: attempts recorded",
          probe.get("verify_repair_attempts", 0) >= 1, str(probe))
    check("repair: ms recorded", probe.get("verify_repair_ms", 0.0) >= 0.0,
          str(probe))

    moved = probe.get("_verify_repaired", {}).get(0)
    check("repair: side channel carries row 0", moved is not None, str(probe))
    if moved is not None:
        check("repair: pushed away from partner only",
              moved[0] < 25.0 and moved[1] == 25.0, str(moved))
        pushed_verify = _sh_translate(verify, xoff=moved[0] - 25.0,
                                      yoff=moved[1] - 25.0)
        pushed_mask = _sh_translate(mask, xoff=moved[0] - 25.0,
                                    yoff=moved[1] - 25.0)
        check("repair: verify disjoint at new coordinate",
              not pushed_verify.intersects(partner)
              and pushed_verify.distance(partner) > 0.02,
              f"bounds {pushed_verify.bounds}")
        check("repair: mask disjoint at new coordinate",
              not pushed_mask.intersects(partner)
              and pushed_mask.distance(partner) > 0.02,
              f"bounds {pushed_mask.bounds}")


def case_repair_gives_up_when_trapped():
    """A bounded loop that cannot converge must reject, not search forever.

    Here pushing out of the first partner lands the candidate in the second
    one's inflated mask, and pushing back re-enters the first. The repair has
    to notice and reject -- the case that makes the loop's bound load-bearing
    rather than decorative.
    """
    import numpy as np

    emit("")
    emit("--- repair: a trapped candidate is rejected, not looped ---")

    _ns.VERIFY_REPAIR = True
    sheet = Sheet(100, 100)
    bin_polygon = rect(0, 0, 100, 100)

    verify = rect(20, 20, 30, 30)
    centroid = verify.centroid
    partner_a = rect(29.5, 20, 40, 30)
    partner_b_verify = rect(5, 20, 15, 30)      # 4.9mm clear of the original
    partner_b_mask = rect(5, 20, 19.9, 30)      # inflated: grazes after the push

    probe = {}
    valid = np.array([True])
    stage(valid, np.array([[25.0, 25.0]]), verify, centroid, sheet,
          [partner_a, partner_b_verify], probe=probe, rotated_poly=verify,
          existing_polygons=[partner_a, partner_b_mask],
          bin_polygon=bin_polygon)
    check("repair: trapped candidate rejected", not valid[0], str(probe))
    check("repair: trapped failure counted",
          probe.get("verify_repair_failures") == 1, str(probe))
    check("repair: attempts bounded",
          0 < probe.get("verify_repair_attempts", 0) <= 6, str(probe))
    check("repair: trapped rejects as a rejection",
          probe.get("verify_rejections") == 1, str(probe))
    check("repair: trapped leaves no side channel",
          "_verify_repaired" not in probe, str(probe))


def case_repair_refuses_to_push_off_sheet():
    """A collision push must not win against the sheet edge.

    The partner sits left of a candidate flush with the right edge, so the only
    available push direction is off the sheet. Bounds win.
    """
    import numpy as np

    emit("")
    emit("--- repair: a push off the sheet is refused ---")

    _ns.VERIFY_REPAIR = True
    sheet = Sheet(100, 100)
    bin_polygon = rect(0, 0, 100, 100)

    verify = rect(90, 45, 100, 55)
    centroid = verify.centroid
    edge_partner = rect(89.5, 45, 94, 55)

    probe = {}
    valid = np.array([True])
    stage(valid, np.array([[95.0, 50.0]]), verify, centroid, sheet, [edge_partner],
          probe=probe, rotated_poly=verify, existing_polygons=[edge_partner],
          bin_polygon=bin_polygon)
    check("repair: off-sheet push rejected", not valid[0], str(probe))
    check("repair: off-sheet failure counted",
          probe.get("verify_repair_failures") == 1, str(probe))
    check("repair: off-sheet leaves no side channel",
          "_verify_repaired" not in probe, str(probe))


def case_repair_handles_concentric_shapes():
    """Concentric overlaps have no usable push direction.

    `_push_vector` returns None and the row must reject, rather than 'succeed'
    at a coordinate nothing moved it to.
    """
    import numpy as np

    emit("")
    emit("--- repair: coincident polygons still reject ---")

    _ns.VERIFY_REPAIR = True
    sheet = Sheet(200, 200)

    mask_poly = rect(0, 0, 10, 10)
    verify_poly = _Polygon([(-1, -1), (11, -1), (11, 8), (-1, 8)])
    mask_r = _sh_rotate(mask_poly, 30, origin=(5, 5))
    verify_r = _sh_rotate(verify_poly, 30, origin=(5, 5))
    centroid = mask_r.centroid

    target = np.array([[50.0, 50.0]])
    dx = target[0, 0] - centroid.x
    dy = target[0, 1] - centroid.y
    existing = _sh_translate(verify_r, xoff=dx, yoff=dy)

    probe = {}
    valid = np.array([True])
    stage(valid, target, verify_r, centroid, sheet, [existing],
          probe=probe, rotated_poly=mask_r, existing_polygons=[existing],
          bin_polygon=rect(0, 0, 200, 200))
    check("repair: coincident overlap still rejected", not valid[0], str(probe))
    check("repair: coincident counted as failure",
          probe.get("verify_repair_failures") == 1, str(probe))
    check("repair: coincident leaves no side channel",
          "_verify_repaired" not in probe, str(probe))


def case_repair_off_is_bit_identical_to_the_reject_only_gate():
    """With the repair off, the same input must reject and touch no counters.

    This is the A/B that NESTING_VERIFY_REPAIR=0 advertises, so it has to be
    true rather than approximately true: same input as the first repair case,
    same verdict, and no repair bookkeeping at all.
    """
    import numpy as np

    emit("")
    emit("--- repair off: reject-only parity ---")

    _ns.VERIFY_REPAIR = False
    try:
        sheet = Sheet(100, 100)
        bin_polygon = rect(0, 0, 100, 100)
        verify = rect(20, 20, 30, 30)
        mask = rect(20.1, 20, 29.9, 30)
        centroid = verify.centroid
        partner = rect(29.5, 20, 40, 30)

        probe = {}
        valid = np.array([True])
        stage(valid, np.array([[25.0, 25.0]]), verify, centroid, sheet, [partner],
              probe=probe, rotated_poly=mask, existing_polygons=[partner],
              bin_polygon=bin_polygon)
        check("repair off: same input rejected", not valid[0], str(probe))
        check("repair off: rejections counted",
              probe.get("verify_rejections") == 1, str(probe))
        check("repair off: no repairs counter",
              probe.get("verify_repairs", 0) == 0, str(probe))
        check("repair off: no side channel", "_verify_repaired" not in probe,
              str(probe))
    finally:
        _ns.VERIFY_REPAIR = True


def case_repair_pulls_an_empty_sheet_overhang_on():
    """The bounds repair's reason for existing.

    On an empty sheet the engine's only candidates are corner-flush: exact in
    MASK space, so the verify bbox overhangs the wall by the DP error. Without
    bounds repair that rejected every rotation of an otherwise placeable part
    -- the rotation-determinism gate measured 44 of 48 placed, which is what
    made the starvation visible at all.

    Two rows: one already on-sheet and must not move, one overhanging and must
    be pulled in. A repair that adjusted both would be trading one bug for
    another.
    """
    import numpy as np

    emit("")
    emit("--- repair: an empty-sheet overhang is pulled on-sheet ---")

    _ns.VERIFY_REPAIR = True
    sheet = Sheet(100, 100)
    bin_polygon = rect(0, 0, 100, 100)

    verify = rect(20, 20, 30, 30)
    mask = rect(20.1, 20, 29.9, 30)
    centroid = verify.centroid

    probe = {}
    valid = np.array([True, True])
    stage(valid, np.array([[5.0, 5.0], [99.0, 5.0]]), verify, centroid, sheet, [],
          probe=probe, rotated_poly=mask, existing_polygons=[],
          bin_polygon=bin_polygon)
    check("repair: empty sheet overhang pulled on-sheet",
          bool(valid[0]) and bool(valid[1]), str(probe))
    check("repair: bounds repair counted",
          probe.get("verify_repairs") == 1
          and probe.get("verify_bounds_repairs") == 1, str(probe))
    check("repair: no bounds rejection left",
          probe.get("verify_bounds_rejections") == 0
          and probe.get("verify_rejections") == 0, str(probe))

    side = probe.get("_verify_repaired", {})
    check("repair: side channel has the pulled row only",
          side.get(1) is not None and 0 not in side, str(probe))
    if side.get(1) is not None:
        moved = side[1]
        pushed = _sh_translate(verify, xoff=moved[0] - 25.0, yoff=moved[1] - 25.0)
        check("repair: pulled position fully on-sheet",
              moved[0] < 99.0
              and pushed.bounds[0] >= 0.0 and pushed.bounds[2] <= 100.0
              and pushed.bounds[1] >= 0.0 and pushed.bounds[3] <= 100.0,
              f"moved {moved} bounds {pushed.bounds}")


def case_repair_cannot_invent_space():
    """A part wider than the sheet has an empty position window.

    Bounds repair is a clamp into that window, so an empty window means the part
    cannot sit on this sheet at this angle at all. It must reject -- with zero
    attempts, which distinguishes 'no position exists' from 'tried and failed'.
    """
    import numpy as np

    emit("")
    emit("--- repair: an unplaceable part still rejects ---")

    _ns.VERIFY_REPAIR = True
    sheet = Sheet(100, 100)
    bin_polygon = rect(0, 0, 100, 100)

    big = rect(-10, 0, 700, 40)   # 710 wide on a 100-wide sheet
    centroid = big.centroid

    probe = {}
    valid = np.array([True])
    stage(valid, np.array([[345.0, 20.0]]), big, centroid, sheet, [],
          probe=probe, rotated_poly=big, existing_polygons=[],
          bin_polygon=bin_polygon)
    check("repair: unplaceable part rejected", not valid[0], str(probe))
    check("repair: empty window counted as failure",
          probe.get("verify_repair_failures") == 1
          and probe.get("verify_repair_attempts") == 0, str(probe))
    check("repair: empty window counted as bounds rejection",
          probe.get("verify_bounds_rejections") == 1, str(probe))
    check("repair: empty window leaves no side channel",
          "_verify_repaired" not in probe, str(probe))


# --------------------------------------------------------------------------
def main():
    global _ns, _stage, _Polygon, _sh_rotate, _sh_translate

    from shapely.affinity import rotate as _sh_rotate, translate as _sh_translate
    from shapely.geometry import Polygon as _Polygon
    from freecad.nestingworkbench.Tools.Nesting.algorithms import (
        nesting_strategy as _ns,
    )
    _stage = _ns._verify_stage

    for case in (case_gate_off_is_a_no_op,
                 case_empty_sheet_bounds,
                 case_registration,
                 case_touch_is_not_overlap,
                 case_bbox_screen_then_exact,
                 case_probe_counters,
                 case_repair_pulls_a_penetration_clear,
                 case_repair_gives_up_when_trapped,
                 case_repair_refuses_to_push_off_sheet,
                 case_repair_handles_concentric_shapes,
                 case_repair_off_is_bit_identical_to_the_reject_only_gate,
                 case_repair_pulls_an_empty_sheet_overhang_on,
                 case_repair_cannot_invent_space):
        try:
            case()
        except Exception:
            emit(f"  ERROR in {case.__name__}:")
            emit(traceback.format_exc())
            _FAILURES.append(f"{case.__name__} raised")
    emit("")
    emit(f"{_CHECKS[0] - len(_FAILURES)}/{_CHECKS[0]} checks passed")
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
        return 1
    return 0


if __name__ in ("__main__", "test_verify_stage"):
    try:
        _status = main()
    except Exception:
        traceback.print_exc()
        _status = 3
    try:
        with open(_STATUS_FILE, "w") as _handle:
            _handle.write(str(_status))
    except OSError:
        pass
    sys.exit(_status)
