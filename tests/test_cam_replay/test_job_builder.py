"""Tests for building the new CAM job for a nested sheet.

Background
----------
Three things have to be right when the job is built, and two of them fail
quietly.

**The job clones its geometry.** `PathJob.Create` does not keep the objects it
is handed. Three input features came back as `['Clone', 'Clone001',
'Clone002']` labelled `Model-p1` .. `Model-p3`, and `Model.Group[0] is not
parts[0]`. An operation's `Base` must therefore point at the clones. A builder
that handed the caller back its own input list would be a quiet way to produce
a job whose operations reference geometry the job does not contain -- the
symptom being toolpaths in the wrong place, or none at all.

**The stock has to be the sheet.** The default stock is a `StockFromBase`
fitted to the model, which measured Z -7.0 .. 1.0 for a 6 mm part: a
millimetre of margin above and below. The nesting job needs the stock to be the
sheet exactly, spanning -thickness to 0, so Z0 is the top of the stock.
Operation depths are derived from the stock on every recompute, so this is the
one place the Z frame is decided.

**Sheets are laid out side by side.** Sheet 2's parts sit at a non-zero world
X, so each sheet's parts are moved back to local coordinates and the stock is
built at the origin. Otherwise sheet 2's G-code would start 600 mm off and the
operator would have to re-zero by hand.

The Z frame comparison is a warning, not an error, and the reason is worth
stating because it is easy to get backwards. Depths are re-derived from the new
stock, so the cut is correct regardless of what the source job used. What
differs is the numbers the *user* saw while setting up. That is worth telling
them, and is not worth refusing to run.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    STOCK_FRAME_TOLERANCE,
    ReplayJob,
    compare_stock_frames,
    describe_replay_job,
    describe_stock_frame,
    read_sheet_dimensions,
    replay_job_name,
    sheet_origin_for,
    translate_to_sheet_local,
)


# -- stand-ins ------------------------------------------------------------

class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z


class _Rotation:
    pass


class _Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base or _Vector()
        self.Rotation = rotation or _Rotation()


class _Box:
    def __init__(self, zmin, zmax):
        self.ZMin, self.ZMax = zmin, zmax
        self.ZLength = zmax - zmin


class _Shape:
    def __init__(self, box=None, null=False):
        self._box = box
        self._null = null
        self.BoundBox = box

    def isNull(self):  # noqa: N802
        return self._null


class _Stock:
    """A stock stand-in.

    With no `zmin`, the stock has *no shape at all* rather than a shape with a
    null bounding box. That is the realistic shape of "the stock has not been
    built yet", and FreeCAD's BoundBox is never None once a shape exists.
    """

    def __init__(self, zmin=None, zmax=None, null=False, label="Stock"):
        self.Label = label
        self.Shape = None if zmin is None else _Shape(_Box(zmin, zmax), null)
        self.Length = self.Width = self.Height = None


class _Part:
    def __init__(self, name, base=None):
        self.Name = name
        self.Label = name
        self.Placement = _Placement(base or _Vector())


class _Layout:
    def __init__(self, **props):
        for key, value in props.items():
            setattr(self, key, value)


class _SheetGroup:
    def __init__(self, label="Sheet_1", children=()):
        self.Label = label
        self.Group = list(children)


class _Boundary:
    def __init__(self, x, y):
        self.Label = "Sheet_Boundary_1"
        self.Placement = _Placement(_Vector(x, y, 0))


class _Obj:
    def __init__(self, label):
        self.Label = label


def _monkeypatch(monkeypatch):
    fake = type("_F", (), {"Placement": _Placement, "Vector": _Vector})()
    monkeypatch.setattr(cam_replay, "FreeCAD", fake)


# -- dimensions -----------------------------------------------------------

class TestReadSheetDimensions:
    def test_reads_the_layout_properties(self):
        layout = _Layout(SheetWidth=600.0, SheetHeight=400.0, SheetThickness=3.0)
        assert read_sheet_dimensions(layout) == (600.0, 400.0, 3.0)

    def test_falls_back_when_a_property_is_missing(self):
        assert read_sheet_dimensions(_Layout(SheetWidth=500.0)) == (500.0, 600.0, 3.0)

    def test_falls_back_for_a_non_numeric_value(self):
        layout = _Layout(SheetWidth="not a number")
        assert read_sheet_dimensions(layout)[0] == 600.0

    def test_falls_back_for_no_layout_at_all(self):
        assert read_sheet_dimensions(None) == (600.0, 600.0, 3.0)

    def test_order_is_width_height_thickness(self):
        # Easy to transpose, and the stock's own names are Length/Width/Height,
        # so a mix-up would not raise anywhere.
        layout = _Layout(SheetWidth=1.0, SheetHeight=2.0, SheetThickness=3.0)
        assert read_sheet_dimensions(layout) == (1.0, 2.0, 3.0)


# -- sheet origin ---------------------------------------------------------

class TestSheetOrigin:
    def test_reads_the_boundary_placement(self, monkeypatch):
        _monkeypatch(monkeypatch)
        origin = sheet_origin_for(_SheetGroup(children=[_Boundary(600, 0)]))
        assert (origin.x, origin.y, origin.z) == (600.0, 0.0, 0.0)

    def test_zero_for_a_sheet_with_no_boundary(self, monkeypatch):
        _monkeypatch(monkeypatch)
        origin = sheet_origin_for(_SheetGroup())
        assert (origin.x, origin.y, origin.z) == (0.0, 0.0, 0.0)

    def test_zero_for_none(self, monkeypatch):
        _monkeypatch(monkeypatch)
        origin = sheet_origin_for(None)
        assert (origin.x, origin.y, origin.z) == (0.0, 0.0, 0.0)

    def test_ignores_a_boundary_with_no_placement(self, monkeypatch):
        _monkeypatch(monkeypatch)
        child = _Boundary(10, 10)
        del child.Placement
        origin = sheet_origin_for(_SheetGroup(children=[child]))
        assert (origin.x, origin.y, origin.z) == (0.0, 0.0, 0.0)

    def test_ignores_a_sheet_boundary_prefixed_sibling(self, monkeypatch):
        _monkeypatch(monkeypatch)
        origin = sheet_origin_for(_SheetGroup(children=[_Obj("Sheet_BoundaryNotes")]))
        assert (origin.x, origin.y, origin.z) == (0.0, 0.0, 0.0)


class TestTranslateToSheetLocal:
    def test_shifts_parts_back_by_the_origin(self, monkeypatch):
        _monkeypatch(monkeypatch)
        parts = [_Part("p1", _Vector(600, 0, -6))]
        translate_to_sheet_local(parts, _Vector(600, 0, 0))
        assert (parts[0].Placement.Base.x, parts[0].Placement.Base.y) == (0.0, 0.0)

    def test_is_a_noop_for_the_first_sheet(self, monkeypatch):
        _monkeypatch(monkeypatch)
        parts = [_Part("p1", _Vector(10, 20, -6))]
        translate_to_sheet_local(parts, _Vector(0, 0, 0))
        assert (parts[0].Placement.Base.x, parts[0].Placement.Base.y) == (10.0, 20.0)

    def test_preserves_z(self, monkeypatch):
        # Z is the frame the operations resolve depths against; shifting it
        # here would silently change every cut.
        _monkeypatch(monkeypatch)
        parts = [_Part("p1", _Vector(600, 0, -6))]
        translate_to_sheet_local(parts, _Vector(600, 0, 0))
        assert parts[0].Placement.Base.z == -6

    def test_leaves_parts_without_a_placement_alone(self, monkeypatch):
        _monkeypatch(monkeypatch)
        part = _Part("p1")
        del part.Placement
        translate_to_sheet_local([part], _Vector(600, 0, 0))
        assert not hasattr(part, "Placement")


# -- the clone trap -------------------------------------------------------

class TestCloneTrapIsVisible:
    """The failure this guards against cannot be reproduced with stand-ins,
    since there is no real PathJob to clone anything. What is pinned here is
    the shape of the contract: the caller gets the job's own model group, and
    the input list is kept separately so the two cannot be confused.
    """

    def _replay(self, clones, source):
        job = type("_J", (), {"Label": "CAM_Replay_Sheet_1", "Model": None})()
        job.Model = type("_M", (), {"Group": clones})()
        return ReplayJob(job, clones, None, None, source, [])

    def test_exposes_clones_not_the_input_list(self):
        clones = [object(), object()]
        replay = self._replay(clones, [object(), object(), object()])
        assert replay.clones == clones
        assert len(replay.source_parts) == 3

    def test_clones_are_copied_so_later_mutation_cannot_change_them(self):
        clones = [object(), object()]
        replay = self._replay(clones, [])
        clones.append(object())
        assert len(replay.clones) == 2

    def test_carries_warnings(self):
        replay = ReplayJob(None, [], None, None, [], ["something"])
        assert replay.warnings == ["something"]


# -- stock frame ----------------------------------------------------------

class TestDescribeStockFrame:
    def test_reads_z_extent(self):
        assert describe_stock_frame(_Stock(-6.0, 0.0)) == (-6.0, 0.0, 6.0)

    def test_none_for_a_null_shape(self):
        assert describe_stock_frame(_Stock(-6.0, 0.0, null=True)) is None

    def test_none_for_a_stock_with_no_shape(self):
        assert describe_stock_frame(_Stock()) is None

    def test_none_for_an_object_that_is_not_a_stock(self):
        assert describe_stock_frame(object()) is None


class TestCompareStockFrames:
    def test_silent_when_the_frames_agree(self):
        new = _Stock(-6.0, 0.0)
        assert compare_stock_frames(_Stock(-6.0, 0.0), new) == []

    def test_warns_on_a_thickness_difference(self):
        warnings = compare_stock_frames(_Stock(-8.0, 0.0), _Stock(-6.0, 0.0))
        assert any("thick" in w for w in warnings)

    def test_warns_on_a_z_frame_difference(self):
        # The real case: a default StockFromBase around a 6 mm part measures
        # Z -7..1, a millimetre of margin either side of the model.
        warnings = compare_stock_frames(_Stock(-7.0, 1.0), _Stock(-6.0, 0.0))
        assert any("spans Z" in w for w in warnings)

    def test_the_frame_warning_explains_the_cut_is_still_correct(self):
        # Depths are re-derived from the new stock, so this is information the
        # operator wants, not a refusal to run.
        warnings = compare_stock_frames(_Stock(-7.0, 1.0), _Stock(-6.0, 0.0))
        assert any("re-derived" in w for w in warnings)

    def test_ignores_differences_below_the_tolerance(self):
        near = _Stock(-6.0 + STOCK_FRAME_TOLERANCE / 10, 0.0)
        assert compare_stock_frames(_Stock(-6.0, 0.0), near) == []

    def test_warns_when_the_new_stock_has_no_shape(self):
        assert compare_stock_frames(_Stock(-6.0, 0.0), _Stock()) != []

    def test_silent_when_the_source_stock_has_no_shape(self):
        assert compare_stock_frames(_Stock(), _Stock(-6.0, 0.0)) == []

    def test_prefixes_with_the_sheet_label(self):
        warnings = compare_stock_frames(_Stock(-8.0, 0.0), _Stock(-6.0, 0.0), "Sheet_2")
        assert all(w.startswith("Sheet_2: ") for w in warnings)


# -- reporting ------------------------------------------------------------

class TestDescribeReplayJob:
    def test_reports_dimensions_and_part_count(self):
        job = type("_J", (), {"Label": "CAM_Replay_Sheet_1"})()
        stock = _Stock(-6.0, 0.0, label="Stock_Replay_Sheet_1")
        stock.Length, stock.Width, stock.Height = 600.0, 400.0, 6.0
        replay = ReplayJob(job, [1, 2], stock, None, [1, 2], [])
        text = "\n".join(describe_replay_job(replay))
        assert "2 part(s)" in text
        assert "600.0 x 400.0 x 6.0" in text

    def test_surfaces_warnings(self):
        job = type("_J", (), {"Label": "J"})()
        stock = _Stock(-6.0, 0.0)
        replay = ReplayJob(job, [], stock, None, [], ["frames differ"])
        assert "WARNING: frames differ" in "\n".join(describe_replay_job(replay))

    def test_lines_are_plain_text(self):
        job = type("_J", (), {"Label": "J"})()
        replay = ReplayJob(job, [], _Stock(-6.0, 0.0), None, [], [])
        assert all(isinstance(line, str) for line in describe_replay_job(replay))


class TestReplayJobName:
    """The name is how the tree says which job a replayed one came from."""

    class _Job:
        def __init__(self, label):
            self.Label = label

    def test_names_the_source_and_the_sheet(self):
        assert replay_job_name(self._Job("Job"), "Sheet_1") == "Job_Replay_Sheet_1"

    def test_keeps_the_sheet_label_in_the_name(self):
        # Two things depend on this. `_UNVERIFIED` is appended to the label on
        # failure, and the harness asserts the job names its sheet -- a name
        # that drops the sheet breaks both.
        for sheet in ("Sheet_1", "Sheet_2", "Sheet_10"):
            assert sheet in replay_job_name(self._Job("Job"), sheet)

    def test_falls_back_when_there_is_no_source(self):
        assert replay_job_name(None, "Sheet_1") == "CAM_Replay_Sheet_1"

    def test_falls_back_when_the_source_has_no_label(self):
        assert replay_job_name(self._Job(""), "Sheet_1") == "CAM_Replay_Sheet_1"

    def test_handles_a_missing_sheet_label(self):
        assert replay_job_name(self._Job("Job"), "") == "Job_Replay"

    def test_two_jobs_stay_distinguishable(self):
        # The point of naming from the source: replaying two different jobs
        # onto the same sheet must not produce the same name.
        a = replay_job_name(self._Job("Job"), "Sheet_1")
        b = replay_job_name(self._Job("OtherJob"), "Sheet_1")
        assert a != b


class TestJobPropertiesNotCopied:
    """The list of job properties the replay skips is pinned, exactly.

    It has to be. That list decides both what is copied AND what the validator
    compares, so a name added to it stops being copied and stops being checked
    in the same move, and nothing notices. Demonstrated: adding `OrderOutputBy`
    to it and re-running the validator produced **0 failures** -- the property
    was neither copied nor verified, which is precisely the defect that list
    exists to prevent.

    So the list is short, every entry is defensible on its own terms, and this
    test fails if one is added without that argument being made here.
    """

    EXPECTED = {
        # the replayed job's own structure -- it has its own, built for this
        # sheet, and the source's point at the source geometry
        "Group", "Model", "Operations", "SetupSheet", "Stock", "Tools",
        # identity
        "Proxy", "Label", "Label2",
        # computed or derived: copying these would state something untrue
        "CycleTime", "Path", "LastPostProcessDate", "LastPostProcessOutput",
        # bookkeeping and presentation
        "ExpressionEngine", "Visibility",
        "_ElementMapVersion", "_GroupTouched",
    }

    def test_the_list_is_exactly_what_is_agreed(self):
        assert set(cam_replay.JOB_PROPERTIES_NOT_COPIED) == self.EXPECTED, (
            "JOB_PROPERTIES_NOT_COPIED changed. It decides what is copied AND "
            "what the validator compares, so a name added here is silently "
            "uncopied and unverified. If that is intended, add it to EXPECTED "
            "here with the reason.")

    def test_it_excludes_structure_computed_and_identity(self):
        excluded = cam_replay.JOB_PROPERTIES_NOT_COPIED
        for name in ("Model", "Operations", "Stock", "Tools", "SetupSheet"):
            assert name in excluded, name
        for name in ("CycleTime", "Path", "Label", "Proxy"):
            assert name in excluded, name

    def test_it_does_not_exclude_anything_a_user_would_set(self):
        # The properties FreeCAD groups under Output and WCS, all of which a
        # user sets and all of which change the output.
        excluded = cam_replay.JOB_PROPERTIES_NOT_COPIED
        for name in ("Machine", "PostProcessor", "PostProcessorArgs",
                     "PostProcessorOutputFile", "PostProcessorPropertyOverrides",
                     "SplitOutput", "Fixtures", "OrderOutputBy", "JobType",
                     "Description", "GeometryTolerance"):
            assert name not in excluded, (
                "%s is a user setting and must be copied" % name)

    def test_settings_to_copy_covers_everything_else(self):
        class _Job:
            PropertiesList = ["Machine", "OrderOutputBy", "Model", "CycleTime",
                             "Label", "Fixtures"]

        names = cam_replay.job_settings_to_copy(_Job(), _Job())
        assert names == ["Fixtures", "Machine", "OrderOutputBy"]

    def test_settings_to_copy_is_empty_without_a_source(self):
        class _Job:
            PropertiesList = ["Machine"]

        assert cam_replay.job_settings_to_copy(None, _Job()) == []
