# SPDX-License-Identifier: LGPL-2.1-or-later
"""Progress for a replay, in the Tasks panel.

The replay runs on the GUI thread, so "show progress" means pumping the event
loop as well as drawing. Both halves are here; neither is interesting on its
own.

**The bar reports the stage, not the run.** Measured on the committed fixture,
the seven stages are: ordering 35%, replaying 34%, recomputing 25%, building
4%, flattening 1%, verifying 0.5%, reading 0%. Ordering and replaying are two
thirds of the run, and ordering is a single indivisible call -- so a bar
ticking once per operation would sit still for nearly four seconds and then
jump. A bar that does that is not reporting the run.

There is no honest single percentage to draw. A stage-weighted one would have
to invent the weights, and an invented weighting is a bar that lies. So:

  * the bar is **within** the current stage, and says so;
  * a stage with no countable work gets a marquee and the words "this stage
    reports no progress", because that is the truth about it;
  * the elapsed clock and the finished-stage count are always visible, so
    there is something that genuinely increases.

**`ReplayProgressView` is pure and is what the tests cover.** It turns
`(stage, current, total, message)` into what the widget should display. The Qt
class below only draws what it is told. A widget cannot be exercised headless,
and a progress bar that has only ever been looked at once is indistinguishable
from one that does not work.
"""
import time

#: Stages that report `total == 0` are work the pipeline cannot break up. The
#: largest of them is "Ordering and tidying the tool table" at about a third of
#: the run, so this is not an edge case.
_NO_COUNT = "this stage reports no progress"

#: What a cancelled or failed job gets labelled. Spelled out here rather than
#: imported from cam_replay so this module can be read on its own; the Cancel
#: tooltip has to name the label, and importing the whole engine to get a
#: string would make the display depend on the engine being importable.
_UNVERIFIED = "_UNVERIFIED"


class ReplayProgressView:
    """What to display, given the progress seam's arguments. No Qt."""

    def __init__(self, stage_total=None, clock=None, subject=None):
        #: Stages the pipeline announced ahead of time, when the caller knows
        #: them. Only used to show "stage N of M"; a stage list that is wrong
        #: must not stop the bar working.
        self._known_stages = list(stage_total or [])
        #: What is being replayed onto. A multi-sheet run replays each sheet
        #: separately and this is the only thing that tells the reader which
        #: one they are watching.
        self.subject = subject
        self._clock = clock or time.perf_counter
        self._started = self._clock()

        self.stage = ""
        self.current = 0
        self.total = 0
        self.detail = ""
        self.indeterminate = False
        self.stages_seen = []
        self.elapsed = 0.0
        self.cancelled = False

    def update(self, stage, current, total, message=None):
        """Fold one progress event in. Returns `self`, so calls chain."""
        if stage and stage != self.stage:
            self.stage = stage
            if stage not in self.stages_seen:
                self.stages_seen.append(stage)
            self.current = 0
            self.total = 0

        if current is None and total is None:
            # `done()`: the stage is over.
            self.detail = message or ""
            self.indeterminate = False
            self.current = 0
            self.total = 0
        else:
            self.current = current or 0
            self.total = total or 0
            self.indeterminate = not self.total
            self.detail = message or ""

        self.elapsed = self._clock() - self._started
        return self

    def mark_cancelled(self):
        self.cancelled = True
        return self

    # -- what the widget draws -------------------------------------------

    def stage_text(self):
        """The headline: what, which stage, and how far through the pipeline."""
        if not self.stage:
            return self.subject or "Replaying CAM setup"
        if self._known_stages:
            try:
                position = self._known_stages.index(self.stage) + 1
            except ValueError:
                # A stage this list has never heard of. Showing where it landed
                # among the ones seen beats refusing, and the denominator stays
                # the real number of stages rather than an invented one.
                position = len(self.stages_seen)
            stage = "%s  (stage %d of %d)" % (self.stage, position,
                                              len(self._known_stages))
        else:
            stage = self.stage
        return "%s  --  %s" % (self.subject, stage) if self.subject else stage

    def bar_range(self):
        """`(maximum, value)` for the progress bar.

        `(0, 0)` is what Qt renders as a marquee, which is the honest rendering
        of a stage that cannot say where it is.
        """
        if self.indeterminate or not self.total:
            return (0, 0)
        return (int(self.total), min(int(self.current), int(self.total)))

    def detail_text(self):
        """The line under the bar: a count, or the reason there is none."""
        if self.cancelled:
            return "Cancelling -- finishing the current operation."
        if not self.stage:
            return "Starting."
        if self.indeterminate or not self.total:
            return "%s  --  %s" % (self.stage, _NO_COUNT)
        if self.detail:
            return "%d of %d  --  %s" % (self.current, self.total, self.detail)
        return "%d of %d" % (self.current, self.total)

    def elapsed_text(self):
        seconds = max(0.0, self.elapsed)
        if seconds < 60:
            return "%.1fs elapsed" % seconds
        return "%dm %02ds elapsed" % (int(seconds) // 60, int(seconds) % 60)


# -- the widget -----------------------------------------------------------
#
# Imported lazily and guarded, so this module can be imported without a GUI.
# The view model above is the part with any behaviour in it, and it is what the
# tests use.

try:
    from PySide import QtCore, QtWidgets
except ImportError:      # pragma: no cover - no GUI build
    QtCore = None
    QtWidgets = None


#: What the pipeline announces, in order. Kept next to the widget because it is
#: a presentational claim -- if the pipeline gains a stage and this does not,
#: the count reads "stage 4 of 7" on a run that has eight.
STAGES = [
    "Reading the source CAM setup",
    "Flattening nested parts",
    "Building the replay job",
    "Replaying the recipe",
    "Ordering and tidying the tool table",
    "Recomputing the toolpaths",
    "Verifying the result",
]


class ReplayTaskWidget(QtWidgets.QWidget if QtWidgets else object):
    """The Tasks panel entry. Draws what `ReplayProgressView` says.

    Deliberately a `QWidget` and not a FreeCAD `TaskPanel`. A `TaskPanel` is
    for a modal task that owns a document edit; this is a read-only progress
    display next to a synchronous replay, and `Control.showDialog` accepts a
    plain widget.

    The Cancel button sets `cancelled`, which `Progress.cancelled` polls. The
    button disables itself rather than disappearing, so a click that lands
    during a stage the replay cannot interrupt reads as "cancelling" instead of
    looking like a button that did nothing.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = ReplayProgressView(stage_total=STAGES)
        self.cancelled = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self._headline = QtWidgets.QLabel(self)
        self._headline.setWordWrap(True)
        font = self._headline.font()
        font.setBold(True)
        self._headline.setFont(font)

        self._bar = QtWidgets.QProgressBar(self)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(14)

        self._detail = QtWidgets.QLabel(self)
        self._detail.setWordWrap(True)

        self._buttons = QtWidgets.QHBoxLayout()
        self._clock = QtWidgets.QLabel(self)
        self._cancel = QtWidgets.QPushButton("Cancel", self)
        self._cancel.setToolTip(
            "Stop the replay. The job so far is kept and labelled %s so you "
            "can see how far it got." % _UNVERIFIED
        )
        self._cancel.clicked.connect(self._on_cancel)
        self._buttons.addWidget(self._clock)
        self._buttons.addStretch(1)
        self._buttons.addWidget(self._cancel)

        layout.addWidget(self._headline)
        layout.addWidget(self._bar)
        layout.addWidget(self._detail)
        layout.addLayout(self._buttons)

    def _on_cancel(self):
        self.cancelled = True
        self.view.mark_cancelled()
        self._cancel.setEnabled(False)
        self._cancel.setText("Cancelling...")
        self._refresh()

    # -- the seam's entry point -----------------------------------------

    def callback(self, stage, current, total, message=None):
        """`Progress`'s callback: draw, then let Qt breathe.

        `FreeCADGui.updateGui()` is the whole reason this can work at all. The
        replay is synchronous on the GUI thread, so without pumping the event
        loop the widget is created and then not painted until the replay has
        finished -- which is the exact behaviour a progress display is meant to
        replace. FreeCAD's own modules use the same call for the same reason.

        It is called once per progress *event*, not once per operation, and
        most stages emit one event and then go quiet for seconds. That is
        visible in the display rather than hidden: the clock is the only thing
        that moves during such a stage, which is honest, and the stage name says
        which one it is.
        """
        self.view.update(stage, current, total, message)
        self._refresh()
        try:
            import FreeCADGui
            FreeCADGui.updateGui()
        except Exception:
            # No GUI, or the window is closing. The panel is a nicety; the
            # replay must not depend on being able to paint it.
            pass

    def _refresh(self):
        self._headline.setText(self.view.stage_text())
        self._detail.setText(self.view.detail_text())
        self._clock.setText(self.view.elapsed_text())
        # `setRange(min, max)` -- two arguments. Called with one it raises
        # `TypeError: setRange expected 2 arguments, got 1`, and that exception
        # propagates out of the progress callback, which is why the panel never
        # appeared: the first event killed it and `Progress` correctly gave up
        # on a callback that had already failed.
        #
        # `(0, 0)` is Qt's busy indicator, which is what the view asks for when
        # a stage cannot say where it is.
        maximum, value = self.view.bar_range()
        self._bar.setRange(0, maximum)
        self._bar.setValue(value)


class ReplayTaskProgress(object):
    """Owns the Tasks panel entry for the length of one replay.

    Used as a context manager so the panel is closed on the way out of
    *every* path, including an exception:

        with ReplayTaskProgress("Sheet_1") as task:
            outcomes = cam_replay.replay_layout(doc, layout, job,
                                                progress_callback=task.callback)

    `Control.closeDialog()` raises if no task dialog is active, and
    `showDialog` raises if one already is. Both are called from teardown paths
    that also run after a failure, so both are guarded. Leaving a task dialog
    open is bad -- FreeCAD keeps the panel up and the document un-editable until
    something closes it, which is a way of breaking the rest of the session
    because one replay went wrong.
    """

    def __init__(self, sheet_label=None):
        self.sheet_label = sheet_label
        self.widget = None
        self.dialog = None
        self.opened = False
        self.closed = False

    def open(self):
        if QtWidgets is None:
            return self
        import FreeCADGui
        try:
            self.widget = ReplayTaskWidget()
            self.widget.view.subject = self.sheet_label
        except Exception:
            # A widget that will not build is not a reason to refuse the run.
            self.widget = None
            return self
        if self.sheet_label:
            self.widget.view.detail = ""
        try:
            # The task pane lives in the Start workbench's dock. If the user is
            # in Model or CAM, `showDialog` still succeeds and the widget is
            # still there -- just off screen, which reads as "no progress".
            # `showTaskView` brings it into view first. Both are guarded and
            # both are best-effort: on a layout with no task pane they raise,
            # and neither is a reason to refuse the replay.
            try:
                FreeCADGui.Control.showTaskView()
            except Exception:
                pass
            self.dialog = FreeCADGui.Control.showDialog(self.widget)
            self.opened = True
        except Exception:
            # "a task is already active". Something else in the session owns
            # the panel. The replay still runs; it just runs without a
            # progress display, which is better than refusing to work.
            self.widget = None
        return self

    def close(self):
        if self.closed or not self.opened:
            self.closed = True
            return
        self.closed = True
        try:
            import FreeCADGui
            FreeCADGui.Control.closeDialog()
        except Exception:
            pass
        self.opened = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()
        return False

    def callback(self, stage, current, total, message=None):
        if self.widget is None:
            # Nothing to draw into. The run continues: the seam is optional and
            # was always optional, and a missing panel is not a failure.
            return
        self.widget.callback(stage, current, total, message)

    @property
    def cancelled(self):
        """Whether the user asked to stop. Never raises.

        Read from the engine on every poll, so it is on a hot path and it runs
        while Qt may be tearing the panel down underneath it. A widget that has
        been deleted is a normal state, not an error: touching its attributes
        raises `ReferenceError: Cannot access attribute ... of deleted object`,
        and letting that escape would make `Progress` disable cancellation --
        turning a harmless race into a button that does nothing.
        """
        if self.widget is None:
            return False
        try:
            return bool(self.widget.cancelled)
        except Exception:
            # The panel went away. Not a cancel, and not a failure.
            return False
