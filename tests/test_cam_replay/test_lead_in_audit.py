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
    """Malformed input degrades to something, rather than raising."""
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


def _conflict(distance=0.0598, part="part_TopStrap_7"):
    return Conflict(LeadRun("lead-in", 0, [], None), distance, None, None, part)


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

def test_a_pierce_point_off_the_sheet_is_found():
    """The sheet-edge case, which is a different failure from a clash."""
    LineString, Polygon = _shapely()
    sheet = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    # Starts 2 mm outside, runs 5 mm in to the contour at x=5.
    run = LeadRun("lead-in", 0, [], LineString([(-2.0, 50.0), (5.0, 50.0)]))
    found = _off_sheet([run], sheet)
    assert len(found) == 1
    assert found[0].distance == pytest.approx(-2.0, abs=1e-6)
    assert "2.0000 mm off the edge" in found[0].describe()


def test_a_pierce_point_on_the_sheet_is_not_found():
    LineString, Polygon = _shapely()
    sheet = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    run = LeadRun("lead-in", 0, [], LineString([(2.0, 50.0), (5.0, 50.0)]))
    assert _off_sheet([run], sheet) == []


def test_the_pierce_point_is_the_end_away_from_the_contour():
    """A lead-out is tested on its far end, not the one on the contour.

    A lead-out starts on the contour it leaves, so that end is on the sheet by
    construction and says nothing. Testing the wrong end would flag every
    lead-out on the sheet.
    """
    LineString, Polygon = _shapely()
    sheet = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    # Starts on the contour at x=5, exits to x=102 -- 2 mm off the far edge.
    run = LeadRun("lead-out", 0, [], LineString([(5.0, 50.0), (102.0, 50.0)]))
    found = _off_sheet([run], sheet)
    assert len(found) == 1
    assert found[0].distance == pytest.approx(-2.0, abs=1e-6)


def test_a_run_that_pierces_on_sheet_but_leaves_the_sheet_is_still_found():
    """Pierce on the material, then the cut runs off the edge.

    A real shape for a lead-in that wanders: its pierce point is inside, so the
    pierce test alone would call it clear, and it is cutting air for 4 mm before
    it comes back. The distance reported is the worst point of the run, not the
    pierce point's.
    """
    LineString, Polygon = _shapely()
    sheet = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    run = LeadRun("lead-in", 0, [],
                  LineString([(50.0, 50.0), (50.0, 104.0), (52.0, 50.0)]))
    found = _off_sheet([run], sheet)
    assert len(found) == 1
    assert found[0].distance == pytest.approx(-4.0, abs=1e-6)
    # The wording has to say what actually happened. The pierce point is on the
    # sheet, so claiming it pierces off the edge would be wrong.
    assert "leaves the sheet" in found[0].describe()
    assert "pierces %.4f mm off" not in found[0].describe()
    assert found[0].pierce_off is False


def test_no_sheet_means_nothing_is_reported_rather_than_everything():
    """A missing outline is not a clean sheet, and `_off_sheet` says so.

    It returns an empty list; the caller distinguishes the two by whether
    `stock_polygon` produced anything, and records the operation as unchecked
    when it did not. That is why this is an empty list and not an exception --
    but also why the audit must never read this as "clear".
    """
    LineString, _polygon = _shapely()
    run = LeadRun("lead-in", 0, [], LineString([(-5.0, 0.0), (1.0, 0.0)]))
    assert _off_sheet([run], None) == []


def test_an_operation_with_only_an_off_sheet_finding_is_still_needing_attention():
    """Either kind of finding has to be acted on, and either can be the only one."""
    LineString, Polygon = _shapely()
    sheet = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    run = LeadRun("lead-in", 0, [], LineString([(-2.0, 50.0), (5.0, 50.0)]))
    audit = _audit(OperationAudit.UNRESOLVED, "no start point cleared it")
    audit.off_sheet = _off_sheet([run], sheet)
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
