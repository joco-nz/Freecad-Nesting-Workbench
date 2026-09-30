# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/units.py

"""
Unit presentation for the Nesting Workbench.

The workbench's contract is that every internal value is millimetres. That is
unchanged and is not negotiable: Shapely polygons, the Minkowski buffers, the
GA fitness terms and every Part.* call all assume a single unit, and threading
units through them would buy nothing but bugs.

So this module is presentation only. It answers two questions and nothing else:

  * which unit system is the document in, so a number can be *shown* the way
    the rest of FreeCAD shows it (``doc_unit_schema``);
  * how should a millimetre value or an area be *written* for a human, and how
    should a typed string be read back into millimetres (``format_length``,
    ``format_area``, ``parse_length``).

It deliberately does not import Qt or FreeCADGui. The spin-box wrapper that
uses it lives in ``length_field.py``, and the two console/dialog message sites
(cam_manager, nesting_logic) use only the formatters, which is what keeps this
module importable from a headless run and from the pytest tier.

Why a per-DOCUMENT schema rather than the global one
----------------------------------------------------
FreeCAD's unit system is a global preference, but each document also carries a
``UnitSystem`` property and the document's value wins -- setting it changes the
global one. A user with a metric model open and an imperial one behind it must
see each in its own units, so the document is the thing to ask. This mirrors
FreeCAD's own CAM workbench, which reads the same property for the same reason
(``Mod/CAM/Path/Tool/Gui/FeedsSpeedsDialog.py``, ``doc_unit_schema``).
"""

import FreeCAD

# Precision used for area strings. schemaTranslate is magnitude-autoscaling, so
# we format the number ourselves rather than letting it choose a unit for
# something it cannot express.
_AREA_DECIMALS = 2


def _schema_name(schema):
    """The human-readable name FreeCAD gives a schema index, or '' if unknown."""
    try:
        names = FreeCAD.Units.listSchemas()
    except Exception:
        return ""
    if isinstance(schema, int) and 0 <= schema < len(names):
        return names[schema] or ""
    return ""


def doc_unit_schema(doc=None):
    """The unit-schema index for ``doc``, falling back to the global setting.

    The document's ``UnitSystem`` enumeration lists the schemas in
    schema-index order, so the position of its current value *is* the index
    ``schemaTranslate`` wants. Returns a usable int in every case, including a
    headless run with no document, because a caller about to build a UI string
    is far better served by an approximate unit than by an exception.
    """
    try:
        if doc is not None:
            names = doc.getEnumerationsOfProperty("UnitSystem")
            current = doc.UnitSystem
            if names and current in names:
                return names.index(current)
    except Exception:
        pass
    try:
        return int(FreeCAD.Units.getSchema())
    except Exception:
        return 0


def _schema_length_unit(schema):
    """A STABLE length unit string for a schema, in millimetres per unit.

    ``schemaTranslate`` picks its unit by magnitude, so a 1 mm value comes back
    as "thou" under the US-customary schema and a 1000 mm value as "yd". Both
    are defensible answers and neither is usable as a label that has to stay
    put while the number changes -- which is exactly what a field suffix or an
    area string needs. So the unit is derived from the schema *name* instead,
    which also survives FreeCAD reordering the enumeration.

    Names come from FreeCAD.Units.listSchemas(), which are short and
    unpunctuated -- 'Centimeter', 'MeterDecimal', 'ImperialCivil' -- and not
    the parenthesised display strings the document's own UnitSystem property
    carries. Punctuation is stripped before matching so 'MeterDecimal' and a
    hypothetical 'Meter decimal' both land.
    """
    squashed = "".join(ch for ch in _schema_name(schema).lower() if ch.isalnum())
    if "civil" in squashed:
        return "ft"
    if "imperial" in squashed:
        return "in"
    if "centimeter" in squashed:
        return "cm"
    if "meterdecimal" in squashed or squashed == "mks":
        return "m"
    return "mm"


def _mm_per_unit(unit):
    """How many millimetres one ``unit`` is. Raises on an unknown unit."""
    return FreeCAD.Units.Quantity(1.0, unit).Value


def _resolve_schema(schema):
    """Turn a document, an index, or None into a schema index."""
    if schema is None:
        return doc_unit_schema()
    if not isinstance(schema, int):
        return doc_unit_schema(schema)
    return schema


def format_length(mm, schema=None):
    """``mm`` rendered in the units of ``schema``, for a message or tooltip.

    Uses FreeCAD's own formatter, so imperial users get the same "1' 11\\" +
    5/8\\"" they see in the property editor rather than a second, subtly
    different dialect invented here.

    ``schema`` may be a document (resolved via doc_unit_schema) or an int. When
    omitted the global schema is used, which is right for callers that have no
    document to hand -- a log line raised during teardown, say.
    """
    try:
        quantity = FreeCAD.Units.Quantity(float(mm), "mm")
        return FreeCAD.Units.schemaTranslate(quantity, _resolve_schema(schema))[0]
    except Exception:
        return "%g mm" % float(mm)


def format_area(mm2, schema=None):
    """``mm2`` rendered in the units of ``schema``, for a message or tooltip.

    FreeCAD cannot do this one for us: ``schemaTranslate`` and
    ``getUserPreferred`` both return ('0.00', 1.0, '') for an Area quantity --
    they are length formatters and quietly produce a wrong answer rather than
    an error, which is why this exists instead of a one-line call.

    The conversion is done through the schema's stable length unit, squared.
    That is exact for the area, and it is why the unit has to be stable: an
    area displayed in a unit that changes with the number is unreadable.
    """
    try:
        mm2 = float(mm2)
        schema = _resolve_schema(schema)
        unit = _schema_length_unit(schema)
        per_unit = _mm_per_unit(unit)
        value = mm2 / (per_unit * per_unit)
        return "%.*f %s\u00b2" % (_AREA_DECIMALS, value, unit)
    except Exception:
        return "%g mm\u00b2" % float(mm2)


def length_mm(value):
    """``value`` as a millimetre float, or None if it is not a length.

    For reading a length out of a document property. ``App::PropertyLength``
    hands back a Quantity, whose ``Value`` is already the internal millimetre
    figure, so the usual case is a one-liner -- but a hand-edited or legacy
    document can hold anything at all under that name, and
    ``float(quantity)`` on a non-Length would adopt a number in whatever unit
    it happened to be carrying.

    A plain float passes through: that is what ``App::PropertyFloat``
    properties hold, and the workbench has always read those as millimetres.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return parse_length(value)
    try:
        unit_type = str(value.Unit.Type)
    except AttributeError:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    except Exception:
        return None
    if unit_type != "Length":
        return None
    try:
        return float(value.Value)
    except Exception:
        return None


def parse_length(text):
    """The millimetre value of a typed length string, or None.

    Accepts every *dimensional* form FreeCAD's own quantity parser does --
    "1/2 in", "2ft 6in", "1' 11\\"", "3.5mm", "1,5 in", "-3 in" -- because
    re-implementing a fraction parser would only ever be a worse version of
    one FreeCAD already ships.

    A bare number is rejected. The parser reads "12.5" as a dimensionless
    12.5, and there is no correct answer without knowing which unit was meant:
    it is 12.5 mm in one document and 12.5 in (317.5 mm) in the next. Callers
    that do have a unit in hand should pass it to Quantity themselves; the
    spin box, which always knows its own schema, does exactly that and so
    never needs this function.

    Empty and unparseable input is rejected for the same reason -- the parser
    reads "" as a denormal float rather than raising, which would sail
    through a range check and then poison the geometry.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        quantity = FreeCAD.Units.Quantity(text)
    except Exception:
        return None
    try:
        if str(quantity.Unit.Type) != "Length":
            return None
    except Exception:
        return None
    try:
        value = float(quantity.Value)
    except Exception:
        return None
    # Quantity.Value is already the internal millimetre figure for a Length,
    # so no conversion is needed. Only non-finite is rejected: zero is a legal
    # Part Spacing, and the denormal a blank string parses to is already gone
    # with the unit check above.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value
