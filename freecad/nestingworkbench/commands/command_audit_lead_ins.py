# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Check a replayed nest's lead-ins and lead-outs against their neighbours.

`Nesting_ReplayCAMSetup` applies the user's own CAM recipe to every part a
nester laid out, one operation per part. It verifies that each operation cuts
something and reaches its targets. It does not look at where the lead-ins land.

A `LeadInOut` dressup extends its operation's cut path outward by `RadiusIn`
before the cut starts and `RadiusOut` after it ends. Those extensions are drawn
on the sheet, among the neighbours the nester placed, and nothing accounts for
them. When one reaches into the space another operation cuts, the tool re-cuts
material that is not its own part's -- or, on a plasma or waterjet, pierces on a
neighbour's contour.

This command finds those, and moves the offending operation's start point until
they clear.

What it changes, and what it does not
-------------------------------------

**Only `StartPoint`, and `UseStartPoint` so that `StartPoint` is read at all.**

`StyleIn`, `StyleOut`, `AngleIn`, `AngleOut`, `RadiusIn`, `RadiusOut`,
`InvertIn`, `InvertOut`, `ExtendIn` and `ExtendOut` are the user's craft and are
never touched. They are scalars or a handedness that survive the rigid motion
the replay applies, and they are the difference between a lead-in that suits a
cut and one that does not.

`StartPoint` is the single exception, and it is the only reason a fix is
possible: it is the one property in a CAM recipe that is an absolute coordinate.
Everything else describes the cut; this names a place on the part. Under a part
the nester rotated, the source value names somewhere the part no longer is. It
is not a value the user got wrong -- it is the one value nesting made
meaningless, and the only correct one is a function of where the neighbours
ended up, which does not exist until after the nest.

Measured, because it is the least obvious thing here: with `UseStartPoint`
False, `StartPoint` is **not read at all**. On the fixture's worst offender,
setting it to the part's XMinYMin corner and then its XMaxYMax corner produced
bit-identical toolpaths and the lead-in did not move.

Two things it will not do
-------------------------

**It does not change the source job.** The fix lands on the replayed job only,
so re-running the replay discards it. That is deliberate: writing back would
mutate the hand-crafted recipe this whole feature exists to preserve, over a
nesting that may itself be re-run.

**It does not fix what it cannot fix, and it says so.** An operation whose every
candidate start point still conflicts -- or whose lead-in and lead-out cannot
both be cleared, which is checked together because one start point places both --
is marked `CONFLICT_` and left exactly as it was. The word is not `SKIP_`: the
dressup is not excluded from the job, it will still cut, and it will cut through
whatever it was cutting through.

The report-only mode exists for the same reason. Run it first: it writes nothing
at all, and on a real nest it is how you find out whether there is anything here
before letting anything move.
"""
import FreeCAD
import FreeCADGui
from PySide import QtWidgets

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam import lead_in_audit
from freecad.nestingworkbench.Tools.Cam import lead_in_progress


class AuditLeadInsCommand:
    """Audit a replayed job's lead-ins, and move the ones that reach a neighbour."""

    def GetResources(self):
        return {
            "Pixmap": "Nesting_Replay_Icon.svg",
            "MenuText": "Check Lead-ins Against Nesting",
            "ToolTip": (
                "Find the lead-ins and lead-outs of a replayed CAM job that "
                "reach another part's cut path, and move their start points "
                "until they clear."
            ),
        }

    # -- selection ------------------------------------------------------

    @staticmethod
    def _selected_job():
        """The selected CAM job, or None. One only, as with the replay."""
        selection = FreeCADGui.Selection.getSelection()
        if len(selection) != 1:
            return None
        return selection[0] if cam_replay.is_cam_job(selection[0]) else None

    def IsActive(self):
        if not FreeCAD.ActiveDocument:
            return False
        return self._selected_job() is not None

    # -- the run --------------------------------------------------------

    def Activated(self):
        job = self._selected_job()
        if job is None:
            FreeCAD.Console.PrintError(
                "Select one replayed CAM job.\n")
            return

        mode = self._ask()
        if mode is None:
            return
        dry_run = (mode == "report")

        try:
            result = lead_in_audit.scan_job(job)
        except Exception as exc:
            FreeCAD.Console.PrintError("Lead-in check failed: %s\n" % exc)
            import traceback
            traceback.print_exc()
            return

        if not result.offending:
            # Nothing to search for, so no panel. Opening a progress display for
            # work that is already done is worse than not opening one: it flashes
            # up and vanishes, and the user is left unsure whether it ran.
            self._report(result, dry_run)
            return

        # The panel spans the search, not the scan. The scan is ~150 ms over 110
        # operations and needs no feedback; the search is a recompute per
        # candidate and does.
        todo = len(result.offending)
        try:
            with lead_in_progress.LeadInTaskProgress(
                    subject=job.Label, total_operations=todo,
                    dry_run=dry_run) as task:
                self._run(result, task, dry_run)
        except Exception as exc:
            FreeCAD.Console.PrintError("Lead-in check failed: %s\n" % exc)
            import traceback
            traceback.print_exc()
            return
        self._report(result, dry_run)

    def _run(self, result, task, dry_run):
        """Search for start points, under the panel, in one transaction.

        **One transaction for the whole run, committed even on Cancel.** Each
        fix is applied and verified on its own, so cancelling leaves the job in
        a consistent state -- some operations moved, the rest untouched -- and
        that is worth keeping. Rolling back would throw away fixes that were
        checked and correct because the user got bored eight operations later.
        Aborting is for the case where the run *failed*, where the document is
        in a state nobody asked for.
        """
        name = "Check lead-ins against nesting"
        opened = False
        try:
            FreeCAD.ActiveDocument.openTransaction(name)
            opened = True
        except Exception:
            opened = False

        try:
            lead_in_audit.resolve_conflicts(
                result, dry_run=dry_run,
                progress=task.callback,
                # `task.cancelled` is a property, so it has to be wrapped. Naming
                # it here would evaluate it *now* and hand the engine the bool it
                # happened to be -- False -- for the whole run, which is how the
                # replay's Cancel came to do nothing.
                cancel_check=lambda: task.cancelled)
        except Exception:
            if opened:
                try:
                    FreeCAD.ActiveDocument.abortTransaction()
                except Exception:
                    pass
            raise

        if opened:
            try:
                FreeCAD.ActiveDocument.commitTransaction()
            except Exception:
                pass

    def _ask(self):
        """`"report"` or `"fix"`, or None if cancelled.

        Report-only is the first button and the default, because it writes
        nothing and it answers the question the user actually has -- is there
        anything here -- before anything on their job moves.
        """
        box = QtWidgets.QMessageBox(FreeCADGui.getMainWindow())
        box.setWindowTitle("Check lead-ins against nesting")
        box.setText(
            "Check whether this job's lead-ins and lead-outs reach another "
            "part's cut path?"
        )
        box.setInformativeText(
            "A lead-in extends outside its own part, and the nester placed "
            "that part among others. Nothing in the replay accounts for where "
            "it lands.\n\n"
            "Report only writes nothing. Fix moves the offending operations' "
            "start points -- and nothing else -- until they clear. Anything "
            "it cannot fix is marked CONFLICT_ and left alone."
        )
        report = box.addButton("Report only", QtWidgets.QMessageBox.AcceptRole)
        fix = box.addButton("Report and fix", QtWidgets.QMessageBox.ApplyRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(report)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is report:
            return "report"
        if clicked is fix:
            return "fix"
        return None

    def _report(self, result, dry_run):
        lines = list(lead_in_audit.describe_result(result, dry_run=dry_run))
        text = "\n".join(lines)
        if result.unresolved or result.unreadable:
            FreeCAD.Console.PrintError(text + "\n")
        else:
            FreeCAD.Console.PrintMessage(text + "\n")

        fixable = len(result.fixable)
        unresolved = len(result.unresolved)
        unreadable = len(result.unreadable)
        not_reached = len(result.not_reached)

        # A fixable conflict is **not** a failure. It is one click away and the
        # user is being told about it before they commit to anything, so raising
        # a warning box for it says the job is broken when it is not -- and warns
        # on exactly the run whose whole purpose was to find out whether there
        # was anything here.
        if result.cancelled:
            self._cancelled_box(lines[0], fixable, not_reached)
        elif unresolved or unreadable:
            body = ["%d operation%s need doing by hand and %d could not be "
                    "checked."
                    % (unresolved, "" if unresolved == 1 else "s", unreadable)]
            if fixable:
                body.append("")
                body.append("Another %d could be moved to a clear start point "
                            "by running 'Report and fix'."
                            % fixable)
            body.append("")
            if dry_run:
                body.append("Nothing has been changed. These need your "
                            "judgement about the cut itself.")
            else:
                body.append("They are marked %s in the tree. Their start "
                            "points need setting by hand -- do not post this "
                            "job until they are resolved."
                            % lead_in_audit.CONFLICT_PREFIX)
            QtWidgets.QMessageBox.warning(
                FreeCADGui.getMainWindow(),
                "Some lead-ins need your judgement",
                "\n".join(body)
            )
        elif fixable:
            QtWidgets.QMessageBox.information(
                FreeCADGui.getMainWindow(),
                "Lead-ins reach another part"
                if dry_run else "Lead-ins were moved",
                "%s" % lines[0]
            )
        else:
            QtWidgets.QMessageBox.information(
                FreeCADGui.getMainWindow(),
                "Lead-ins are clear",
                "%s" % lines[0]
            )

    def _cancelled_box(self, headline, fixable, not_reached):
        """A cancel is a warning, but not the "do not post this" warning.

        The operations that *were* searched are in a known state -- moved or
        marked -- so the job is no worse than before the run. The ones not
        reached are unknown, and saying "do not post" about a job the user
        merely stopped looking at would be crying wolf.
        """
        body = ["%s" % headline, ""]
        if fixable:
            body.append("%d start point%s already moved and checked."
                        % (fixable, "" if fixable == 1 else "s"))
        if not_reached:
            body.append("%d operation%s not looked at. Run this again to "
                        "check them."
                        % (not_reached, "" if not_reached == 1 else "s"))
        QtWidgets.QMessageBox.warning(
            FreeCADGui.getMainWindow(),
            "Lead-in check cancelled",
            "\n".join(body)
        )


if FreeCAD.GuiUp:
    FreeCADGui.addCommand('Nesting_AuditLeadIns',
                          AuditLeadInsCommand())
