"""Layout deletion must happen on the thread allowed to touch the document.

FreeCAD document objects must be created and destroyed on the main thread, and
`GACoordinator.run()` executes on the worker's QThread whenever the run is
driven from the panel. So a layout deletion has to be handed to the draw
callback's owner, exactly as layout creation already is.

Three of the four deletion sites did that. One did not: the `except Exception`
handler in `run()` called `layout_manager.delete_layout` directly, on the worker
thread. The layouts survived, which is the "temporary folders left behind after
an early exit" report -- and the error path is the one an early exit is most
likely to reach, since a cancel that surfaces as an exception, a geometry
failure, or a Qt error all land there rather than on one of the `break`s.

Structural rather than behavioural: reproducing a wrong-thread document
mutation needs a real GUI session and a Qt event loop, and a test that
segfaults is not a test. The behavioural half is
`tests/freecad_harness/probe_ga_layout_cleanup.py`.
"""
import ast
import os

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_COORDINATOR = os.path.join(
    _REPO, "freecad", "nestingworkbench", "Tools", "Nesting", "ga_coordinator.py")
_SRC = open(_COORDINATOR).read()
_TREE = ast.parse(_SRC)


def _function(name):
    return next(n for n in ast.walk(_TREE)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _delete_layout_calls():
    """Every `delete_layout(...)` call, with the function it sits in."""
    out = []
    for node in ast.walk(_TREE):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete_layout"):
            continue
        owner = None
        for cand in ast.walk(_TREE):
            if (isinstance(cand, ast.FunctionDef)
                    and cand.lineno <= node.lineno <= (cand.end_lineno or 0)):
                if owner is None or cand.lineno > owner.lineno:
                    owner = cand
        out.append((node.lineno, owner.name if owner else "?"))
    return sorted(out)


class TestLayoutDeletionIsThreadCorrect:
    def test_extractors_find_the_thing_they_claim_to(self):
        calls = _delete_layout_calls()
        assert calls, "no delete_layout call found -- did the helper get renamed?"
        assert len(calls) == 1, (
            "expected exactly one delete_layout call, in _delete_layouts; found "
            f"{calls}. Each new one is a document mutation that may run on the "
            f"worker thread.")

    def test_the_only_direct_deletion_is_inside_the_helper(self):
        line, owner = _delete_layout_calls()[0]
        assert owner == "_delete_layouts", (
            f"the direct delete_layout at line {line} is in {owner}(). Only "
            f"_delete_layouts may touch the layout manager directly; every "
            f"other site must go through it so the main-thread hop is not "
            f"forgotten.")

    def test_the_helper_hops_to_the_callback_when_there_is_one(self):
        """The whole point: hand the work over rather than do it here."""
        body = ast.unparse(_function("_delete_layouts"))
        assert "if self.draw_callback:" in body, (
            "_delete_layouts no longer checks for a draw callback, so it would "
            "delete on the worker thread whenever the run is driven from the "
            "panel -- the original defect, reintroduced inside the fix")
        assert "cleanup_layouts" in body, (
            "_delete_layouts must emit the cleanup_layouts payload the main "
            "thread handler understands")
        hop = body.index("if self.draw_callback:")
        direct = body.index("self.layout_manager.delete_layout(layout")
        assert direct > hop, (
            "the direct deletion appears before the callback check, so the "
            "handover may be bypassed")

    def test_no_bare_deletion_survives_in_run(self):
        """`run()` is the function that executes on the worker thread."""
        body = ast.unparse(_function("run"))
        assert "self.layout_manager.delete_layout(" not in body, (
            "run() deletes a layout directly again. It runs on the worker's "
            "QThread, and FreeCAD will not let document objects be removed "
            "from there, so the deletion silently does nothing and the layout "
            "is left in the document.")

    def test_the_error_path_goes_through_the_helper(self):
        """The error handler is the path an early exit actually reaches."""
        handler = ast.unparse(_function("run")).split("except Exception", 1)[1]
        assert "self._delete_layouts(layouts)" in handler, (
            "the error path does not use _delete_layouts. Cancels that surface "
            "as exceptions, geometry failures and Qt errors all arrive here, "
            "so this is the path that leaks.")

    def test_generation_deletion_goes_through_the_helper(self):
        body = ast.unparse(_function("_build_next_generation"))
        assert "self._delete_layouts(" in body, (
            "_build_next_generation discards the outgoing population without "
            "the helper. It is delegated whole to the main thread when a draw "
            "callback is set, so those deletions happened to be correctly "
            "threaded, but routing them through the helper keeps 'one way to "
            "delete a layout' true rather than true by coincidence.")

    def test_the_helper_does_not_touch_layout_manager_before_deciding(self):
        """`layout_manager` is built inside run(), so the helper must not
        touch it unless it is actually deleting."""
        early = ast.unparse(_function("_delete_layouts")).split(
            "if self.draw_callback:", 1)[0]
        assert "self.layout_manager" not in early, (
            "_delete_layouts reaches for layout_manager before deciding which "
            "thread to use. layout_manager is created inside run(), so on a "
            "path that only hands the work over this is a NoneType crash.")


class TestKnownLeakOnTheNonCommitPath:
    """The shared master group, which the code already documents as leaking.

    Recorded rather than fixed, because it is a different mechanism from the
    wrong-thread deletion and changing both at once would make the measurement
    of either impossible. The note in run()'s finally block already states the
    condition: a run that ends without a committed job has nothing that will
    ever promote the masters, and disposal is skipped when `_job_committed` is
    set -- which it is on the success path before the controller has committed.
    """

    def test_the_condition_is_the_one_documented(self):
        body = ast.unparse(_function("run"))
        assert "if not self._job_committed" in body, (
            "the finally block no longer keys master disposal on "
            "_job_committed. That gate is what keeps the shared master group "
            "from being deleted out from under a pending commit, and losing it "
            "leaks more than the thing it prevents.")
        assert "self._job_committed = True" in body, (
            "_job_committed is never set, so masters are disposed even on the "
            "success path and the winning layout loses its shared shapes")

    def test_it_is_still_a_leak(self):
        """Pins the known-leak status so it cannot be quietly forgotten.

        If a future change makes the non-commit path clean, this fails and the
        note beside it should be deleted in the same commit.
        """
        body = ast.unparse(_function("run"))
        assert "dispose_shared_master_group" in body, (
            "dispose_shared_master_group is no longer called from the finally "
            "block. Either the non-commit leak has been fixed -- in which case "
            "delete this test and the note beside it -- or the disposal was "
            "lost, which leaks the whole shared master group.")
