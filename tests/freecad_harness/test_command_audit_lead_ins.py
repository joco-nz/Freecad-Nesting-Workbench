"""Does the lead-in check command construct and register on a real GUI?

Written for the same reason as `test_panel_construction.py`: nothing else gated
touches this command, and a command that cannot be constructed is invisible to
every other test here. `test_lead_in_audit.py` imports the module and reads
`GetResources()`, which is enough for the logic and not enough for the command --
`GetResources` is a plain method that would answer perfectly well on a class
whose dialog code was broken.

This has to run on the **`freecad` GUI binary**, not `freecadcmd`: the command
imports `FreeCADGui` and `PySide.QtWidgets`, and builds a `QMessageBox`. The
GUI binary starts with a live GUI on no display at all, so no Xvfb is needed --
`QT_QPA_PLATFORM=offscreen` as in `test_panel_construction.py`.

What is asserted
----------------

  * **the workbench activates**, which is what runs every `addCommand`. An import
    error in any of the seven command modules stops the workbench loading, and
    the workbench is the only way anyone reaches this command.

  * **the menu lists it and the toolbar does not.** The toolbar omission is a
    decision (see `init_gui.py`), and a decision nobody checks is a decision that
    gets undone by the next person tidying up an icon list.

  * **the dialog builds.** `_ask` is called here with the dialog constructed and
    then closed rather than exec'd, so a broken `QMessageBox` fails the gate
    without waiting for a click.

  * **`IsActive` follows the selection.** False with nothing selected, false with
    two jobs selected -- two recipes is ambiguous and guessing would cut the
    wrong features, the same rule the replay command uses.

Run via tests/freecad_harness/run.sh, which uses the GUI binary for this one.
Writes `.last_status_leadincmd`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_leadincmd")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import FreeCAD
import FreeCADGui
from PySide import QtWidgets

_failures = []
_checks = [0]


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        print(message)


def check(condition, detail):
    _checks[0] += 1
    if not condition:
        _failures.append(detail)
        emit("  FAIL: %s" % detail)
    return bool(condition)


def check_the_workbench_loads():
    """Every command module imports, which is what `Initialize` does.

    **`Initialize()` is not called.** `appendMenu` and `appendToolbar` are C++
    methods on `Gui::PythonWorkbench` and need the `__Workbench__` back-pointer
    that only FreeCAD's own workbench machinery installs, so calling it on a
    hand-made instance raises
    `AttributeError: 'NestingWorkbench' object has no attribute '__Workbench__'`
    regardless of whether this command is correct. The first version of this
    check called it and reported that as a failure.

    Importing the modules is the part that can fail for a real reason: each one
    runs its `FreeCADGui.addCommand` at import time, so an import error means
    the command is not registered and the workbench does not load.
    """
    emit("")
    emit("-- 1. every command module imports, so every addCommand runs --")
    modules = (
        "command_nest",
        "command_stack_sheets",
        "command_manual_nester",
        "command_export_sheets",
        "command_create_cam_job",
        "command_replay_cam",
        "command_audit_lead_ins",
        "command_create_silhouette",
    )
    ok = True
    for name in modules:
        try:
            __import__("freecad.nestingworkbench.commands.%s" % name)
            check(True, "%s imports" % name)
        except Exception:
            check(False, "%s imports:\n%s" % (name, traceback.format_exc()))
            ok = False
    return ok


def check_menu_lists_it_without_the_toolbar():
    """Read the lists `Initialize` passes to `appendMenu`/`appendToolbar`.

    By source inspection rather than by calling them, for the reason above.
    This is the check that would catch the toolbar decision being undone by the
    next person tidying an icon list, so it is worth asserting explicitly.
    """
    emit("")
    emit("-- 3. in the menu, deliberately not in the toolbar --")
    path = os.path.join(_REPO, "freecad", "nestingworkbench", "init_gui.py")
    try:
        with open(path) as handle:
            source = handle.read()
    except Exception as exc:
        check(False, "init_gui.py is readable (%s)" % exc)
        return

    menu = _command_list(source, "appendMenu")
    toolbar = _command_list(source, "appendToolbar")
    emit("  menu has %d, toolbar has %d" % (len(menu), len(toolbar)))

    check("Nesting_AuditLeadIns" in menu,
          "Nesting_AuditLeadIns is in the menu")
    check("Nesting_AuditLeadIns" not in toolbar,
          "Nesting_AuditLeadIns is NOT in the toolbar, as decided")
    for name in ("Nesting_Run", "Nesting_ReplayCAMSetup",
                 "Nesting_CreateSilhouette"):
        check(name in menu and name in toolbar,
              "%s is still in both" % name)
    # The other command modules all register; the menu should carry all of them
    # even if the toolbar does not.
    registered = [n for n in menu if n.startswith("Nesting_")]
    check(len(set(registered)) == len(registered),
          "the menu has no duplicates")


def _command_list(source, call):
    """The `Nesting_*` names inside one `appendMenu`/`appendToolbar` call."""
    start = source.index("self.%s(" % call)
    depth = 0
    end = start
    for position in range(start, len(source)):
        if source[position] == "(":
            depth += 1
        elif source[position] == ")":
            depth -= 1
            if depth == 0:
                end = position
                break
    body = source[start:end]
    return [token.strip().strip("',\"")
            for token in body.split()
            if token.strip().strip("',\"") .startswith("Nesting_")]


def check_the_command_is_registered():
    emit("")
    emit("-- 2. the command is registered and describes itself --")
    try:
        from freecad.nestingworkbench.commands import command_audit_lead_ins
    except Exception:
        check(False, "the command module imports:\n%s"
              % traceback.format_exc())
        return None
    command = command_audit_lead_ins.AuditLeadInsCommand()
    check(command is not None, "the command instantiates")
    resources = command.GetResources()
    for key in ("Pixmap", "MenuText", "ToolTip"):
        check(bool(resources.get(key)), "GetResources has a %s (%r)"
              % (key, resources.get(key)))
    return command


def check_the_dialog_builds(command):
    """`_ask` builds a QMessageBox; broken dialog code must fail here."""
    emit("")
    emit("-- 4. the report-or-fix dialog builds --")
    box = None
    try:
        original = QtWidgets.QMessageBox.exec_

        def _no_wait(self, *args, **kwargs):
            # Close rather than wait: this is a construction check, and exec_()
            # on a modal box would block the harness forever.
            return 0

        QtWidgets.QMessageBox.exec_ = _no_wait
        try:
            box = command._ask()
        finally:
            QtWidgets.QMessageBox.exec_ = original
    except Exception:
        check(False, "_ask() raised:\n%s" % traceback.format_exc())
        return
    check(True, "_ask() built and ran the dialog")
    # With exec_ stubbed to return 0 the box reports no button was clicked, so
    # `_ask` must decline rather than default to a mode -- cancelling must not
    # silently become "fix the user's job".
    check(box is None,
          "a closed dialog returns None rather than defaulting to a mode "
          "(got %r)" % (box,))


def close_everything():
    """Discard every document, then shut the GUI down. **In that order.**

    **Documents first.** Measured on this binary, four minimal scripts, rc taken
    from FreeCAD itself rather than from a pipeline:

        closeDocument, then window.close()   rc 0
        window.close(), then closeDocument   rc 124 -- hangs

    So `closeDocument` before `getMainWindow().close()` is not tidiness, it is
    what lets the session end. Closing the window first hangs, and a gate that
    hangs is worse than one that fails.

    `FreeCAD.closeDocument` also **discards**: it never prompts, so the Unsaved
    Document dialogue does not appear and nothing is written. That is why the
    test document is closed explicitly rather than left for the exit path, and
    why nothing here calls `doc.save()`.

    **Then the window.** Ending the script with `sys.exit()` alone leaves the
    main window on screen: the process is gone but the window still has to be
    dismissed by hand. `getMainWindow().close()` with `QApplication.quit()` ends
    the session by itself.

    Both halves are `probe_target_sheets.py`'s, which documents them; this is
    that function, copied rather than shared because a harness that imports
    another one re-executes it (`harness_common.py`'s module docstring explains
    why).

    **One piece of shutdown noise is expected here and is not a failure.** A CAM
    job registers a tool bar whose teardown reaches for its view provider, and
    by then the job is gone:

        ReferenceError: Cannot access attribute 'ViewObject' of deleted object
        in <built-in method hide of PySide6.QtWidgets.QToolBar>

    It arrives after the checks and after the status file is written, and the
    exit code is still 0. It cannot be avoided by reordering -- reordering is
    what causes the hang -- so it is left alone and named here so nobody reads a
    green gate plus a traceback as a broken one.
    """
    for name in list(FreeCAD.listDocuments()):
        try:
            FreeCAD.closeDocument(name)
        except Exception as exc:
            emit("  note: closing %s raised %s" % (name, exc))
    try:
        FreeCADGui.getMainWindow().close()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.quit()
    except Exception as exc:
        emit("  note: GUI close raised %s" % exc)


def check_the_progress_panel_builds():
    """The panel is the whole reason a long search is usable. It must build.

    On the GUI binary, because it is Qt. Nothing else gated constructs it: the
    audit harness runs under `freecadcmd` and drives the seam through a plain
    function, so a widget that cannot be constructed is invisible to it -- which
    is the same reason `test_panel_construction.py` exists for the nesting panel.
    """
    emit("")
    emit("-- 6. the progress panel builds and draws --")
    from freecad.nestingworkbench.Tools.Cam import lead_in_progress
    try:
        widget = lead_in_progress.LeadInTaskWidget()
    except Exception:
        check(False, "LeadInTaskWidget builds:\n%s" % traceback.format_exc())
        return None
    check(widget is not None, "LeadInTaskWidget builds")

    # Drive the seam the way the search does -- both call shapes -- and read the
    # widgets back, so the panel is asserted to *display* rather than merely to
    # accept the calls.
    widget.view.subject = "Job_Replay_Sheet_1"
    widget.view.total_operations = 5
    widget.callback(operation_index=1, operation_total=5, label="part_A")
    widget.callback(candidates=3, candidate_total=48)
    headline = widget._headline.text()
    detail = widget._detail.text()
    emit("  headline: %s" % headline)
    emit("  detail:   %s" % detail)
    check("Job_Replay_Sheet_1" in headline, "the headline names the job")
    check("candidate 3 of 48" in detail,
          "the detail says how far through the search it is")
    check("operation 1 of 5" in detail, "and which operation")
    maximum, value = widget._bar.maximum(), widget._bar.value()
    check((maximum, value) == (48, 3),
          "the bar is at 3 of 48 (got %s of %s)" % (value, maximum))

    # A report-only run must not read as though it changed anything.
    widget.view.dry_run = True
    widget.callback(operation_index=2, operation_total=5, label="part_B")
    widget.callback(candidates=1, candidate_total=48, outcome="fixable")
    check("report only" in widget._headline.text(),
          "a report-only panel says so")
    check("could be fixed" in widget._tally.text(),
          "and its tally says 'could be fixed' (%s)" % widget._tally.text())
    return widget


def check_cancel_stops_the_panel():
    """The Cancel button, and closing the panel, both stop the search.

    Both paths, because they are different code: the button calls the widget,
    and FreeCAD's own "the user closed the panel" hook goes through
    `LeadInTaskProgress.reject`.
    """
    emit("")
    emit("-- 7. Cancel stops the search --")
    from freecad.nestingworkbench.Tools.Cam import lead_in_progress

    widget = lead_in_progress.LeadInTaskWidget()
    widget.callback(operation_index=1, operation_total=5, label="part_A")
    check(widget.cancelled is False, "not cancelled to begin with")
    widget._on_cancel()
    check(widget.cancelled is True, "the button cancels it")
    check(widget._cancel.isEnabled() is False,
          "and disables itself, rather than looking like a dead button")
    check("Cancelling" in widget._cancel.text(),
          "and says 'Cancelling' (%s)" % widget._cancel.text())
    check("Cancelling" in widget._detail.text(),
          "the detail line says so too")

    # A cancel is latched: a later event must not clear it.
    widget.callback(candidates=9, candidate_total=48)
    check(widget.cancelled is True, "and a later event does not un-cancel it")

    # `reject` is FreeCAD's "the panel was closed", which is also a cancel.
    task = lead_in_progress.LeadInTaskProgress()
    task.widget = lead_in_progress.LeadInTaskWidget()
    task.reject()
    check(task.cancelled is True,
          "closing the panel by hand is also a cancel")

    # And a torn-down widget reads as "not cancelled" rather than raising, since
    # the poll happens while Qt may be destroying it.
    task2 = lead_in_progress.LeadInTaskProgress()
    task2.widget = None
    check(task2.cancelled is False,
          "a panel that has gone reads as not cancelled, rather than raising")


def check_the_command_opens_the_panel():
    """The command wires the panel in, and the cancel through correctly.

    `task.cancelled` is a property, so passing it as `cancel_check=lambda:
    task.cancelled` is right and passing it bare would hand the engine the bool
    it happened to be when the lambda was built -- False, for the whole run.
    That is not hypothetical: it is what the replay's Cancel did.
    """
    emit("")
    emit("-- 8. the command wires the panel in --")
    source = _read_command_source()
    if not check(source is not None, "the command source is readable"):
        return
    for needle, why in (
            ("LeadInTaskProgress", "uses the progress panel"),
            ("cancel_check=lambda: task.cancelled",
             "polls `cancelled` through a lambda, not by value"),
            ("commitTransaction", "commits the transaction"),
            ("dry_run=dry_run", "passes the mode through")):
        check(needle in source, why)


def _read_command_source():
    path = os.path.join(_REPO, "freecad", "nestingworkbench", "commands",
                        "command_audit_lead_ins.py")
    try:
        with open(path) as handle:
            return handle.read()
    except Exception:
        return None


def check_is_active_follows_selection(command):
    emit("")
    emit("-- 5. IsActive follows the selection --")
    doc = FreeCAD.newDocument("lead_in_cmd_check")
    try:
        FreeCADGui.Selection.clearSelection()
        check(command.IsActive() is False,
              "inactive with nothing selected")

        from freecad.nestingworkbench.Tools.Cam import cam_replay

        # A real job object, so `is_cam_job` answers the question it is asked
        # rather than being handed a stand-in that trivially passes.
        job = cam_replay_path_job(doc)
        if job is None:
            check(False,
                  "a CAM job could be built, so IsActive could be checked")
            return

        FreeCADGui.Selection.clearSelection()
        FreeCADGui.Selection.addSelection(doc.Name, job.Name)
        check(command.IsActive() is True,
              "active with one CAM job selected")

        other = doc.addObject("Part::Feature", "not_a_job")
        FreeCADGui.Selection.clearSelection()
        FreeCADGui.Selection.addSelection(doc.Name, job.Name)
        FreeCADGui.Selection.addSelection(doc.Name, other.Name)
        check(command.IsActive() is False,
              "inactive with two objects selected -- two recipes is ambiguous")
    finally:
        try:
            FreeCADGui.Selection.clearSelection()
        except Exception:
            pass
        # The document is left open for `close_everything`, which owns the
        # shutdown order. Closing it here as well would be harmless but would
        # mean two places that have to stay in step.


def cam_replay_path_job(doc):
    """A real CAM job, or None.

    Built through `Path.Main.Job.Create` rather than by hand, so `is_cam_job` is
    asked the question it actually answers -- it checks for `Operations`,
    `Model` and `Stock` on a `Path::Feature`, not for a proxy class. An earlier
    version made a `Path::FeaturePython` and called it a job; `is_cam_job` said
    no, which was the right answer and left the selection checks unrun.
    """
    from freecad.nestingworkbench.Tools.Cam import cam_replay
    import Part

    box = doc.addObject("Part::Box", "Box")
    box.Length = 10.0
    box.Width = 10.0
    box.Height = 2.0
    doc.recompute()
    try:
        job = PathJobCreate([box])
    except Exception:
        emit("  (PathJob.Create failed: %s)" % traceback.format_exc())
        return None
    if not cam_replay.is_cam_job(job):
        emit("  (the created job does not satisfy is_cam_job)")
        return None
    return job


def PathJobCreate(models):
    """Build a CAM job, the way `cam_manager` does.

    The import is `Path.Main.Gui.Job`, **not** `Path.Main.Job.Gui.Job`. The
    first attempt guessed the latter and got
    `ModuleNotFoundError: No module named 'Path.Main.Job.Gui'; 'Path.Main.Job'
    is not a package` -- `Path/Main/Job.py` is a module, so it has no
    submodules. `cam_manager.py:189` has the correct spelling.
    """
    from Path.Main.Gui import Job as PathJobGui
    return PathJobGui.Create(models, None, openTaskPanel=False)


def main():
    emit("lead-in check command check")
    check_the_workbench_loads()
    command = check_the_command_is_registered()
    check_menu_lists_it_without_the_toolbar()
    if command is not None:
        check_the_dialog_builds(command)
        check_is_active_follows_selection(command)
    check_the_progress_panel_builds()
    check_cancel_stops_the_panel()
    check_the_command_opens_the_panel()

    emit("")
    emit("-- summary --")
    if _failures:
        emit("%d of %d checks FAILED:" % (len(_failures), _checks[0]))
        for message in _failures:
            emit("   - %s" % message)
    else:
        emit("all %d checks passed" % _checks[0])
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write("1" if _failures else "0")
    except Exception:
        pass
    return 1 if _failures else 0


if __name__ in ("__main__", "test_command_audit_lead_ins"):
    _exit = 1
    try:
        _exit = main()
    except Exception:
        emit("harness raised:")
        for line in traceback.format_exc().split("\n"):
            emit("   %s" % line)
        try:
            with open(_STATUS_FILE, "w") as handle:
                handle.write("1")
        except Exception:
            pass
    # **Every exit path shuts the GUI down.** This one needs it because it builds
    # a document and a CAM job: both are modified, so leaving them for the exit
    # path raises the Unsaved Document dialogue, which blocks the session until
    # somebody clicks it. `close_everything` closes the documents before the
    # window, for the reason documented there.
    #
    # In the `finally`, so an exception above still cleans up: the failure is
    # already recorded in the status file, and the point of the gate is that a
    # red run ends rather than hanging.
    finally:
        close_everything()
    raise SystemExit(_exit)
