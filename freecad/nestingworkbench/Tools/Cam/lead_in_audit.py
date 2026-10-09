"""Lead-in and lead-out interference between the parts of a replayed nest.

The problem
-----------

A successful nest and a successful CAM replay leave every operation cutting its
own part, and the two are checked independently. `check_tool_clearance` compares
part *outlines* against the tool and warns when they are closer than the cutter
is wide; `verify_replay` checks that each operation has cutting motion and
reaches its targets. Neither looks at where a lead-in *lands*.

A `Path.Dressup.LeadInOut` on operation A extends A's cut path outward by
`RadiusIn`/`RadiusOut` before it starts and after it ends. That extension is
drawn on the nest, among the neighbours the nester placed, and nothing in the
replay accounts for it. When it reaches into the space another operation cuts,
the tool re-cuts material that is not A's -- or, on a plasma or a waterjet,
the pierce happens on a neighbour's contour.

The measurement, on the committed fixture
-----------------------------------------

`replay-fixture-CAM-Nested.FCStd` replays to **110 operations, 110 LeadInOut
dressups, 54 parts** on a 600 x 300 mm sheet, with **142 lead-ins and 28
lead-outs** -- 170 runs.

    21 operations have a lead-in or lead-out reaching another part
    5 of those cross outright, at a distance of exactly 0
    3 operations pierce off the edge of the sheet

The worst clash:

    DressupLeadInOut001_replay_nested_BottomStrap_6  lead-in on wire 0
      -> part_TopStrap_1's cut path
      distance 0.0000 mm

**A detector keyed on a near miss would have missed the five crossings**, and
one keyed only on intersection would have reported 16 of the 21 and called the
rest clean. Both criteria are needed, which is why there are two tests rather
than one threshold: a crossing is a clash however large the margin, and a
near miss is a clash however little it intersects.

    margin 0.0 mm (exact)  ->  5
    margin 0.6 mm (radius) ->  21

Why so many: the lead-ins on this fixture are 3-4.8 mm long, `RadiusIn` being
3.0 and 4.8 mm, and the tightest gap between any two parts' *cut paths* is
2.70 mm. The lead-in is longer than the gap it has to fit into, so this is a
property of the layout rather than of any one operation.

Two geometry bugs were found while measuring this, and both produced findings
that did not exist. They are written up at `body_geometry` and
`_crosses_away_from_attachment`; the short version is that every number above
was wrong before they were fixed, and the fixture changed under them.

What is *not* touched, deliberately
-----------------------------------

**The user's recipe.** `StyleIn`, `StyleOut`, `AngleIn`, `AngleOut`,
`RadiusIn`, `RadiusOut`, `InvertIn`, `InvertOut` and `ExtendIn`/`ExtendOut` are
the user's craft. They are scalars or a handedness, they survive the rigid
motion that `flatten_container` applies, and the replay already carries them
verbatim and correctly. Nothing here changes them -- in particular `InvertIn` is
never flipped, because mirroring a lead-in to the other side of the contour is
a different cut, not a relocated one.

`StartPoint` is the one exception, and the reason is what makes a fix possible
at all: it is the only property in a CAM recipe that is an **absolute
coordinate**. Everything else describes the cut; this names a place on the
part. Under a part the nester has rotated, the source value names somewhere the
part no longer is. It is not a value the user got wrong -- it is the one value
nesting made meaningless, and the only correct one is a function of where the
neighbours ended up, which does not exist until after the nest.

See `geodesic_start_point_candidates` for how a replacement is searched for,
and `NEST-033` for the issue this file answers.
"""
import math

import FreeCAD

# -- what counts as a cut -------------------------------------------------
#
# **A feed move that changes XY. Deliberately not a Z test.**
#
# The first version tracked Z modally and compared against a cut plane. It was
# wrong twice over, and both failures were silent:
#
#   * seeding the modal Z to 0.0, which is what "no Z seen yet" looks like,
#     makes the machine's unknown starting height indistinguishable from the
#     cut plane. The first rapid is then read as a cut-depth move, and every
#     number derived from it -- the lead-in polyline's first point, its length,
#     every clearance measured against it -- is wrong in a way no assertion in
#     this file would catch.
#   * a cut plane is not a single Z. `FinalDepth` 1.5 mm with `StartDepth` 3 mm
#     cuts at several levels, and the `ArcZ`/`LineZ`/`Helix` lead-in styles
#     deliberately start *above* the cut plane and ramp down. A Z test drops
#     exactly the moves that define the lead-in it is looking for.
#
# Name and XY change together answer the question actually being asked: does
# this move remove material where it is going? `G0` never does, and a plunge
# does not change XY, so both fall out without a tolerance to tune.
#
# The one thing this does assume is that linking travel between features is
# emitted as `G0`, which is what the fixture does (`RetractThreshold` 0.0 mm,
# full retract between wires). Were it emitted as a feed move it would still be
# safe here, because a link appears in the base path *and* the dressup path and
# so cancels in the difference that `lead_runs` takes -- but it would then be
# classified by position rather than by what it is. See `lead_runs`.
CUT_MOTIONS = ("G1", "G2", "G3")

#: Points closer together than this are the same point. Tight because these are
#: millimetre-scale coordinates read straight out of a toolpath, not a
#: discretised outline where a looser bound is defensible.
COINCIDENT_MM = 1e-6

#: Chords per arc when an arc is flattened to a polyline.
#:
#: An arc's clearance to a straight edge is underestimated by a chord, and the
#: error is largest on a small arc close to the middle of a long one -- which is
#: the shape a corner lead-in makes. 24 chords over a quarter turn bounds the
#: sagitta at `r * (1 - cos(pi/48))`, which is under 0.001 mm at the 6 mm radii
#: these lead-ins use, three orders below the 0.6 mm margin they are tested
#: against. Sampling less finely to save time would be buying a rounding error
#: against a threshold it cannot affect.
ARC_CHORDS = 24

#: A polyline shorter than this is a point, not a path, and a point's distance
#: to another path is a real number but a meaningless one to report as a clash.
DEGENERATE_MM = 1e-9


# -- toolpaths as geometry ------------------------------------------------

class CutMove:
    """One cut move: where it starts, where it ends, and the path between.

    `points` is the move expanded into a polyline, arcs sampled from their
    `I`/`J` centre. It exists because a move and its geometry have to travel
    together: an earlier version kept them apart and rebuilt geometry from the
    endpoints, which flattened every arc to a chord. That pulls a cut path
    *inward* -- a `G3` semicircle chorded is 5 mm inside the arc at mid-span,
    four times the tool radius -- so a neighbour's cut path reads as further
    away than it is and the audit reports a clean sheet.

    Measured on the committed fixture: the largest arc sag is **14.5239 mm**, on
    the `G3` of `DressupLeadInOut006_replay_nested_SimpleSpacer_2` -- a 52.3926 mm
    sampled path against a 37.8687 mm chord. Chording that pulls the cut path
    14.5 mm *inside* the material, so a neighbour reads as clear when the torch
    is 14.5 mm away from it. There are 659 such arcs on the fixture.

    The direction of the error is what makes it dangerous rather than merely
    wrong: chording never invents a conflict, it only loses them.
    """

    __slots__ = ("name", "x0", "y0", "x1", "y1", "points")

    def __init__(self, name, x0, y0, x1, y1, points):
        self.name = name
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.points = points

    def describe(self):
        return "%s (%s,%s)->(%s,%s)" % (self.name, self.x0, self.y0,
                                       self.x1, self.y1)

    @property
    def length(self):
        """The move's own path length, arcs following their curve."""
        total = 0.0
        for index in range(len(self.points) - 1):
            ax, ay = self.points[index]
            bx, by = self.points[index + 1]
            total += math.hypot(bx - ax, by - ay)
        return total

    def start(self):
        return (self.x0, self.y0)

    def end(self):
        return (self.x1, self.y1)


def cut_moves(path, chords=ARC_CHORDS):
    """Return the `CutMove`s in `path`. Empty when there are none.

    See the note above `CUT_MOTIONS` for why a cut is a feed move that changes
    XY and not a Z test.
    """
    if path is None:
        return []
    out = []
    x = y = 0.0
    for command in getattr(path, "Commands", None) or ():
        name = command.Name
        if name not in CUT_MOTIONS:
            # Still track position: a G0 moves the tool.
            nx = command.Parameters.get("X")
            ny = command.Parameters.get("Y")
            x = float(nx) if nx is not None else x
            y = float(ny) if ny is not None else y
            continue
        params = command.Parameters
        nx = params.get("X")
        ny = params.get("Y")
        nx = float(nx) if nx is not None else x
        ny = float(ny) if ny is not None else y
        moved = abs(nx - x) > COINCIDENT_MM or abs(ny - y) > COINCIDENT_MM
        if moved:
            if name in ("G2", "G3"):
                points = _arc_points(command, x, y, nx, ny, chords)
            else:
                points = [(x, y), (nx, ny)]
            out.append(CutMove(name, x, y, nx, ny, points))
        x, y = nx, ny
    return out


def _dedupe(points):
    """`points` with consecutive duplicates dropped, preserving order."""
    if not points:
        return []
    out = [points[0]]
    for point in points[1:]:
        if math.hypot(point[0] - out[-1][0], point[1] - out[-1][1]) > COINCIDENT_MM:
            out.append(point)
    return out


def moves_to_line(moves):
    """Shapely LineString through a list of `CutMove`s, or None.

    Arcs arrive already sampled in `CutMove.points`, so this only has to join
    them and drop the joins that repeated a point.
    """
    try:
        from shapely.geometry import LineString
    except ImportError:
        return None
    if not moves:
        return None
    points = []
    for move in moves:
        points.extend(move.points)
    points = _dedupe(points)
    if len(points) < 2:
        return None
    try:
        geometry = LineString(points)
    except Exception:
        return None
    if geometry.is_empty or geometry.length <= DEGENERATE_MM:
        return None
    return geometry


def cut_polyline(path, chords=ARC_CHORDS):
    """Return a Shapely LineString of `path`'s cut, or None. See `moves_to_line`."""
    return moves_to_line(cut_moves(path, chords))


def _arc_points(command, x0, y0, x1, y1, chords):
    """Sample a `G2`/`G3` from its `I`/`J` centre. Falls back to the chord."""
    params = command.Parameters
    i = params.get("I")
    j = params.get("J")
    if i is None or j is None:
        return [(x0, y0), (x1, y1)]
    cx, cy = x0 + float(i), y0 + float(j)
    radius = math.hypot(float(i), float(j))
    if radius <= DEGENERATE_MM:
        return [(x0, y0), (x1, y1)]

    start = math.atan2(y0 - cy, x0 - cx)
    end = math.atan2(y1 - cy, x1 - cx)
    # G2 turns clockwise, so `end` must fall *below* `start`; unwrap in that
    # direction rather than taking the short way round, which for a
    # counter-clockwise G3 of more than half a turn is the wrong arc entirely.
    if command.Name == "G2":
        while end >= start:
            end -= 2 * math.pi
    else:
        while end <= start:
            end += 2 * math.pi
    step = (end - start) / chords
    return [(cx + radius * math.cos(start + step * k),
             cy + radius * math.sin(start + step * k))
            for k in range(chords + 1)]


def lead_runs(base_moves, dressup_moves, tolerance=COINCIDENT_MM):
    """Split `dressup_moves` into `(lead_ins, lead_outs, body)`.

    `leadinout.generate` emits, for each closed wire it profiles,
    `lead-in, wire, lead-out`, so the dressup's path is its base's path with
    moves inserted. Walking the two together and calling a dressup move *body*
    when it matches the base's next move recovers the insertion points exactly,
    without having to recognise a lead-in by its shape -- which is the only way
    that works across all thirteen styles, since an `Arc` lead-in and a
    `Perpendicular` one have nothing in common but being extra.

    Two things this has to get right, both measured on the committed fixture:

    * **A zero-length base move is dropped before the walk.** The base Profile
      plunges at its own first point, so its first move has no XY change; the
      dressup instead rapids to the lead-in's entry point and plunges there, so
      the corresponding move has a *different* start. Matching on endpoints
      survives that, matching on starts does not -- it fails on all 98
      operations. Dropping the plunge is what lets the walk line up at all.
    * **A lead-in's endpoint is the wire's first point**, which is also the
      endpoint of a body move. Left alone the walk consumes the lead-in as
      body. The walk takes the *earliest* match, so the lead-in stays extra.

    Returns `(lead_ins, lead_outs, body)`, each a list of lists of `CutMove` --
    one inner list per lead-in or lead-out, and `body` the reconciled cut.
    `(None, None, None)` when the two paths cannot be reconciled, which the
    caller reports rather than guesses at.

    Grouping rather than one entry per move because a lead-in is not
    necessarily one move: `ExtendIn` on an `Arc` style prepends a straight move
    to the arc, and `Helix` ramps down over several. A run reported as three
    separate 1 mm fragments would read as three problems where there is one, and
    each fragment's clearance is not the lead-in's.
    """
    # `move.length > 0`, **not** `> COINCIDENT_MM`. Dropping the base's
    # zero-length plunge is right, but the threshold was doing a second job it
    # is not entitled to: after a start point is moved onto a curve, the base
    # emits a first arc far shorter than 1 um -- measured 0.8265 mm here, and
    # the walk dropped it. Every later move then matched one position early, the
    # reconciliation returned None, and the search rejected all 48 candidates as
    # "could not read" while reporting the operation as unfixable. A move is
    # either zero-length or it is a move; there is no in-between worth dropping.
    body = [move for move in base_moves if move.length > 0.0]
    if not body or len(dressup_moves) < len(body):
        return None, None, None

    # One walk, classifying every dressup move as body or extra as it goes.
    # `last_body_end` is the index just past the final body move, which is where
    # the lead-outs begin: everything extra after it departs from a wire that
    # has already been cut, everything extra before it precedes one.
    index = 0
    last_body_end = 0
    classified = []
    for position, move in enumerate(dressup_moves):
        if index < len(body) and _same_move(move, body[index], tolerance):
            index += 1
            last_body_end = position + 1
            classified.append(False)
        else:
            classified.append(True)
    if index < len(body):
        # The dressup does not contain the base's whole path. Treating the
        # remainder as "extra" would report the user's own cut as a lead-in.
        return None, None, None

    lead_ins = []
    lead_outs = []
    for position, is_extra in enumerate(classified):
        if not is_extra:
            continue
        move = dressup_moves[position]
        target = lead_ins if position < last_body_end else lead_outs
        if target and _contiguous(target[-1][-1], move, tolerance):
            target[-1].append(move)
        else:
            target.append([move])
    return lead_ins, lead_outs, body


def _contiguous(previous, move, tolerance=COINCIDENT_MM):
    """True if `move` starts where `previous` ended."""
    return (math.hypot(previous.x1 - move.x0, previous.y1 - move.y0) <= tolerance)


def _same_move(first, second, tolerance):
    """True if two moves run between the same two points."""
    return (math.hypot(first.x1 - second.x1, first.y1 - second.y1) <= tolerance
            and math.hypot(first.x0 - second.x0, first.y0 - second.y0) <= tolerance)


# -- what is being cut, and by what ---------------------------------------

def part_label_of(operation):
    """The label of the part `operation` cuts, or None.

    Read from `Base`, which after a replay holds exactly one entry per
    operation: `replay_recipe` splits one operation per target part precisely
    so that "which part does this cut" has an answer (see its note on why the
    split was made -- one Profile over 23 parts cost 8.85 s against 0.58 s for
    twenty-three single-part operations).

    Falls back to the label the replay stamped on the flattened part, because a
    failure message that says `CAMPart_677` is useless and `nested_TopStrap_7`
    is not -- the same reasoning as `cam_replay.display_label_of`, which reads
    `SourceObject` and `NestedLabel` off the Model entry.
    """
    base = getattr(operation, "Base", None)
    if not base or not isinstance(base, (list, tuple)):
        return None
    for entry in base:
        try:
            geometry = entry[0]
        except (TypeError, IndexError):
            continue
        if geometry is None:
            continue
        source = getattr(geometry, "SourceObject", None)
        if source is not None and getattr(source, "Label", ""):
            return source.Label
        nested = getattr(geometry, "NestedLabel", "")
        if nested:
            return nested
        return getattr(geometry, "Label", None)
    return None


def tool_radius_of(operation, job=None):
    """The cutting radius of `operation`'s own tool, in mm.

    The operation's own tool, not the job's widest. `check_tool_clearance`
    deliberately reports against the widest tool because a *layout* is unsafe
    for a tool the job merely carries; that reasoning does not transfer here. A
    lead-in is cut by the tool on its own operation, so it is that tool's
    radius that decides whether travelling the lead-in removes a neighbour's
    material. A 0.5 mm tool's lead-in reaching 0.4 mm into a neighbour's cut
    path removes nothing, and warning about it would be the same false alarm
    `check_tool_clearance`'s own docstring warns against.

    Falls back to the job's widest tool when the operation has no readable
    controller, and returns None when neither can be read -- the caller then
    reports without a margin rather than inventing one.
    """
    controller = getattr(operation, "ToolController", None)
    tool = getattr(controller, "Tool", None)
    diameter = getattr(tool, "Diameter", None)
    if diameter is not None:
        try:
            value = float(diameter.Value)
        except Exception:
            value = None
        if value and value > 0:
            return value / 2.0
    if job is not None:
        diameters = _job_tool_diameters(job)
        if diameters:
            return diameters[0] / 2.0
    return None


def _job_tool_diameters(job):
    """Tool diameters carried by `job`, widest first. See `tool_radius_of`."""
    try:
        from . import cam_replay
    except Exception:
        return []
    try:
        return [value for _label, value in cam_replay.tool_diameters(job)]
    except Exception:
        return []


# -- findings -------------------------------------------------------------

#: What an index entry is: an operation's cut path, or one of its lead runs.
#:
#: Module-level rather than attributes of `_CutIndex`, because `Conflict` is
#: written above the index and needs one for its own default argument.
ROLE_BODY = "body"
ROLE_LEAD_IN = "lead-in"
ROLE_LEAD_OUT = "lead-out"

class LeadRun:
    """One lead-in or lead-out as a piece of geometry.

    `kind` is `"lead-in"` or `"lead-out"`. `wire` is the index of the closed
    wire it belongs to within its operation, so a report can say *which* of an
    operation's five holes is the problem rather than only which operation.
    `moves` is the list of `CutMove` the run is made of.

    `run_index` is the run's own position among its operation's runs. It exists
    because runs are themselves indexed (see `_CutIndex`): with lead runs in the
    sheet index, a run has to be able to recognise and skip its own entry rather
    than report itself at a distance of zero.
    """

    __slots__ = ("kind", "wire", "moves", "geometry", "run_index")

    def __init__(self, kind, wire, moves, geometry, run_index=-1):
        self.kind = kind
        self.wire = wire
        self.moves = moves
        self.geometry = geometry
        self.run_index = run_index

    @property
    def length(self):
        return self.geometry.length if self.geometry is not None else 0.0

    def describe(self):
        return "%s on wire %d, %.3f mm long" % (self.kind, self.wire, self.length)


class Conflict:
    """A lead-in or lead-out that reaches another part's cut path.

    `role` is what it reached: `_CutIndex.BODY` for an operation's cut path, or
    the run's kind when it reached another lead-in or lead-out. A report naming
    the part alone would say "comes within 0.0000 mm of TopStrap_14's cut path"
    for a clash with that part's *lead-in*, which points the operator at the
    wrong thing to look at.

    `distance` is 0.0 for a crossing -- they share the point -- and the
    clearance in mm otherwise.
    """

    __slots__ = ("run", "distance", "other_dressup", "other_operation",
                 "other_part", "role")

    def __init__(self, run, distance, other_dressup, other_operation, other_part,
                 role=ROLE_BODY):
        self.run = run
        self.distance = distance
        self.other_dressup = other_dressup
        self.other_operation = other_operation
        self.other_part = other_part
        self.role = role

    @property
    def what(self):
        """What was run into, in words a report can print."""
        if self.role == _CutIndex.BODY:
            return "%s's cut path" % (self.other_part or
                                      getattr(self.other_dressup, "Label", "?"))
        return "%s's %s" % (self.other_part or
                            getattr(self.other_dressup, "Label", "?"), self.role)

    def describe(self, margin):
        if self.distance <= 0.0:
            return "%s crosses %s" % (self.run.describe(), self.what)
        return ("%s comes within %.4f mm of %s (margin %.4f mm)"
                % (self.run.describe(), self.distance, self.what, margin))


class OperationAudit:
    """What the audit found on one operation, and what it did about it."""

    __slots__ = ("dressup", "operation", "part", "margin", "lead_runs",
                 "conflicts", "status", "detail", "body", "off_sheet", "sheet")

    #: `status` values.
    CLEAN = "clean"
    FIXED = "fixed"
    #: Found a start point that clears, but `dry_run` was set so nothing was
    #: applied. **Not** `FIXED`: nothing moved, and a report that said "fixed"
    #: would be claiming a change the document does not have.
    #:
    #: This exists because a report that cannot distinguish "fixable" from
    #: "unfixable" answers the wrong question. The first version of the dry run
    #: found the fix, returned early with it, and let the status fall through to
    #: `UNRESOLVED` -- so report-only on a job where every conflict was fixable
    #: said every one of them needed doing by hand, and offered to mark them all
    #: `CONFLICT_`. Someone reading that would conclude the feature does not work
    #: on their nest.
    FIXABLE = "fixable"
    UNRESOLVED = "unresolved"
    SKIPPED = "skipped"

    def __init__(self, dressup, operation, part, margin, lead_runs, conflicts,
                 status=CLEAN, detail="", body=None, off_sheet=None,
                 sheet=None):
        self.dressup = dressup
        self.operation = operation
        self.part = part
        self.margin = margin
        self.lead_runs = lead_runs
        self.conflicts = conflicts
        self.status = status
        self.detail = detail
        #: `OffSheet`s on this operation's runs. Empty list when clear, `None`
        #: when the check could not run -- the difference matters, because an
        #: empty list says "looked and fine" and None says "not looked at".
        self.off_sheet = off_sheet if off_sheet is not None else None
        #: The sheet outline the off-sheet test ran against. Held on the audit
        #: as well as on the result because `resolve_audit` is driven from an
        #: audit and has to re-run that test per candidate -- it cannot reach the
        #: result, and rebuilding the polygon per candidate would discretise every
        #: wire of the stock shape 48 times for one operation.
        self.sheet = sheet
        #: The operation's reconciled cut moves, carried from `_prepare` so the
        #: search can walk the wire without re-reading the path.
        self.body = body if body is not None else []

    @property
    def label(self):
        return getattr(self.dressup, "Label", "?")

    @property
    def name(self):
        return getattr(self.dressup, "Name", "?")

    @property
    def worst(self):
        """The closest `Conflict`, or None. `None` when the only finding is
        off-sheet -- so a caller must not assume a conflict exists just because
        an operation is `UNRESOLVED`.
        """
        best = None
        for conflict in self.conflicts:
            if best is None or conflict.distance < best.distance:
                best = conflict
        return best

    @property
    def worst_off_sheet(self):
        """The `OffSheet` furthest outside the sheet, or None."""
        if not self.off_sheet:
            return None
        return min(self.off_sheet, key=lambda o: o.distance)

    @property
    def needs_attention(self):
        """Whether anything at all was found, of either kind.

        An operation with no `Conflict` but an `OffSheet` still has to be acted
        on, and moving its start point may well be what fixes it -- the two
        checks are independent and either can be the only one to fire. So this
        is a union, and `scan_job` sets the status from it.
        """
        return bool(self.conflicts) or bool(self.off_sheet)

    def describe(self):
        head = "%s (%s)" % (self.label, self.part or "?")
        if self.status == self.CLEAN:
            return "%s: clear" % head
        if self.status == self.FIXED:
            return "%s: %s. %s" % (head, self.detail,
                                   self.conflicts[0].describe(self.margin))
        if self.status == self.FIXABLE:
            # Reads as the good news it is: a start point exists that clears.
            worst = self.worst
            return "%s: fixable -- %s%s" % (
                head, self.detail,
                (". " + worst.describe(self.margin)) if worst else "")
        if self.status == self.SKIPPED:
            return "%s: SKIPPED. %s" % (head, self.detail)
        worst = self.worst
        # An operation can be UNRESOLVED with no `Conflict` at all, when the
        # only thing wrong is a pierce point off the sheet edge. Saying
        # "conflicts found" there would be describing a finding that was never
        # made, so the off-sheet reason is named instead.
        if worst is not None:
            reason = worst.describe(self.margin)
        elif self.off_sheet:
            reason = self.worst_off_sheet.describe()
        else:
            reason = "conflicts found"
        return "%s: UNRESOLVED. %s%s" % (
            head, reason, (". " + self.detail) if self.detail else "")


class OffSheet:
    """A lead-in or lead-out whose pierce point is not on the material.

    A different failure from a `Conflict`, and kept apart from them on purpose.
    A `Conflict` is a lead-in running into another operation's cut -- two cuts
    in the same place. This is a lead-in running into nothing at all: the sheet
    edge. Nothing is there to cut, so on a plasma or waterjet the pierce is a
    flame-out and the lead-in is a scar on the air.

    `distance` is signed and in mm: **negative** means off the sheet by that
    much, positive that it is inside. The convention is Shapely's, so a caller
    does not have to learn a second one.

    `pierce_off` says *which* end is the problem, and it is carried separately
    from the sign because the sign cannot be used for it. A run whose pierce
    point is off the sheet and a run that pierces on the material and then cuts
    off the edge both report a negative distance, but they are different events:
    one never pierces anything, the other pierces in the right place and then
    wanders off the material. Deriving the wording from the sign would tell the
    second one it pierces off the edge, which is the opposite of the truth.
    """

    __slots__ = ("run", "distance", "pierce_off")

    def __init__(self, run, distance, pierce_off=True):
        self.run = run
        self.distance = distance
        #: Whether the pierce point itself is off the sheet. False means the run
        #: pierces on the material and leaves it afterwards.
        self.pierce_off = pierce_off

    def describe(self):
        if not self.pierce_off:
            return ("%s leaves the sheet by %.4f mm, though it pierces on it"
                    % (self.run.describe(), -self.distance))
        return ("%s pierces %.4f mm off the edge of the sheet"
                % (self.run.describe(), -self.distance))


class AuditResult:
    """Every operation examined, plus what the sheet-level scan cost."""

    __slots__ = ("job", "margin", "audits", "unreadable", "seconds",
                 "cancelled", "sheet")

    def __init__(self, job, margin, audits, unreadable, seconds, sheet=None):
        self.job = job
        self.margin = margin
        self.audits = audits
        self.unreadable = unreadable
        self.seconds = seconds
        #: Whether a search stopped early. Set by `resolve_conflicts`; False
        #: after a scan, and False after a search that ran to the end.
        self.cancelled = False
        #: The sheet outline the off-sheet test ran against, or None when there
        #: was none to read. Kept so a report can say which check did not run
        #: instead of leaving a count silently short.
        self.sheet = sheet

    @property
    def not_reached(self):
        """Operations a cancelled search never looked at."""
        return [a for a in self.audits
                if a.status == OperationAudit.SKIPPED]

    @property
    def offending(self):
        return [a for a in self.audits
                if a.status in (OperationAudit.UNRESOLVED, OperationAudit.FIXED)]

    @property
    def fixed(self):
        return [a for a in self.audits if a.status == OperationAudit.FIXED]

    @property
    def fixable(self):
        """Conflicts a start point was found for, but which were not applied."""
        return [a for a in self.audits if a.status == OperationAudit.FIXABLE]

    @property
    def unresolved(self):
        return [a for a in self.audits if a.status == OperationAudit.UNRESOLVED]

    @property
    def needs_a_human(self):
        """The operations a report should tell the user are theirs to do.

        **Both** `FIXABLE` and `UNRESOLVED` when nothing was applied, because
        from the user's side both mean "this job is not ready to post". The
        difference is who resolves it: `FIXABLE` is one click away,
        `UNRESOLVED` needs the recipe.
        """
        return [a for a in self.audits
                if a.status in (OperationAudit.FIXABLE,
                                OperationAudit.UNRESOLVED)]

    def counts(self):
        out = {OperationAudit.CLEAN: 0, OperationAudit.FIXED: 0,
               OperationAudit.FIXABLE: 0, OperationAudit.UNRESOLVED: 0,
               OperationAudit.SKIPPED: 0}
        for audit in self.audits:
            out[audit.status] = out.get(audit.status, 0) + 1
        return out


# -- the scan -------------------------------------------------------------

def lead_in_out_dressups(job):
    """Every `Path.Dressup.LeadInOut` in `job`, with the operation beneath it.

    Returned as `(dressup, operation)` pairs, walking each entry in
    `Operations.Group` down its dressup stack with `cam_replay.walk_stack`, so
    a LeadInOut sitting on top of another dressup is still found and is still
    paired with the operation that actually cuts.

    The replay's own convention is that the group lists the outermost dressup
    and never the operation beneath it, which is why the walk is needed rather
    than a filter on the group: `Operations.Group` is not a reliable place to
    look for the Profile itself.
    """
    try:
        from . import cam_replay
    except Exception:
        return []
    pairs = []
    seen = set()
    for entry in (getattr(getattr(job, "Operations", None), "Group", None) or ()):
        if id(entry) in seen:
            continue
        operation, dressups = cam_replay.walk_stack(entry)
        for dressup in dressups:
            if id(dressup) in seen:
                continue
            kind, module_name = cam_replay.dressup_kind(dressup)
            if module_name != "Path.Dressup.Gui.LeadInOut":
                continue
            if kind != "known":
                continue
            seen.add(id(dressup))
            pairs.append((dressup, operation))
    return pairs


def _wire_indices(lead_ins, lead_outs):
    """Build a `LeadRun` per segment, numbering each by the wire it belongs to.

    `lead_runs` walks the dressup's path in order, so the Nth lead-in it
    returns precedes the Nth wire the operation cuts and needs no sorting here.
    A lead-out follows a wire rather than preceding one; it belongs to the last
    wire that was started, which is what makes it the last wire's lead-out.

    The number is only ever used to say *which* hole in a report -- "the
    lead-in on wire 2 of 5" rather than an index into nothing -- so it does not
    have to survive a change to how the wires are counted.
    """
    runs = []
    for index, group in enumerate(lead_ins):
        runs.append(LeadRun("lead-in", index, group, moves_to_line(group),
                            run_index=len(runs)))
    if lead_outs:
        last_wire = len(lead_ins) - 1 if lead_ins else 0
        for group in lead_outs:
            runs.append(LeadRun("lead-out", max(0, last_wire), group,
                                moves_to_line(group), run_index=len(runs)))
    return runs


class _CutIndex:
    """Every operation's cut path, and every lead run, in one spatial index.

    One `STRtree` over one geometry per entry -- **280 entries on the committed
    fixture** -- so a lead-in is tested against the sheet rather than against the
    rest one at a time. The whole scan of 110 operations costs about **150 ms**.

    **Entries are bodies *and* lead runs, not bodies alone.** An index of
    bodies only cannot see a lead-in crossing another part's lead-in, because a
    lead-in is not part of anybody's body -- that is the interference the
    original report of this problem was a photograph of, and the index could not
    have found it. The sheet gets one entry per lead-in and per lead-out as
    well, tagged `role`, with `run_index` naming which run of its operation it
    is so a run can skip its own entry.

    The pairwise form would be 280 times the work for the same answer.
    """

    #: `role` values, shared with `Conflict`.
    BODY = ROLE_BODY
    LEAD_IN = ROLE_LEAD_IN
    LEAD_OUT = ROLE_LEAD_OUT

    def __init__(self):
        #: `(part_label, dressup, operation, geometry, role, run_index)`
        self.entries = []
        self._geometries = []
        self._tree = None

    def add(self, part_label, dressup, operation, geometry, role=ROLE_BODY,
            run_index=-1):
        if geometry is None or geometry.is_empty:
            return
        self.entries.append((part_label, dressup, operation, geometry, role,
                             run_index))
        self._geometries.append(geometry)

    def build(self):
        try:
            from shapely.strtree import STRtree
        except ImportError:
            self._tree = None
            return
        if self._geometries:
            self._tree = STRtree(self._geometries)
        else:
            self._tree = None

    def near(self, geometry, margin):
        """Indices of cut paths within `margin` of `geometry`.

        Falls back to a linear scan when Shapely is unavailable or there is
        nothing to index, so the audit degrades to slow rather than to wrong.

        **A margin of 0 asks "what does this touch", and `buffer(0)` cannot ask
        it.** Shapely's zero buffer is the *empty* geometry, not the original
        shape, so querying with it returns nothing at all: the five exact
        crossings on the committed fixture were invisible at a 0 mm margin, and
        the audit reported the sheet clean. At zero the query is the geometry
        itself, which is the right question -- the `STRtree` still filters on
        envelopes, and the exact distance test happens in `conflicts_for`.
        """
        if self._tree is not None:
            try:
                probe = geometry.buffer(margin) if margin > 0.0 else geometry
                return [int(j) for j in self._tree.query(probe)]
            except Exception:
                pass
        return [i for i, g in enumerate(self._geometries)
                if g.distance(geometry) <= margin]


def body_geometry(body, fallback_path=None):
    """Shapely geometry for an operation's reconciled cut, **one part per wire**.

    **Not one `LineString` across the whole body.** A Profile over several wires
    links between them with a `G0`, which `cut_moves` correctly drops -- so the
    moves either side of a wire boundary do not share a point. Concatenating
    them into one polyline therefore *draws the link*: a straight segment across
    the part that the torch never cuts.

    Measured on the 54-part fixture, `DressupLeadInOut_replay_nested_BottomStrap_7`:
    7 body moves, one non-contiguous join at index 4 spanning **34.941 mm**. The
    concatenated `LineString` came out 83.9135 mm long against 48.9727 mm of real
    move length, so **34.9409 mm of it was invented**. A lead-in was then reported
    as crossing its own operation's body at exactly that seam -- 0.000 mm from the
    join point -- which is the join, not a cut.

    That is not a cosmetic error. Every clearance in this file is measured against
    this geometry, so a fabricated segment can invent a cross-part conflict as
    readily as hide one, and the committed harness anchored itself to a distance
    measured this way.

    Returns a `MultiLineString` when there is more than one wire, a `LineString`
    for a single one, and None when nothing usable came out. Every operation here
    -- `distance`, `crosses`, `intersection`, `buffer`, and the `STRtree`
    envelope -- accepts either.
    """
    wires = [moves_to_line(wire) for wire in _body_wires(body)]
    wires = [wire for wire in wires if wire is not None]
    if not wires:
        return cut_polyline(fallback_path) if fallback_path is not None else None
    if len(wires) == 1:
        return wires[0]
    try:
        from shapely.geometry import MultiLineString
    except ImportError:
        # Degrade to the first wire rather than to a joined polyline. A partial
        # answer is wrong but bounded; a fabricated 35 mm link is not.
        return wires[0]
    try:
        return MultiLineString(wires)
    except Exception:
        return wires[0]


def _body_wires(body):
    """The body's wires: consecutive groups of moves that share their endpoints.

    Split on contiguity, which is the same test `_wire_of_run` uses for walking a
    wire. A single wire is one group; a Profile over two wires is two, separated
    by the `G0` link that is not in `body` at all.
    """
    wires = []
    current = []
    for move in body:
        if current and not _contiguous(current[-1], move):
            wires.append(current)
            current = []
        current.append(move)
    if current:
        wires.append(current)
    return wires


def _operation_cut_geometry(body, dressup_path, operation):
    """The path `operation` cuts: its body as the dressup emits it.

    The dressup's body rather than the base Profile's own path, because
    `OffsetIn`/`OffsetOut` can shorten the cut at either end and the Profile's
    unshortened path is then not what the tool follows. `lead_runs` has already
    reconciled the two, so this is a difference of paths the walk proved
    consistent.

    See `body_geometry` for why the result is per-wire rather than joined.
    """
    return body_geometry(body, dressup_path) if body else cut_polyline(dressup_path)


def _attachment_points(geometry):
    """`(start, end)` of a run: where it joins the cut it leads into."""
    try:
        coords = list(geometry.coords)
    except Exception:
        return ()
    if len(coords) < 2:
        return ()
    return ((coords[0][0], coords[0][1]), (coords[-1][0], coords[-1][1]))


def _crosses_away_from_attachment(geometry, other, tolerance=COINCIDENT_MM):
    """Whether `geometry` crosses `other` anywhere *except* at its own attachment.

    A lead-in ends on the contour it leads into, so "does this run cross its own
    operation's cut path" cannot be answered by `crosses()` or by a distance.
    Both say yes to every lead-in on the sheet, because by construction the run
    touches the thing it is attached to.

    The attachment cannot simply be filtered out by Shapely either. Measured on
    the 54-part fixture: the closure of a body wire agrees with its own start to
    **8.039e-14 mm**, and a lead-in's end sits on that start at **exactly 0**.
    So the attachment arrives as a noise-level crossing of two segments that only
    appear to cross -- which is how six own-body crossings were reported, none of
    them visible in the GUI.

    `tolerance` therefore has to sit between two measured numbers: the closure
    noise below it, and above it a clearance of 1e-6 mm, which is a micron and
    no longer any kind of clearance at all. Both bounds are nine orders apart,
    which is the whole reason this can be a constant rather than a judgement.

    An overlap of positive length counts as a crossing. It is not a junction:
    two cuts running along each other is precisely the failure being looked for.
    """
    endpoints = _attachment_points(geometry)
    try:
        intersection = geometry.intersection(other)
    except Exception:
        return False
    if intersection is None or intersection.is_empty:
        return False
    try:
        pieces = list(intersection.geoms)
    except AttributeError:
        pieces = [intersection]
    for piece in pieces:
        if piece.is_empty:
            continue
        if piece.geom_type != "Point":
            return True
        if not endpoints:
            return True
        if all(math.hypot(piece.x - x, piece.y - y) > tolerance
               for x, y in endpoints):
            return True
    return False


def conflicts_for(runs, operation, part_label, index, margin):
    """Return the `Conflict`s in `runs` that reach another part's cut path.

    The one place the test is written, shared by `scan_job` and by the search
    in `resolve_conflicts`. They have to agree exactly: the search accepts a
    candidate because *this* function says it is clear, so a second
    implementation to drift from the first would mean a fix that was accepted
    and never verified.

    **Two kinds of entry, tested two different ways.**

    Another operation's -- body *or* lead run -- is a clearance question and is
    asked as one: within `margin`, in any direction. That covers a crossing
    (distance 0) as readily as a near miss.

    This operation's own entries are a crossing question and are asked as one, by
    `_crosses_away_from_attachment`. A clearance test cannot be used here at all,
    because a run is attached to its own cut path and measures 0.0 mm against it
    by construction -- every lead-in on the sheet would be reported.

    **The same-part filter is gone.** It was there to quieten a Profile and a
    Drilling on one part overlapping, which is not a cross-nest problem. But a
    Drilling is *already gone* by the time a Profile runs over the same region:
    the hole has been cut out, so there is no material left to absorb a lead-in
    crossing it, and the tool crashes instead of plowing. The filter hid exactly
    the failure it was meant to exclude. Measured cost of removing it on the
    54-part fixture: nothing, because these fixtures have no drilling in them.
    """
    conflicts = []
    for run in runs:
        geometry = run.geometry
        if geometry is None or geometry.length <= DEGENERATE_MM:
            continue
        for j in index.near(geometry, margin):
            other_part, other_dressup, other_operation, other_geometry, \
                role, run_index = index.entries[j]
            if role != _CutIndex.BODY and other_operation is operation \
                    and run_index == run.run_index:
                continue        # the run, against itself
            if other_operation is operation:
                if not _crosses_away_from_attachment(geometry, other_geometry):
                    continue
                conflicts.append(Conflict(run, 0.0, other_dressup,
                                          other_operation, other_part, role))
                continue
            try:
                distance = geometry.distance(other_geometry)
            except Exception:
                continue
            if distance <= margin:
                conflicts.append(Conflict(run, distance, other_dressup,
                                          other_operation, other_part, role))
    conflicts.sort(key=lambda c: c.distance)
    return conflicts


def _prepare(job, margin):
    """Read every LeadInOut operation's lead runs and build the sheet index.

    Returns `(prepared, index, unreadable)`. `prepared` is a list of
    `(dressup, operation, part_label, margin, runs, body)`; `body` is carried
    because the search needs the operation's own cut path to walk when it looks
    for a new start point.
    """
    index = _CutIndex()
    prepared = []
    unreadable = []
    for dressup, operation in lead_in_out_dressups(job):
        if operation is None:
            unreadable.append((dressup, "its dressup stack has no operation under it"))
            continue
        base_moves = cut_moves(operation.Path)
        dressup_moves = cut_moves(dressup.Path)
        lead_ins, lead_outs, body = lead_runs(base_moves, dressup_moves)
        if lead_ins is None:
            unreadable.append((dressup, "its lead-ins could not be told apart from "
                                       "its base path, so they were not checked"))
            continue
        runs = _wire_indices(lead_ins, lead_outs)
        if not runs:
            unreadable.append((dressup, "it has no lead-in or lead-out to check"))
            continue
        part_label = part_label_of(operation)
        margin_for_op = margin if margin is not None else tool_radius_of(operation, job)
        if margin_for_op is None:
            unreadable.append((dressup, "no tool radius could be read, so there "
                                       "was no clearance to test against"))
            continue
        geometry = _operation_cut_geometry(body, dressup.Path, operation)
        prepared.append((dressup, operation, part_label, margin_for_op, runs, body))
        index.add(part_label, dressup, operation, geometry)
        for run in runs:
            index.add(part_label, dressup, operation, run.geometry,
                      role=run.kind, run_index=run.run_index)
    index.build()
    return prepared, index, unreadable


def scan_job(job, margin=None, progress=None):
    """Find every lead-in and lead-out that reaches another part's cut path.

    `margin` is the clearance a lead-in must keep, in mm. `None` means each
    operation's own tool radius, falling back to the job's widest tool -- see
    `tool_radius_of` for why the operation's own tool and not the widest.

    Returns an `AuditResult`. **Nothing is modified**: this is the read half,
    and it is safe to run on a job someone is still editing. `resolve_conflicts`
    is the half that writes.

    Operations whose two paths cannot be reconciled are collected in
    `result.unreadable` rather than passed over. They are the operations this
    cannot vouch for, and a silent skip would report a clean sheet because the
    extractor gave up. The same holds when no tool radius can be read: the
    margin has to come from somewhere, and inventing one would test
    interference against a number nobody chose.

    **Not wired into `replay_sheet`**, and that is deliberate. It is a separate
    command run after a replay, for three reasons: the fix writes to the job, so
    it must not be a surprise stage inside a replay someone is watching; it
    needs a report-only mode a pipeline stage cannot ask for; and it can take a
    second on a bad nest -- the scan is 150 ms over 110 operations, but a fix
    recomputes FreeCAD per candidate, and nobody wants that appearing as a
    stalled replay with no explanation. The cost is that a replayed job is not
    checked until someone runs the check.
    """
    import time
    started = time.time()

    prepared, index, unreadable = _prepare(job, margin)

    # Read once per job, not once per operation: the stock is the same polygon
    # for all 110, and building it involves discretising every wire of its shape.
    sheet = stock_polygon(job)
    sheet_checked = sheet is not None

    audits = []
    for dressup, operation, part_label, margin_for_op, runs, body in prepared:
        conflicts = conflicts_for(runs, operation, part_label, index, margin_for_op)
        off = _off_sheet(runs, sheet) if sheet_checked else None
        # A conflict found by the scan and not yet acted on is UNRESOLVED, not
        # CLEAN. Leaving it at the default would let the scan record real
        # conflicts and still report a clean sheet, which is the one thing a
        # checker must not do -- `verify_replay`'s own note is that "an
        # operation that cuts nothing is a failure, not a warning" because a
        # quiet checker is worse than no checker.
        #
        # Either kind of finding sets it, because either one has to be resolved
        # before the job is posted and moving the start point is the fix for
        # both.
        found = bool(conflicts) or bool(off)
        audits.append(OperationAudit(dressup, operation, part_label,
                                     margin_for_op, runs, conflicts,
                                     status=OperationAudit.UNRESOLVED
                                     if found else OperationAudit.CLEAN,
                                     body=body, off_sheet=off, sheet=sheet))

    return AuditResult(job, margin, audits, unreadable, time.time() - started,
                       sheet=sheet)


# -- marking an operation the operator has to see -------------------------

#: Prepended to a dressup's **Label** when its lead-in or lead-out could not be
#: fixed and needs doing by hand.
#:
#: A prefix rather than a suffix because these labels are already long --
#: `DressupLeadInOut001_replay_nested_TopStrap_14` is 44 characters -- and a
#: suffix is the part that scrolls off the end of a narrow tree column.
#:
#: The word is `CONFLICT_` and not `SKIP_` on purpose. The dressup is **not**
#: skipped: it is left exactly as it was and it will still cut, and it will cut
#: through whatever it was cutting through. A label reading `SKIP_` invites an
#: operator to believe the operation has been excluded from the job, which is
#: the belief that turns one bad lead-in into scrapped parts.
CONFLICT_PREFIX = "CONFLICT_"

#: Property recording the conflict, added in the same `"CAM Replay"` group the
#: replay uses for its own bookkeeping (see `PROP_NESTED_LABEL`).
#:
#: The Label says it for the human in the tree; this says it for the program.
#: A label is editable and cosmetic, so a report cannot rely on it -- a user who
#: renames an operation would silently remove it from every future report. It
#: also carries *which* part is in the way, which the label has no room for.
PROP_CONFLICT = "LeadInConflict"


def _strip_conflict(label):
    """`label` without any number of `CONFLICT_` prefixes.

    Idempotent by construction, so running the audit twice on the same job
    cannot produce `CONFLICT_CONFLICT_`.
    """
    while label.startswith(CONFLICT_PREFIX):
        label = label[len(CONFLICT_PREFIX):]
    return label


def mark_conflict(dressup, part, worst=None):
    """Flag `dressup` in the tree as needing a manual fix. Returns the new label.

    Sets `PROP_CONFLICT` to a sentence naming the part in the way, and prefixes
    the Label. Both are applied, and both are idempotent.
    """
    where = ""
    if worst is not None:
        where = (" -- %s comes within %.4f mm of %s's cut path"
                 % (worst.run.describe(), worst.distance,
                    worst.other_part or "?"))
    text = "Needs a manual start point%s" % where
    if not hasattr(dressup, PROP_CONFLICT):
        dressup.addProperty("App::PropertyString", PROP_CONFLICT, "CAM Replay",
                            "Another part's cut path that this lead-in or "
                            "lead-out reaches; the start point needs setting "
                            "by hand")
    setattr(dressup, PROP_CONFLICT, text)
    label = _strip_conflict(getattr(dressup, "Label", "") or "")
    new_label = CONFLICT_PREFIX + label
    if dressup.Label != new_label:
        dressup.Label = new_label
    return new_label


def clear_conflict(dressup):
    """Remove any conflict marking from `dressup`. Returns True if it had one.

    "Had one" means the Label carried the prefix **or** the property was
    non-empty. Not merely "the property exists": `mark_conflict` adds it, and a
    second `clear_conflict` must report no change rather than claiming one
    again -- a caller using the return value to decide whether it needs to
    recompute would otherwise never settle.
    """
    had = False
    label = getattr(dressup, "Label", "") or ""
    if label.startswith(CONFLICT_PREFIX):
        dressup.Label = _strip_conflict(label)
        had = True
    if getattr(dressup, PROP_CONFLICT, ""):
        setattr(dressup, PROP_CONFLICT, "")
        had = True
    return had


# -- searching for a new start point --------------------------------------

#: Spacing between candidate start points along the wire, in mm.
#:
#: 2 mm rather than something finer, and that is a measured choice rather than a
#: round one. On the committed fixture's worst offender, 24 candidates at a
#: 158.6 mm perimeter -- a 6.6 mm step -- found 23 that cleared the margin, the
#: first of them at 8.87 mm of clearance. The step only has to be fine enough
#: that a clear stretch of wire exists between samples; on a contour the
#: candidates are *hints*, not the point itself (`Path.fromShapes` snaps to the
#: nearest point on the wire), so a 2 mm step already lands on the nearest real
#: vertex for every feature in these nests. Halving it would double the work to
#: improve a figure nothing tests against.
CANDIDATE_STEP_MM = 2.0

#: Ceiling on candidates tried per operation in the **first** stage.
#:
#: Bounded because the search recomputes FreeCAD, and the cost is linear: 22 ms
#: a candidate measured on the fixture, so 48 is about a second per operation.
#: A wire is not going to be clear of every neighbour at every offset, and past
#: a certain distance the candidates are no longer "near the user's start
#: point" in any sense they would recognise.
MAX_CANDIDATES = 48

#: Ceiling for the **escalated** second stage, tried only when the first stage
#: found nothing.
#:
#: The first-stage ceiling was a guess about how far a clear start point can be
#: from the operation's own, and on the fixture one real operation disproved it:
#: `BottomStrap_13`'s nearest clear start point sits past the first stage's reach,
#: so it was reported as "needs doing by hand" when a start point existed that
#: clears it (measured: fixable once the ceiling is 384, at 22 ms a candidate).
#:
#: The search is first-fit and returns the moment a candidate clears, so this is
#: paid for **only by operations that fail the first stage** -- the ones that
#: would otherwise be handed back to the user as a hand-job. An operation that
#: succeeds in a few candidates still pays for a few candidates. Measured on the
#: fixture: 9 of 12 conflicts are fixable with the escalation, 3 are not fixable
#: at any budget (genuinely structural -- no start point on the wire clears).
#:
#: It is a second *ceiling*, not a second pass: the walk is one continuous
#: sequence and the first stage is simply its first `MAX_CANDIDATES` steps, so
#: escalating re-tests nothing.
ESCALATED_CANDIDATES = 384


def _wire_of_run(body, wire):
    """The moves of `body` making up wire `wire`, as one list.

    `body` is a flat list of moves across however many closed wires the
    operation profiles, and a wire boundary is simply where consecutive moves
    stop being contiguous -- the link between wires is a `G0`, so the two moves
    either side of it do not share a point.

    Split on that rather than trusting the move count, because the two-wire
    operations on the fixture profile a circle and a rounded rectangle and
    there is no arithmetic that recovers the split from their lengths.
    """
    wires = []
    current = []
    for move in body:
        if current and not _contiguous(current[-1], move):
            wires.append(current)
            current = []
        current.append(move)
    if current:
        wires.append(current)
    if not wires:
        return []
    return wires[min(wire, len(wires) - 1)]


def geodesic_start_point_candidates(body, wire, origin, step=CANDIDATE_STEP_MM,
                                   limit=MAX_CANDIDATES):
    """Yield `(x, y)` candidates spiralling out from `origin` along wire `wire`.

    **Alternating clockwise and anticlockwise outward**, because the point of
    the search is the smallest change that works. Taking candidates in one
    direction only would walk the whole way round a wire before trying the
    other side; alternating means the first candidate accepted is the nearest
    one *in either direction*, which on a contour is very nearly the nearest
    one overall.

    `origin` is the operation's existing start, so the walk begins where the
    user's own setup began and moves away from it in whichever direction clears
    first.

    The candidate is a *hint* and `Path.fromShapes` will snap it to the nearest
    point on the wire, so candidates closer together than the wire's own feature
    spacing produce the same result. They are still yielded, because skipping
    them would need the wire's topology to know they were duplicates, and the
    wasted recompute is 22 ms against a search that has already been bounded.
    """
    moves = _wire_of_run(body, wire)
    geometry = moves_to_line(moves)
    if geometry is None:
        return
    try:
        length = geometry.length
    except Exception:
        return
    if length <= DEGENERATE_MM:
        return

    try:
        here = geometry.project(_point(origin))
    except Exception:
        return

    tried = 0
    distance = step
    while tried < limit:
        for direction in (1.0, -1.0):
            if tried >= limit:
                return
            target = here + direction * distance
            # Wrap, so the walk can pass the far end and come back the other
            # side rather than stopping at the wire's start.
            target = target % length
            try:
                spot = geometry.interpolate(target)
            except Exception:
                continue
            tried += 1
            yield (float(spot.x), float(spot.y))
        distance += step


def _point(xy):
    try:
        from shapely.geometry import Point
    except ImportError:
        return None
    return Point(xy[0], xy[1])


def _origin_of(operation, runs):
    """Where the operation's cut currently begins, or None.

    The start of the first lead-in's wire when there is one, else the start of
    the body. Read from the geometry rather than from `StartPoint`, because
    `UseStartPoint` is `False` on all 110 operations of the fixture and
    `Path.fromShapes` resolves a stale or absent point to the nearest point on
    the wire anyway -- so the toolpath is the only reliable statement of where
    the cut actually begins.
    """
    for run in runs:
        if run.kind == "lead-in" and run.moves:
            first = run.moves[-1]
            return (first.x1, first.y1)
    return None


def _start_point_z(operation):
    """The Z to give a new `StartPoint`, taken from `operation`'s own heights.

    **`ClearanceHeight`, never the transform's Z.** A `StartPoint`'s Z is a
    derived value, not an authored coordinate: `CommandSetStartPoint` writes
    `obj.StartPoint.z = obj.ClearanceHeight.Value`
    (`Path/Op/Gui/Base.py:1716-1719`). Carrying a transformed Z moves a derived
    value into a frame it was never authored in, and it reaches the toolpath as
    a spurious level -- measured on the fixture's `Profile005`, a Z ridden
    rigidly from 5.000 to 6.000 added a level to the operation's `[0, 3, 5]`.
    This is the same rule, and the same reasoning, as
    `cam_replay.carried_geometry_frame_points`.
    """
    clearance = getattr(operation, "ClearanceHeight", None)
    if clearance is not None:
        try:
            return float(clearance.Value)
        except Exception:
            pass
    return 0.0


def _apply_start_point(operation, xy):
    """Point `operation` at `xy` and recompute it and its dressups.

    Sets `UseStartPoint` as well as the point. **This is required, not
    incidental**: with `UseStartPoint` False, `StartPoint` is not read at all.
    Measured on the fixture's worst offender, `StartPoint` set to the part's
    XMinYMin corner and then its XMaxYMax corner produced bit-identical
    toolpaths, and the lead-in did not move. Only `UseStartPoint = True` makes
    the point mean anything.

    Returns True if the operation and its dressups recomputed without error.
    A recompute that raises leaves the paths in an unknown state, so the caller
    treats a failure as "this candidate does not work" rather than as a result.
    """
    try:
        if hasattr(operation, "UseStartPoint"):
            operation.UseStartPoint = True
        operation.StartPoint = FreeCAD.Vector(xy[0], xy[1],
                                             _start_point_z(operation))
    except Exception:
        return False

    try:
        operation.recompute()
    except Exception:
        return False
    return _recompute_dressups(operation)


def _recompute_dressups(operation, depth=0):
    """Recompute every dressup stacked on `operation`. True if all succeeded.

    Walks up the stack rather than down, because this is called with the
    operation and the dressups hang off it. `depth` bounds a malformed file
    that makes the `Base` links circular; `walk_stack` guards the same thing
    going the other way.
    """
    if depth > 16:
        return False
    ok = True
    for dressup in _dressups_on(operation):
        try:
            dressup.recompute()
        except Exception:
            ok = False
            continue
        if not _recompute_dressups(dressup, depth + 1):
            ok = False
    return ok


def _dressups_on(operation):
    """Dressups whose `Base` is `operation`. Scans the job's Operations group.

    There is no back-link from an operation to the dressups above it, so this
    walks the group and matches. The group is the replay's own list and holds
    outermost entries only, which is what makes it a complete place to look from
    the bottom up: every dressup in the job is reachable by following `Base`
    down from something in the group.
    """
    job = _job_of(operation)
    if job is None:
        return []
    out = []
    for entry in (getattr(getattr(job, "Operations", None), "Group", None) or ()):
        if getattr(entry, "Base", None) is operation:
            out.append(entry)
    return out


def _job_of(operation):
    """The job `operation` belongs to, or None.

    A Profile's `Base` is a **list** of `(geometry, subnames)` pairs, not a link
    to another document object, so the "walk up the Base chain" this was first
    written to do cannot work: it reads a list, finds no `Name` on it, and stops
    at the operation itself. Every caller then got None and the search
    recomputed operations without ever recomputing the dressups above them, so
    each candidate was tested against the *previous* candidate's lead-in.

    The reliable direction is down from the group, which is why this scans the
    document's jobs for the one whose `Operations.Group` holds an entry whose
    stack bottoms out at `operation`. The scan is over jobs rather than all
    objects, so it stays cheap on a document with a nest's worth of geometry in
    it.
    """
    document = getattr(operation, "Document", None)
    if document is None:
        return None
    try:
        objects = list(document.Objects)
    except Exception:
        return None
    for candidate in objects:
        if type(getattr(candidate, "Proxy", None)).__name__ != "ObjectJob":
            continue
        for entry in (getattr(getattr(candidate, "Operations", None),
                              "Group", None) or ()):
            if entry is operation:
                return candidate
            base = getattr(entry, "Base", None)
            if base is operation:
                return candidate
    return None


# -- the write half -------------------------------------------------------

def resolve_audit(audit, index, job, step=CANDIDATE_STEP_MM,
                  limit=MAX_CANDIDATES, dry_run=False, progress=None,
                  cancel_check=None, escalate=ESCALATED_CANDIDATES):
    """Move one operation's start point until its lead-in and lead-out both clear.

    **`dry_run` changes nothing** and reports what would have happened. It is
    the default for the command's report mode and the only mode a user should
    run on a job they have not saved.

    **Lead-in and lead-out are tested together, always.** A Profile operation
    carrying both starts at one point and ends at another, and both are placed
    by the same tangent on the same contour -- moving the start moves the
    lead-in, and a wire's end is wherever the wire's traversal finishes. A
    candidate that clears the lead-in is not thereby a candidate that clears
    the lead-out, and accepting it on the lead-in alone would fix the reported
    conflict by creating an unreported one. Both are therefore re-read and
    re-tested after every candidate, and one is accepted only when *every* run
    in the operation is clear.

    First fit, walking outwards and alternating direction from the operation's
    current start -- see `geodesic_start_point_candidates`. On the fixture that
    found a fix on the **first** candidate for the worst offender.

    **`progress(operation_index, operation_total, label, outcome)`** is called
    once per operation, with `outcome` in `None`/`"fixed"`/`"unresolved"` once
    the search has finished. Optional.

    **`cancel_check()`** is polled *between candidates*, not between operations.
    One operation is up to 48 candidates at about 22 ms each -- a full second of
    uninterruptible work, which is about how long a user decides they have had
    enough. Polling per operation would make Cancel feel broken on exactly the
    operations that need it most. Latched once true.

    Cancelling leaves the operation exactly as it was found: `_restore` runs on
    the way out of every path, including this one, so a half-finished search
    cannot leave a start point on its last candidate.

    **`escalate` is the second-stage ceiling** (see `ESCALATED_CANDIDATES`). It
    applies only when the caller left `limit` at its default: a caller that asks
    for a small `limit` is asking for a small search -- that is how the harness
    drives a known-expensive scenario on purpose -- so an explicit smaller
    `limit` is honoured and never escalated. At the default, the walk runs to
    `escalate`, its first `limit` steps being the first stage. Because the search
    is first-fit and returns on the first candidate that clears, the extra reach
    is paid for only by operations the first stage could not fix.

    Returns `(accepted, tried, detail)`.
    """
    operation = audit.operation
    dressup = audit.dressup
    if operation is None:
        return False, 0, "the operation could not be read"

    # The effective per-operation ceiling: the second stage widens the first
    # stage's, and only for a caller that took the default. A smaller explicit
    # `limit` is a deliberate small budget and is left alone.
    budget = limit
    if escalate and limit >= MAX_CANDIDATES and escalate > limit:
        budget = escalate

    # The original state, restored on every failure path so a failed search
    # leaves the operation exactly as it was found. A search that gave up
    # halfway, with `StartPoint` on the last candidate it tried, would be worse
    # than no search: it would have moved the user's cut and not fixed it.
    saved = _snapshot(operation)

    # Conflicts name the wire they sit on. Fixing the lead-in on wire 0 while
    # wire 3 still conflicts leaves the operation half-fixed, so the search
    # starts from the wire that is actually in trouble and, failing that,
    # reports rather than pretending.
    wires = []
    for conflict in audit.conflicts:
        if conflict.run.wire not in wires:
            wires.append(conflict.run.wire)
    if not wires:
        wires = [run.wire for run in audit.lead_runs]

    body = audit.body
    origin = _origin_of(operation, audit.lead_runs)
    if origin is None:
        _restore(operation, saved)
        return False, 0, "the operation's cut does not begin anywhere readable"

    # Latched, so a Cancel click that lands while a widget is being destroyed
    # cannot be un-done by the next poll reading a torn-down object.
    _cancelled = [False]

    def should_cancel():
        if _cancelled[0] or cancel_check is None:
            return _cancelled[0]
        try:
            _cancelled[0] = bool(cancel_check())
        except Exception:
            # A panel that cannot answer is not a cancel. The run carries on
            # unobserved, which is what closing the panel asked for.
            _cancelled[0] = False
        return _cancelled[0]

    def announce(outcome=None):
        """Tell the seam how far through *this* operation's search we are.

        Keyword arguments, and only candidates: the operation index was already
        reported by `resolve_conflicts` when it reached this operation, and the
        view carries it forward. Passing candidates positionally would land them
        in the operation-index parameter and make the bar count operations by
        the number of candidates tried -- which is a real number and a wrong one.
        """
        if progress is not None:
            try:
                progress(candidates=tried, candidate_total=budget,
                         outcome=outcome)
            except Exception:
                # Reporting is never a reason to stop a search. A widget that
                # has been closed mid-run raises, and letting that escape would
                # abandon a fix that was working.
                pass

    tried = 0
    for wire in wires:
        wire_moves = _wire_of_run(body, wire)
        wire_origin = origin
        if wire_moves and origin not in [(m.x0, m.y0) for m in wire_moves]:
            # The offending wire is not the one the cut starts on. Anchor on
            # this wire's own start so the walk begins on the wire being fixed
            # rather than at a point that is not on it.
            wire_origin = (wire_moves[0].x0, wire_moves[0].y0)
        for xy in geodesic_start_point_candidates(body, wire, wire_origin,
                                                  step=step, limit=budget - tried):
            if should_cancel():
                # Restored, not left on the last candidate tried. A search
                # stopped halfway with `StartPoint` moved is worse than no
                # search: it moved the user's cut and did not fix it.
                _restore(operation, saved)
                audit.status = OperationAudit.SKIPPED
                audit.detail = ("the search was cancelled after %d candidate%s"
                                % (tried, "" if tried == 1 else "s"))
                announce(OperationAudit.SKIPPED)
                return False, tried, audit.detail
            tried += 1
            announce()
            if not _apply_start_point(operation, xy):
                continue
            remaining = _conflicts_after(operation, dressup, audit, index)
            if remaining is None:
                continue
            if not remaining:
                # Re-read the sheet finding from what was actually applied. The
                # scan's copy was measured at the *original* start point, so
                # carrying it forward would report a candidate as fixed while
                # leaving the report claiming the pierce point is still off the
                # edge.
                audit.off_sheet = [] if audit.sheet is not None \
                    else audit.off_sheet
                audit.conflicts = []
                if dry_run:
                    _restore(operation, saved)
                    audit.status = OperationAudit.FIXABLE
                    audit.detail = ("a start point at (%.3f, %.3f) clears it, "
                                    "found after %d candidate%s"
                                    % (xy[0], xy[1], tried,
                                       "" if tried == 1 else "s"))
                    announce(OperationAudit.FIXABLE)
                    return True, tried, audit.detail
                clear_conflict(dressup)
                audit.conflicts = []
                audit.status = OperationAudit.FIXED
                audit.detail = ("moved the start point to (%.3f, %.3f) after "
                                "%d candidate%s" % (xy[0], xy[1], tried,
                                                    "" if tried == 1 else "s"))
                announce(OperationAudit.FIXED)
                return True, tried, audit.detail
        if tried >= budget:
            break

    _restore(operation, saved)
    if not dry_run:
        # **Not in a dry run.** `mark_conflict` writes a Label and a property,
        # and "report-only writes nothing" has to be literally true -- otherwise
        # running the report to find out whether there is anything here leaves
        # marks on every operation it could not fix.
        #
        # The harness caught this only after the `FIXABLE` status was added, and
        # by then for a different reason: `check_dry_run_writes_nothing` passed
        # because the fixture's one conflict *was* fixable, so this line was
        # never reached. It now also asserts the marking happens on the real run.
        mark_conflict(dressup, audit.part, audit.worst)
    audit.status = OperationAudit.UNRESOLVED
    audit.detail = ("no start point on wire%s %s cleared every lead-in and "
                    "lead-out within %.4f mm after %d candidate%s; it %s"
                    % ("s" if len(wires) != 1 else "",
                       ", ".join(str(w) for w in wires), audit.margin, tried,
                       "" if tried == 1 else "s",
                       "is marked %s and was left as it was" % CONFLICT_PREFIX
                       if not dry_run else "was left as it was"))
    announce(OperationAudit.UNRESOLVED)
    return False, tried, audit.detail


#: Sentinel standing in for "still off the sheet" in the list `_conflicts_after`
#: returns, so the search's one `if not remaining:` covers both kinds of thing
#: that must be cleared.
#:
#: A sentinel rather than a synthesised `Conflict` because the two are different
#: failures: a `Conflict` names another part's cut path and a real one of those
#: would be a lie in a report. This one is only ever counted, never described --
#: the operation's own `audit.off_sheet` is the authoritative record, and a
#: candidate that gets this far has already failed the test that set it.
_OFF_SHEET_MARKER = object()


def _conflicts_after(operation, dressup, audit, index):
    """Conflicts after a candidate was applied, or None if it could not be read.

    Re-reads the operation and the dressup rather than trusting anything
    cached, because the whole question is whether the paths changed. `None` and
    `[]` are different answers: the first is "this candidate produced something
    unreadable, do not accept it", the second is "this candidate worked".
    """
    lead_ins, lead_outs, _body = lead_runs(cut_moves(operation.Path),
                                           cut_moves(dressup.Path))
    if lead_ins is None:
        return None
    runs = _wire_indices(lead_ins, lead_outs)
    if not runs:
        return None
    conflicts = conflicts_for(runs, operation, audit.part, index, audit.margin)
    # **The sheet edge is part of "clear".** A candidate that leaves every
    # neighbour alone but still pierces off the stock has not fixed the thing
    # the user came here to fix, so it is not a fix. Checking only
    # `conflicts_for` here would have made the search report success on a job
    # whose lead-ins still run off the material -- and it would have been
    # reported as `CONFLICT_`-free, so nothing would ever look at it again.
    #
    # `audit.sheet` is None when the stock could not be read, and then
    # `audit.off_sheet` is None too: the check did not run, and inventing a
    # verdict for it would be the quiet-checker failure this file exists to
    # avoid.
    if audit.sheet is not None and _off_sheet(runs, audit.sheet):
        return conflicts if conflicts else [_OFF_SHEET_MARKER]
    return conflicts


def _snapshot(operation):
    """`(use_start_point, start_point)` as they were."""
    use = getattr(operation, "UseStartPoint", None)
    point = getattr(operation, "StartPoint", None)
    return (use, FreeCAD.Vector(point) if point is not None else None)


def _restore(operation, saved):
    """Put back what `_snapshot` took."""
    use, point = saved
    try:
        if use is not None and hasattr(operation, "UseStartPoint"):
            operation.UseStartPoint = use
        if point is not None:
            operation.StartPoint = point
    except Exception:
        return
    try:
        operation.recompute()
    except Exception:
        return
    _recompute_dressups(operation)


def resolve_conflicts(result, step=CANDIDATE_STEP_MM, limit=MAX_CANDIDATES,
                      dry_run=False, progress=None, cancel_check=None):
    """Try to fix every conflict `scan_job` found. Returns `(result, seconds)`.

    `result` is updated in place and also returned, so the caller can read one
    object after the run. `result.cancelled` says whether it stopped early.

    **`dry_run` writes nothing at all** -- no property, no label, no
    recompute that sticks. It reports what would have been done.

    Operations that cannot be fixed are marked `CONFLICT_` and left alone, per
    the decision that this is the user's call to make by hand: the recipe is
    theirs, and a lead-in that cannot be moved without breaking its own lead-out
    is a question about the cut, not about the nest.

    **`progress(operation_index, operation_total, label, outcome)`** is called
    once per operation as it is *reached*, and `progress` is passed down to
    `resolve_audit` for the per-candidate detail. Optional throughout, so the
    harness and every existing caller are unaffected.

    **`cancel_check()`** stops the run between operations. Cancelling keeps
    everything already fixed: each fix is applied and verified on its own, so
    stopping between them leaves the job consistent, with the operations already
    moved moved and the rest untouched. The report says which is which.
    """
    import time
    started = time.time()
    result.cancelled = False

    prepared, index, unreadable = _prepare(result.job, result.margin)
    by_name = {}
    for entry in prepared:
        by_name[getattr(entry[0], "Name", None)] = entry

    todo = [a for a in result.audits if a.status == OperationAudit.UNRESOLVED]
    total = len(todo)
    for position, audit in enumerate(todo, 1):
        if _cancelled_already(cancel_check):
            result.cancelled = True
            # The ones not reached stay UNRESOLVED, which is what they are:
            # nobody has looked. They are reported, and not marked, because
            # "could not be fixed" and "was never tried" are different claims.
            audit.status = OperationAudit.SKIPPED
            audit.detail = ("not reached; the search was cancelled after %d of "
                            "%d operation(s)" % (position - 1, total))
            break
        if progress is not None:
            try:
                progress(operation_index=position, operation_total=total,
                         label=audit.label)
            except Exception:
                # Same as in `resolve_audit`: a progress seam that cannot be
                # drawn into must not stop the search.
                pass
        entry = by_name.get(getattr(audit.dressup, "Name", None))
        if entry is None:
            # The operation was not in this pass's index, so its neighbours are
            # unknown and any candidate would be accepted on no evidence.
            audit.status = OperationAudit.SKIPPED
            audit.detail = ("it could not be re-read, so no start point was "
                            "searched for")
            continue
        _dressup, _operation, _part, _margin, _runs, body = entry
        audit.body = body
        resolve_audit(audit, index, result.job, step=step, limit=limit,
                      dry_run=dry_run, progress=progress,
                      cancel_check=cancel_check)

    result.unreadable = unreadable
    result.seconds = time.time() - started
    return result


def _cancelled_already(cancel_check):
    """Whether `cancel_check` says stop. Never raises.

    A panel that cannot answer is not a cancel. Read defensively because it is
    polled between operations while Qt may be tearing the panel down, and a
    `ReferenceError` escaping here would abandon a run that was working.
    """
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        return False


# -- the sheet edge --------------------------------------------------------

#: How far outside the stock a pierce point may be and still count as on the
#: sheet, in mm. Zero by default: a pierce point is a hole, and a hole whose
#: centre is off the material is not a hole in the material.
SHEET_MARGIN_MM = 0.0


def stock_polygon(job):
    """The sheet's outline as a Shapely polygon, or None.

    Read from the job's own `Stock` rather than from the layout's part extents.
    The difference is the whole point: the part extents say where the parts are,
    and a lead-in pierces in the gap *between* parts, which by construction lies
    outside every part and inside the sheet.

    **Shapely is required, and its absence is reported rather than swallowed.**
    Every other clearance in this file degrades to a weaker answer when Shapely
    is missing. This one cannot: the question is whether a point is inside a
    polygon, and there is no cheaper honest way to ask it. A caller gets None
    and records the operation as unchecked, which is the difference between "no
    problem found" and "nothing was looked at".
    """
    stock = getattr(job, "Stock", None)
    if stock is None:
        return None
    try:
        import shapely
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except ImportError:
        return None
    shape = getattr(stock, "Shape", None)
    if shape is None:
        return None
    try:
        if shape.isNull():
            return None
    except Exception:
        return None

    faces = []
    for wire in shape.Wires if hasattr(shape, "Wires") else []:
        try:
            points = [(v.X, v.Y) for v in wire.discretize(Deflection=0.05)]
        except Exception:
            try:
                points = [(v.Point.x, v.Point.y)
                          for v in wire.OrderedVertexes]
            except Exception:
                continue
        if len(points) < 3:
            continue
        try:
            face = Polygon(points).buffer(0)
        except Exception:
            continue
        if not face.is_empty:
            faces.append(face)
    if not faces:
        return None
    try:
        return unary_union(faces)
    except Exception:
        return faces[0]


def _off_sheet(runs, sheet, margin=SHEET_MARGIN_MM):
    """The `OffSheet`s among `runs` -- those reaching past the sheet edge.

    An `OffSheet`'s `distance` is signed and in mm: negative off the sheet,
    positive inside, Shapely's own convention.

    **The pierce point is the run's free end, the one away from the contour.**
    A lead-in's other end is on the contour it leads into, so it is on the sheet
    by construction and says nothing. For a lead-out the free end is the second
    one. It is the free end that has to be on material: that is where the torch
    pierces, and a pierce outside the stock is air.

    Also reported is the *whole* run, not just its end, when any part of it
    falls outside. A lead-in that starts on the sheet and runs off it mid-way
    has the same failure -- the torch is cutting air -- with a different cause,
    and the two numbers are worth having.
    """
    if sheet is None:
        return []
    found = []
    for run in runs:
        geometry = run.geometry
        if geometry is None or geometry.length <= DEGENERATE_MM:
            continue
        try:
            coords = list(geometry.coords)
        except Exception:
            continue
        if len(coords) < 2:
            continue
        lead_in = run.kind == _CutIndex.LEAD_IN
        # Away from the contour: the start for a lead-in, the end for a lead-out.
        pierce = (coords[0][0], coords[0][1]) if lead_in \
            else (coords[-1][0], coords[-1][1])
        try:
            from shapely.geometry import Point
            distance = Point(pierce[0], pierce[1]).distance(sheet)
            inside = sheet.contains(Point(pierce[0], pierce[1]))
        except Exception:
            continue
        signed = distance if inside else -distance
        if signed < margin:
            found.append(OffSheet(run, signed))
            continue
        # The pierce point is on the sheet. Is any of the run off it? A lead-in
        # that pierces on the sheet and then runs off it is cutting air just the
        # same, and reports the same failure.
        worst = None
        for xy in coords:
            here = _signed_distance(xy, sheet)
            if here is not None and (worst is None or here < worst):
                worst = here
        if worst is not None and worst < margin:
            found.append(OffSheet(run, worst, pierce_off=False))
    return found


def _signed_distance(xy, sheet):
    """Distance from `xy` to the sheet edge: positive inside, negative outside."""
    try:
        from shapely.geometry import Point
        point = Point(xy[0], xy[1])
        distance = point.distance(sheet)
        return distance if sheet.contains(point) else -distance
    except Exception:
        return None


def describe_result(result, limit=20, dry_run=False):
    """Lines describing an `AuditResult`. The first is a summary.

    Written to be read in a FreeCAD report view, so the summary carries the
    counts and the numbers, and each offending operation gets one line naming
    the part, the wire and the distance. `limit` bounds the per-operation lines
    because a sheet with a bad nest can have forty of them and the summary has
    already said how many.

    **`dry_run` changes the wording, and it has to.** A report that applied
    nothing must not talk about operations being "left for you to fix" as though
    a search had failed, because the search usually succeeds -- and the first
    version of this summary did exactly that: report-only on a job where every
    conflict was fixable reported all of them as unfixable, because the verdict
    the search had produced was thrown away and only `UNRESOLVED` survived. See
    `OperationAudit.FIXABLE`.
    """
    counts = result.counts()
    lines = []
    total = len(result.audits)
    clear = counts.get(OperationAudit.CLEAN, 0)
    skipped = counts.get(OperationAudit.SKIPPED, 0)

    if dry_run:
        fixable = counts.get(OperationAudit.FIXABLE, 0)
        unresolved = counts.get(OperationAudit.UNRESOLVED, 0)
        headline = ("Checked %d lead-in/lead-out operation%s in %.2f s: "
                    "%d clear, %d could be moved to a clear start point, "
                    "%d need doing by hand, %d skipped."
                    % (total, "" if total == 1 else "s", result.seconds,
                       clear, fixable, unresolved, skipped))
    else:
        headline = ("Checked %d lead-in/lead-out operation%s in %.2f s: "
                    "%d clear, %d moved to a clear start point, "
                    "%d need doing by hand, %d skipped."
                    % (total, "" if total == 1 else "s", result.seconds,
                       clear, counts.get(OperationAudit.FIXED, 0),
                       counts.get(OperationAudit.UNRESOLVED, 0), skipped))
    lines.append(headline)

    fixable = result.fixable
    if fixable:
        lines.append("")
        if dry_run:
            lines.append(
                "These reach another part's cut path, and a start point was "
                "found that clears them. Nothing has been changed:"
            )
        else:
            lines.append("Moved to a clear start point:")
        for audit in fixable[:limit]:
            lines.append("  %s (%s) -- %s" % (audit.label, audit.part or "?",
                                             audit.detail))
        if len(fixable) > limit:
            lines.append("  ... and %d more" % (len(fixable) - limit))

    fixed = result.fixed
    if fixed:
        lines.append("")
        lines.append("Moved to a clear start point:")
        for audit in fixed[:limit]:
            lines.append("  %s (%s) -- %s" % (audit.label, audit.part or "?",
                                             audit.detail))
        if len(fixed) > limit:
            lines.append("  ... and %d more" % (len(fixed) - limit))

    off_sheet = [a for a in result.audits if a.off_sheet]
    if off_sheet:
        lines.append("")
        lines.append("These pierce off the edge of the sheet, where there is no "
                     "material to pierce into:")
        for audit in off_sheet[:limit]:
            worst = audit.worst_off_sheet
            lines.append("  %s (%s) -- %s"
                         % (audit.label, audit.part or "?", worst.describe()))
        if len(off_sheet) > limit:
            lines.append("  ... and %d more" % (len(off_sheet) - limit))
    elif result.sheet is None:
        lines.append("")
        lines.append("The sheet edge was not checked: no stock outline could be "
                     "read from this job, so a pierce point off the material "
                     "would not have been reported.")

    unresolved = result.unresolved
    if unresolved:
        lines.append("")
        if dry_run:
            lines.append(
                "No start point on these clears every lead-in and lead-out, so "
                "they need your judgement about the cut. Nothing has been "
                "changed:"
            )
        else:
            lines.append(
                "No start point on these clears every lead-in and lead-out. "
                "Each is marked %s in the tree and its LeadInConflict property "
                "says which part; they were left exactly as they were:"
                % CONFLICT_PREFIX)
        for audit in unresolved[:limit]:
            lines.append("  %s" % audit.describe())
        if len(unresolved) > limit:
            lines.append("  ... and %d more" % (len(unresolved) - limit))

    if result.unreadable:
        lines.append("")
        lines.append("Could not be checked:")
        for dressup, reason in result.unreadable[:limit]:
            lines.append("  %s: %s" % (getattr(dressup, "Label", "?"), reason))

    not_reached = result.not_reached
    if not_reached:
        lines.append("")
        lines.append(
            "Cancelled. %d operation%s not looked at -- nobody has said whether "
            "they conflict, so they are not marked. Run this again to check "
            "them."
            % (len(not_reached), "" if len(not_reached) == 1 else "s"))

    if dry_run and (fixable or unresolved):
        lines.append("")
        lines.append("Nothing was changed. Run this again and choose "
                     "'Report and fix' to move the %d start point%s that can "
                     "be moved."
                     % (len(fixable), "" if len(fixable) == 1 else "s"))

    return lines
