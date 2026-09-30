"""Tests for the replay orchestration and job detection.

Background
----------
`replay_sheet` is the pipeline the command drives: flatten, build a job,
replay the recipe into it, order for hole nesting, verify. The engine steps
each have their own tests; what is pinned here is the pipeline's own
behaviour, in particular how it fails.

Two failure policies, both decided deliberately:

  * **A sheet that fails is kept, not deleted.** An operator investigating a
    bad run needs the job still in the document. It is labelled
    `_UNVERIFIED` so a half-built job cannot be mistaken for a working one, and
    a loud message says not to post from it.

  * **One bad sheet does not stop the others.** A multi-sheet nest produces
    independent jobs, and one failing sheet should not hide a good one.

`resolve_layout_group` auto-detects the layout rather than requiring a
selection, because there is normally one. Where there is not, the ambiguity is
reported with the candidates, because picking the wrong layout means replaying
somebody else's nest and calling it a success.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    UNVERIFIED_SUFFIX,
    SheetOutcome,
    describe_sheet_outcome,
    is_cam_job,
    resolve_layout_group,
)


# -- stand-ins ------------------------------------------------------------

class _Doc:
    def __init__(self, objects=()):
        self.Objects = list(objects)

    def recompute(self):
        pass


class _Group:
    def __init__(self, label, objects=()):
        self.Label = label
        self.Name = label
        self.Objects = list(objects)

    def isDerivedFrom(self, type_id):  # noqa: N802
        return type_id == "App::DocumentObjectGroup"


class _Sheet(_Group):
    def __init__(self, label="Sheet_1"):
        super().__init__(label)


class _Job:
    def __init__(self, label="Job", path_attr=True):
        self.Label = label
        self._path_attr = path_attr

    def isDerivedFrom(self, type_id):  # noqa: N802
        return type_id in ("Path::Feature", "Path::FeaturePython")

    def __getattr__(self, name):
        if name in ("Operations", "Model", "Stock"):
            return object()
        raise AttributeError(name)


# -- job detection --------------------------------------------------------

class TestIsCamJob:
    def test_accepts_a_path_feature_with_the_job_attributes(self):
        assert is_cam_job(_Job()) is True

    def test_rejects_a_plain_document_object(self):
        plain = _Group("Layout_001")
        assert is_cam_job(plain) is False

    def test_rejects_a_path_feature_missing_a_job_attribute(self):
        class _Partial:
            Label = "x"

            def isDerivedFrom(self, type_id):  # noqa: N802
                return type_id == "Path::Feature"

        assert is_cam_job(_Partial()) is False

    def test_rejects_none(self):
        assert is_cam_job(None) is False


# -- layout resolution ----------------------------------------------------

class TestResolveLayoutGroup:
    def test_finds_a_single_layout(self, monkeypatch):
        monkeypatch.setattr(
            "freecad.nestingworkbench.freecad_helpers.get_layout_group",
            lambda doc: _Group("Layout_001"))
        layout, warnings = resolve_layout_group(_Doc([_Group("Layout_001")]))
        assert layout is not None
        assert warnings == []

    def test_warns_when_several_layouts_are_present(self, monkeypatch):
        # Guessing silently here would mean replaying the wrong nest and
        # reporting success.
        monkeypatch.setattr(
            "freecad.nestingworkbench.freecad_helpers.get_layout_group",
            lambda doc: _Group("Layout_002"))
        doc = _Doc([_Group("Layout_001"), _Group("Layout_002")])
        layout, warnings = resolve_layout_group(doc)
        assert layout is not None
        assert len(warnings) == 1
        assert "Layout_001" in warnings[0] and "Layout_002" in warnings[0]

    def test_strict_refuses_rather_than_guesses(self, monkeypatch):
        monkeypatch.setattr(
            "freecad.nestingworkbench.freecad_helpers.get_layout_group",
            lambda doc: _Group("Layout_002"))
        doc = _Doc([_Group("Layout_001"), _Group("Layout_002")])
        layout, warnings = resolve_layout_group(doc, strict=True)
        assert layout is None
        assert warnings

    def test_counts_a_temp_layout_as_a_candidate(self, monkeypatch):
        # __temp_Layout is a separate group and is preferred by the helper, so
        # it counts towards the ambiguity.
        monkeypatch.setattr(
            "freecad.nestingworkbench.freecad_helpers.get_layout_group",
            lambda doc: _Group("__temp_Layout"))
        doc = _Doc([_Group("Layout_001"), _Group("__temp_Layout")])
        _layout, warnings = resolve_layout_group(doc)
        assert len(warnings) == 1

    def test_no_layout_is_not_a_warning(self, monkeypatch):
        monkeypatch.setattr(
            "freecad.nestingworkbench.freecad_helpers.get_layout_group",
            lambda doc: None)
        layout, warnings = resolve_layout_group(_Doc([]))
        assert layout is None
        assert warnings == []


# -- outcomes -------------------------------------------------------------

class TestSheetOutcome:
    def test_a_sheet_with_no_errors_is_ok(self):
        assert SheetOutcome(_Sheet()).ok is True

    def test_errors_make_it_not_ok(self):
        outcome = SheetOutcome(_Sheet(), errors=["boom"])
        assert outcome.ok is False

    def test_a_failed_verification_makes_it_not_ok(self):
        class _V:
            ok = False
        assert SheetOutcome(_Sheet(), verification=_V()).ok is False

    def test_a_passing_verification_leaves_it_ok(self):
        class _V:
            ok = True
        assert SheetOutcome(_Sheet(), verification=_V()).ok is True

    def test_sheet_label_is_read_from_the_group(self):
        assert SheetOutcome(_Sheet("Sheet_3")).sheet_label == "Sheet_3"

    def test_repr_says_whether_it_worked(self):
        assert "ok=False" in repr(SheetOutcome(_Sheet(), errors=["x"]))


class TestDescribeSheetOutcome:
    def test_includes_the_sheet_label(self):
        assert "Sheet_1" in describe_sheet_outcome(SheetOutcome(_Sheet()))[0]

    def test_reports_errors_as_failures(self):
        text = "\n".join(describe_sheet_outcome(SheetOutcome(_Sheet(), errors=["boom"])))
        assert "FAIL: boom" in text

    def test_says_ok_when_clean(self):
        text = "\n".join(describe_sheet_outcome(SheetOutcome(_Sheet())))
        assert "OK" in text

    def test_does_not_claim_ok_when_it_failed(self):
        text = "\n".join(describe_sheet_outcome(SheetOutcome(_Sheet(), errors=["x"])))
        assert "  OK" not in text

    def test_includes_ordering_notes(self):
        outcome = SheetOutcome(_Sheet(), ordering=["reordered for nesting"])
        assert "reordered for nesting" in "\n".join(describe_sheet_outcome(outcome))

    def test_lines_are_plain_text(self):
        outcome = SheetOutcome(_Sheet(), errors=["x"], ordering=["y"])
        assert all(isinstance(line, str) for line in describe_sheet_outcome(outcome))


class TestUnverifiedSuffix:
    def test_is_appended_to_the_label(self):
        # The label is the only thing standing between a half-built job and
        # somebody posting from it.
        assert UNVERIFIED_SUFFIX == "_UNVERIFIED"

    def test_is_recognisable(self):
        assert "_UNVERIFIED" in ("CAM_Replay_Sheet_1" + UNVERIFIED_SUFFIX)


# -- pipeline guards ------------------------------------------------------

class TestReplaySheetGuards:
    """These paths must return an outcome carrying a reason rather than raise.

    A command that raises mid-run leaves the document half-changed with nothing
    said, which is the state this feature exists to avoid.
    """

    def test_no_sheet(self):
        outcome = cam_replay.replay_sheet(None, None, None, None)
        assert outcome.ok is False
        assert "No sheet" in outcome.errors[0]

    def test_a_source_job_with_no_operations(self, monkeypatch):
        doc = _Doc()
        outcome = cam_replay.replay_sheet(
            doc, _Group("Layout_001"), _Sheet(), _Job())
        assert outcome.ok is False
        assert any("no operations" in e for e in outcome.errors)

    def test_a_sheet_with_no_parts(self, monkeypatch):
        # Every sheet failing is fine; the command should say which and why.
        outcome = cam_replay.replay_sheet(
            _Doc(), _Group("Layout_001"), _Sheet(), _Job())
        assert outcome.ok is False
        assert outcome.errors
