"""Tests for finding lead-ins that reach another part's cut path.

Background
----------
A `LeadInOut` dressup extends its operation's cut path outward before the cut
starts and after it ends. Those extensions are drawn among the neighbours the
nester placed, and nothing in the replay accounts for where they land. These
tests cover the reading of that geometry, because the reading is where the
silent failures live.

Every check here exists because the naive version of it produced a *confident
clean sheet* rather than an error:

  * **A cut is a feed move that changes XY, not a move at a cut-plane Z.**
    Seeding the modal Z to 0.0 makes the machine's unknown starting height
    indistinguishable from the cut plane, so the first rapid is read as a cut
    and every number derived from it is wrong.
  * **Arcs are sampled from their `I`/`J` centre.** Chorded, a semicircle is
    5 mm inside the arc at mid-span. Measured on the committed fixture: the
    worst offender clears its neighbour by 0.0598 mm sampled, 0.8921 mm chorded
    -- and the second figure is over the tool radius, so a chorded audit stays
    silent about a real interference.
  * **A base↔dressup walk matches on endpoints, not starts.** The base rapids to
    the contour and the dressup to the lead-in entry, so the same move has a
    different start in each. Matching starts fails on all 98 operations.
  * **A conflict found is not a clean operation.** Recording conflicts without
    changing the status reports a clean sheet while holding the evidence.
"""
import math

import pytest

from freecad.nestingworkbench.Tools.Cam import lead_in_audit
from freecad.nestingworkbench.Tools.Cam.lead_in_audit import (
    CONFLICT_PREFIX,
    COINCIDENT_MM,
    Conflict,
    CutMove,
    LeadRun,
    OffSheet,
    OperationAudit,
    _CutIndex,
    _off_sheet,
    body_geometry,
    cut_moves,
    cut_polyline,
    lead_runs,
    moves_to_line,
)


def _shapely():
    try:
        from shapely.geometry import LineString, Polygon
    except ImportError:
        pytest.skip("shapely not available")
    return LineString, Polygon


# -- stand-ins ------------------------------------------------------------

class _Cmd:
    """One toolpath command. `arc` builds a `G2`/`G3` from a centre offset."""

    def __init__(self, name, **params):
        self.Name = name
        self.Parameters = dict(params)

    def __repr__(self):
        return "%s%r" % (self.Name, self.Parameters)


class _Path:
    def __init__(self, commands):
        self.Commands = list(commands)


def _line(commands):
    return _Path(commands)


#: A square, as four G1 moves. Used as a stand-in for a Profile's cut.
SQUARE = _line([
    _Cmd("G0", X=0.0, Y=0.0),
    _Cmd("G1", X=0.0, Y=0.0),
    _Cmd("G1", X=10.0, Y=0.0),
    _Cmd("G1", X=10.0, Y=10.0),
    _Cmd("G1", X=0.0, Y=10.0),
    _Cmd("G1", X=0.0, Y=0.0),
])


# -- what counts as a cut -------------------------------------------------

def test_a_rapid_is_not_a_cut():
    """A `G0` never removes material, however far it travels."""
    path = _line([_Cmd("G0", X=0.0, Y=0.0), _Cmd("G0", X=100.0, Y=100.0),
                  _Cmd("G1", X=101.0, Y=100.0)])
    moves = cut_moves(path)
    assert len(moves) == 1
    assert (moves[0].x0, moves[0].y0) == (100.0, 100.0)


def test_a_plunge_is_not_a_cut():
    """A feed move that only changes Z moves no material in XY."""
    path = _line([_Cmd("G0", X=5.0, Y=5.0), _Cmd("G1", Z=-1.0),
                  _Cmd("G1", X=6.0, Y=5.0)])
    moves = cut_moves(path)
    assert len(moves) == 1
    assert moves[0].x0 == 5.0


def test_a_lead_in_is_not_read_as_part_of_the_cut_plane_test():
    """A `G0` at Z 0 -- the height the cut happens at -- is still not a cut.

    This is the trap the Z-based version fell into. The rapid here is at the
    same Z as the cut that follows it, so a "is it at the cut plane" test calls
    it a cut and the operation's own rapid becomes the first move of its lead-in.
    """
    path = _line([
        _Cmd("G0", Z=0.0),
        _Cmd("G0", X=4.0, Y=0.0),
        _Cmd("G1", X=10.0, Y=0.0),
    ])
    moves = cut_moves(path)
    assert len(moves) == 1
    assert moves[0].x0 == 4.0


def test_cut_moves_carry_position_modally():
    path = _line([_Cmd("G1", X=3.0, Y=4.0), _Cmd("G1", X=6.0)])
    moves = cut_moves(path)
    assert [(m.x0, m.y0, m.x1, m.y1) for m in moves] == [
        (0.0, 0.0, 3.0, 4.0), (3.0, 4.0, 6.0, 4.0)]


# -- arcs -----------------------------------------------------------------

def _semicircle(code="G3"):
    """A half circle from (0,0) to (10,0) about centre (5,0).

    **Counter-clockwise (`G3`) bulges to negative y.** The start is at 180 deg
    and travelling anticlockwise means increasing angle, so the sweep runs
    180 -> 360 and passes through 270 deg, which is (5, -5). Stated because the
    first version of these tests assumed +5 and was wrong: the code was right
    and the expectation was not.
    """
    return _line([_Cmd(code, X=10.0, Y=0.0, I=5.0, J=0.0)])


def test_an_arc_is_sampled_rather_than_chorded():
    """The midpoint of the sampled arc is on the arc, not on the chord.

    The chord from (0,0) to (10,0) passes through (5,0); the arc reaches
    (5,-5). A 5 mm error on a 10 mm arc is larger than the tool radius these are
    tested against, so this is the difference between finding a conflict and
    missing it.
    """
    geometry = cut_polyline(_semicircle())
    assert geometry is not None
    midpoint = geometry.interpolate(geometry.length / 2.0)
    assert midpoint.x == pytest.approx(5.0, abs=0.01)
    assert midpoint.y == pytest.approx(-5.0, abs=0.01)


def test_an_arc_is_measured_on_its_own_curve():
    """A point 1 mm off the arc is 1 mm away; the chord would say 5 mm."""
    arc = cut_polyline(_semicircle())
    try:
        from shapely.geometry import Point
    except ImportError:
        pytest.skip("shapely not available")
    assert arc.distance(Point(5.0, -4.0)) == pytest.approx(1.0, abs=0.02)


def test_a_clockwise_arc_takes_the_clockwise_way_round():
    """`G2` and `G3` between the same endpoints are different arcs.

    Both are sampled from the same centre here, so the only thing separating
    them is the direction of travel. Taking the short way round -- or unwrapping
    both the same way -- would make one of them bulge the wrong side.
    """
    ccw = cut_polyline(_semicircle("G3"))
    cw = cut_polyline(_semicircle("G2"))
    midpoint_ccw = ccw.interpolate(ccw.length / 2.0)
    midpoint_cw = cw.interpolate(cw.length / 2.0)
    assert midpoint_ccw.y == pytest.approx(-5.0, abs=0.01)
    assert midpoint_cw.y == pytest.approx(5.0, abs=0.01)


def test_an_arc_without_a_centre_falls_back_to_its_chord():
    """Malformed input degrades to something, rather than raising.

    Pinned deliberately, and the fallback is not equivalent to the arc: a chord
    lies inside it, so it reports *more* clearance than the truth and can only
    lose a conflict, never invent one. Measured on a quarter arc at r = 6 with a
    neighbour 0.300 mm away, the chorded path reports 2.0573 mm -- a false clean.

    That is why this is a last resort rather than a mode. Asserting it here puts
    the behaviour on the record as a decision rather than a silent default: if a
    real path ever reaches it, `R`-form arcs are the likely cause, and they need
    the centre derived from the radius rather than the chord accepted.
    """
    geometry = cut_polyline(_line([_Cmd("G3", X=10.0, Y=0.0)]))
    assert geometry is not None
    assert geometry.length == pytest.approx(10.0, abs=1e-6)


# -- telling a lead-in from the cut ---------------------------------------

def _square_cut():
    return cut_moves(SQUARE)


def _with_lead_in(entry=(6.0, 0.0)):
    """The square, preceded by a lead-in arriving at the square's first point.

    The lead-in ends where the square *begins*, which is the shape `leadinout`
    produces: it lands on the wire at the point the cut starts from. An earlier
    version of this fixture ran the lead-in from (6,0) to (10,0) and kept the
    square unchanged, which is not a lead-in added to a profile -- it replaces
    the profile's first edge, so the two paths were not the square at all and
    `lead_runs` correctly refused to reconcile them.
    """
    return _Path([
        _Cmd("G0", X=entry[0], Y=entry[1]),
        _Cmd("G1", X=entry[0], Y=entry[1]),
        _Cmd("G1", X=0.0, Y=0.0),          # the lead-in, onto the contour
        _Cmd("G1", X=10.0, Y=0.0),
        _Cmd("G1", X=10.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=0.0),
    ])


def test_a_lead_in_is_told_from_the_cut_around_it():
    lead_ins, lead_outs, body = lead_runs(_square_cut(), cut_moves(_with_lead_in()))
    assert lead_ins is not None
    assert len(lead_ins) == 1
    assert lead_outs == []
    assert len(body) == 4


def test_the_lead_in_keeps_its_own_direction_and_length():
    lead_ins, _outs, _body = lead_runs(_square_cut(), cut_moves(_with_lead_in()))
    geometry = moves_to_line(lead_ins[0])
    # (6,0) to (0,0) is 6 mm, and it is a lead-in of its own rather than a
    # fragment of the square's 10 mm first edge.
    assert geometry.length == pytest.approx(6.0, abs=1e-6)
    assert geometry.coords[0] == pytest.approx((6.0, 0.0))
    assert geometry.coords[-1] == pytest.approx((0.0, 0.0))


def test_the_walk_matches_on_endpoints_when_the_starts_differ():
    """The base plunges at the contour; the dressup at the lead-in entry.

    These two paths share no move *start*, only endpoints, so a walk that
    matched on starts would reconcile nothing at all.
    """
    base = _Path([
        _Cmd("G0", X=0.0, Y=0.0),
        _Cmd("G1", X=0.0, Y=0.0),           # the plunge: no XY change
        _Cmd("G1", X=10.0, Y=0.0),
        _Cmd("G1", X=10.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=0.0),
    ])
    dressup = _Path([
        _Cmd("G0", X=6.0, Y=0.0),
        _Cmd("G1", X=6.0, Y=0.0),           # plunge somewhere else entirely
        _Cmd("G1", X=0.0, Y=0.0),           # the lead-in
        _Cmd("G1", X=10.0, Y=0.0),
        _Cmd("G1", X=10.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=0.0),
    ])
    lead_ins, _outs, body = lead_runs(cut_moves(base), cut_moves(dressup))
    assert lead_ins is not None
    assert len(lead_ins) == 1
    assert len(body) == 4


def test_a_path_that_is_not_the_others_plus_a_lead_in_is_refused():
    """Unreconcilable input returns None rather than inventing a lead-in.

    The caller reports these; treating the base's own cut as "extra" would
    report the user's machining as a lead-in reaching a neighbour.
    """
    other = _Path([_Cmd("G1", X=100.0, Y=100.0), _Cmd("G1", X=110.0, Y=100.0)])
    assert lead_runs(_square_cut(), cut_moves(other)) == (None, None, None)


def test_a_lead_out_after_the_last_cut_move_is_a_lead_out():
    """The tail is separated from the head by where the body ends."""
    dressup = _Path([
        _Cmd("G0", X=6.0, Y=0.0), _Cmd("G1", X=6.0, Y=0.0),
        _Cmd("G1", X=0.0, Y=0.0),           # the lead-in
        _Cmd("G1", X=10.0, Y=0.0),
        _Cmd("G1", X=10.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=0.0),
        _Cmd("G1", X=-4.0, Y=0.0),          # the lead-out
    ])
    lead_ins, lead_outs, body = lead_runs(_square_cut(), cut_moves(dressup))
    assert len(lead_ins) == 1
    assert len(lead_outs) == 1
    assert len(body) == 4
    assert moves_to_line(lead_outs[0]).length == pytest.approx(4.0, abs=1e-6)


def test_a_lead_in_of_several_moves_is_one_run():
    """`ExtendIn` on an arc prepends a line, so a lead-in is not one move.

    Reported as three fragments it would read as three problems where there is
    one, and each fragment's clearance is not the lead-in's.
    """
    dressup = _Path([
        _Cmd("G0", X=6.0, Y=0.0), _Cmd("G1", X=6.0, Y=0.0),
        _Cmd("G1", X=3.0, Y=0.0),
        _Cmd("G1", X=1.5, Y=0.0),
        _Cmd("G1", X=0.0, Y=0.0),
        _Cmd("G1", X=10.0, Y=0.0),
        _Cmd("G1", X=10.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=10.0),
        _Cmd("G1", X=0.0, Y=0.0),
    ])
    lead_ins, _outs, _body = lead_runs(_square_cut(), cut_moves(dressup))
    assert len(lead_ins) == 1
    assert len(lead_ins[0]) == 3


def test_one_lead_in_per_wire_when_the_operation_profiles_two():
    """A Profile over two wires gets a lead-in on each.

    `leadinout.generate` emits `lead-in, wire, lead-in, wire`, and the two wires
    are separated in the path by a `G0` link, so the runs split on contiguity.
    """
    first = [_Cmd("G1", X=0.0, Y=0.0), _Cmd("G1", X=4.0, Y=0.0),
             _Cmd("G1", X=4.0, Y=4.0), _Cmd("G1", X=0.0, Y=4.0),
             _Cmd("G1", X=0.0, Y=0.0)]
    second = [_Cmd("G1", X=20.0, Y=0.0), _Cmd("G1", X=24.0, Y=0.0),
              _Cmd("G1", X=24.0, Y=4.0), _Cmd("G1", X=20.0, Y=4.0),
              _Cmd("G1", X=20.0, Y=0.0)]
    # The base profiles **both** wires, with the same `G0` link between them
    # that the dressup has. Two earlier versions of this fixture got it wrong in
    # opposite ways and both produced a *correct* refusal for the wrong reason:
    # giving the base only the first wire made the second genuinely absent, and
    # omitting the link made the base cut straight from wire 1 to wire 2 in one
    # move that the dressup does not have.
    base = _Path(first + [_Cmd("G0", X=20.0, Y=0.0)] + second)
    dressup = _Path([
        _Cmd("G0", X=-3.0, Y=0.0), _Cmd("G1", X=-3.0, Y=0.0),
        _Cmd("G1", X=0.0, Y=0.0),          # lead-in onto wire 1
    ] + first + [
        _Cmd("G0", X=17.0, Y=0.0), _Cmd("G1", X=17.0, Y=0.0),
        _Cmd("G1", X=20.0, Y=0.0),         # lead-in onto wire 2
    ] + second)
    lead_ins, lead_outs, body = lead_runs(cut_moves(base), cut_moves(dressup))
    assert lead_ins is not None
    assert len(lead_ins) == 2
    assert lead_outs == []
    assert moves_to_line(lead_ins[0]).length == pytest.approx(3.0, abs=1e-6)
    assert moves_to_line(lead_ins[1]).length == pytest.approx(3.0, abs=1e-6)
    # Both wires are in the body: 4 moves each, the opening plunges having no
    # XY change.
    assert len(body) == 8


# -- the conflict test ----------------------------------------------------

class _Index:
    """Just enough of `_CutIndex` for `conflicts_for`.

    `entries` are the real tuples `_CutIndex` holds --
    `(part_label, dressup, operation, geometry, role, run_index)` -- because
    `conflicts_for` unpacks them and a test double with a different shape would
    be testing the double.
    """

    def __init__(self, entries):
        self.entries = entries

    def near(self, geometry, margin):
        return [position
                for position, entry in enumerate(self.entries)
                if entry[3].distance(geometry) <= margin]


def _entry(part, geometry, operation=None, role="body", run_index=-1):
    """One index entry: a part label and the geometry it cuts."""
    return (part, None, operation, geometry, role, run_index)


def _square_geometry(x0, y0, size=10.0):
    """A closed square's cut path, with its lower-left corner at (x0, y0).

    The opening `G0` positions the tool without cutting. Without it the first
    `G1` would run from wherever the tool was left -- (0,0) here -- to the
    corner, adding a diagonal that crosses the space these tests are measuring.
    """
    return cut_polyline(_Path([
        _Cmd("G0", X=x0, Y=y0),
        _Cmd("G1", X=x0 + size, Y=y0),
        _Cmd("G1", X=x0 + size, Y=y0 + size),
        _Cmd("G1", X=x0, Y=y0 + size),
        _Cmd("G1", X=x0, Y=y0),
    ]))


def test_a_lead_in_reaching_another_parts_cut_path_is_a_conflict():
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    lead_in = LineString([(6.0, 0.0), (0.0, 0.0)])
    run = LeadRun("lead-in", 0, [], lead_in)
    # A neighbour whose cut path runs 0.5 mm above the lead-in.
    index = _Index([_entry("other", _square_geometry(4.0, 0.5))])
    conflicts = lead_in_audit.conflicts_for([run], object(), "mine", index, 0.6)
    assert len(conflicts) == 1
    assert conflicts[0].distance < 0.6
    assert conflicts[0].other_part == "other"


def test_a_lead_in_clear_of_the_margin_is_not_a_conflict():
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    lead_in = LineString([(6.0, 0.0), (0.0, 0.0)])
    run = LeadRun("lead-in", 0, [], lead_in)
    # The neighbour's path starts at x=4, so it is 4 mm from the lead-in's far
    # end -- well outside the 0.6 mm margin.
    index = _Index([_entry("other", _square_geometry(4.0, 5.0))])
    assert lead_in_audit.conflicts_for([run], object(), "mine", index, 0.6) == []


def test_a_run_is_not_a_conflict_with_the_cut_it_is_attached_to():
    """Its own body is in the index and must not be reported at a distance of 0.

    A lead-in ends on the contour it leads into, so every lead-in on the sheet
    touches its own operation's cut path. Asking for a clearance against it
    would report all of them; the attachment is not interference.
    """
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    operation = object()
    # A contour from (0,0) to (10,0); the lead-in ends on it and starts away.
    contour = LineString([(0.0, 0.0), (10.0, 0.0)])
    lead_in = LineString([(0.0, -3.0), (0.0, 0.0)])
    run = LeadRun("lead-in", 0, [], lead_in, run_index=0)
    index = _Index([_entry("mine", contour, operation),
                    _entry("mine", lead_in, operation, role="lead-in",
                           run_index=0)])
    assert lead_in_audit.conflicts_for([run], operation, "mine", index, 0.6) == []


def test_a_run_that_crosses_its_own_cut_path_away_from_the_end_is_a_conflict():
    """The attachment is excluded; a crossing past it is the real thing.

    This is the case the junction test must not swallow. The lead-in runs from
    (0,-3) up to (0,0), where it is attached to the contour. The contour then
    wraps around and comes back across at (0,-1.5), which is 1.5 mm from either
    end -- a second, legitimate crossing of the same wire.
    """
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    operation = object()
    contour = LineString([(0.0, 0.0), (10.0, 0.0), (10.0, 6.0), (-4.0, 6.0),
                          (-4.0, -1.5), (4.0, -1.5)])
    lead_in = LineString([(0.0, -3.0), (0.0, 0.0)])
    run = LeadRun("lead-in", 0, [], lead_in, run_index=0)
    index = _Index([_entry("mine", contour, operation)])
    conflicts = lead_in_audit.conflicts_for([run], operation, "mine", index, 0.6)
    assert len(conflicts) == 1
    assert conflicts[0].distance == 0.0
    assert "crosses" in conflicts[0].describe(0.6)


def test_the_attachment_is_not_mistaken_for_a_crossing_by_float_noise():
    """The measured shape of the problem: closure noise at 1e-13, not a crossing.

    On the 54-part fixture a body wire's closure agrees with its start to
    8.039e-14 mm and a lead-in's end sits on that start at exactly 0. This
    reproduces that as one part in a trillion on a 1 m span, which is where
    Shapely's own `crosses()` says yes and the answer has to be no.
    """
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    operation = object()
    noise = 8.039e-14
    contour = LineString([(0.0, 0.0), (1000.0, 0.0), (1000.0, 500.0),
                          (0.0, 500.0), (noise, 0.0)])
    lead_in = LineString([(0.0, -3.0), (0.0, 0.0)])
    # The premise: a naive crossing test really does say yes here.
    assert lead_in_audit.body_geometry is not None
    naive = contour.crosses(lead_in) or contour.intersects(lead_in)
    assert naive, "the fixture no longer reproduces the noise it exists for"
    run = LeadRun("lead-in", 0, [], lead_in, run_index=0)
    index = _Index([_entry("mine", contour, operation)])
    assert lead_in_audit.conflicts_for([run], operation, "mine", index, 0.6) == []


def test_a_run_is_not_a_conflict_with_its_own_index_entry():
    """Runs are indexed now, so a run has to recognise and skip itself."""
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    operation = object()
    lead_in = LineString([(6.0, 0.0), (0.0, 0.0)])
    run = LeadRun("lead-in", 0, [], lead_in, run_index=3)
    index = _Index([_entry("mine", lead_in, operation, role="lead-in",
                           run_index=3)])
    assert lead_in_audit.conflicts_for([run], operation, "mine", index, 0.6) == []


def test_a_lead_in_crossing_another_parts_lead_in_is_a_conflict():
    """The case an index of bodies only cannot see: a lead-in is not a body."""
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    mine = LineString([(6.0, 0.0), (0.0, 0.0)])
    theirs = LineString([(3.0, -4.0), (3.0, 4.0)])   # cuts straight across
    run = LeadRun("lead-in", 0, [], mine, run_index=0)
    index = _Index([_entry("other", theirs, object(), role="lead-in")])
    conflicts = lead_in_audit.conflicts_for([run], object(), "mine", index, 0.6)
    assert len(conflicts) == 1
    assert conflicts[0].role == "lead-in"
    assert "lead-in" in conflicts[0].describe(0.6)


def test_a_lead_in_crossing_a_drill_on_its_own_part_is_a_conflict():
    """The same-part filter is gone, and this is what it was hiding.

    A Drilling is already cut by the time a Profile runs over the same region:
    the hole has removed the material, so there is nothing left to absorb a
    lead-in crossing it. Filtering same-part entries out hid exactly the failure
    the filter was written to exclude.
    """
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    lead_in = LineString([(6.0, 0.0), (0.0, 0.0)])
    drilled = LineString([(3.0, -4.0), (3.0, 4.0)])
    run = LeadRun("lead-in", 0, [], lead_in, run_index=0)
    index = _Index([_entry("same", drilled, object())])
    conflicts = lead_in_audit.conflicts_for([run], object(), "same", index, 0.6)
    assert len(conflicts) == 1
    assert conflicts[0].other_part == "same"


def test_a_zero_length_run_is_ignored():
    """A point's distance to a path is a number, and not a meaningful one."""
    try:
        from shapely.geometry import LineString
    except ImportError:
        pytest.skip("shapely not available")
    run = LeadRun("lead-in", 0, [], LineString([(1.0, 1.0), (1.0, 1.0)]))
    index = _Index([_entry("other", _square_geometry(0.0, 0.0))])
    assert lead_in_audit.conflicts_for([run], object(), "mine", index, 0.6) == []


# -- a found conflict is not a clean operation ---------------------------

def test_a_conflict_found_does_not_leave_the_operation_clean():
    """The status has to follow the evidence, or the report lies.

    This is the shape of the bug: conflicts recorded, status left at its
    default, and `offending` filtering on status -- so a scan held the evidence
    of a real interference and reported a clean sheet.
    """
    audit = OperationAudit.__new__(OperationAudit)
    audit.conflicts = [Conflict(LeadRun("lead-in", 0, [], None), 0.1,
                                None, None, "other")]
    assert audit.worst.distance == 0.1
    counts = {OperationAudit.CLEAN: 0, OperationAudit.FIXED: 0,
              OperationAudit.UNRESOLVED: 1, OperationAudit.SKIPPED: 0}
    # The scan builds audits with UNRESOLVED when conflicts exist; this asserts
    # the statuses are distinct enough for a report to tell them apart.
    assert OperationAudit.UNRESOLVED != OperationAudit.CLEAN
    assert counts[OperationAudit.UNRESOLVED] == 1


# -- the marking ----------------------------------------------------------

class _Dressup:
    """A dressup with what `mark_conflict` touches: a Label and a property.

    Properties live in `_properties` rather than on the instance, so `hasattr`
    answers False for one that was never added -- which is the distinction
    `clear_conflict` turns on.
    """

    def __init__(self, label="DressupLeadInOut001_replay_nested_TopStrap_14"):
        self.__dict__["Label"] = label
        self.__dict__["_properties"] = {}

    def addProperty(self, kind, name, group, doc):
        self.__dict__["_properties"][name] = kind

    def __getattr__(self, name):
        try:
            return self.__dict__["_properties"][name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        # `Label` is a real attribute; everything else is a dynamic property, so
        # that `hasattr(dressup, PROP_CONFLICT)` is False until `addProperty`
        # has been called, which is what FreeCAD does.
        if name == "Label":
            self.__dict__[name] = value
        else:
            self.__dict__["_properties"][name] = value


def test_an_unfixable_dressup_is_marked_with_the_prefix():
    dressup = _Dressup()
    label = lead_in_audit.mark_conflict(dressup, "part_Bracket_2")
    assert label.startswith(CONFLICT_PREFIX)
    assert dressup.Label.startswith(CONFLICT_PREFIX)
    # The prefix leads, because the labels are long enough that a suffix is
    # what scrolls off the end of a narrow tree column.
    assert dressup.Label.startswith(CONFLICT_PREFIX + "DressupLeadInOut001")
    assert getattr(dressup, lead_in_audit.PROP_CONFLICT)


def test_marking_names_the_part_in_the_way():
    dressup = _Dressup()
    conflict = Conflict(LeadRun("lead-in", 0, [], None), 0.0598,
                        None, None, "part_TopStrap_7")
    lead_in_audit.mark_conflict(dressup, "part_TopStrap_14", conflict)
    text = getattr(dressup, lead_in_audit.PROP_CONFLICT)
    assert "part_TopStrap_7" in text
    assert "0.0598" in text


def test_marking_twice_does_not_double_the_prefix():
    """A second run must not produce `CONFLICT_CONFLICT_`."""
    dressup = _Dressup()
    lead_in_audit.mark_conflict(dressup, "part_Bracket_2")
    lead_in_audit.mark_conflict(dressup, "part_Bracket_2")
    assert dressup.Label.count(CONFLICT_PREFIX) == 1


def test_clearing_removes_the_prefix_and_the_property():
    dressup = _Dressup()
    lead_in_audit.mark_conflict(dressup, "part_Bracket_2")
    assert lead_in_audit.clear_conflict(dressup) is True
    assert not dressup.Label.startswith(CONFLICT_PREFIX)
    assert getattr(dressup, lead_in_audit.PROP_CONFLICT) == ""
    assert lead_in_audit.clear_conflict(dressup) is False


def test_clearing_an_untouched_dressup_is_not_a_change():
    dressup = _Dressup()
    assert lead_in_audit.clear_conflict(dressup) is False
    assert dressup.Label == "DressupLeadInOut001_replay_nested_TopStrap_14"


# -- reporting says what is fixable ---------------------------------------

def _result(audits, unreadable=()):
    result = lead_in_audit.AuditResult.__new__(lead_in_audit.AuditResult)
    result.job = None
    result.margin = 0.6
    result.audits = audits
    result.unreadable = list(unreadable)
    result.seconds = 0.1
    result.sheet = None      # no stock: the off-sheet check did not run
    # The clearance factor. `AuditResult.__init__` always sets it, and this
    # helper bypasses the constructor, so it is set here too -- `reverify`
    # reads it back to re-scan at the same sensitivity the scan used.
    result.clearance = 1.0
    return result


def _audit(status, detail="", conflicts=()):
    audit = OperationAudit.__new__(OperationAudit)
    audit.dressup = _Dressup()
    audit.operation = None
    audit.part = "part_Bracket_2"
    audit.margin = 0.6
    audit.lead_runs = []
    audit.conflicts = list(conflicts)
    audit.status = status
    audit.detail = detail
    audit.body = []
    # `off_sheet` is None, meaning "the sheet edge was not checked". The
    # fixtures here are single-wire synthetic paths with no stock, so a list
    # would be claiming a check that never ran.
    audit.off_sheet = None
    return audit


def _conflict(distance=0.0598, part="part_TopStrap_7", role="body"):
    return Conflict(LeadRun("lead-in", 0, [], None), distance, None, None, part,
                    role)


# -- the index has to be corrected as the search moves things ---------------

def test_a_neighbour_fixed_earlier_is_advertised_where_it_is_now():
    """The defect `index_after` exists for, in the geometry that produces it.

    One operation's fix moves its lead-in onto a neighbour's lead-in. The
    neighbour is searched next, and if the index still carries the *first*
    operation's pre-fix geometry -- 20.0 mm away, where it is clear -- the
    neighbour is cleared against a lead-in that is not there and the run
    finishes with two lead-ins crossing and a report that says everything is
    fine.

    Measured on this one: the neighbour's lead run is **0.0 mm** from the fixed
    operation's new lead-in and **20.0 mm** from its old one. That gap is the
    whole defect, and it is why the index has to be corrected on the way in
    rather than reconciled at the end.
    """
    LineString, _polygon = _shapely()
    mine, theirs = object(), object()
    dressup = _Dressup()

    # `theirs` cuts a vertical line; `mine`'s lead-in is somewhere else.
    theirs_lead_in = LineString([(20.0, 0.0), (20.0, 10.0)])
    before_fix = LineString([(0.0, 5.0), (0.0, 10.0)])
    after_fix = LineString([(18.0, 5.0), (20.0, 5.0)])
    assert before_fix.distance(theirs_lead_in) == pytest.approx(20.0)
    assert after_fix.distance(theirs_lead_in) == pytest.approx(0.0)

    index = _CutIndex()
    index.add("mine", dressup, mine, _square_geometry(0.0, 20.0))
    index.add("mine", dressup, mine, before_fix,
              role=lead_in_audit.ROLE_LEAD_IN, run_index=0)
    index.add("theirs", dressup, theirs, theirs_lead_in,
              role=lead_in_audit.ROLE_LEAD_IN, run_index=0)
    index.build()

    # The neighbour's own run, asked whether it is clear.
    run = LeadRun("lead-in", 0, [], theirs_lead_in, run_index=0)
    assert lead_in_audit.conflicts_for([run], theirs, "theirs", index, 0.6) == []

    # `mine` is fixed: its lead-in lands on the neighbour's.
    index.replace("mine", dressup, mine, _square_geometry(0.0, 20.0),
                  [LeadRun("lead-in", 0, [], after_fix, run_index=0)])

    clashes = lead_in_audit.conflicts_for([run], theirs, "theirs", index, 0.6)
    assert len(clashes) == 1
    assert clashes[0].role == lead_in_audit.ROLE_LEAD_IN
    assert clashes[0].other_part == "mine"


def test_replacing_an_operation_leaves_its_neighbours_alone():
    """`replace` is by identity, so one operation's fix cannot rewrite another's.

    Two operations cutting overlapping regions is what the same-part filter used
    to exclude, and this is where that would have bitten: a rewrite matched on
    the part label would have overwritten the second operation's entries and
    deleted its lead runs.
    """
    _LineString, _polygon = _shapely()
    mine, theirs = object(), object()
    dressup = _Dressup()
    LineString, _polygon = _shapely()
    run = LeadRun("lead-in", 0, [], LineString([(6.0, 0.0), (0.0, 0.0)]),
                  run_index=0)

    index = _CutIndex()
    index.add("mine", dressup, mine, _square_geometry(0.0, 0.0))
    index.add("theirs", dressup, theirs, _square_geometry(50.0, 50.0))
    index.build()

    index.replace("mine", dressup, mine, _square_geometry(10.0, 0.0), [run])

    assert len(index.entries) == 3
    untouched = [e for e in index.entries if e[2] is theirs]
    assert len(untouched) == 1
    assert untouched[0][3].bounds == (50.0, 50.0, 60.0, 60.0)


def test_replacing_swaps_an_operations_entries_rather_than_adding_to_them():
    """Three entries for one operation would mean the old body still advertised."""
    LineString, _polygon = _shapely()
    operation = object()
    dressup = _Dressup()

    index = _CutIndex()
    index.add("mine", dressup, operation, _square_geometry(0.0, 0.0))
    index.add("mine", dressup, operation, LineString([(6.0, 0.0), (0.0, 0.0)]),
              role=lead_in_audit.ROLE_LEAD_IN, run_index=0)
    index.build()

    index.replace("mine", dressup, operation, _square_geometry(10.0, 0.0), [])
    assert len(index.entries) == 1
    assert index.entries[0][4] == lead_in_audit.ROLE_BODY
    assert index.entries[0][3].bounds == (10.0, 0.0, 20.0, 10.0)


def test_the_index_is_rebuilt_before_the_next_query_not_by_the_replace():
    """`replace` marks it stale; the query does the work.

    Splitting it this way is what makes a candidate whose recompute failed cost
    nothing: there is no query, so there is no rebuild.
    """
    _LineString, _polygon = _shapely()
    operation = object()
    dressup = _Dressup()
    index = _CutIndex()
    index.add("mine", dressup, operation, _square_geometry(0.0, 0.0))
    index.build()
    assert index._stale is False

    index.replace("mine", dressup, operation, _square_geometry(10.0, 0.0), [])
    assert index._stale is True

    # Querying is what clears it.
    index.near(_square_geometry(0.0, 0.0).centroid, 0.6)
    assert index._stale is False


class _Paths:
    """The two attributes `index_after` reads."""

    def __init__(self, path):
        self.Path = path


def test_index_after_returns_none_when_the_paths_cannot_be_reconciled():
    """It leaves the index alone rather than replacing it with a guess.

    An operation whose lead-ins cannot be told from its cut has no runs to
    publish. Writing an entry with none would advertise "this operation has no
    lead-in", which is a claim nobody checked and one the rest of the scan would
    then rely on.
    """
    _LineString, _polygon = _shapely()
    dressup = _Dressup()
    operation = _Paths(_Path([_Cmd("G1", X=3.0, Y=3.0)]))
    index = _CutIndex()
    index.add("mine", dressup, operation, _square_geometry(0.0, 0.0))
    index.build()
    before = list(index.entries)

    # A dressup path that is not the base path plus a lead-in: `lead_runs`
    # refuses it, which is how the audit reports an operation it cannot judge.
    bad_dressup = _Paths(_Path([_Cmd("G1", X=99.0, Y=99.0)]))
    assert lead_in_audit.index_after(operation, bad_dressup, "mine",
                                     index) is None
    assert index.entries == before


def test_index_after_republishes_an_operations_runs():
    """The ordinary case: the same call that refuses also does the rewrite."""
    _LineString, _polygon = _shapely()
    operation = _Paths(SQUARE)
    dressup = _Paths(_with_lead_in())

    index = _CutIndex()
    index.add("mine", dressup, operation, _square_geometry(0.0, 0.0))
    index.build()

    runs = lead_in_audit.index_after(operation, dressup, "mine", index)
    assert runs is not None
    assert len(runs) == 1
    assert runs[0].kind == lead_in_audit.ROLE_LEAD_IN
    # Body plus the one run, and the run is published with its role.
    assert len(index.entries) == 2
    assert sorted(e[4] for e in index.entries) == [lead_in_audit.ROLE_BODY,
                                                   lead_in_audit.ROLE_LEAD_IN]


# -- the clearance the user dials -----------------------------------------

class _Job:
    """Just enough of an operation/dressup pair for `_prepare` to read."""

    def __init__(self):
        self.Path = _Path([])


class _Prepared:
    """Stands up `_prepare`'s five dependencies, so its arithmetic can be tested.

    `_prepare` reaches the job through module-level names, and the thing under
    test is the multiplication between them rather than any of them, so they
    are stood in for here rather than a whole replay built.

    Used as a context manager, because patching module attributes and restoring
    them by hand is how a test leaves the module broken for the next one.
    """

    _NAMES = ("lead_in_out_dressups", "cut_moves", "lead_runs",
              "tool_radius_of", "_operation_cut_geometry")

    def __init__(self, tool_radius=0.6):
        self.tool_radius = tool_radius
        self.pairs = [(_Job(), _Job())]
        self._saved = None

    def __enter__(self):
        pairs, radius = self.pairs, self.tool_radius
        # Resolved here rather than captured from the caller: the test's
        # `_LineString` is a local, and a closure over it would be a NameError
        # at the moment the stub is installed rather than where it is written.
        line = _shapely()[0]
        self._saved = {name: getattr(lead_in_audit, name)
                       for name in self._NAMES}
        lead_in_audit.lead_in_out_dressups = lambda job: pairs
        lead_in_audit.tool_radius_of = lambda op, job=None: radius
        lead_in_audit.cut_moves = lambda path: []
        lead_in_audit.lead_runs = lambda base, dress: ([[]], [], [object()])
        lead_in_audit._operation_cut_geometry = \
            lambda body, path, op: line([(0.0, 0.0), (1.0, 0.0)])
        return self

    def __exit__(self, *exc):
        for name, value in (self._saved or {}).items():
            setattr(lead_in_audit, name, value)
        self._saved = None
        return False

    def margin_at(self, margin, clearance):
        with self:
            prepared, _index, _unreadable = lead_in_audit._prepare(
                None, margin, clearance)
        return prepared[0][3]


def test_the_clearance_scales_the_margin_it_is_measured_against():
    """One multiplication, applied to the margin, is the whole mechanism.

    Both checks read the figure that comes out of `_prepare` and neither
    re-derives it, so scaling there scales the neighbour clearance, the edge
    clearance and the search's acceptance test together. Pinned on `_prepare`
    because that is the only place the factor is applied, and a second
    multiplication anywhere else would silently square it.
    """
    _LineString, _polygon = _shapely()
    prepared = _Prepared(tool_radius=0.6)
    assert [prepared.margin_at(None, c) for c in (0.0, 1.0, 2.5)] == \
        [0.0, 0.6, 1.5]


def test_the_clearance_scales_an_explicit_margin_too():
    """`scan_job(job, margin=2.0, clearance=1.5)` means 3.0 mm, not "2.0, but the
    other check disagrees".

    One rule with no special cases. If the factor applied only to a derived
    tool radius, a caller passing an explicit margin would get two checks at two
    different sensitivities and no way to see it.
    """
    _LineString, _polygon = _shapely()
    assert _Prepared(tool_radius=0.6).margin_at(2.0, 1.5) == pytest.approx(3.0)


def test_the_result_carries_the_clearance_so_the_search_cannot_lose_it():
    """The one that would have been silent.

    `resolve_conflicts` rebuilds the index from scratch and reads `margin` back
    off the result. Without the factor carried alongside it, a run at 2x would
    have its candidates tested at 1x -- so a report saying "40 operations need
    doing by hand at 2x" would have been produced by a search that accepted only
    what was already clear at 1x, and nothing would have said so.
    """
    _LineString, _polygon = _shapely()
    seen = []
    saved_scan = lead_in_audit.scan_job
    lead_in_audit.scan_job = lambda job, margin=None, progress=None, \
        clearance=1.0: seen.append(clearance) or _result([])
    try:
        result = _result([_audit(OperationAudit.FIXED)])
        result.clearance = 2.5
        lead_in_audit.reverify(result)
        assert seen == [2.5]
    finally:
        lead_in_audit.scan_job = saved_scan


def test_the_search_rebuilds_its_index_at_the_same_clearance_the_scan_used():
    """The whole point of carrying it, checked where the bug would land.

    `resolve_conflicts` builds a fresh index from the job before searching. If
    it used the default factor there, every candidate would be tested against a
    *different* sensitivity from the one the scan reported -- so at 2x the
    search would accept only what was already clear at 1x, and the report would
    still say 2x.

    Behavioural, not structural: with no operations to search the call runs to
    completion without a FreeCAD job, and `_prepare` is recorded directly.
    Asserting on the source instead would catch the same edit, but would not
    catch the one where the value is read from somewhere else.
    """
    _LineString, _polygon = _shapely()
    seen = []
    saved_prepare = lead_in_audit._prepare

    def _record(job, margin, clearance=1.0):
        seen.append((margin, clearance))
        return [], _CutIndex(), []

    lead_in_audit._prepare = _record
    try:
        result = _result([_audit(OperationAudit.CLEAN)])
        result.margin = 0.6
        result.clearance = 2.5
        lead_in_audit.resolve_conflicts(result, dry_run=True)
    finally:
        lead_in_audit._prepare = saved_prepare

    assert seen == [(0.6, 2.5)]


def test_a_result_with_no_clearance_recorded_is_treated_as_the_default():
    """Defensive, because `describe_result` prints the figure.

    A result built by anything that predates the factor has no `clearance`
    attribute, and a report that raised on one would turn a missing dial into a
    crash in the reporting path.
    """
    _LineString, _polygon = _shapely()
    audit = _audit(OperationAudit.CLEAN)
    result = _result([audit])
    del result.clearance
    lines = lead_in_audit.describe_result(result)
    assert any("clearance of" in line for line in lines)


def test_the_report_states_the_clearance_it_used():
    """The counts are a function of it, so a headline without it is unreadable.

    The same job reports 40 offending operations at 1x and rather more at 2x.
    A report quoting either number without saying which it was measured at
    makes them incomparable, and a number nobody can compare is decoration.
    """
    _LineString, _polygon = _shapely()
    audit = _audit(OperationAudit.CLEAN)
    result = _result([audit])
    result.job = None
    # No tool readable and no explicit margin: the factor alone, still stated.
    result.margin = None
    assert "1.00 x tool radius" in lead_in_audit.describe_result(result)[0]


def test_the_report_states_the_clearance_in_millimetres_when_a_tool_is_readable(
        monkeypatch):
    """The production path, and the one a reader actually acts on.

    "2.50 x tool radius" is a ratio; a plasma operator's question is how many
    millimetres, and the factor only means something next to a tool. This is
    the branch every real run takes, because `scan_job` leaves `margin` as None
    so that each operation uses its own tool.

    `tool_radius_of` falls back to the job's widest tool, so the widest is the
    figure to quote: it is the largest clearance actually in force.
    """
    _LineString, _polygon = _shapely()
    result = _result([_audit(OperationAudit.CLEAN)])
    result.margin = None
    result.clearance = 2.5

    from freecad.nestingworkbench.Tools.Cam import cam_replay
    monkeypatch.setattr(cam_replay, "tool_diameters",
                        lambda job: [("plasma", 1.2), ("mill", 6.0)])
    line = lead_in_audit.describe_result(result)[0]
    # 6.0 mm diameter -> 3.0 mm radius -> 7.5 mm at 2.5x.
    assert "7.5000 mm" in line
    assert "2.50 x tool radius" in line


def test_a_default_clearance_reads_as_the_tool_radius_rather_than_a_ratio(
        monkeypatch):
    """At 1.0 the report says what it means instead of restating the default.

    "1.00 x tool radius" is a number the reader has to multiply before it means
    anything; "the tool radius (3.0000 mm)" is the clearance. Both carry the
    factor's value -- this is wording, not a second source of truth.
    """
    _LineString, _polygon = _shapely()
    result = _result([_audit(OperationAudit.CLEAN)])
    result.margin = None
    result.clearance = 1.0

    from freecad.nestingworkbench.Tools.Cam import cam_replay
    monkeypatch.setattr(cam_replay, "tool_diameters", lambda job: [("mill", 6.0)])
    assert "the tool radius (3.0000 mm)" in lead_in_audit.describe_result(result)[0]


def test_an_explicit_margin_is_reported_in_millimetres():
    """A caller who passed millimetres wants millimetres back, not a factor."""
    _LineString, _polygon = _shapely()
    result = _result([_audit(OperationAudit.CLEAN)])
    result.margin = 0.75
    result.clearance = 2.0
    assert "1.5000 mm" in lead_in_audit.describe_result(result)[0]


# -- a lead run is named as a lead run -------------------------------------

def test_a_clash_with_a_lead_run_is_not_described_as_a_clash_with_a_cut_path():
    """The mis-description, stated as a test because it was the bug.

    `mark_conflict` used to build its sentence from the part name and the
    literal words "cut path". A lead-in that reached another operation's
    *lead-in* was therefore marked as reaching a cut path -- sending the
    operator to a contour that was never touched while the two lead runs
    actually crossing went unexamined. On plasma that is the difference between
    looking at the right thing and posting the job.
    """
    run = LeadRun("lead-in", 2, [], None)
    clash = Conflict(run, 0.0, None, None, "part_TopStrap_14",
                     lead_in_audit.ROLE_LEAD_IN)
    assert "lead-in" in clash.what
    assert clash.summary().endswith("part_TopStrap_14's lead-in")
    assert "crosses" in clash.summary()

    dressup = _Dressup()
    lead_in_audit.mark_conflict(dressup, "part_TopStrap_14", clash)
    text = getattr(dressup, lead_in_audit.PROP_CONFLICT)
    assert "part_TopStrap_14's lead-in" in text
    assert "cut path" not in text


def test_a_clash_with_a_body_is_still_described_as_a_cut_path():
    """The other half: the fix must not lose what it had."""
    clash = _conflict(distance=0.0598, part="part_TopStrap_7")
    assert "cut path" in clash.what
    assert "0.0598" in clash.summary()
    # And the margin clause still rides along for the report line.
    assert clash.describe(0.6).endswith("(margin 0.6000 mm)")


def test_a_lead_in_crossing_another_parts_lead_out_is_a_conflict():
    """The mirror of the lead-in case: a lead-out is a neighbour too.

    `RadiusOut` leads away from the cut just as `RadiusIn` leads into it, and
    on a part with two wires the lead-out of one wire lands in the gap beside
    the lead-in of another. The index carries both roles, so this is the same
    test with the other word in it -- and it is the one a body-only index
    cannot see at all.
    """
    LineString, _polygon = _shapely()
    mine = LineString([(0.0, 0.0), (0.0, 10.0)])
    theirs = LineString([(-4.0, 5.0), (4.0, 5.0)])   # cuts straight across
    run = LeadRun("lead-out", 0, [], mine, run_index=0)
    index = _Index([_entry("other", theirs, object(),
                           role=lead_in_audit.ROLE_LEAD_OUT)])
    conflicts = lead_in_audit.conflicts_for([run], object(), "mine", index, 0.6)
    assert len(conflicts) == 1
    assert conflicts[0].role == lead_in_audit.ROLE_LEAD_OUT
    assert "lead-out" in conflicts[0].summary()


def test_the_report_counts_lead_run_clashes_in_the_headline():
    """Zero is a measurement the reader is entitled to.

    It used to be silently zero on every layout this was developed against,
    which is indistinguishable from "not checked" -- and that is how a check
    that never fires survives for years.
    """
    run_clash = _conflict(distance=0.0, part="part_TopStrap_14",
                          role=lead_in_audit.ROLE_LEAD_IN)
    body_clash = _conflict(distance=0.0598, part="part_SimpleSpacer_1")
    audits = [
        _audit(lead_in_audit.OperationAudit.UNRESOLVED,
               conflicts=[run_clash, body_clash]),
        _audit(lead_in_audit.OperationAudit.UNRESOLVED,
               conflicts=[_conflict(distance=0.2, part="part_Bracket_2")]),
    ]
    lines = "\n".join(lead_in_audit.describe_result(_result(audits),
                                                   dry_run=True))
    assert "1 reaches another operation's lead-in or lead-out." in lines


def test_the_headline_agrees_with_itself_about_one_and_many():
    """`1 reach` is the kind of thing that ships because nobody read it aloud.

    Two assertions because both shapes come from one expression and only the
    plural case catches a missing conditional: with everything saying "reach",
    the singular still has to be exercised on its own.
    """
    _LineString, _polygon = _shapely()
    clash = _conflict(distance=0.0, part="part_TopStrap_14",
                      role=lead_in_audit.ROLE_LEAD_IN)
    one = lead_in_audit.describe_result(
        _result([_audit(lead_in_audit.OperationAudit.UNRESOLVED,
                        conflicts=[clash])]), dry_run=True)[0]
    many = lead_in_audit.describe_result(
        _result([_audit(lead_in_audit.OperationAudit.UNRESOLVED,
                        conflicts=[clash, clash])]), dry_run=True)[0]
    assert "1 reaches another operation's lead-in or lead-out." in one
    assert "2 reach another operation's lead-in or lead-out." in many


def test_the_report_has_a_section_of_its_own_for_lead_run_clashes():
    """Same margin and same fix, but not the same sentence.

    Reaching a cut path re-cuts material. Crossing another lead run bridges the
    arc across a kerf it has already opened, which on plasma or waterjet drops
    the flame and errors the controller. The operator decides whether to post
    the job differently in the two cases, so a report that reads alike for them
    has thrown away the one thing they need.
    """
    run_clash = _conflict(distance=0.0, part="part_TopStrap_14",
                          role=lead_in_audit.ROLE_LEAD_IN)
    audit = _audit(lead_in_audit.OperationAudit.UNRESOLVED,
                   conflicts=[run_clash])
    lines = lead_in_audit.describe_result(_result([audit]), dry_run=True)
    body = "\n".join(lines)
    assert "another operation's lead-in or lead-out, rather than its cut path" \
        in body
    assert "kerf" in body
    assert "part_TopStrap_14's lead-in" in body


def test_a_clean_report_still_says_the_lead_run_count_is_zero():
    """Stated even when there is nothing, because that is the measurement."""
    audit = _audit(lead_in_audit.OperationAudit.CLEAN)
    lines = lead_in_audit.describe_result(_result([audit]))
    assert "0 reach another operation's lead-in or lead-out." in lines[0]


# -- the end of a run re-reads the sheet -----------------------------------

class _RecheckDressup(_Dressup):
    """A dressup with a `Name`, which is how `reverify` pairs the two passes.

    Pairing is by `Name` rather than identity because a re-scan builds its own
    `OperationAudit` objects from the job: they are different objects
    describing the same operation, and matching on identity would find none.
    """

    def __init__(self, name="Dressup001"):
        _Dressup.__init__(self, "Dressup001")
        self.__dict__["Name"] = name


def _recheck_audit(status, conflicts=()):
    audit = _audit(status, detail="moved the start point to (1.0, 2.0)",
                   conflicts=conflicts)
    audit.dressup = _RecheckDressup()
    return audit


def test_reverify_leaves_a_fix_alone_when_the_finished_job_agrees(monkeypatch):
    """The common case, and the one that must not cost a false alarm."""
    _LineString, _polygon = _shapely()
    fixed = _recheck_audit(lead_in_audit.OperationAudit.FIXED)
    result = _result([fixed])

    monkeypatch.setattr(lead_in_audit, "scan_job", lambda job, margin=None,
                        progress=None, clearance=1.0: _result(
                            [_recheck_audit(lead_in_audit.OperationAudit.CLEAN)]))
    assert lead_in_audit.reverify(result) == []
    assert fixed.status == lead_in_audit.OperationAudit.FIXED


def test_reverify_does_not_re_word_an_operation_that_was_already_unresolved(
        monkeypatch):
    """Confirmed is not corrected, and the report must not say otherwise.

    An earlier version appended "Re-checked against the finished job" to every
    `UNRESOLVED` audit, including the hand-jobs the re-read never had any
    reason to doubt. On a sheet with twenty of them the line became noise, and
    it implied a change that had not happened.
    """
    _LineString, _polygon = _shapely()
    stuck = _recheck_audit(lead_in_audit.OperationAudit.UNRESOLVED,
                           conflicts=[_conflict(distance=0.0,
                                                part="part_TopStrap_14")])
    before = stuck.detail
    monkeypatch.setattr(lead_in_audit, "scan_job", lambda job, margin=None,
                        progress=None, clearance=1.0: _result([
                            _recheck_audit(
                                lead_in_audit.OperationAudit.UNRESOLVED,
                                conflicts=[_conflict(distance=0.0,
                                                    part="part_TopStrap_14")])]))
    assert lead_in_audit.reverify(_result([stuck])) == []
    assert stuck.detail == before


def test_reverify_downgrades_a_fix_the_finished_job_does_not_support(monkeypatch):
    """The safety net, and the reason the run cannot claim a clean sheet.

    Every per-operation check in the search is honest about what it was given.
    What none of them can see is the operation *after* it -- one fix moves a
    lead-in into its neighbour, and the neighbour's own search is the only thing
    between that and a clean report. So the claim is re-read at the end, and an
    operation that does not hold up is marked rather than passed off as fixed.
    """
    _LineString, _polygon = _shapely()
    fixed = _recheck_audit(lead_in_audit.OperationAudit.FIXED)
    result = _result([fixed])

    still_bad = _conflict(distance=0.0, part="part_TopStrap_14",
                          role=lead_in_audit.ROLE_LEAD_IN)
    monkeypatch.setattr(lead_in_audit, "scan_job", lambda job, margin=None,
                        progress=None, clearance=1.0: _result([
                            _recheck_audit(lead_in_audit.OperationAudit.UNRESOLVED,
                                           conflicts=[still_bad])]))

    downgraded = lead_in_audit.reverify(result)
    assert list(downgraded) == [fixed]
    assert fixed.status == lead_in_audit.OperationAudit.UNRESOLVED
    assert "Re-checked against the finished job" in fixed.detail
    # And it is marked, because the user has just been told not to look.
    assert fixed.dressup.Label.startswith(CONFLICT_PREFIX)


def test_reverify_re_reads_the_findings_not_only_the_status(monkeypatch):
    """A status alone would leave the report describing the pre-fix conflict.

    The audit carries a `conflicts` list and an `off_sheet` list, and both were
    measured at the original start point. Reporting `FIXED` alongside them means
    the summary counts a problem the operation no longer has -- the same
    quiet-checker failure, running the other way.
    """
    _LineString, _polygon = _shapely()
    fixed = _recheck_audit(lead_in_audit.OperationAudit.FIXED,
                           conflicts=[_conflict(distance=0.4,
                                               part="part_Old_1")])
    fixed.off_sheet = []
    result = _result([fixed])

    monkeypatch.setattr(lead_in_audit, "scan_job", lambda job, margin=None,
                        progress=None, clearance=1.0: _result(
                            [_recheck_audit(lead_in_audit.OperationAudit.CLEAN)]))
    lead_in_audit.reverify(result)
    assert fixed.conflicts == []
    assert fixed.off_sheet is None


def test_reverify_only_looks_at_operations_the_run_moved(monkeypatch):
    """A `CLEAN` or `SKIPPED` audit was not touched, so re-reading it can only
    restate the opening scan -- and a re-scan of 110 operations is not free."""
    _LineString, _polygon = _shapely()
    clean = _recheck_audit(lead_in_audit.OperationAudit.CLEAN)
    result = _result([clean])

    monkeypatch.setattr(lead_in_audit, "scan_job", lambda job, margin=None,
                        progress=None, clearance=1.0: _result([]))
    assert lead_in_audit.reverify(result) == []


def test_a_dry_run_never_re_reads_the_job():
    """"Report only writes nothing at all" includes not going looking again.

    The guard is the call site in `resolve_conflicts`, so this reads the source
    rather than the behaviour: a dry run and a real run are told apart by one
    `if`, and this says the one is load-bearing rather than incidental.

    On the AST rather than on the text, because a textual match on
    `resolve_conflicts` breaks the moment anyone wraps the call in a `try` --
    which is exactly what happened here, and it would have looked like the guard
    had been deleted.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(
        lead_in_audit.resolve_conflicts)))
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_not_dry_run = (isinstance(test, ast.UnaryOp)
                          and isinstance(test.op, ast.Not)
                          and isinstance(test.operand, ast.Name)
                          and test.operand.id == "dry_run")
        if not is_not_dry_run:
            continue
        for child in ast.walk(node):
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "reverify"):
                guarded = True
    assert guarded, ("resolve_conflicts must not call reverify() in a dry run; "
                     "'report only writes nothing' includes not re-reading")


def test_fixable_is_distinct_from_fixed_and_unresolved():
    """Three distinct outcomes, not two.

    A dry run that reports a fix as `FIXED` claims a change the document does
    not have; one that reports it as `UNRESOLVED` says the feature cannot fix
    it. Both were wrong at different times.
    """
    statuses = {OperationAudit.CLEAN, OperationAudit.FIXED,
                OperationAudit.FIXABLE, OperationAudit.UNRESOLVED,
                OperationAudit.SKIPPED}
    assert len(statuses) == 5


def test_a_dry_run_reports_a_fix_as_fixable_not_unresolved():
    audit = _audit(OperationAudit.FIXABLE,
                   "a start point at (1.000, 2.000) clears it, found after 1 "
                   "candidate")
    result = _result([_audit(OperationAudit.CLEAN),
                      _audit(OperationAudit.FIXABLE, audit.detail)])
    counts = result.counts()
    assert counts[OperationAudit.FIXABLE] == 1
    assert counts[OperationAudit.UNRESOLVED] == 0
    assert len(result.fixable) == 1
    assert not result.unresolved
    # Both mean "not ready to post", which is what the dialog needs.
    assert len(result.needs_a_human) == 1


def test_the_dry_run_summary_counts_what_could_be_moved():
    result = _result([_audit(OperationAudit.CLEAN),
                      _audit(OperationAudit.FIXABLE, "a start point clears it"),
                      _audit(OperationAudit.UNRESOLVED, "none does")])
    headline = lead_in_audit.describe_result(result, dry_run=True)[0]
    assert "1 could be moved to a clear start point" in headline
    assert "1 need doing by hand" in headline
    # The applied wording. Checked as "N moved", because "could be moved to a
    # clear start point," contains "moved to a clear start point," and the
    # first version of this assertion could therefore never fail.
    assert "0 moved to a clear start point" not in headline
    assert "1 moved to a clear start point," not in headline


def test_the_applied_summary_says_moved_not_could_be_moved():
    result = _result([_audit(OperationAudit.CLEAN),
                      _audit(OperationAudit.FIXED, "moved it")])
    headline = lead_in_audit.describe_result(result, dry_run=False)[0]
    assert "1 moved to a clear start point" in headline
    assert "could be moved" not in headline


def test_the_two_outcomes_are_reported_in_separate_sections():
    """Both appear, and under headings that do not blame the user for both."""
    result = _result([
        _audit(OperationAudit.FIXABLE, "a start point clears it",
               [_conflict()]),
        _audit(OperationAudit.UNRESOLVED, "none does", [_conflict()]),
    ])
    lines = lead_in_audit.describe_result(result, dry_run=True)
    joined = "\n".join(lines)
    assert "a start point was found that clears them" in joined
    assert "need your judgement about the cut" in joined
    # The fixable one is not called "left for you to fix".
    assert "left for you to fix" not in joined
    # And the closing line points at the run that would apply them.
    assert "Report and fix" in joined


def test_a_dry_run_report_says_nothing_was_changed():
    result = _result([_audit(OperationAudit.FIXABLE, "a start point clears it")])
    joined = "\n".join(lead_in_audit.describe_result(result, dry_run=True))
    assert "Nothing has been changed" in joined
    assert "1 start point that can be moved" in joined


def test_a_clean_sheet_reports_no_next_step():
    """Nothing to fix means nothing to offer, and no nagging."""
    result = _result([_audit(OperationAudit.CLEAN)])
    joined = "\n".join(lead_in_audit.describe_result(result, dry_run=True))
    assert "98 clear" in joined or "1 clear" in joined
    assert "Report and fix" not in joined


def test_the_fixable_line_names_the_start_point():
    """A report that says "could be moved" without saying where is thin.

    The fixable section prints `label (part) -- detail`, not `describe()`, so
    the coordinate lives in the detail and the per-operation line is where the
    user finds it.
    """
    audit = _audit(OperationAudit.FIXABLE,
                   "a start point at (260.844, 52.870) clears it, found after "
                   "1 candidate", [_conflict()])
    lines = lead_in_audit.describe_result(_result([audit]), dry_run=True)
    assert any("(260.844, 52.870)" in line for line in lines)
    # The section header is what tells the user this one is good news.
    assert any("a start point was found that clears them" in line
               for line in lines)
    assert any("Nothing has been changed" in line for line in lines)


def test_the_unresolved_section_says_marked_only_when_it_was():
    """Not marked in a dry run; marked in a real one. The text has to agree."""
    audit = _audit(OperationAudit.UNRESOLVED,
                   "no start point cleared it; it was left as it was",
                   [_conflict()])
    dry = "\n".join(lead_in_audit.describe_result(_result([audit]),
                                                  dry_run=True))
    assert CONFLICT_PREFIX not in dry

    marked = _audit(OperationAudit.UNRESOLVED,
                    "no start point cleared it; it is marked %s and was left "
                    "as it was" % CONFLICT_PREFIX, [_conflict()])
    real = "\n".join(lead_in_audit.describe_result(_result([marked]),
                                                  dry_run=False))
    assert CONFLICT_PREFIX in real


def test_unreadable_operations_are_still_reported():
    result = _result([_audit(OperationAudit.CLEAN)],
                     unreadable=[(_Dressup(), "its tool radius could not be "
                                              "read")])
    joined = "\n".join(lead_in_audit.describe_result(result, dry_run=True))
    assert "Could not be checked" in joined
    assert "tool radius" in joined


# -- candidate ordering ---------------------------------------------------

def test_candidates_alternate_about_the_original_start():
    """The first candidate accepted is the nearest one in *either* direction.

    Taking one direction only would walk the whole way round a wire before
    trying the other side, so a start point 3 mm away anticlockwise would lose
    to one 40 mm away clockwise.
    """
    body = cut_moves(SQUARE)          # a 40 mm square
    first = list(lead_in_audit.geodesic_start_point_candidates(
        body, 0, (10.0, 0.0), step=4.0, limit=4))
    assert len(first) == 4
    # Each pair is one step out in each direction from the previous.
    assert first[0] != first[1]
    # Alternating means the walk does not run away in one direction: the spread
    # of the first four stays within two steps of the origin's neighbourhood.
    assert all(0.0 <= x <= 10.0 and 0.0 <= y <= 10.0 for x, y in first)


def test_the_candidate_walk_wraps_so_it_can_pass_the_wires_end():
    """Past the far end, the walk continues on the other side rather than stopping."""
    body = cut_moves(SQUARE)
    points = list(lead_in_audit.geodesic_start_point_candidates(
        body, 0, (10.0, 0.0), step=8.0, limit=12))
    assert len(points) == 12
    assert all(0.0 <= x <= 10.0 and 0.0 <= y <= 10.0 for x, y in points)


def test_candidates_are_bounded():
    body = cut_moves(SQUARE)
    assert len(list(lead_in_audit.geodesic_start_point_candidates(
        body, 0, (10.0, 0.0), limit=5))) == 5


def test_a_wire_that_does_not_exist_yields_no_candidates():
    assert list(lead_in_audit.geodesic_start_point_candidates(
        [], 0, (0.0, 0.0))) == []


def test_wires_are_split_where_the_path_stops_being_contiguous():
    """Two closed wires with a `G0` link between them are two wires."""
    body = [
        CutMove("G1", 0, 0, 4, 0, [(0, 0), (4, 0)]),
        CutMove("G1", 4, 0, 4, 4, [(4, 0), (4, 4)]),
        CutMove("G1", 20, 0, 24, 0, [(20, 0), (24, 0)]),
        CutMove("G1", 24, 0, 24, 4, [(24, 0), (24, 4)]),
    ]
    first = lead_in_audit._wire_of_run(body, 0)
    second = lead_in_audit._wire_of_run(body, 1)
    assert len(first) == 2
    assert len(second) == 2
    assert first[0].x0 == 0.0
    assert second[0].x0 == 20.0


# -- the body geometry is not a line the torch never cuts -----------------

def _two_wire_body(gap=34.941):
    """A body of two contiguous wires, with a `G0` link between them dropped.

    `cut_moves` leaves no trace of the link -- that is the whole point -- so the
    moves either side of it simply do not share a point. Reproduces
    `DressupLeadInOut_replay_nested_BottomStrap_7`: 7 moves, one join spanning
    34.941 mm.
    """
    return [
        CutMove("G1", 0, 0, 4, 0, [(0, 0), (4, 0)]),
        CutMove("G1", 4, 0, 4, 4, [(4, 0), (4, 4)]),
        CutMove("G1", 4, 4, 0, 4, [(4, 4), (0, 4)]),
        CutMove("G1", 0, 4, 0, 0, [(0, 4), (0, 0)]),
        # A `G0` would come here. It is not a cut, so it is not in `body`.
        CutMove("G1", 60, 0, 64, 0, [(60, 0), (64, 0)]),
        CutMove("G1", 64, 0, 64, 4, [(64, 0), (64, 4)]),
        CutMove("G1", 64, 4, 60, 4, [(64, 4), (60, 4)]),
    ]


def test_the_body_geometry_does_not_draw_the_link_between_wires():
    """The measured failure: 34.9409 mm of a body that is not a cut.

    Joining the wires into one polyline produced a LineString 83.9135 mm long
    against 48.9727 mm of real move length. Every clearance in this file is
    measured against this geometry, so that straight line across the part could
    invent a conflict as readily as hide one.
    """
    LineString, _polygon = _shapely()
    body = _two_wire_body()
    real = sum(math.hypot(m.x1 - m.x0, m.y1 - m.y0) for m in body)
    geometry = body_geometry(body)
    assert abs(geometry.length - real) < 1e-6, (
        "body geometry is %.4f mm but the moves are %.4f mm -- something is "
        "being drawn that is not cut" % (geometry.length, real))


def test_a_multi_wire_body_is_a_multi_linestring_and_a_single_wire_is_not():
    LineString, _polygon = _shapely()
    multi = body_geometry(_two_wire_body())
    assert multi.geom_type == "MultiLineString"
    assert len(multi.geoms) == 2
    single = body_geometry([
        CutMove("G1", 0, 0, 4, 0, [(0, 0), (4, 0)]),
        CutMove("G1", 4, 0, 4, 4, [(4, 0), (4, 4)]),
    ])
    assert single.geom_type == "LineString"


def test_a_body_of_nothing_falls_back_to_the_path_it_was_given():
    """No body and no path yields None, which callers must handle.

    Not an empty geometry: an empty one has a length of 0 and would read as
    "this operation cuts nothing", which is a finding rather than an absence.
    """
    assert body_geometry([], None) is None


def test_a_lead_in_that_only_touches_the_link_it_fabricated_is_not_a_crossing():
    """The six own-body crossings, as a test.

    A lead-in ending on a body's wire was reported as crossing its own
    operation's cut path at the seam between the wires -- 0.000 mm from the join
    point. With the link no longer drawn, the intersection at the seam is the
    attachment, and `_crosses_away_from_attachment` drops it.
    """
    LineString, _polygon = _shapely()
    body = _two_wire_body()
    geometry = body_geometry(body)
    # A lead-in that ends on the first wire's start.
    lead_in = LineString([(-3.0, 0.0), (0.0, 0.0)])
    assert lead_in_audit._crosses_away_from_attachment(lead_in, geometry) is False


# -- the sheet edge --------------------------------------------------------

#: The tool radius these are measured against, in mm.
EDGE_MARGIN_MM = 0.6


def _sheet_100():
    LineString, Polygon = _shapely()
    return Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])


def test_the_distance_is_to_the_edge_and_not_to_the_polygon():
    """The bug that made a sheet clearance impossible to express.

    Shapely's `distance` between two geometries of different dimensions is 0
    whenever they intersect, so a point *inside* a polygon measures 0 mm from
    it. `_signed_distance` therefore used to return 0 for every pierce point on
    the material, whether it was 5 mm from the edge or 150 mm -- which is why
    the margin had to be 0, and why "is this pierce too near the edge?" could
    not be asked at all.

    The centre of this sheet is 50 mm from every edge and must measure so.
    """
    _LineString, _polygon = _shapely()
    sheet = _sheet_100()
    assert lead_in_audit._signed_distance((50.0, 50.0), sheet) == \
        pytest.approx(50.0, abs=1e-6)
    assert lead_in_audit._signed_distance((5.0, 50.0), sheet) == \
        pytest.approx(5.0, abs=1e-6)
    # Outside the sheet the sign flips and the magnitude is the real gap.
    assert lead_in_audit._signed_distance((-3.0, 50.0), sheet) == \
        pytest.approx(-3.0, abs=1e-6)


def test_a_pierce_point_off_the_sheet_is_found():
    """The sheet-edge case, which is a different failure from a clash."""
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    # Starts 2 mm outside, runs 5 mm in to the contour at x=5.
    run = LeadRun("lead-in", 0, [], LineString([(-2.0, 50.0), (5.0, 50.0)]))
    found = _off_sheet([run], sheet, EDGE_MARGIN_MM)
    assert len(found) == 1
    assert found[0].distance == pytest.approx(-2.0, abs=1e-6)
    assert "2.0000 mm off the edge" in found[0].describe()


def test_a_pierce_point_on_the_sheet_and_clear_of_the_edge_is_not_found():
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    # 2 mm in, comfortably more than the 0.6 mm margin.
    run = LeadRun("lead-in", 0, [], LineString([(2.0, 50.0), (5.0, 50.0)]))
    assert _off_sheet([run], sheet, EDGE_MARGIN_MM) == []


def test_a_pierce_point_inside_the_margin_is_found_though_it_is_on_the_sheet():
    """The case the clearance exists for.

    The pierce is on the material, so nothing is off the sheet -- but it is
    0.3 mm from the edge with a 0.6 mm tool, so two thirds of the kerf is
    hanging over air. That is not a hole in the material, it is a hole on the
    boundary of one, and it is the thing the operator has to know about before
    posting the job.
    """
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    run = LeadRun("lead-in", 0, [], LineString([(0.3, 50.0), (5.0, 50.0)]))
    found = _off_sheet([run], sheet, EDGE_MARGIN_MM)
    assert len(found) == 1
    # Positive: on the sheet, and said to be.
    assert found[0].distance == pytest.approx(0.3, abs=1e-6)
    assert found[0].off_the_sheet is False
    said = found[0].describe()
    assert "0.3000 mm inside the edge" in said
    assert "0.6000 mm tool radius" in said
    # And it must not claim to be off the sheet, which is a different failure.
    assert "off the edge" not in said


def test_the_margin_is_the_only_thing_separating_clear_from_too_close():
    """One geometry, two margins, opposite verdicts -- the clearance made explicit.

    Pinned because it is the property that a regression to a hardcoded 0 would
    silently undo: the same pierce point is a finding at one tool radius and
    clean at a quarter of it.
    """
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    run = LeadRun("lead-in", 0, [], LineString([(0.3, 50.0), (5.0, 50.0)]))
    assert len(_off_sheet([run], sheet, 0.6)) == 1
    assert _off_sheet([run], sheet, 0.1) == []


def test_a_run_that_skims_the_edge_after_piercing_clear_is_found():
    """Pierces on the material, then the cut runs to within the margin.

    The counterpart of `test_a_run_that_pierces_on_sheet_but_leaves_the_sheet_is_still_found`
    for the clearance rather than the sign: the pierce is fine and the travel is
    not, which is the case a pierce-only test misses.
    """
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    # Out to y=99.8 -- 0.2 mm *inside* the top edge -- and back. (100.2 would be
    # off the sheet, which is the neighbouring test and the opposite finding.)
    run = LeadRun("lead-in", 0, [],
                  LineString([(50.0, 50.0), (50.0, 99.8), (52.0, 50.0)]))
    found = _off_sheet([run], sheet, EDGE_MARGIN_MM)
    assert len(found) == 1
    assert found[0].distance == pytest.approx(0.2, abs=1e-6)
    assert found[0].pierce_off is False
    assert "runs to within 0.2000 mm" in found[0].describe()


def test_the_pierce_point_is_the_end_away_from_the_contour():
    """A lead-out is tested on its far end, not the one on the contour.

    A lead-out starts on the contour it leaves, so that end is on the sheet by
    construction and says nothing. Testing the wrong end would flag every
    lead-out on the sheet.
    """
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    # Starts on the contour at x=5, exits to x=102 -- 2 mm off the far edge.
    run = LeadRun("lead-out", 0, [], LineString([(5.0, 50.0), (102.0, 50.0)]))
    found = _off_sheet([run], sheet, EDGE_MARGIN_MM)
    assert len(found) == 1
    assert found[0].distance == pytest.approx(-2.0, abs=1e-6)


def test_a_run_that_pierces_on_sheet_but_leaves_the_sheet_is_still_found():
    """Pierce on the material, then the cut runs off the edge.

    A real shape for a lead-in that wanders: its pierce point is inside, so the
    pierce test alone would call it clear, and it is cutting air for 4 mm before
    it comes back. The distance reported is the worst point of the run, not the
    pierce point's.
    """
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    run = LeadRun("lead-in", 0, [],
                  LineString([(50.0, 50.0), (50.0, 104.0), (52.0, 50.0)]))
    found = _off_sheet([run], sheet, EDGE_MARGIN_MM)
    assert len(found) == 1
    assert found[0].distance == pytest.approx(-4.0, abs=1e-6)
    # The wording has to say what actually happened. The pierce point is on the
    # sheet, so claiming it pierces off the edge would be wrong.
    assert "runs %.4f mm off the edge" % 4.0 in found[0].describe()
    assert found[0].pierce_off is False


def test_the_four_findings_each_read_as_what_they_are():
    """All four combinations, because each is an instruction to the operator.

    Before the clearance there were only two possible findings and one
    sentence. Printing `-distance` for a positive one -- which is what the old
    `describe` did -- says *"pierces -0.3000 mm off the edge"*, a number going
    the wrong way, for a pierce that is on the material.
    """
    _LineString, _polygon = _shapely()
    run = LeadRun("lead-in", 0, [], None)
    margin = 0.6
    off_pierce = lead_in_audit.OffSheet(run, -2.0, True, margin)
    close_pierce = lead_in_audit.OffSheet(run, 0.3, True, margin)
    off_run = lead_in_audit.OffSheet(run, -4.0, False, margin)
    close_run = lead_in_audit.OffSheet(run, 0.2, False, margin)
    assert "pierces 2.0000 mm off the edge" in off_pierce.describe()
    assert "pierces only 0.3000 mm inside the edge" in close_pierce.describe()
    assert "runs 4.0000 mm off the edge" in off_run.describe()
    assert "runs to within 0.2000 mm" in close_run.describe()
    # Every one of them quotes the margin it was measured against, so a reader
    # can tell "0.3 mm from the edge" from "0.3 mm from the edge, and you have
    # a 0.1 mm torch" without going back to the machine.
    for finding in (close_pierce, close_run):
        assert "0.6000 mm tool radius" in finding.describe()
    # And none of the four says the opposite of the sign it carries.
    assert not off_pierce.describe().startswith("pierces only")
    assert "inside" not in off_pierce.describe()


def test_both_call_sites_pass_the_tools_own_margin_to_the_edge_check():
    """The wiring, checked where the behaviour cannot be reached.

    `_conflicts_after` reads real toolpaths through FreeCAD, so nothing in this
    tier can drive it and a hardcoded `0.0` there is invisible to every other
    check here -- verified: reverting it to `0.0` passes all 80 of them. It
    would also be exactly the bug this work fixed, reintroduced one line away
    from the fix.

    So the call is asserted on the AST rather than on behaviour. That is a
    weaker claim than a behavioural test and is not pretending otherwise: the
    behavioural backstop is
    `tests/freecad_harness/test_lead_in_audit.py` →
    `check_the_sheet_edge_is_checked`, which builds a real pierce point off the
    edge and drives a real search over it.
    """
    import ast
    import inspect
    import textwrap

    def calls(function):
        found = []
        for node in ast.walk(ast.parse(textwrap.dedent(
                inspect.getsource(function)))):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_off_sheet"):
                found.append(node)
        return found

    for function, why in ((lead_in_audit.scan_job, "the read half"),
                          (lead_in_audit._conflicts_after, "the search half")):
        for node in calls(function):
            args = [a for a in node.args if not isinstance(a, ast.Constant)]
            assert len(args) == 3, (
                "%s calls _off_sheet with %d arguments; the margin is required, "
                "and a default is what let the old 0.0 hide there"
                % (why, len(args)))
            assert "margin" in ast.dump(args[-1]), (
                "%s must pass the operation's tool radius as the edge margin, "
                "not a constant" % why)


def test_no_sheet_means_nothing_is_reported_rather_than_everything():
    """A missing outline is not a clean sheet, and `_off_sheet` says so.

    It returns an empty list; the caller distinguishes the two by whether
    `stock_polygon` produced anything, and records the operation as unchecked
    when it did not. That is why this is an empty list and not an exception --
    but also why the audit must never read this as "clear".
    """
    LineString, _polygon = _shapely()
    run = LeadRun("lead-in", 0, [], LineString([(-5.0, 0.0), (1.0, 0.0)]))
    assert _off_sheet([run], None, EDGE_MARGIN_MM) == []


def test_an_operation_with_only_an_off_sheet_finding_is_still_needing_attention():
    """Either kind of finding has to be acted on, and either can be the only one."""
    LineString, _polygon = _shapely()
    sheet = _sheet_100()
    run = LeadRun("lead-in", 0, [], LineString([(-2.0, 50.0), (5.0, 50.0)]))
    audit = _audit(OperationAudit.UNRESOLVED, "no start point cleared it")
    audit.off_sheet = _off_sheet([run], sheet, EDGE_MARGIN_MM)
    assert audit.conflicts == []
    assert audit.needs_attention
    assert audit.worst is None            # no conflict, but not clean
    assert audit.worst_off_sheet is not None
    # And the reason it prints is the sheet edge, not "conflicts found" --
    # a finding that was never made.
    assert "off the edge of the sheet" in audit.describe()
    assert "conflicts found" not in audit.describe()


def test_a_stock_that_cannot_be_read_is_reported_as_not_checked():
    """The quiet-checker failure this file exists to avoid.

    `describe_result` says the sheet edge was not checked when there was no
    outline, so a reader is not left believing a clean result covers the edge.
    """
    result = _result([_audit(OperationAudit.CLEAN)])
    result.sheet = None
    joined = "\n".join(lead_in_audit.describe_result(result, dry_run=True))
    assert "sheet edge was not checked" in joined
