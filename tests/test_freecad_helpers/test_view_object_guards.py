"""Regression tests for the ViewObject access guards.

Background
----------
`hasattr(obj, "ViewObject")` is not a sufficient test for whether a view
exists. Under a GUI-less FreeCAD (`freecadcmd`, `FreeCAD.GuiUp == False`)
the attribute is present but its value is None, so a hasattr-only guard lets
None through and the next attribute write raises AttributeError.

In the nesting workbench that failure was silent rather than loud: each master
container raised while setting `ViewObject.Visibility`, the part was logged as
"Could not create boundary ... it will be skipped", and `prepare_parts`
returned an empty list. A headless nesting run therefore produced zero parts
and looked merely like an empty result.

These tests pin the two helpers that make such access safe.
"""
import pytest

from freecad.nestingworkbench.freecad_helpers import get_view_object, set_visibility


class _FakeView:
    """Stands in for a live ViewObject."""
    def __init__(self):
        self.Visibility = None
        self.LineColor = None


class _ObjWithView:
    """A document object that has a live view, as under a GUI."""
    def __init__(self):
        self.ViewObject = _FakeView()


class _ObjWithoutView:
    """A document object whose ViewObject is None, as under freecadcmd.

    This is the shape that broke the hasattr-only guards: the attribute exists,
    so hasattr() returns True, but the value is None.
    """
    def __init__(self):
        self.ViewObject = None


class _ObjNoAttr:
    """A document object with no ViewObject attribute at all."""
    pass


class TestGetViewObject:
    def test_returns_view_when_present(self):
        obj = _ObjWithView()
        assert get_view_object(obj) is obj.ViewObject

    def test_returns_none_when_view_is_none(self):
        # hasattr() is True here — this is the case that defeated the old guard.
        obj = _ObjWithoutView()
        assert hasattr(obj, "ViewObject")
        assert get_view_object(obj) is None

    def test_returns_none_when_attribute_absent(self):
        assert get_view_object(_ObjNoAttr()) is None

    def test_returns_none_for_none_input(self):
        assert get_view_object(None) is None


class TestSetVisibility:
    def test_applies_visibility_when_view_present(self):
        obj = _ObjWithView()
        assert set_visibility(obj, True) is True
        assert obj.ViewObject.Visibility is True
        assert set_visibility(obj, False) is True
        assert obj.ViewObject.Visibility is False

    def test_is_noop_when_view_is_none(self):
        obj = _ObjWithoutView()
        assert set_visibility(obj, True) is False
        # Must not have raised, and must not have invented a view.
        assert obj.ViewObject is None

    def test_is_noop_when_attribute_absent(self):
        obj = _ObjNoAttr()
        assert set_visibility(obj, True) is False
        assert not hasattr(obj, "ViewObject")

    def test_is_noop_for_none_input(self):
        assert set_visibility(None, True) is False

    def test_never_raises_for_any_shape(self):
        # The whole point: a broad sweep over heterogeneous objects must be
        # safe, including the headless shapes that used to raise.
        for obj in (_ObjWithView(), _ObjWithoutView(), _ObjNoAttr(), None):
            set_visibility(obj, True)
