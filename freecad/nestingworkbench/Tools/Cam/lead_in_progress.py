# SPDX-License-Identifier: LGPL-2.1-or-later
"""Progress for the lead-in check, in the Tasks panel.

The audit is not quick on a bad nest. Measured on the committed fixture: the
**scan** is 72-96 ms over 98 operations, but a *fix* recomputes FreeCAD once per
candidate at about 22 ms, and the search is bounded at 48 candidates per
operation. So one offender is 0.08 s when the first candidate works and about a
second when none does, and a sheet with the 32 offenders a 2.0 mm margin finds
is tens of seconds of a frozen GUI with no Cancel.

That is what this is for. It follows `replay_progress.py` closely, because the
situation is the same one: work on the GUI thread, so showing progress means
pumping the event loop as well as drawing.

**The bar counts candidates, not operations.** Operations is the obvious thing
to count and the wrong one: the fixture's single real offender is fixed on the
*first* candidate, so an operations-based bar sits still for a whole second and
then jumps to 100%, which reads as a hung program. Candidates tick every 22 ms,
so the bar moves smoothly through the work it is actually doing. The operation
count is shown as text beside it, because "which part am I on" is the other
thing worth knowing.

**Cancel is checked between candidates, not between operations.** One operation
can be a full second of uninterruptible work, which is exactly the length of
time a user decides they have had enough. `resolve_audit` takes a `cancel_check`
and is polled inside its candidate loop.

**Cancelling keeps what was fixed.** Every fix is applied and verified
independently, so stopping between them leaves the job in a consistent state --
some operations moved, the rest untouched, and the report says which. Nothing is
rolled back, because a half-applied fix is not a broken one here: each moved
start point was checked against every other part before it was accepted.
"""
import time

try:
    import FreeCAD
except ImportError:      # pragma: no cover - no FreeCAD in the pure tier
    FreeCAD = None

#: Shown in place of a count for work that cannot report one.
_NO_COUNT = "this stage reports no progress"


class LeadInProgressView:
    """What to display, given the progress seam's arguments. No Qt.

    Same split as `ReplayProgressView`: this is the part with the behaviour, and
    it is what the tests cover. A progress bar cannot be exercised headless, and
    one that has only ever been looked at once is indistinguishable from one
    that does not work.
    """

    def __init__(self, subject=None, clock=None, total_operations=0,
                 dry_run=False):
        #: Which job is being checked. FreeCAD's Tasks panel shows several at
        #: once, so without this the user cannot tell which sheet they are
        #: looking at.
        self.subject = subject
        self._clock = clock or time.perf_counter
        self._started = self._clock()
        #: How many operations will be searched, when the caller knows. Used for
        #: the "operation 3 of 32" text; a wrong figure must not stop the bar.
        self.total_operations = total_operations or 0
        #: Whether this run will apply fixes. Changes the wording throughout,
        #: because a report-only run must never read as though it cut
        #: anything.
        self.dry_run = dry_run

        self.stage = ""
        self.current = 0
        self.total = 0
        self.detail = ""
        self.operation = 0
        self.elapsed = 0.0
        self.cancelled = False
        self.fixed = 0
        self.unresolved = 0
        self.skipped = 0

    def update(self, operation_index=None, operation_total=None, label=None,
               outcome=None, candidates=None, candidate_total=None):
        """Fold one progress event in. Returns `self`, so calls chain.

        Two event kinds, because there are two things happening:

        * **Per operation** -- `operation_index` of `operation_total`, with
          `label` naming the part. Says which operation is being worked on.
        * **Per candidate** -- `candidates` of `candidate_total`. Says how far
          through *this* operation's search, which is the thing that moves.

        The candidate count resets when the operation index changes, so the bar
        tracks the operation in front of the user rather than running 0-48 five
        times over without saying so.
        """
        if operation_index is not None:
            if operation_index != self.operation:
                # A new operation: its candidate budget starts again.
                self.current = 0
                self.total = candidate_total or 0
                self.operation = operation_index
            if operation_total:
                self.total_operations = operation_total
            self.stage = label or self.stage

        if candidates is not None:
            self.current = candidates
        if candidate_total:
            self.total = candidate_total

        if outcome in ("fixed", "fixable"):
            self.fixed += 1
        elif outcome == "unresolved":
            self.unresolved += 1
        elif outcome == "skipped":
            self.skipped += 1

        self.elapsed = self._clock() - self._started
        return self

    def mark_cancelled(self):
        self.cancelled = True
        return self

    # -- what the widget draws -------------------------------------------

    def stage_text(self):
        """The headline: which job, and what is being done to it."""
        what = "Checking lead-ins" if self.dry_run else "Fixing lead-ins"
        if self.stage:
            what = "%s -- %s" % (what, self.stage)
        if self.dry_run:
            what = "%s (report only)" % what
        if self.subject:
            return "%s  --  %s" % (self.subject, what)
        return what

    def bar_range(self):
        """`(maximum, value)` for the progress bar.

        `(0, 0)` is Qt's marquee, which is the honest rendering of a stage that
        cannot say where it is.
        """
        if not self.total:
            return (0, 0)
        return (int(self.total), min(int(self.current), int(self.total)))

    def detail_text(self):
        """The line under the bar: which operation, and how far through it.

        Both counts, because each is the answer to a different question the user
        is asking: "how much is left" wants the operation count, "is this one
        stuck" wants the candidate count. The candidate count is what actually
        moves -- 22 ms per candidate -- so it leads.
        """
        if self.cancelled:
            return "Cancelling -- finishing the current candidate."
        if not self.operation:
            return "Starting."

        parts = []
        if self.total:
            parts.append("candidate %d of %d" % (self.current, self.total))
        else:
            parts.append(_NO_COUNT)
        if self.total_operations:
            parts.append("operation %d of %d"
                         % (min(self.operation, self.total_operations),
                            self.total_operations))
        return "  --  ".join(parts)

    def tally_text(self):
        """The running result, which is the thing the user is waiting to see.

        Worded for what the run *does*: a report-only run has fixed nothing, and
        a tally reading "3 fixed" on a run that changed no properties would be a
        lie about the document.
        """
        verb = "could be fixed" if self.dry_run else "fixed"
        parts = ["%d %s" % (self.fixed, verb)]
        if self.unresolved:
            parts.append("%d need doing by hand" % self.unresolved)
        if self.skipped:
            parts.append("%d not looked at" % self.skipped)
        return ", ".join(parts)

    def elapsed_text(self):
        seconds = max(0.0, self.elapsed)
        if seconds < 60:
            return "%.1fs elapsed" % seconds
        return "%dm %02ds elapsed" % (int(seconds) // 60, int(seconds) % 60)


# -- the widget -----------------------------------------------------------
#
# Imported lazily and guarded, so this module imports without a GUI.

try:
    from PySide import QtCore, QtWidgets
except ImportError:      # pragma: no cover - no GUI build
    QtCore = None
    QtWidgets = None


class LeadInTaskWidget(QtWidgets.QWidget if QtWidgets else object):
    """The Tasks panel entry. Draws what `LeadInProgressView` says.

    Deliberately a bare `QWidget`: `Control.showDialog` does not embed the
    object it is given, it looks for an attribute named `form` and embeds that.
    So this widget is the payload of `LeadInTaskProgress.form`, not the thing
    handed to FreeCAD.

    The Cancel button disables itself rather than disappearing, so a click that
    lands between candidates reads as "cancelling" instead of looking like a
    button that did nothing.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = LeadInProgressView()
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

        self._tally = QtWidgets.QLabel(self)
        self._tally.setWordWrap(True)

        self._buttons = QtWidgets.QHBoxLayout()
        self._clock = QtWidgets.QLabel(self)
        self._cancel = QtWidgets.QPushButton("Cancel", self)
        self._cancel.setToolTip(
            "Stop the search. Everything already moved stays moved, and the "
            "report says what is left."
        )
        self._cancel.clicked.connect(self._on_cancel)
        self._buttons.addWidget(self._clock)
        self._buttons.addStretch(1)
        self._buttons.addWidget(self._cancel)

        layout.addWidget(self._headline)
        layout.addWidget(self._bar)
        layout.addWidget(self._detail)
        layout.addWidget(self._tally)
        layout.addLayout(self._buttons)

    def _on_cancel(self):
        self.cancelled = True
        self.view.mark_cancelled()
        self._cancel.setEnabled(False)
        self._cancel.setText("Cancelling...")
        self._refresh()

    # -- the seam's entry point -----------------------------------------

    def callback(self, *args, **kwargs):
        """Draw, then let Qt breathe.

        Takes `*args`/`**kwargs` and hands them straight to `LeadInProgressView`,
        which is where the argument names are written down. Two producers call
        this -- `resolve_conflicts` per operation, `resolve_audit` per candidate
        -- and they do not pass the same arguments, so pinning the signature
        here would mean translating one of them for no benefit.

        `FreeCADGui.updateGui()` is the whole reason this can work at all. The
        search is synchronous on the GUI thread, so without pumping the event
        loop the widget is created and then not painted until the run has
        finished -- which is the exact behaviour a progress display exists to
        replace. FreeCAD's own modules use the same call for the same reason.
        """
        self.view.update(*args, **kwargs)
        self._refresh()
        try:
            import FreeCADGui
            FreeCADGui.updateGui()
        except Exception:
            # No GUI, or the window is closing. The panel is a nicety; the
            # search must not depend on being able to paint it.
            pass

    def _refresh(self):
        self._headline.setText(self.view.stage_text())
        self._detail.setText(self.view.detail_text())
        self._tally.setText(self.view.tally_text())
        self._clock.setText(self.view.elapsed_text())
        # `setRange(min, max)` -- two arguments. Called with one it raises
        # `TypeError: setRange expected 2 arguments, got 1`, and that exception
        # propagates out of the callback into the search.
        maximum, value = self.view.bar_range()
        self._bar.setRange(0, maximum)
        self._bar.setValue(value)


class LeadInTaskProgress(object):
    """Owns the Tasks panel entry for the length of one audit.

    **This object is what `Control.showDialog` is given, not the widget.**
    `TaskDialogPython` looks for an attribute named `form` and embeds the
    QWidget it finds; handed the widget itself it builds an empty dialog, so
    FreeCAD reports success and the panel opens with nothing in it. Same idiom
    as `ReplayTaskProgress` and `NestingTaskPanel`.

    Used as a context manager so the panel is closed on every path out,
    including an exception. `Control.closeDialog()` raises if no task dialog is
    active and `showDialog` raises if one already is, and both are called from
    teardown that also runs after a failure -- so both are guarded. Leaving a
    task dialog open keeps the panel up and the document un-editable until
    something closes it, which breaks the rest of the session because one audit
    went wrong.

    `reject()` is FreeCAD's "the user closed the panel", which is a cancel --
    closing the panel by hand stops the search rather than hiding it and letting
    it run on unwatched.
    """

    def __init__(self, subject=None, total_operations=0, dry_run=False):
        self.subject = subject
        self.total_operations = total_operations
        self.dry_run = dry_run
        #: The QWidget FreeCAD embeds. Named `form` because that is the
        #: attribute `TaskDialogPython` looks for.
        self.form = None
        #: The same object under a readable name. Two names for one thing is the
        #: cost of the `form` convention; they are always set together.
        self.widget = None
        self.dialog = None
        self.opened = False
        self.closed = False
        self._rejecting = False
        #: Why the panel did not open, as `exception: message`, or None.
        #: Recorded rather than swallowed: a panel that fails to open is silent
        #: by construction, and indistinguishable from one that was never tried.
        self.show_error = None

    def reject(self):
        """The panel was closed. Treat it as a cancel."""
        if self._rejecting or self.closed:
            return True
        self._rejecting = True
        try:
            if self.widget is not None:
                self.widget._on_cancel()
        except Exception:
            pass
        finally:
            self._rejecting = False
        return True

    def accept(self):
        """FreeCAD's OK. Nothing to accept mid-run, so this just closes."""
        self.close()
        return True

    def getStandardButtons(self):
        """None. The panel's only action is its own Cancel.

        FreeCAD puts OK and Cancel on a task panel by default, which is wrong
        twice over: OK means nothing while a search is in flight, and Cancel
        would *close* the panel, taking the feedback with it. This panel's own
        Cancel does not close it -- it disables itself, says "Cancelling", and
        the panel stays up until the run really has stopped.

        Returning 0 rather than importing QtWidgets for the enum: FreeCAD
        converts the return value straight to a `QDialogButtonBox.StandardButtons`,
        and 0 is no buttons.
        """
        return 0

    def show(self):
        # **Not called `open`.** FreeCAD's task panel calls `open()` on the
        # object it is given, and that name is not ours: showing the panel called
        # `open()`, which built another widget and showed another panel, which
        # called `open()` again.
        if QtWidgets is None:
            self.show_error = "PySide/QtWidgets is not importable"
            return self
        import FreeCADGui
        try:
            self.form = LeadInTaskWidget()
            self.form.view.subject = self.subject
            self.form.view.total_operations = self.total_operations
            self.form.view.dry_run = self.dry_run
            self.widget = self.form
        except Exception as exc:
            # A widget that will not build is not a reason to refuse the run.
            self.show_error = "%s building the panel widget: %s" % (
                type(exc).__name__, exc)
            self.form = None
            self.widget = None
            return self
        try:
            # The task pane lives in the Start workbench's dock. If the user is
            # in Model or CAM the panel opens off screen, which reads as "no
            # progress". Both guarded: on a layout with no task pane they raise,
            # and neither is a reason to refuse the audit.
            try:
                FreeCADGui.Control.showTaskView()
            except Exception:
                pass
            document = _gui_document(FreeCADGui)
            if document is None:
                # Nothing to attach to. The audit runs without a progress
                # display rather than not at all.
                self.show_error = "no active document to attach the panel to"
                self.form = None
                self.widget = None
                return self
            self.dialog = FreeCADGui.Control.showDialog(self, document)
            self.opened = True
        except Exception as exc:
            # Usually "a task is already active" -- something else in the
            # session owns the panel. The audit still runs; it just runs without
            # a progress display. The reason is kept.
            self.show_error = "%s: %s" % (type(exc).__name__, exc)
            self.form = None
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
        return self.show()

    def __exit__(self, *_exc):
        self.close()
        return False

    def callback(self, *args, **kwargs):
        if self.widget is None:
            # Nothing to draw into. The run continues: the seam is optional and
            # was always optional, and a missing panel is not a failure.
            return
        try:
            self.widget.callback(*args, **kwargs)
        except (RuntimeError, ReferenceError):
            # The panel was closed by hand mid-run, so the widget is a deleted
            # C++ object. Drawing into it raises, and an exception here would
            # travel back into the search. The run carries on unobserved, which
            # is what closing the panel asked for.
            self.form = None
            self.widget = None

    @property
    def cancelled(self):
        """Whether the user asked to stop. Never raises.

        Read on every poll, so it runs while Qt may be tearing the panel down
        underneath it. A widget that has been deleted is a normal state, not an
        error: touching its attributes raises `ReferenceError`, and letting that
        escape would make the search treat a harmless race as a cancel.
        """
        if self.widget is None:
            return False
        try:
            return bool(self.widget.cancelled)
        except Exception:
            # The panel went away. Not a cancel, and not a failure.
            return False


def _gui_document(gui):
    """The active document as a **Gui** document, or None.

    `Control.showDialog` wants `Gui.Document`. `FreeCAD.ActiveDocument` is the
    `App.Document`, and passing it raises
    `TypeError: argument 2 must be Gui.Document, not App.Document`. None means
    "attach to nothing", which FreeCAD also refuses -- silently enough to look
    like an empty panel.
    """
    try:
        active = FreeCAD.ActiveDocument
        if active is None:
            return None
        return gui.getDocument(active.Name)
    except Exception:
        return None
