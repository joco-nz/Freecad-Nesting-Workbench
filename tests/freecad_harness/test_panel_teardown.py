#!/usr/bin/env freecadcmd
"""Regression test for closing the Nesting panel while a run is in flight.

What this covers
----------------
The worker outlives the panel. Its signals are queued, so a slot can execute
long after the panel has been closed and its widgets destroyed, and PySide6
then raises `RuntimeError: Internal C++ object ... already deleted`.

Found by hand: a long GA run cancelled from the panel produced two of these in
the console, one from the queued `status_changed` slot and one from
`_on_nesting_finished`. Both were cosmetic on the surface, but the second was
not: it raised part-way through the handler, so `self._worker = None` never
ran and the next nest saw a stale worker reference.

How it is driven
----------------
`NestingController` is instantiated against a stub `ui` whose widget methods
raise `RuntimeError`, which is exactly the state the panel is in after it is
closed. That avoids needing a real Qt event loop -- the same reason
`test_ga_loop.py` drives `GACoordinator` with `draw_callback=None`, so this is
the controller-side counterpart rather than a mock. What it cannot cover is the
QThread/Qt event loop, which needs a GUI session.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_panel")
_FAILURES = []
_CHECKS = [0]


def emit(message=""):
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


class _DeletedWidget:
    """Stands in for a PySide widget whose C++ object has been destroyed."""

    def __init__(self, name):
        self._name = name

    def _raise(self, *args, **kwargs):
        raise RuntimeError(
            f"Internal C++ object (PySide6.QtWidgets.{self._name}) already deleted.")

    setText = _raise
    setEnabled = _raise
    isChecked = _raise
    value = _raise
    setValue = _raise


class _LiveWidget:
    """A working widget, so the guarded path is exercised too."""

    def __init__(self):
        self.text = None
        self.enabled = None

    def setText(self, value):
        self.text = value

    def setEnabled(self, value):
        self.enabled = value


class _StubPanel:
    """Minimal panel whose widgets all raise, as after a close."""

    def __init__(self, deleted=True):
        if deleted:
            # Bound methods, not the bare function: `self.ui.update_progress` is
            # handed to _safe_ui, which calls it with arguments. A class-level
            # reference would not take self, and a per-instance lambda has to be
            # built here to close over the right raiser.
            def _update(*_args, **_kwargs):
                raise RuntimeError(
                    "Internal C++ object (PySide6.QtWidgets.QLabel) already deleted.")
            self.update_progress = _update
            self.reset_progress = _update
        else:
            self.update_progress = lambda *a, **k: None
            self.reset_progress = lambda *a, **k: None
        self.status_label = _DeletedWidget("QLabel") if deleted else _LiveWidget()
        self.nest_button = _DeletedWidget("QPushButton") if deleted else _LiveWidget()
        self.cancel_button = _DeletedWidget("QPushButton") if deleted else _LiveWidget()
        self.sound_checkbox = _DeletedWidget("QCheckBox") if deleted else _LiveWidget()


def make_controller(panel):
    from freecad.nestingworkbench.Tools.Nesting.nesting_controller import (
        NestingController,
    )

    # No document and no __init__: NestingController.__init__ reaches for
    # FreeCAD.ActiveDocument, ShapePreparer and VisualizationManager, and the
    # stub panel carries no font_label. None of that is under test -- the state
    # reset and the widget guards are -- so the instance is built directly
    # rather than dragging a real document and GUI stack into a test that needs
    # neither. That also keeps the test off the Qt event loop, which needs a
    # GUI session.
    controller = NestingController.__new__(NestingController)
    controller.ui = panel
    controller.doc = None
    controller.current_job = None
    controller.is_running = True
    controller.cancel_requested = False
    controller._saved_source_placements = []
    controller._worker = object()  # a stale worker reference we must see cleared
    return controller


def _no_console():
    """Force the import past FreeCADGui.

    nesting_controller imports FreeCADGui at module scope, and this test uses
    no document and no drawing, so the GUI stack is never needed -- but merely
    importing it under freecadcmd brings up a Qt main window and segfaults
    without a display. An inert stand-in keeps the test headless, the same
    trick tests/conftest.py uses for the pytest tier.
    """
    import types

    gui = types.ModuleType("FreeCADGui")
    gui.__getattr__ = lambda attr: None
    sys.modules.setdefault("FreeCADGui", gui)


_no_console()


# --------------------------------------------------------------------------
# Case 1: completion after the panel closed
# --------------------------------------------------------------------------
def case_finished_after_close():
    emit("")
    emit("--- finish after panel close ---")
    controller = make_controller(_StubPanel(deleted=True))

    raised = None
    try:
        controller._on_nesting_finished(None)
    except Exception as exc:  # noqa: BLE001 - the point is to observe any raise
        raised = exc

    check("finishing with dead widgets does not raise", raised is None,
          f"raised {type(raised).__name__}: {raised}")
    check("is_running was cleared despite the dead panel",
          controller.is_running is False, f"is_running={controller.is_running}")
    check("cancel_requested was cleared", controller.cancel_requested is False)
    # The substantive one. Reported by hand as the second half of the
    # cancel-time traceback, and a real consequence rather than console noise:
    # the handler raised part-way through, so the assignment never ran and the
    # next nest saw a live worker reference.
    check("the stale worker reference was cleared", controller._worker is None,
          f"_worker={controller._worker!r}")


# --------------------------------------------------------------------------
# Case 2: the error path, which had the same ordering hazard
# --------------------------------------------------------------------------
def case_error_after_close():
    emit("")
    emit("--- error after panel close ---")
    controller = make_controller(_StubPanel(deleted=True))

    raised = None
    try:
        controller._on_nesting_error("synthetic failure\nsecond line")
    except Exception as exc:  # noqa: BLE001
        raised = exc

    check("erroring with dead widgets does not raise", raised is None,
          f"raised {type(raised).__name__}: {raised}")
    check("is_running was cleared despite the dead panel",
          controller.is_running is False, f"is_running={controller.is_running}")
    check("the stale worker reference was cleared", controller._worker is None,
          f"_worker={controller._worker!r}")


# --------------------------------------------------------------------------
# Case 3: the guarded path still works with a live panel
# --------------------------------------------------------------------------
def case_live_panel_unchanged():
    """The guard must not swallow anything when the panel is alive."""
    emit("")
    emit("--- live panel still updates ---")
    panel = _StubPanel(deleted=False)
    controller = make_controller(panel)

    raised = None
    try:
        controller._on_nesting_finished(None)
    except Exception as exc:  # noqa: BLE001
        raised = exc

    check("finishing with live widgets does not raise", raised is None,
          f"raised {type(raised).__name__}: {raised}")
    check("the nest button was re-enabled", panel.nest_button.enabled is True,
          f"enabled={panel.nest_button.enabled!r}")
    check("the cancel button was disabled", panel.cancel_button.enabled is False,
          f"enabled={panel.cancel_button.enabled!r}")
    check("is_running was cleared", controller.is_running is False)


# --------------------------------------------------------------------------
# Case 4: _safe_ui passes through a real result
# --------------------------------------------------------------------------
def case_safe_ui_passthrough():
    emit("")
    emit("--- _safe_ui pass-through ---")
    controller = make_controller(_StubPanel(deleted=False))

    check("_safe_ui returns a real widget call's value",
          controller._safe_ui(lambda x: x + 1, 41) == 42)
    check("_safe_ui returns None when the widget is gone",
          controller._safe_ui(_DeletedWidget("QLabel")._raise, "x") is None)

    def _not_a_widget_error():
        raise ValueError("a genuine bug, not a deleted widget")

    propagated = None
    try:
        controller._safe_ui(_not_a_widget_error)
    except ValueError as exc:
        propagated = exc
    check("_safe_ui does not swallow unrelated errors",
          isinstance(propagated, ValueError),
          f"got {type(propagated).__name__}")


# --------------------------------------------------------------------------
# Case 5: the dialog's Cancel button must actually stop a running nest
# --------------------------------------------------------------------------
def case_dialog_cancel_stops_the_run():
    """Closing the dialog has to stop the worker, not just tidy up.

    Found by hand: pressing the dialog's Cancel next to OK closed the panel but
    left the nest running to completion. NestingPanel.reject() called
    controller.cancel_job(), which cleans up a job that does not exist yet and
    never sets cancel_requested -- the one flag the worker's _check_cancel
    consults. So the run carried on invisibly.

    The controller is real; only the panel is a stub, and the stub is what
    records which controller method reject() reached for. The panel's own
    re-entrancy guard is exercised too, since reject() -> request_cancel() ->
    reject() is the loop that would otherwise recurse.
    """
    emit("")
    emit("--- dialog Cancel reaches the running worker ---")
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    controller = make_controller(_StubPanel(deleted=False))
    calls = []

    class _RecordingPanel:
        _rejecting = False
        hidden_originals = ()

        def __init__(self):
            self.rejects = 0

        def reject(self):
            self.rejects += 1
            calls.append("panel.reject")

    panel = _RecordingPanel()
    controller.ui = panel
    controller.is_running = True
    controller.cancel_requested = False
    controller.request_cancel = _Recorder(calls, controller)

    form = NestingPanel.__new__(NestingPanel)
    form.controller = controller
    form.ui = panel
    form.hidden_originals = []
    form._rejecting = False

    form.reject()

    check("dialog Cancel asked the worker to stop",
          controller.cancel_requested is True,
          f"cancel_requested={controller.cancel_requested}")
    check("dialog Cancel went through request_cancel, not cancel_job",
          "request_cancel" in calls, f"calls={calls}")
    check("dialog Cancel did not call cancel_job on a running nest",
          "cancel_job" not in calls, f"calls={calls}")

    # _check_cancel is what the worker actually polls.
    check("the worker's cancel probe reports the cancellation",
          controller._check_cancel() is True)

    # The reject() <-> request_cancel() loop must not recurse.
    check("the panel was not re-dismissed from inside the cancel",
          panel.rejects == 0, f"rejects={panel.rejects}")
    check("no recursion escaped", len(calls) <= 2, f"calls={calls}")

    # And the guard must not stick: once reject() has returned the panel is free
    # to be dismissed again.
    check("the guard is released after reject() returns",
          form._rejecting is False)

    # The genuine reject() <-> request_cancel() loop. The controller's `ui` is
    # the panel itself, as in production, so request_cancel()'s idle branch
    # calls back into this same form. Without the guard the two bounce until
    # RecursionError. _CountingForm counts entries to its own reject so the
    # depth is observable rather than just "it did not blow up".
    depth_seen = []

    class _CountingForm(NestingPanel):
        def reject(self):
            depth_seen.append(1)
            return super().reject()

    live = _CountingForm.__new__(_CountingForm)
    live.controller = None
    live.ui = None
    live.hidden_originals = []
    live._rejecting = False

    idle = make_controller(_StubPanel(deleted=False))
    idle.ui = live
    idle.is_running = False
    idle.cancel_requested = False
    live.controller = idle

    live.reject()

    # One entry: the bounce is stopped on the controller side, which checks
    # _rejecting before calling back into the panel.
    check("the idle cancel path never bounces back into reject()",
          len(depth_seen) == 1, f"reject() entered {len(depth_seen)} times")
    check("the guard is released after the idle path returns",
          live._rejecting is False)

    # The panel's own guard is the backstop, and is independently sufficient:
    # with the controller-side check removed the call does come back, and the
    # panel absorbs it rather than recursing. That is why both are kept.
    depth_seen.clear()
    live.controller = _BounceAlwaysController(idle, live)
    live.reject()
    check("the panel guard alone also stops the loop",
          len(depth_seen) == 2,
          f"reject() entered {len(depth_seen)} times, expected 2 (one + one absorbed bounce)")
    check("the guard is still released afterwards", live._rejecting is False)


class _BounceAlwaysController:
    """request_cancel that always calls back into the panel.

    Stands in for the controller with its _rejecting check removed, to show the
    panel's guard is independently load-bearing rather than dead code.
    """

    def __init__(self, real, panel):
        self._real = real
        self._panel = panel
        self.is_running = False
        self.cancel_requested = False

    def request_cancel(self):
        self._real.cancel_job()
        self._panel.reject()  # no _rejecting check -- the pre-fix behaviour


class _Recorder:
    """Stands in for controller.request_cancel, recording that it was used."""

    def __init__(self, calls, controller):
        self.calls = calls
        self.controller = controller

    def __call__(self):
        self.calls.append("request_cancel")
        self.controller.cancel_requested = True


def main():
    for case in (case_finished_after_close, case_error_after_close,
                 case_live_panel_unchanged, case_safe_ui_passthrough,
                 case_dialog_cancel_stops_the_run):
        try:
            case()
        except Exception:
            emit(f"  ERROR in {case.__name__}:")
            emit(traceback.format_exc())
            _FAILURES.append(f"{case.__name__} raised")
    emit("")
    emit(f"{_CHECKS[0] - len(_FAILURES)}/{_CHECKS[0]} checks passed")
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
        return 1
    return 0


if __name__ in ("__main__", "test_panel_teardown"):
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
