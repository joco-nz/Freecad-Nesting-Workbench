#!/usr/bin/env freecadcmd
"""Regression test: master shapes must reach the target layout on commit.

The defect
----------
`LayoutManager` in headless mode (`create_doc_objects=False`) deliberately
parents its pooled master shapes into a *document-level* `MasterShapes` group
rather than under a layout group, because `delete_layout` recursively deletes a
layout and everything under it -- masters parented there would be destroyed by
the teardown of whichever layout was discarded first.

`NestingJob.commit()` only ever looked for a `MasterShapes` child of the
temporary layout:

    temp_masters = next((c for c in self.temp_layout.Group
                         if c.Label.startswith("MasterShapes")), None)

which never matches in headless mode. The observable result was that a commit
did nothing to the masters:

  * the previous run's master row in the target layout was neither replaced nor
    deleted, so a re-nest left the stale geometry alongside the new
  * the new masters stayed parented to a document-level group, unreachable from
    any layout

That second point is what makes it worse than cosmetic. A saved layout reopened
later expects to find its master row as a child of its own group, and any
visibility helper that walks `Layout*` groups will not see a doc-level group at
all.

Measured, on the same scenario before and after the fix:

    pre-fix   target MasterShapes -> ['master_STALE']   (stale row survived)
              6 master-ish objects unreachable from the target layout
    post-fix  target MasterShapes -> ['master_A', 'master_B']
              0 unreachable

Runs both headless and simulate mode: headless is the fixed path, simulate is
the pre-existing layout-parented path that must not regress.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_test")

_PARAMS = {
    "sheet_width": 450,
    "sheet_height": 350,
    "spacing": 5.0,
    "sheet_thickness": 3.0,
    "deflection_angle": 30,
    "simplification": 0.1,
    "font_path": "",
    "show_bounds": True,
    "add_labels": False,
    "label_height": 25.0,
    "label_size": 10.0,
    "rotation_steps": 4,
    "generations": 1,
    "population_size": 1,
    "nesting_direction": 0,
}

_UI = {
    "spacing": 5.0,
    "deflection": 0.05,
    "simplification": 0.1,
    "rotation_steps": 4,
    "add_labels": False,
    "font_path": "",
    "verbose": False,
}

_QUANTITIES = {
    "A": {"quantity": 1, "rotation_steps": 4, "up_direction": "Z+", "fill_sheet": False},
    "B": {"quantity": 1, "rotation_steps": 4, "up_direction": "Z+", "fill_sheet": False},
}

_FAILURES = []
_CHECKS = [0]


def emit(message=""):
    """Prints a report line in a way that survives FreeCAD's console redirect.

    Under freecadcmd, FreeCAD.Console captures plain print() once a document
    exists, so check output silently disappears.
    """
    try:
        import FreeCAD

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


def reachable_from(root):
    """Names of every object transitively under root."""
    seen = set()

    def walk(group):
        for child in getattr(group, "Group", []) or []:
            if child.Name in seen:
                continue
            seen.add(child.Name)
            walk(child)

    walk(root)
    return seen


def master_shapes_group_under(layout_group):
    return next(
        (c for c in layout_group.Group if c.Label.startswith("MasterShapes")), None
    )


def run_case(create_doc_objects):
    import FreeCAD
    import Part

    from freecad.nestingworkbench.Tools.Nesting.layout_manager import LayoutManager
    from freecad.nestingworkbench.Tools.Nesting.nesting_job import NestingJob

    mode = "simulate" if create_doc_objects else "headless"
    emit(f"")
    emit(f"--- {mode} mode ---")

    doc = FreeCAD.newDocument(f"promote_{mode}")

    sources = {}
    for name, (length, width) in (("A", (60, 40)), ("B", (30, 30))):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = Part.makeBox(length, width, 10)
        sources[name] = obj
    doc.recompute()

    target = doc.addObject("App::DocumentObjectGroup", "Layout_target")

    # A previous run already committed a master row into the target.
    stale_group = doc.addObject("App::DocumentObjectGroup", "MasterShapes")
    stale_group.Label = "MasterShapes"
    target.addObject(stale_group)
    stale = doc.addObject("Part::Feature", "master_STALE")
    stale_group.addObject(stale)

    manager = LayoutManager(doc, {}, create_doc_objects=create_doc_objects)
    layout = manager.create_layout("Layout_temp", sources, _QUANTITIES, _UI)

    # What commit() actually depends on is that a MasterShapes group is
    # findable, and that it ends up under the target layout.
    #
    # Note the label trap: FreeCAD de-duplicates a Label as well as a Name, so
    # on a re-nest -- when the document already holds a "MasterShapes" group --
    # the new one comes back labelled "MasterShapes001". The exact-match
    # `child.Label == "MasterShapes"` in LayoutManager.create_layout therefore
    # leaves layout.master_shapes_group None in simulate mode. That is
    # pre-existing (confirmed identical before this fix) and harmless, because
    # nothing outside layout_manager reads the attribute and commit() matches
    # by prefix. Asserted here by prefix so the test tracks the real invariant.
    shared = getattr(manager, "shared_master_group", None)
    # Both modes now hold their masters in the document-level shared group, so
    # this is no longer a headless-only invariant. Simulate used to parent a
    # MasterShapes group under its own layout instead, on the reasoning that a
    # drawn layout must own its masters -- which conflated sharing (safe) with
    # parenting (the actual hazard, since delete_layout recursively removes
    # everything under a layout group). See
    # LayoutManager._get_shared_master_group.
    mode = "simulate" if create_doc_objects else "headless"
    check(f"[{mode}] a document-level shared group exists", shared is not None)
    check(f"[{mode}] layout points at the shared group",
          layout.master_shapes_group is shared,
          "create_layout must fall back to the shared group, or commit "
          "cannot find the masters")
    check(f"[{mode}] shared group holds the masters",
          shared is not None and len(shared.Group) == 2,
          f"got {0 if shared is None else len(shared.Group)}")
    # The property that makes sharing safe, asserted directly: no layout may own
    # the masters, or the teardown of whichever layout is discarded first would
    # destroy objects another still points at.
    under_layout = [
        c for c in layout.layout_group.Group
        if c.Label.startswith("MasterShapes")
    ]
    check(f"[{mode}] no layout-owned MasterShapes group",
          len(under_layout) == 0,
          f"got {[c.Label for c in under_layout]} -- masters parented under a "
          f"layout are destroyed by its teardown")

    job = NestingJob.from_ga_result(
        doc=doc,
        target_layout=target,
        params=_PARAMS,
        preparer=None,
        layout_group=layout.layout_group,
        parts_group=layout.parts_group,
        sheets=[],
        **({"shared_master_group": shared} if shared is not None else {}),
    )
    # Captured before commit: cleanup() deletes the temporary layout, and
    # touching its Python wrapper afterwards raises.
    temp_layout_name = layout.layout_group.Name
    job.commit()

    committed = master_shapes_group_under(target)
    check(f"[{mode}] target layout has a MasterShapes group", committed is not None)

    if committed is not None:
        labels = sorted(c.Label for c in committed.Group)
        check(f"[{mode}] committed masters are this run's, not the stale row",
              labels == ["master_A", "master_B"], f"got {labels}")
        check(f"[{mode}] stale master was deleted",
              doc.getObject("master_STALE") is None)

    # Every master-ish object must be reachable from the target layout. This is
    # the orphaning check: getParentGroup() reports None for children of an
    # App::Part, so reachability is the only reliable test.
    from_target = reachable_from(target)
    masters = [
        o for o in doc.Objects
        if o.Label.startswith(("master_", "temp_master_", "master_shape_", "bound_"))
    ]
    orphans = sorted(o.Label for o in masters if o.Name not in from_target)
    check(f"[{mode}] no master objects orphaned outside the target layout",
          not orphans, f"orphaned: {orphans}")

    if shared is not None:
        check("[headless] shared group left in place but empty",
              len(shared.Group) == 0, f"got {len(shared.Group)}")
        check("[headless] shared group still exists for the next run",
              doc.getObject(shared.Name) is not None)

    check(f"[{mode}] temporary layout was torn down",
          doc.getObject(temp_layout_name) is None)


def run_leak_case():
    """A cancelled run must not leave the shared master group behind.

    The shared group is parented at document level, so `delete_layout` cannot
    reach it -- it is not under any layout. Deleting every layout of a run,
    which is what the cancel and early-stop paths do, therefore left the group
    in the document still populated and visible. Measured before this fix:

        group still exists, Visibility True
        6 objects not under any Layout* group
        (master_A/B, master_shape_A/B, bound_A_1/B_1)

    `dispose_shared_master_group` is what the coordinator's `finally` calls
    when no NestingJob will commit the masters. It is deliberately NOT called
    on the success path, where the job promotes them instead.
    """
    import FreeCAD
    import Part

    from freecad.nestingworkbench.Tools.Nesting.layout_manager import LayoutManager

    emit("")
    emit("--- abandoned (cancelled) run ---")

    doc = FreeCAD.newDocument("leak")
    sources = {}
    for name, (length, width) in (("A", (60, 40)), ("B", (30, 30))):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = Part.makeBox(length, width, 10)
        sources[name] = obj
    doc.recompute()

    manager = LayoutManager(doc, {}, create_doc_objects=False)
    first = manager.create_layout("Layout_GA_1_1", sources, _QUANTITIES, _UI)
    second = manager.create_layout("Layout_GA_1_2", sources, _QUANTITIES, _UI)

    shared = manager.shared_master_group
    check("[cancel] a shared group was created", shared is not None)
    check("[cancel] it holds the masters",
          shared is not None and len(shared.Group) == 2)
    shared_name = shared.Name if shared is not None else None

    # The cancel path deletes every layout in the population.
    manager.delete_layout(first, verbose=False)
    manager.delete_layout(second, verbose=False)

    # This is the leak itself: a populated group that no layout teardown can
    # reach. Asserted so the test records why dispose is needed at all.
    check("[cancel] delete_layout cannot reach the shared group",
          doc.getObject(shared_name) is not None and len(shared.Group) == 2)

    disposed = manager.dispose_shared_master_group()
    check("[cancel] dispose reports the masters it removed", disposed == 2,
          f"got {disposed}")
    check("[cancel] the group is gone from the document",
          doc.getObject(shared_name) is None)
    check("[cancel] the manager no longer caches a deleted group",
          manager.shared_master_group is None)

    leaked = sorted(
        o.Label for o in doc.Objects
        if o.Label.startswith(("master_", "temp_master_", "master_shape_", "bound_"))
    )
    check("[cancel] no master objects left orphaned", not leaked, f"left {leaked}")

    check("[cancel] dispose is idempotent",
          manager.dispose_shared_master_group() == 0)
    check("[cancel] dispose with no group is a no-op",
          LayoutManager(doc, {}, create_doc_objects=True)
          .dispose_shared_master_group() == 0)

    # A later run must still be able to create a group.
    again = LayoutManager(doc, {}, create_doc_objects=False)
    again.create_layout("Layout_GA_2_1", sources, _QUANTITIES, _UI)
    check("[cancel] a later run can still create a shared group",
          again.shared_master_group is not None
          and len(again.shared_master_group.Group) == 2)


def main():
    for create_doc_objects in (False, True):
        run_case(create_doc_objects)
    run_leak_case()
    emit("")
    emit(f"{_CHECKS[0] - len(_FAILURES)}/{_CHECKS[0]} checks passed")
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
        return 1
    return 0


if __name__ in ("__main__", "test_master_promotion"):
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
