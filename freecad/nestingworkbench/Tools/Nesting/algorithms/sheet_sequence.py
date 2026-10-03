# SPDX-License-Identifier: LGPL-2.1-or-later
"""The sheet sequence: the ordered list of stock sheets a run is cut from.

Sheet i of a layout is cut from row i; once the list runs out, the last row
repeats. A row is the dict write_sheet_instance stamps on a Sheet_N group.
Pure Python apart from Shapely; no FreeCAD import.
"""
import json

from ....datatypes.sheet import Sheet
from ....sheet_library.model import FLIP_CHOICES

ROW_KEYS = ("sheet_id", "sheet_name", "material", "cost", "width", "height", "thickness", "flip")


def custom_row(width, height, thickness, flip="none"):
    """A Custom-size row (no library sheet)."""
    return {"sheet_id": "", "sheet_name": "", "material": "", "cost": 0.0,
            "width": float(width), "height": float(height), "thickness": float(thickness),
            "flip": flip}


def library_row(sheet):
    """A row for a sheet-library record (sheet_library.model record dict).
    *sheet* must be an effective record (model.resolve_sheet / sheet_rows), not
    a variant's stored record.
    """
    return {"sheet_id": sheet["id"], "sheet_name": sheet["name"],
            "material": sheet["material"] or "", "cost": float(sheet["cost"]),
            "width": float(sheet["width"]), "height": float(sheet["height"]),
            "thickness": float(sheet["thickness"]), "flip": sheet["flip"]}


def to_json(rows):
    return json.dumps([{key: row[key] for key in ROW_KEYS} for row in rows])


def from_json(text):
    """Parse to_json output. Raises ValueError on anything that is not a
    non-empty list of rows carrying every ROW_KEYS key with a positive width
    and height and a valid flip."""
    data = json.loads(text)          # json.JSONDecodeError is a ValueError
    if not isinstance(data, list) or not data:
        raise ValueError("sheet sequence must be a non-empty list")
    rows = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict) or any(key not in item for key in ROW_KEYS):
            raise ValueError(f"sheet sequence row {i} is missing keys")
        row = {"sheet_id": str(item["sheet_id"]), "sheet_name": str(item["sheet_name"]),
               "material": str(item["material"]), "cost": float(item["cost"]),
               "width": float(item["width"]), "height": float(item["height"]),
               "thickness": float(item["thickness"]), "flip": str(item["flip"])}
        if row["width"] <= 0 or row["height"] <= 0:
            raise ValueError(f"sheet sequence row {i} has a non-positive size")
        if row["flip"] not in FLIP_CHOICES:
            raise ValueError(f"sheet sequence row {i} has an unknown flip {row['flip']!r}")
        rows.append(row)
    return rows


def sizes(rows):
    """[(width, height), ...] for a list of rows."""
    return [(float(r["width"]), float(r["height"])) for r in rows]


def row_for_index(rows, index):
    """The entry sheet *index* is cut from: rows[index], or the last entry once
    the list runs out. Works for a list of rows or a list of (w, h) sizes."""
    if not rows:
        raise ValueError("sheet sequence is empty")
    return rows[min(index, len(rows) - 1)]


def is_repeat_index(rows, index):
    """True when sheet *index* is cut from the repeating last entry, so every
    later sheet would be the same size."""
    return index >= len(rows) - 1


def assign_origins(sheets):
    """Renumber *sheets* 0..n-1 in list order and lay them out side by side:
    each sheet's origin_x is the previous sheet's origin_x + width + spacing
    (the previous sheet's own spacing). Mutates and returns *sheets*.
    spec_index is left alone: it records which row the sheet was cut from."""
    x = 0.0
    for i, sheet in enumerate(sheets):
        sheet.id = i
        sheet.origin_x = x
        x += sheet.width + sheet.spacing
    return sheets


def open_sheet(sheets, sheet_sizes, spacing=0.0):
    """A new, empty sheet that would follow *sheets*. It is NOT appended; the
    caller appends it when it keeps it."""
    index = len(sheets)
    width, height = row_for_index(sheet_sizes, index)
    sheet = Sheet(index, width, height, spacing=spacing)
    sheet.spec_index = index
    if sheets:
        last = sheets[-1]
        sheet.origin_x = last.origin_x + last.width + last.spacing
    else:
        sheet.origin_x = 0.0
    return sheet


def compact_sheets(sheets):
    """The non-empty sheets, renumbered and re-laid-out (assign_origins).
    A listed sheet opened for a part it could not hold, and never used by a
    later part, is dropped here. Returns a new list."""
    return assign_origins([s for s in sheets if s.parts])


def thickness_mismatch(rows, tol):
    """[(row_number, thickness), ...] for every row whose thickness differs
    from row 1 by more than *tol*; [] when all rows agree. Row numbers are 1-based."""
    first = float(rows[0]["thickness"])
    return [(i, float(r["thickness"])) for i, r in enumerate(rows, start=1)
            if abs(float(r["thickness"]) - first) > tol]


def parts_that_fit_no_sheet(parts, sheet_sizes, default_rotation_steps=1, tol=1e-7):
    """Parts that fit no listed sheet size even when the sheet is empty.

    A part fits an empty rectangular sheet exactly when, at one of its allowed
    rotations, its rotated bounding box is no wider and no taller than the
    sheet. That is the same test the placement bounds check applies, so this is
    exact, not a heuristic. The allowed rotations are the PlacementOptimizer's:
    i * 360 / steps for the part's own rotation_steps, else the default.

    Returns [(type_label, width, height), ...] with one entry per part type
    (master_label, else id), using the unrotated bounding box, in first-seen
    order.
    """
    from shapely.affinity import rotate
    misfits = {}
    for part in parts:
        label = getattr(part, "master_label", None) or part.id
        if label in misfits:
            continue
        poly = part.original_polygon if part.original_polygon is not None else part.polygon
        steps = getattr(part, "rotation_steps", None)
        if steps is None or steps < 1:
            steps = default_rotation_steps
        steps = max(1, int(steps))
        fits = False
        for i in range(steps):
            minx, miny, maxx, maxy = rotate(poly, i * 360.0 / steps, origin="centroid").bounds
            w, h = maxx - minx, maxy - miny
            if any(w <= sw + tol and h <= sh + tol for sw, sh in sheet_sizes):
                fits = True
                break
        if not fits:
            minx, miny, maxx, maxy = poly.bounds
            misfits[label] = (label, maxx - minx, maxy - miny)
    return list(misfits.values())
