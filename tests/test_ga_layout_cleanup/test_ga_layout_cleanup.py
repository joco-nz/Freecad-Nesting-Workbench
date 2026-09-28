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
        assert len(calls) >= 1, "no delete_layout call found"
        owners = sorted({owner for _, owner in calls})
        assert owners == ["_build_next_generation", "_delete_layouts"], (
            f"direct delete_layout calls are now in {owners}. There are two "
            f"legitimately: the helper, and _build_next_generation, which is "
            f"delegated whole to the main thread and must not add a nested "
            f"handover. A third means a new site that has not been considered.")

    def test_run_never_deletes_directly(self):
        in_run = [(l, o) for l, o in _delete_layout_calls() if o == "run"]
        assert not in_run, (
            f"direct delete_layout calls are in run() again: {in_run}. That "
            f"function executes on the worker's QThread and FreeCAD will not "
            f"remove document objects from there. Absence is the passing state, "
            f"which is why this asserts the list is empty rather than looking "
            f"for one and comparing it.")

    def test_generation_deletion_is_direct_not_handed_over(self):
        """A corrected test. It previously demanded the opposite.

        `_build_next_generation` used to be required to route its deletions
        through `_delete_layouts`, on the grounds that "one way to delete a
        layout" is tidier. That was wrong and it is a freeze risk: the function
        is delegated whole to the main thread, so going through the helper emits
        a handover -- an emit plus a blocking wait -- from *inside* the handler
        that is already serving one. If that acknowledgement is lost, the
        worker never returns and FreeCAD stops responding.
        """
        body = ast.unparse(_function("_build_next_generation"))
        assert "self._delete_layouts(" not in body, (
            "_build_next_generation is handing its deletions over again. It "
            "already runs on the main thread, so the handover is a nested "
            "blocking wait for no benefit.")
        assert "self.layout_manager.delete_layout" in body, (
            "_build_next_generation no longer deletes directly. It is delegated "
            "whole to the main thread, which is the thread allowed to do it.")

    def test_no_delegating_function_hands_over(self):
        """A function that runs inside a draw handler must not emit another.

        `_build_next_generation` and `create_ga_population` are both executed
        *by* `_handle_draw_request` when a draw callback is set. Anything they
        emit is a handover nested inside a handover.
        """
        for name in ("_build_next_generation", "create_ga_population"):
            if not any(n.name == name for n in ast.walk(_TREE)
                       if isinstance(n, ast.FunctionDef)):
                continue
            body = ast.unparse(_function(name))
            assert "self.draw_callback(" not in body, (
                f"{name} is executed by the main-thread draw handler, so a "
                f"draw_callback call inside it is a nested blocking wait")

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

    def test_the_error_path_is_bounded(self):
        """A failing run must be able to give up on the handover quickly.

        The error path previously deleted directly and returned. Routing it
        through the helper made it wait on the main thread -- and if that
        acknowledgement was lost it waited forever, which is a freeze, not a
        leak. Leaving layouts behind is recoverable; a locked FreeCAD is not.
        """
        run_src = _SRC
        body = ast.unparse(_function("run"))
        handler = body.split("except Exception", 1)[1]
        assert "_delete_layouts(layouts, timeout=" in handler, (
            "the error path hands over without a short budget. A run that has "
            "already failed should unwind quickly, not block a thread until "
            "the user kills the process.")
        assert "_ERROR_HANDOFFER_TIMEOUT_S" in run_src, (
            "the error path's timeout constant is not referenced; either the "
            "budget was removed or it is defined and unused")
        assert "_ERROR_HANDOFFER_TIMEOUT_S = 5.0" in run_src, (
            "the error handover budget should be short. It only gates how long "
            "a *failing* run waits before leaving layouts behind.")

    def test_the_helper_does_not_touch_layout_manager_before_deciding(self):
        """`layout_manager` is built inside run(), so the helper must not
        touch it unless it is actually deleting."""
        early = ast.unparse(_function("_delete_layouts")).split(
            "if self.draw_callback:", 1)[0]
        assert "self.layout_manager" not in early, (
            "_delete_layouts reaches for layout_manager before deciding which "
            "thread to use. layout_manager is created inside run(), so on a "
            "path that only hands the work over this is a NoneType crash.")


class TestNoHandoverCanFreezeTheApplication:
    """A lost acknowledgement must not be able to stop FreeCAD responding.

    The report was a freeze: FreeCAD locked and the run had to be killed. The
    mechanism is a single `threading.Event` shared by every handover from the
    worker to the main thread. `request_draw_on_main_thread` cleared it, emitted
    a signal and called `wait()` with no timeout. If the acknowledgement was
    lost the worker never returned, and nothing on the main thread could make it.
    """

    CONTROLLER = os.path.join(
        _REPO, "freecad", "nestingworkbench", "Tools", "Nesting",
        "nesting_controller.py")
    _CTRL = open(CONTROLLER).read()
    _CTRL_TREE = ast.parse(_CTRL)

    def _fn(self, name):
        return next(n for n in ast.walk(self._CTRL_TREE)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_the_wait_has_a_timeout(self):
        body = ast.unparse(self._fn("request_draw_on_main_thread"))
        waits = [n for n in ast.walk(self._fn("request_draw_on_main_thread"))
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "wait"]
        assert waits, "no event wait found in request_draw_on_main_thread"
        for node in waits:
            assert node.args, (
                "the handover waits with no timeout. One shared event, cleared "
                "before every emit, means a lost acknowledgement blocks a "
                "worker thread forever and the application stops responding.")
        assert "_DRAW_HANDOVER_TIMEOUT_S" in body, (
            "the timeout is not the module constant; a literal would drift "
            "between the definition and the wait")

    def test_the_handler_acknowledges_even_if_the_worker_reference_is_gone(self):
        """`_on_nesting_finished` and `cancel_job` both set `_worker = None`.

        A `draw_requested` signal already queued can then be delivered, the
        handler's `finally` raised AttributeError on `self._worker`, and the
        acknowledgement never happened. Capturing the worker on entry is what
        makes the acknowledgement unconditional.
        """
        fn = self._fn("_handle_draw_request")
        body = ast.unparse(fn)
        assert "worker = self._worker" in body, (
            "_handle_draw_request does not capture the worker on entry")
        assert "if worker is None:" in body, (
            "no guard for the worker reference having been cleared")
        assert "self._worker.notify_draw_complete()" not in body, (
            "the acknowledgement still reads the controller's own reference, "
            "which _on_nesting_finished sets to None. A queued payload "
            "delivered after "
            "that raises in the finally and the worker waits forever.")
        assert "worker.notify_draw_complete()" in body, (
            "the acknowledgement must use the captured reference")
        # and the body must not reach back to the controller's own reference
        assert "self._worker.coordinator" not in body, (
            "the handler reads self._worker.coordinator; every one of those "
            "can raise for the same reason the finally did")

    def test_the_budget_is_finite_and_generous(self):
        assert "_DRAW_HANDOVER_TIMEOUT_S = " in self._CTRL
        value = float(self._CTRL.split(
            "_DRAW_HANDOVER_TIMEOUT_S = ")[1].split(chr(10))[0])
        assert 30 <= value <= 900, (
            f"the handover budget is {value}s. Below ~30s a legitimate "
            f"handover -- drawing a layout, or finalize plus recompute -- would "
            f"be abandoned on a slow machine; above ~900s a lost "
            f"acknowledgement looks like a hang again.")


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


JOB = os.path.join(_REPO, "freecad", "nestingworkbench", "Tools", "Nesting",
                   "nesting_job.py")
_JOB_SRC = open(JOB).read()
_JOB_TREE = ast.parse(_JOB_SRC)


def _job_fn(name):
    return next(n for n in ast.walk(_JOB_TREE)
                if isinstance(n, ast.FunctionDef) and n.name == name)


class TestTheSharedMasterGroupIsJobOwned:
    """One MasterShapes row per run, not one accumulating per run.

    Measured with the real panel and a real QThread, two successful runs with
    the controller's own inter-run cleanup:

        before:  after run 1 -> MasterShapes
                 after run 2 -> MasterShapes, MasterShapes001      (228 objects)
        after:   after run 1 -> MasterShapes
                 after run 2 -> MasterShapes                       (194 objects)

    Two separate things had to be true, and only one of them was the obvious
    bug:

      - The group lives at document level, so it survives the teardown of every
        layout in the run and `delete_layout` cannot reach it. It was not in
        `NestingJob._owned_object_names`, so every cleanup -- on commit, on
        supersede, and on cancel -- left it behind.

      - `NestingJob._promote_masters` emptied the shared group and deliberately
        left the empty shell, on the stated grounds that "the next run reuses it
        harmlessly". It does not: `dispose_shared_master_group`'s own docstring
        calls the group run-scoped, so each run builds a fresh one and FreeCAD
        suffixed the old shell to MasterShapes001. That was true even when the
        user did click OK and the commit path ran.

    Owning the group fixes both: on commit `_promote_masters` has already
    re-parented the masters into a new group under the target layout, so the
    source is empty when `commit()` calls `cleanup()`; on supersede the masters
    inside are that run's own.
    """

    def test_the_group_is_disposed_through_its_owner(self):
        """cleanup() disposes the group, via the LayoutManager that caches it.

        The first version of this fix added the group to
        `NestingJob._owned_object_names`, which is the obvious move and is wrong.
        The group is parented at document level, so the job does not own it in the
        sense that set implies, and `LayoutManager` holds a *cached reference* to
        it for the run. Deleting it from behind that cache left a stale wrapper,
        and the next reader raised

            ReferenceError: Cannot access attribute 'Group' of deleted object

        which `test_master_promotion.py` caught immediately. So the job is handed
        a disposer, and `dispose_shared_master_group` is the one to use: it
        clears the cache before removing the object, for exactly that reason, and
        it is idempotent.
        """
        body = ast.unparse(_job_fn("from_ga_result"))
        assert "job.dispose_shared_master_group = dispose_shared_master_group" in body, (
            "the job is not given a way to dispose the shared master group, so "
            "cleanup() cannot remove it and one MasterShapes row accumulates per "
            "run.")
        assert "shared_master_group=None, dispose_shared_master_group=None" in \
            _JOB_SRC, (
            "from_ga_result does not accept a disposer. Without the keyword the "
            "call is a NameError inside the factory, which is how this was first "
            "broken.")

    def test_the_group_is_not_added_to_the_owned_set(self):
        """Pins the failure mode of the obvious fix.

        The owned set is keyed by name and resolved with `doc.getObject`, and
        nothing in it clears the LayoutManager's cache. So this assertion is not
        pedantry: it is the difference between the group disappearing and the
        next reader raising on a deleted wrapper.
        """
        body = ast.unparse(_job_fn("from_ga_result"))
        assert "job._owned_object_names.add(shared_master_group.Name)" not in body, (
            "the shared master group is back in the owned set. That deletes it "
            "without clearing LayoutManager's cached reference, so the next "
            "reader gets a deleted wrapper and raises ReferenceError.")
        cleanup = ast.unparse(_job_fn("cleanup"))
        assert "dispose_shared_master_group" in cleanup, (
            "cleanup() does not call the disposer, so nothing removes the group "
            "on commit, on supersede, or on cancel.")
        # The two removals are independent: the group is not in the owned set,
        # and the things it holds are this run's master shapes rather than the
        # job's layout and parts groups. An earlier version of this test asserted
        # an ordering between them, with a rationale that was simply wrong, and
        # it could not fail because `str.index` finds the first occurrence of the
        # cleared-set call either way. So what is actually required is checked
        # instead: the disposer runs at method level, not inside the loop that
        # deletes the owned objects, so it still happens when that loop empties
        # the set or when the set was already empty.
        fn = _job_fn("cleanup")
        disposer_calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "dispose"
        ]
        assert disposer_calls, "cleanup() never calls the disposer"
        loops = [n for n in ast.walk(fn)
                 if isinstance(n, ast.For)]
        for call in disposer_calls:
            for loop in loops:
                assert not (loop.lineno <= call.lineno <= (loop.end_lineno or 0)), (
                    "the disposer is called inside the owned-objects loop, so it "
                    "is skipped whenever that loop does not iterate -- including "
                    "on a re-run where the set is already empty")

    def test_cleanup_survives_a_failing_disposer(self):
        """A disposal that raises must not stop the rest of the teardown.

        The group is the last thing removed, so a failure there is the easiest
        place to abandon the rest of cleanup. Losing the group is the bug being
        fixed; raising out of cleanup() instead would leave the run's own groups
        behind too, which is strictly worse.
        """
        cleanup = ast.unparse(_job_fn("cleanup"))
        assert "except Exception" in cleanup, (
            "cleanup() calls the disposer unguarded. One failure there would "
            "abort the teardown and leak the layout and parts groups as well.")
        assert "PrintWarning" in cleanup or "PrintError" in cleanup, (
            "a failed disposal must be reported, not swallowed silently")

    def test_promote_no_longer_claims_the_group_is_reused(self):
        """The stale assumption is the part that made this look harmless.

        Worth pinning as text because the reasoning, not the code, is what
        allowed the leak to persist: the code looked deliberate and reasonable.
        """
        source = open(JOB).read()
        # Phrase-checked rather than argued, and the comment in nesting_job.py
        # deliberately paraphrases the stale claim rather than quoting it: the
        # first version of that comment quoted it verbatim while explaining why
        # it was wrong, and this assertion then failed on the explanation.
        for phrase in ("reuses it harmlessly", "reused harmlessly",
                       "the next run reuses"):
            assert phrase not in source, (
                f"the comment claiming a later run re-picks the emptied shared "
                f"group is back ({phrase!r}). It does not -- the group is "
                f"run-scoped, so every run builds a fresh one and the empty "
                f"shell accumulates, taking the MasterShapes name with it.")
        # And the removal this now relies on has to be real. Asserting only that
        # the name appears is worthless: replacing `list(self._owned_object_names)`
        # with `list()` leaves the identifier in the source and the loop iterates
        # nothing, which is how the first version of this check passed against a
        # cleanup() that deleted nothing at all.
        cleanup = ast.unparse(_job_fn("cleanup"))
        assert "self._owned_object_names" in cleanup, (
            "cleanup() no longer references the owned set, so nothing removes "
            "the shared group on any path.")
        loops_over_it = any(
            isinstance(n, ast.For)
            and isinstance(n.iter, ast.Call)
            and isinstance(n.iter.func, ast.Name)
            and n.iter.func.id == "list"
            and any(isinstance(a, ast.Attribute) and a.attr == "_owned_object_names"
                    for a in ast.walk(n.iter))
            for n in ast.walk(_job_fn("cleanup")))
        assert loops_over_it, (
            "cleanup() mentions the owned set but does not iterate it, so it "
            "deletes nothing. The master group would accumulate again.")
        assert "recursive_delete" in cleanup, (
            "cleanup() must delete each owned object, not merely clear the set")
        commit = ast.unparse(_job_fn("commit"))
        assert "_promote_masters()" in commit and "self.cleanup()" in commit, (
            "commit() must promote first and then clean up, in that order. "
            "Reordering would delete the masters before they were re-parented.")
        assert commit.index("_promote_masters()") < commit.index("self.cleanup()"), (
            "commit() cleans up before promoting; the masters would be deleted "
            "rather than moved to the target layout")
