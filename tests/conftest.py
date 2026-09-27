"""Shared pytest configuration for the Nesting Workbench test suite.

Several workbench modules import FreeCAD and Part at module scope. Those
modules are not importable under a plain `pytest` interpreter, which is what
made it tempting to test only hand-written Shapely approximations instead of
the real code.

This conftest installs minimal stand-ins for the FreeCAD modules *only when
they are genuinely absent*, so the suite runs in two environments:

  - plain CPython (`python3 -m pytest`)  -> stubs are installed
  - FreeCAD's own interpreter
    (`freecadcmd script.py`)             -> the real modules are used

The stubs are deliberately inert: attribute access returns None. That is
enough to satisfy module-level `import FreeCAD` / `import Part` and to let
pure-Python logic (geometry helpers, view guards, NFP maths) be exercised for
real. It is NOT enough to drive document or GUI code — that is what the
FreeCAD-based end-to-end harness is for.
"""
import sys
import types

_REAL_MODULES = ("FreeCAD", "Part", "FreeCADGui", "Draft")
_STUBBED = []


def _install_stub(name):
    """Registers an inert placeholder for a module that cannot be imported."""
    module = types.ModuleType(name)

    def __getattr__(attr, _name=name):
        # A real FreeCAD submodule or attribute. Returning None keeps callers
        # from exploding on import-time lookups while making any actual use
        # fail loudly with a clear AttributeError on None.
        return None

    module.__getattr__ = __getattr__
    sys.modules[name] = module
    _STUBBED.append(name)
    return module


def ensure_freecad_importable():
    """Ensures FreeCAD/Part/FreeCADGui/Draft are importable, stubbing if needed.

    Returns the list of module names that had to be stubbed (empty when a real
    FreeCAD is present).
    """
    for name in _REAL_MODULES:
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            _install_stub(name)
    return list(_STUBBED)


# Installed at collection time so test modules can import workbench code at
# module scope, matching how the workbench itself imports them.
STUBBED_MODULES = ensure_freecad_importable()
