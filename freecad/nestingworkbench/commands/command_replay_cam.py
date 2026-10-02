# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Apply the user's own CAM setup to a nested layout.

A user sets up CAM on their source parts in the CAM workbench: Profile and
Drilling operations, LeadInOut dressups, per-feature settings. This command
takes that job and replays it onto the parts a nesting run has laid out on a
sheet, so the resulting G-code is the user's own machining recipe applied to
the nest rather than a template's.

It sits alongside `Nesting_CreateCAMJob` rather than sharing code with it.
That is deliberate: that command is someone else's work and coupling the two
means every change here is a change to it. The only thing reused is
`freecad_helpers`, which is shared infrastructure rather than the CAM author's
own module.

The work is in `Tools/Cam/cam_replay.py`. This file runs the pipeline and
reports.

**There is no options dialog, and there was one.** It asked for a CAM template
and a post processor. Both answers are already in the job the user selected:
the template exists to set up machine, post and tool, and the post processor is
derived by FreeCAD from the job's `Machine`. Asking again cannot add
information, and it did add an error -- the dialog listed post processors from
`Path.Preferences.allEnabledPostProcessors()` while FreeCAD's own Job builds its
`PostProcessor` enumeration from `allEnabledLegacyPostProcessors()`, and the
two lists differ. The user's `monokrom_plasma` is in the first and not the
second, so the dialog offered a value the new job then refused:

    Could not set post processor 'monokrom_plasma' on CAM_Replay_Sheet_1

Underneath that warning the job's `Machine` was not being copied at all --
`'Origarmi Plasma'` in the source, `''` in the replay -- and nothing said so.

Two behaviours worth knowing before using it:

  * **A sheet that fails verification is kept, not deleted.** An operator
    investigating a bad run needs the job still there, so it is left in the
    document with `_UNVERIFIED` appended to its label and a loud message in the
    report. Nothing should be posted from a job so labelled.

  * **Every sheet is replayed, one job per sheet.** A multi-sheet nest produces
    a separate job for each, and a sheet that fails does not stop the others.
    They are independent jobs, and one bad sheet should not hide a good one.
"""
import time

import FreeCAD
import FreeCADGui
from PySide import QtWidgets

from freecad.nestingworkbench.Tools.Cam import cam_replay


class ReplayCAMSetupCommand:
    """Replay the selected CAM job's operations onto a nested layout."""

    def GetResources(self):
        return {
            # Its own icon, not the CNC one. The two commands sit next to each
            # other in both the menu and the toolbar, and sharing
            # `Nesting_CNC_Icon.svg` made them indistinguishable at a glance.
            #
            # `Nesting_Replay_Icon` is a replay ring around a 2x2 nest of parts,
            # one of them the source. It follows the house spec exactly -- a 48
            # unit grid, axis-aligned rectangles only, one `<path>` per colour,
            # no curves or strokes -- and its palette is Tango taken from the
            # CAM workbench's own icons rather than the bespoke ramps the other
            # six use: #ffffff, #fff110, #cf7008, #8f5902, #8ae234, #73d216,
            # #4e9a06, #2e3436. Each is present in at least two of the 125
            # icons in FreeCAD's `src/Mod/CAM/Gui/Resources/icons`.
            #
            # It survives 16px, which matters because FreeCAD scales a 48-unit
            # grid down to the toolbar and a 2-unit stroke is already one
            # device pixel there.
            "Pixmap": "Nesting_Replay_Icon.svg",
            "MenuText": "Replay CAM Setup onto Nesting",
            "ToolTip": (
                "Apply the operations and dressups from the selected CAM job "
                "to a nested layout, one job per sheet."
            ),
        }

    # -- selection ------------------------------------------------------

    def _selected_job(self):
        """Return the selected CAM job, or None.

        Only a single selection is considered. Two jobs selected is ambiguous
        -- which recipe should be applied to the nest? -- and guessing would
        mean cutting the wrong features, so it is refused.
        """
        selection = FreeCADGui.Selection.getSelection()
        if not selection:
            return None
        if len(selection) > 1:
            return None
        return selection[0] if cam_replay.is_cam_job(selection[0]) else None

    def IsActive(self):
        if not FreeCAD.ActiveDocument:
            return False
        selection = FreeCADGui.Selection.getSelection()
        if len(selection) != 1:
            return False
        return cam_replay.is_cam_job(selection[0])

    # -- the run --------------------------------------------------------

    def Activated(self):
        doc = FreeCAD.ActiveDocument
        if doc is None:
            FreeCAD.Console.PrintError("No active document.\n")
            return

        selection = FreeCADGui.Selection.getSelection()
        if len(selection) > 1:
            FreeCAD.Console.PrintError(
                "Select one CAM job. %d objects are selected; which recipe "
                "should be applied to the nest is ambiguous.\n" % len(selection)
            )
            return

        source_job = self._selected_job()
        if source_job is None:
            FreeCAD.Console.PrintError(
                "Select the CAM job whose operations should be replayed.\n"
            )
            return

        layout_group, layout_warnings = cam_replay.resolve_layout_group(doc)
        for warning in layout_warnings:
            FreeCAD.Console.PrintWarning("%s\n" % warning)
        if layout_group is None:
            FreeCAD.Console.PrintError(
                "No nesting layout found in this document. Run nesting first.\n"
            )
            return

        from freecad.nestingworkbench.freecad_helpers import get_sheet_groups
        sheets = get_sheet_groups(layout_group)
        if not sheets:
            FreeCAD.Console.PrintError(
                "Layout '%s' has no sheets. Run nesting first.\n"
                % layout_group.Label
            )
            return

        # No options. Everything the replay needs is in the job the user
        # selected and the layout in front of them; see the module docstring
        # for what the dialog used to ask and why it was removed.
        #
        # The progress display is opened per sheet rather than once for the
        # run, because `replay_layout` reports one sheet at a time and a
        # panel that kept the first sheet's label through the second would be
        # describing the wrong sheet. The seam is the same either way.
        #
        # `ReplayTaskProgress` closes itself on the way out of every path,
        # including an exception. A task dialog left open keeps FreeCAD's
        # document un-editable until something closes it, which is a poor way
        # to end a session because one replay went wrong.
        #
        # Timed here, at the one place that spans the whole run. `Progress`
        # banks each stage's seconds as it goes, so this is only a subtraction
        # at the end -- but it has to be at this level, because a per-sheet
        # number cannot be compared against a whole-run one.
        from freecad.nestingworkbench.Tools.Cam.replay_progress import (
            ReplayTaskProgress,
        )

        started = time.perf_counter()
        outcomes = []
        cancelled_at = None
        try:
            for position, sheet in enumerate(sheets):
                with ReplayTaskProgress(sheet.Label) as task:
                    outcomes.extend(cam_replay.replay_layout(
                        doc, layout_group, source_job,
                        sheets=(sheet,),
                        progress_callback=task.callback,
                        # A lambda, not `task.cancelled`. `cancelled` is a
                        # property, so naming it here would evaluate it *now*
                        # and hand `replay_layout` the bool it happened to be --
                        # False. `Progress` would then call that False, get
                        # `TypeError: 'bool' object is not callable`, and
                        # disable cancelling for the entire run. Which is
                        # exactly what happened: the console said "cancel check
                        # raised; cancelling is disabled" and the button would
                        # have done nothing.
                        cancel_check=lambda: task.cancelled))
                if task.cancelled:
                    # Cancelling stops the run, not the sheet. Carrying on would
                    # open a panel for the next sheet and start working on it
                    # while the user is still looking at the Cancel button they
                    # just pressed.
                    cancelled_at = position
                    break
        except Exception as exc:
            FreeCAD.Console.PrintError("Replay failed: %s\n" % exc)
            import traceback
            traceback.print_exc()
            return
        wall_clock = time.perf_counter() - started

        self._report(layout_group, source_job, outcomes, wall_clock,
                     cancelled_at=cancelled_at,
                     skipped=[s.Label for s in sheets[cancelled_at + 1:]]
                     if cancelled_at is not None else [])

    def _report(self, layout_group, source_job, outcomes, wall_clock=None,
                cancelled_at=None, skipped=()):
        """Print the whole run to the Report view and raise a dialog for it.

        Console output rather than `FreeCADGui.ReportView`, because Console
        already reaches the Report view when a GUI is up and also works under
        `freecadcmd`, so the same reporting path is exercised by the harness.
        """
        lines = []
        for outcome in outcomes:
            lines.extend(cam_replay.describe_sheet_outcome(outcome))

        failed = [o for o in outcomes if not o.ok]
        unverified = [o for o in outcomes
                      if o.replay_job is not None
                      and o.verification is not None and not o.verification.ok]

        if skipped:
            lines.append("")
            lines.append(
                "Cancelled. %d sheet(s) were not replayed at all: %s."
                % (len(skipped), ", ".join(skipped))
            )
        if unverified:
            lines.append("")
            lines.append(
                "%d job(s) FAILED verification and are labelled %s. They are "
                "in the document so you can look at them. Do not post from "
                "them." % (len(unverified), cam_replay.UNVERIFIED_SUFFIX)
            )
        if not failed:
            lines.append("")
            lines.append(
                "Replayed '%s' onto %d sheet(s) of '%s'."
                % (source_job.Label, len(outcomes), layout_group.Label)
            )

        # Always in the report, never behind a preference. The figures that got
        # this feature from 43.5s to 10.6s were all measured this way, and a
        # timing nobody can see is a timing nobody re-measures after a change
        # makes it worse.
        if wall_clock is not None:
            timed = sum(o.timed_seconds for o in outcomes)
            lines.append("")
            lines.extend(cam_replay.describe_timings(
                [row for o in outcomes for row in o.timings],
                timed_seconds=timed, wall_clock=wall_clock))

        text = "\n".join(lines)
        if failed:
            FreeCAD.Console.PrintError(text + "\n")
        else:
            FreeCAD.Console.PrintMessage(text + "\n")

        # A modal summary, because a failure that scrolls past in the Report
        # view is not much of a failure.
        if failed:
            QtWidgets.QMessageBox.critical(
                FreeCADGui.getMainWindow(),
                "CAM Replay cancelled" if cancelled_at is not None
                else "CAM Replay failed",
                ("You cancelled the replay, so what was built is incomplete.%s\n\n"
                 if cancelled_at is not None else "") +
                "%d of %d sheet(s) did not replay cleanly.\n\n"
                "Jobs that failed verification are kept in the document, "
                "labelled %s, so you can inspect them. Do not post from them.\n\n"
                "See the Report view for detail."
                % ("\n\n%d sheet(s) were not started." % len(skipped)
                   if skipped else "",
                   len(failed), len(outcomes) + len(skipped),
                   cam_replay.UNVERIFIED_SUFFIX)
            )
        else:
            QtWidgets.QMessageBox.information(
                FreeCADGui.getMainWindow(),
                "CAM Replay complete",
                "Applied '%s' to %d sheet(s).\n\n"
                "Check the Report view for details."
                % (source_job.Label, len(outcomes))
            )


if FreeCAD.GuiUp:
    FreeCADGui.addCommand('Nesting_ReplayCAMSetup', ReplayCAMSetupCommand())
