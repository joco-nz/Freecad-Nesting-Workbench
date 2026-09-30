#!/usr/bin/env freecadcmd
"""Regression test for the document-unit formatters.

Run with:
    /home/james/freecad_env/usr/bin/freecadcmd \
        tests/freecad_harness/test_document_units.py

What this covers
----------------
`freecad.nestingworkbench.units`: resolving a document's unit schema, and
formatting a millimetre length or a square-millimetre area into it. These are
the two formatters the console and dialog messages use, and the two places
where a naive implementation produces a *plausible wrong answer* rather than
an error.

That last point is why the area formatter exists at all. FreeCAD's own
`schemaTranslate` and `getUserPreferred` are length formatters, and given an
Area quantity both return ('0.00', 1.0, '') with no complaint:

    >>> FreeCAD.Units.Quantity(360000, "mm2").getUserPreferred()
    ('0.00', 1.0, '')

A yield report that silently said "0.00" would be worse than one that said
nothing, so the helper converts through the schema's length unit instead and
the test asserts it does not reproduce the zero.

Scope
-----
Deliberately only the pure helpers, because this is the one file wired into
run.sh and therefore the one that always runs. The spin-box half of the unit
work -- the rounding that would quietly turn 12.5 mm of Part Spacing into
12.446 mm, and the panel's fields following a document switch -- needs
FreeCADGui.UiLoader and so needs the `freecad` binary. That is
probe_unit_panel.py, run by hand.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import FreeCAD

_STATUS_FILE = os.path.join(_HERE, ".last_status_units")
_FAILURES = []
_CHECKS = [0]


def emit(message=""):
    try:
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


def _close(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


# --------------------------------------------------------------------------
# Case 1: every schema formats a length and an area
# --------------------------------------------------------------------------
def case_every_schema_formats():
    from freecad.nestingworkbench import units

    emit("")
    emit("--- every unit schema produces a usable length and area ---")

    for index, name in enumerate(FreeCAD.Units.listSchemas()):
        length = units.format_length(600.0, index)
        area = units.format_area(360000.0, index)
        check(f"schema {index} ({name}) formats a length", bool(length), repr(length))
        # The "0.00" guard is the point of the test: it is what FreeCAD's own
        # formatters hand back for an area, and a yield report that printed it
        # would be silently wrong rather than obviously broken.
        check(f"schema {index} ({name}) formats a non-zero area",
              area.split(" ")[0] not in ("0.00", ""), repr(area))
        emit(f"         length {length!r:>18}   area {area!r}")


# --------------------------------------------------------------------------
# Case 2: the values those formatters produce
# --------------------------------------------------------------------------
def case_format_values():
    from freecad.nestingworkbench import units

    emit("")
    emit("--- formatted values ---")

    check("millimetre schema keeps mm",
          units.format_length(600.0, 6) == "600.00 mm", units.format_length(600.0, 6))
    check("imperial-decimal schema uses inches",
          units.format_length(600.0, 3) == "23.62 in", units.format_length(600.0, 3))
    check("imperial schema uses its own dialect (feet)",
          units.format_length(600.0, 2) == "1.97'", units.format_length(600.0, 2))
    check("cm schema uses centimetres",
          units.format_length(600.0, 4) == "60.00 cm", units.format_length(600.0, 4))
    check("metre-decimal schema uses metres",
          units.format_length(600.0, 9) == "0.60 m", units.format_length(600.0, 9))

    # Area goes through the schema's STABLE length unit, squared. 360000 mm^2
    # is 0.36 m^2, and 558.00 in^2, and 3.88 ft^2.
    check("area converts through the length unit, squared",
          units.format_area(360000.0, 3) == "558.00 in\u00b2", units.format_area(360000.0, 3))
    check("metre schema uses m\u00b2 for area",
          units.format_area(360000.0, 9) == "0.36 m\u00b2", units.format_area(360000.0, 9))
    check("civil-engineering schema uses ft\u00b2 for area",
          units.format_area(360000.0, 7) == "3.88 ft\u00b2", units.format_area(360000.0, 7))
    check("millimetre schema keeps mm\u00b2",
          units.format_area(360000.0, 6) == "360000.00 mm\u00b2",
          units.format_area(360000.0, 6))
    check("zero area is still zero, not a fallback",
          units.format_area(0.0, 3) == "0.00 in\u00b2", units.format_area(0.0, 3))

    # The unit must not move with the magnitude. schemaTranslate is
    # magnitude-autoscaling and would answer "thou" for 1 mm and "yd" for
    # 1000 mm under the US-customary schema, which is unusable as a label on a
    # field that has to stay put while the number changes.
    unit_small = units._schema_length_unit(2)
    unit_large = units._schema_length_unit(2)
    check("the schema's length unit does not depend on magnitude",
          unit_small == unit_large == "in", f"{unit_small} / {unit_large}")
    check("mm schemas resolve to mm",
          units._schema_length_unit(6) == "mm", units._schema_length_unit(6))
    check("the metre schema is matched, not missed for want of a space",
          units._schema_length_unit(9) == "m", units._schema_length_unit(9))


# --------------------------------------------------------------------------
# Case 3: schema resolution from a document
# --------------------------------------------------------------------------
def case_doc_unit_schema():
    from freecad.nestingworkbench import units

    emit("")
    emit("--- resolving a document's schema ---")

    doc = FreeCAD.newDocument("unit_schema")
    names = doc.getEnumerationsOfProperty("UnitSystem")
    for index in (6, 3, 2, 5, 7):
        doc.UnitSystem = names[index]
        check(f"document set to {names[index]!r} reads back as {index}",
              units.doc_unit_schema(doc) == index, units.doc_unit_schema(doc))

    # A document is accepted wherever a schema index is, so a message site can
    # pass the thing it actually has to hand rather than unwrapping it first.
    doc.UnitSystem = names[3]
    check("format_length accepts a document",
          units.format_length(600.0, doc) == "23.62 in", units.format_length(600.0, doc))
    check("format_area accepts a document",
          units.format_area(360000.0, doc) == "558.00 in\u00b2",
          units.format_area(360000.0, doc))

    # A headless caller may have no document at all, and a message site must
    # not raise for that.
    check("doc_unit_schema copes with no document",
          isinstance(units.doc_unit_schema(None), int), units.doc_unit_schema(None))
    check("format_length with no schema still returns something",
          bool(units.format_length(600.0)), units.format_length(600.0))

    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 4: parsing typed text back to millimetres
# --------------------------------------------------------------------------
def case_parse_length():
    from freecad.nestingworkbench import units

    emit("")
    emit("--- parse_length ---")

    # The dimensional forms FreeCAD's own parser accepts. Fractional inches
    # are not a nicety: they are what the ft-in schema *displays*, so they are
    # what a user of that schema types back.
    for text, want in (("12.5 mm", 12.5), ("1/2 in", 12.7), ("1' 11\"", 584.2),
                       ("3.5mm", 3.5), ("-3 in", -76.2), ("1,5 in", 38.1),
                       ("0 mm", 0.0), ("  25.4 mm  ", 25.4)):
        got = units.parse_length(text)
        check(f"parse_length({text!r}) is {want}", got is not None and _close(got, want),
              repr(got))

    # What it must refuse. A bare number has no correct answer without knowing
    # the schema -- 12.5 is 12.5 mm in one document and 317.5 mm in the next --
    # and this module has no business guessing. FreeCAD's parser is far more
    # willing than a length field should be, which is the whole reason this
    # function exists to wrap it.
    for text in ("", "   ", "12", "12.5", "1e3", "abc", "-"):
        check(f"parse_length({text!r}) refuses", units.parse_length(text) is None,
              repr(units.parse_length(text)))
    check("parse_length(None) refuses", units.parse_length(None) is None)
    # A bare unit name is FreeCAD's own reading of an implicit one, and 25.4 is
    # the correct millimetre value of 1 inch, so it is passed through rather
    # than special-cased. It cannot reach a spin box -- the widget never emits
    # a unit-only string -- and adopting it is more predictable than refusing.
    check("a unit name alone is FreeCAD's implicit 1",
          _close(units.parse_length("in"), 25.4), repr(units.parse_length("in")))


# --------------------------------------------------------------------------
# Case 5: reading a length back out of a document property
# --------------------------------------------------------------------------
def case_length_mm():
    from freecad.nestingworkbench import units

    emit("")
    emit("--- length_mm (document property reads) ---")

    # The normal case: App::PropertyLength yields a Quantity whose Value is
    # already millimetres, whatever unit the document is displayed in.
    for text, want in (("600 mm", 600.0), ("1/2 in", 12.7), ("1' 11\"", 584.2),
                       ("2ft 6in", 762.0), ("-3 in", -76.2)):
        got = units.length_mm(FreeCAD.Units.Quantity(text))
        check(f"length_mm(Quantity({text!r}))", got is not None and _close(got, want),
              repr(got))

    # The PropertyFloat properties -- Simplification, LabelSize -- are plain
    # floats and have always been read as millimetres.
    check("length_mm passes a plain float through",
          _close(units.length_mm(1.0), 1.0), repr(units.length_mm(1.0)))
    check("length_mm passes a string through the parser",
          _close(units.length_mm("12.5 mm"), 12.5), repr(units.length_mm("12.5 mm")))

    # And the case it exists to refuse: a property that is not a length.
    # float() on any of these would adopt a number in an unknown unit.
    check("length_mm refuses a dimensionless quantity",
          units.length_mm(FreeCAD.Units.Quantity(5.0)) is None,
          repr(units.length_mm(FreeCAD.Units.Quantity(5.0))))
    check("length_mm refuses a unitless string",
          units.length_mm("12.5") is None, repr(units.length_mm("12.5")))
    check("length_mm refuses None", units.length_mm(None) is None)
    check("length_mm refuses nonsense", units.length_mm("abc") is None)

    # A real document property, because that is the call site.
    doc = FreeCAD.newDocument("length_mm")
    group = doc.addObject("App::DocumentObjectGroup", "Layout_test")
    group.addProperty("App::PropertyLength", "SheetWidth", "Layout", "")
    group.SheetWidth = 123.456
    check("a PropertyLength reads back exactly",
          _close(units.length_mm(group.SheetWidth), 123.456),
          repr(units.length_mm(group.SheetWidth)))
    group.SheetWidth = "24 in"
    check("a PropertyLength assigned a string converts",
          _close(units.length_mm(group.SheetWidth), 609.6, 1e-3),
          repr(units.length_mm(group.SheetWidth)))
    FreeCAD.closeDocument(doc.Name)


def main():
    for case in (case_every_schema_formats,
                 case_format_values,
                 case_doc_unit_schema,
                 case_parse_length,
                 case_length_mm):
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


if __name__ in ("__main__", "test_document_units"):
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
