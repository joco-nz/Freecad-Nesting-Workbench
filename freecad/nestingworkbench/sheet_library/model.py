# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/sheet_library/model.py
"""Library sheets: one flat record per stock sheet.

Pure Python, no FreeCAD or Qt, so it is unit testable. Disk I/O is in store.py.
A sheet is a container of properties: material (a name), size, thickness, price,
grain and flip, plus feature lists (keep-outs, locator holes, images) that later
lists define. A *variant* (`parent_id` set) stores only the properties it
overrides and inherits the rest from its parent chain (resolve_sheet).
"""

import copy
from collections import namedtuple

SCHEMA_VERSION = 3

# Feature lists that later lists define (keep-outs: KO-/_faces, images:
# SHLIB-004, locator holes: not yet planned). Until then each must be an empty
# list, so a record from a newer version is reported instead of being nested
# without its keep-outs.
FEATURE_FIELDS = ("keepouts", "locators", "images")

# Properties a variant inherits from its parent unless it sets them itself. A root
# sheet (parent_id None) sets every one. id, name, notes, parent_id and
# FEATURE_FIELDS belong to the record and are never inherited.
INHERITABLE_FIELDS = ("material", "width", "height", "thickness", "cost", "grain_angle", "flip")

# Turning the sheet over to machine its back: "x" turns it end to end (top edge
# to bottom edge), "y" side to side (left edge to right edge). "none" = one side.
FLIP_CHOICES = ("none", "x", "y")

# Seed for a new materials list (store.load_materials). Stored as typed, so they
# are data, not translated UI text.
DEFAULT_MATERIALS = ("Acrylic", "Aluminium", "HDPE", "Hardwood", "MDF", "Plywood", "Softwood")

# sheets: dict id -> record. problems: list of str (files that were skipped, for
# display). skipped: tuple of the skipped files' names, one per problem, same order.
Library = namedtuple("Library", ["sheets", "problems", "skipped"], defaults=((),))


def new_sheet(new_id, name="", material="", width=2440.0, height=1220.0,
              thickness=18.0, cost=0.0, grain_angle=0.0, notes="", flip="none"):
    """A new, unsaved root sheet record with every key present."""
    return {"schema": SCHEMA_VERSION, "id": new_id, "name": name, "parent_id": None,
            "material": material, "width": float(width), "height": float(height),
            "thickness": float(thickness), "cost": float(cost), "grain_angle": grain_angle,
            "flip": flip, "keepouts": [], "locators": [], "images": [], "notes": notes}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def resolve_sheet(sheet, library):
    """(effective record, None), or (None, error) when the parent chain is broken.

    The effective record is a deep copy of *sheet* with each INHERITABLE_FIELDS
    key it lacks taken from the nearest ancestor that sets it, so it has a root
    sheet's keys. A root (parent_id None or missing) resolves to a copy of itself;
    a key it lacks stays missing, for validate_sheet to report. *sheet* may be
    unsaved: its own dict is the leaf, never library.sheets[sheet["id"]].
    """
    chain = [sheet]
    seen = {sheet.get("id")}
    parent_id = sheet.get("parent_id")
    while parent_id is not None:
        if not isinstance(parent_id, str):
            return None, "parent_id is not text"
        if parent_id in seen:
            return None, "it inherits from itself"
        parent = library.sheets.get(parent_id)
        if parent is None:
            return None, "a sheet it inherits from is no longer in the library"
        chain.append(parent)
        seen.add(parent_id)
        parent_id = parent.get("parent_id")
    effective = copy.deepcopy(sheet)
    for key in INHERITABLE_FIELDS:
        if key in effective:
            continue
        source = next((record for record in chain[1:] if key in record), None)
        if source is not None:
            effective[key] = copy.deepcopy(source[key])
    return effective, None


def validate_sheet(sheet, library):
    """Errors that block saving or nesting *sheet*; empty when valid.
    A missing key is an error, not a default, except that a variant may leave out any
    INHERITABLE_FIELDS key: the property checks read the resolved record
    (resolve_sheet). *library* supplies the parent chain and the unique-name check; a
    record with the same id as *sheet* is ignored for names.
    """
    errors = []
    if not isinstance(sheet.get("name"), str):
        errors.append("name is not text")
    else:
        name = sheet["name"].strip()
        if not name:
            errors.append("name is empty")
        elif any(s["id"] != sheet["id"] and isinstance(s.get("name"), str) and s["name"].strip().casefold() == name.casefold()
                 for s in library.sheets.values()):
            errors.append(f"another sheet is already called '{name}'")

    if "parent_id" not in sheet:
        errors.append("parent_id is missing")

    if not isinstance(sheet.get("notes"), str):
        errors.append("notes is missing or not text")
    for key in FEATURE_FIELDS:
        value = sheet.get(key)
        if not isinstance(value, list):
            errors.append(f"{key} is missing or not a list")
        elif value:
            errors.append(f"{key} are not supported by this version of the workbench")

    effective, chain_error = resolve_sheet(sheet, library)
    if chain_error:
        errors.append(chain_error)
        return errors

    if not isinstance(effective.get("material"), str):
        errors.append("material is not text")

    for key in ("width", "height", "thickness"):
        if not (_is_number(effective.get(key)) and effective[key] > 0):
            errors.append(f"{key} must be greater than 0")
    if not (_is_number(effective.get("cost")) and effective["cost"] >= 0):
        errors.append("price must be 0 or more")
    if "grain_angle" not in effective:
        errors.append("grain angle is missing")
    else:
        grain = effective["grain_angle"]
        if grain is not None and not (_is_number(grain) and 0 <= grain < 180):
            errors.append("grain angle must be at least 0 and less than 180, or none")

    if effective.get("flip") not in FLIP_CHOICES:
        errors.append("flip must be none, x or y")

    return errors


def find_material(materials, name):
    """The list's spelling of *name*, matched case-insensitively, or None.

    An empty or blank *name* is never found.
    """
    key = name.strip().casefold()
    if not key:
        return None
    return next((m for m in materials if m.casefold() == key), None)


def validate_material_name(name, materials, old=None):
    """Errors that block adding *name*, or renaming *old* to it; empty when valid."""
    name = name.strip()
    if not name:
        return ["name is empty"]
    others = [m for m in materials if old is None or m.casefold() != old.casefold()]
    clash = find_material(others, name)
    if clash is not None:
        return [f"a material called '{clash}' already exists"]
    return []


def material_users(library, name):
    """Sheets whose material is *name* (case-insensitive), sorted by sheet name then id.

    Only sheets that set the material themselves; variants that inherit it follow
    a rename without being rewritten.
    """
    key = name.strip().casefold()
    if not key:
        return []
    return sorted(
        (s for s in library.sheets.values()
         if isinstance(s.get("material"), str) and s["material"].strip().casefold() == key),
        key=lambda s: (str(s.get("name", "")).casefold(), s["id"]))


def material_effective_users(library, name):
    """Sheets whose effective material (resolve_sheet) is *name*, case-insensitive,
    sorted by sheet name then id. Unlike material_users this includes variants that
    inherit it. A sheet that doesn't resolve is left out."""
    key = name.strip().casefold()
    if not key:
        return []
    users = []
    for sheet in library.sheets.values():
        effective, _error = resolve_sheet(sheet, library)
        material = effective.get("material") if effective is not None else None
        if isinstance(material, str) and material.strip().casefold() == key:
            users.append(sheet)
    return sorted(users, key=lambda s: (str(s.get("name", "")).casefold(), s["id"]))


def sheet_rows(library):
    """Sheets in display order: (depth, sheet_id, name, record, error or None).

    Roots sorted by name (case-insensitive) then id, each followed by its
    variants, recursively, at depth + 1. A sheet not reachable from a root (its
    parent is missing, or it is in a cycle) is listed at depth 0 after the tree,
    followed by its own variants. *record* is the effective record
    (resolve_sheet) when it resolves, else the stored record. *error* joins
    validate_sheet's messages with "; ".
    """
    def order(s):
        return (str(s.get("name", "")).casefold(), s["id"])

    children = {}
    for s in library.sheets.values():
        parent = s.get("parent_id")
        key = parent if parent is None or isinstance(parent, str) else ("invalid",)
        children.setdefault(key, []).append(s)

    rows, visited = [], set()

    def add(sheet, depth):
        visited.add(sheet["id"])
        effective, _error = resolve_sheet(sheet, library)
        errors = validate_sheet(sheet, library)
        rows.append((depth, sheet["id"], sheet.get("name", ""),
                     effective if effective is not None else sheet,
                     "; ".join(errors) or None))
        for child in sorted(children.get(sheet["id"], []), key=order):
            if child["id"] not in visited:
                add(child, depth + 1)

    for root in sorted(children.get(None, []), key=order):
        add(root, 0)
    for s in sorted(library.sheets.values(), key=order):
        if s["id"] not in visited:
            add(s, 0)
    return rows


def duplicate_sheet(sheet, name, new_id):
    """An independent copy of *sheet* with a new id and name; created/modified dropped. A variant's copy keeps its parent_id and overrides, so it is a sibling variant (SVF decision 4)."""
    copied = copy.deepcopy(sheet)
    copied.pop("created", None)
    copied.pop("modified", None)
    copied.update(schema=SCHEMA_VERSION, id=new_id, name=name)
    return copied


def make_variant(parent, name, new_id):
    """A new, unsaved variant of the *parent* record that overrides nothing yet."""
    return {"schema": SCHEMA_VERSION, "id": new_id, "name": name, "parent_id": parent["id"],
            "keepouts": [], "locators": [], "images": [], "notes": ""}


def direct_variants(library, sheet_id):
    """Records whose parent_id is *sheet_id*, sorted by name (case-insensitive) then id."""
    return sorted((s for s in library.sheets.values() if s.get("parent_id") == sheet_id),
                  key=lambda s: (str(s.get("name", "")).casefold(), s["id"]))


def detach_variants(library, sheet_id):
    """The direct variants of *sheet_id*, rewritten so they no longer depend on it.

    Each is a deep copy with parent_id None and every INHERITABLE_FIELDS key it
    lacks filled from *sheet_id*'s effective record (resolve_sheet), or from
    *sheet_id*'s own record when that doesn't resolve. A key neither has stays
    missing, for validate_sheet to report. Sorted like direct_variants. Writes nothing.
    """
    parent = library.sheets[sheet_id]
    source, _error = resolve_sheet(parent, library)
    if source is None:
        source = parent
    detached = []
    for variant in direct_variants(library, sheet_id):
        record = copy.deepcopy(variant)
        record["parent_id"] = None
        for key in INHERITABLE_FIELDS:
            if key not in record and key in source:
                record[key] = copy.deepcopy(source[key])
        detached.append(record)
    return detached


def format_cost(value, symbol=""):
    """*value* as money: the currency *symbol*, thousands separators, two decimals."""
    return f"{symbol}{value:,.2f}"
