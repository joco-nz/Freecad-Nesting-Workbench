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

The work is in `Tools/Cam/cam_replay.py`. This file only chooses options,
runs the pipeline, and reports.

Two behaviours worth knowing before using it:

  * **A sheet that fails verification is kept, not deleted.** An operator
    investigating a bad run needs the job still there, so it is left in the
    document with `_UNVERIFIED` appended to its label and a loud message in the
    report. Nothing should be posted from a job so labelled.

  * **Every sheet is replayed, one job per sheet.** A multi-sheet nest produces
    a separate job for each, and a sheet that fails does not stop the others.
    They are independent jobs, and one bad sheet should not hide a good one.
"""
import FreeCAD
import FreeCADGui
import os
from PySide import QtWidgets, QtCore

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.constants import PREFS_PATH


class ReplayOptionsDialog(QtWidgets.QDialog):
    """Options for the replay. Deliberately thin.

    Everything the replay needs beyond these -- which parts, which operations,
    in what order -- comes from the user's own CAM job and the nesting layout.
    Asking for it again would be asking the user to state something they have
    already stated, and the two answers could disagree.
    """

    def __init__(self, source_job=None, sheet_count=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Replay CAM Setup onto Nesting")
        self.setMinimumWidth(340)

        layout = QtWidgets.QVBoxLayout(self)

        # What is about to happen. Worth stating plainly, because the command
        # acts on a job the user may have spent a long time setting up.
        intro = QtWidgets.QLabel(
            "Take the operations from\n"
            "<b>%s</b>\n\n"
            "and apply them to %d sheet(s) of the nested layout. "
            "A separate CAM job is created per sheet."
            % (source_job.Label if source_job else "the selected job", sheet_count)
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addSpacing(6)

        # Template, for the new job's machine / post / tool setup.
        layout.addWidget(QtWidgets.QLabel("CAM Template (optional):"))
        template_row = QtWidgets.QHBoxLayout()
        self.template_combo = QtWidgets.QComboBox()
        self.template_combo.addItem("None", None)
        self.template_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._populate_templates()
        self.browse_button = QtWidgets.QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse_template)
        template_row.addWidget(self.template_combo)
        template_row.addWidget(self.browse_button)
        layout.addLayout(template_row)

        layout.addWidget(QtWidgets.QLabel("Post Processor:"))
        self.post_processor_combo = QtWidgets.QComboBox()
        self._populate_post_processors()
        layout.addWidget(self.post_processor_combo)

        self._load_last_template()

        layout.addSpacing(6)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- template and post processor discovery --------------------------
    #
    # Copied in shape from the existing CAM command's dialog, because the
    # search paths are FreeCAD's and have to be searched the same way. The
    # lookup itself is duplicated rather than shared, for the same reason the
    # command is: shared code is shared maintenance.

    def _populate_templates(self):
        paths = [
            os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "CAM", "Templates"),
            os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "Path", "Templates"),
            os.path.join(FreeCAD.getHomePath(), "Mod", "CAM", "Templates"),
            os.path.join(FreeCAD.getHomePath(), "Mod", "Path", "Templates"),
            os.path.join(FreeCAD.getHomePath(), "data", "Mod", "CAM", "Templates"),
            os.path.join(FreeCAD.getHomePath(), "data", "Mod", "Path", "Templates"),
        ]

        custom_path = FreeCAD.ParamGet(
            "User parameter:BaseApp/Preferences/Path/Job").GetString("Template", "")
        if custom_path and os.path.isdir(custom_path):
            paths.insert(0, custom_path)

        # FreeCAD 1.1 CamAssets: directly under the user app data dir on
        # Linux, under a versioned subdir on Windows.
        for candidate in (
            os.path.join(FreeCAD.getUserAppDataDir(), "CamAssets", "Templates"),
            os.path.join(FreeCAD.getUserAppDataDir(), "v1-1", "CamAssets", "Templates"),
        ):
            if os.path.isdir(candidate):
                paths.insert(0, candidate)

        seen = set()
        for path in paths:
            if not os.path.isdir(path):
                continue
            try:
                names = os.listdir(path)
            except OSError:
                continue
            for name in sorted(names):
                if not name.lower().endswith(".json"):
                    continue
                full = os.path.join(path, name)
                if full not in seen:
                    seen.add(full)
                    self.template_combo.addItem(name, full)

    def browse_template(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select CAM Job Template",
            FreeCAD.getUserAppDataDir(), "JSON Files (*.json);;All Files (*)")
        if not filename:
            return
        index = self.template_combo.findData(filename)
        if index == -1:
            self.template_combo.addItem(os.path.basename(filename), filename)
            index = self.template_combo.count() - 1
        self.template_combo.setCurrentIndex(index)

    def _populate_post_processors(self):
        processors = []
        try:
            import Path.Preferences
            processors = sorted(Path.Preferences.allEnabledPostProcessors())
        except Exception:
            # CAM not loaded, or no processors configured. The stored choice
            # still goes in the combo so a preference is not lost.
            pass
        for name in processors:
            self.post_processor_combo.addItem(name)

        prefs = FreeCAD.ParamGet(PREFS_PATH)
        last = prefs.GetString("LastCAMPostProcessor", "grbl")
        index = self.post_processor_combo.findText(last)
        if index != -1:
            self.post_processor_combo.setCurrentIndex(index)
        elif not processors:
            self.post_processor_combo.addItem(last)

    def _load_last_template(self):
        prefs = FreeCAD.ParamGet(PREFS_PATH)
        last = prefs.GetString("LastCAMTemplate", "")
        if not last or not os.path.exists(last):
            return
        index = self.template_combo.findData(last)
        if index == -1:
            self.template_combo.addItem(os.path.basename(last), last)
            index = self.template_combo.count() - 1
        self.template_combo.setCurrentIndex(index)

    # -- results --------------------------------------------------------

    def accept(self):
        prefs = FreeCAD.ParamGet(PREFS_PATH)
        template = self.template_combo.itemData(self.template_combo.currentIndex())
        if template:
            prefs.SetString("LastCAMTemplate", template)
        prefs.SetString("LastCAMPostProcessor",
                        self.post_processor_combo.currentText())
        super().accept()

    def get_options(self):
        return {
            "template_path": self.template_combo.itemData(
                self.template_combo.currentIndex()),
            "post_processor": self.post_processor_combo.currentText() or "grbl",
        }


class ReplayCAMSetupCommand:
    """Replay the selected CAM job's operations onto a nested layout."""

    def GetResources(self):
        return {
            "Pixmap": "Nesting_CNC_Icon.svg",
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

        dialog = ReplayOptionsDialog(source_job, len(sheets),
                                    FreeCADGui.getMainWindow())
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        options = dialog.get_options()

        try:
            outcomes = cam_replay.replay_layout(
                doc, layout_group, source_job,
                post_processor=options.get("post_processor"),
                template_path=options.get("template_path"),
            )
        except Exception as exc:
            FreeCAD.Console.PrintError("Replay failed: %s\n" % exc)
            import traceback
            traceback.print_exc()
            return

        self._report(layout_group, source_job, outcomes)

    def _report(self, layout_group, source_job, outcomes):
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
                "CAM Replay failed",
                "%d of %d sheet(s) did not replay cleanly.\n\n"
                "Jobs that failed verification are kept in the document, "
                "labelled %s, so you can inspect them. Do not post from them.\n\n"
                "See the Report view for detail."
                % (len(failed), len(outcomes), cam_replay.UNVERIFIED_SUFFIX)
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
