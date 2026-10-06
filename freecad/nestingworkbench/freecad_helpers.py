# SPDX-License-Identifier: LGPL-2.1-or-later
# nestingworkbench/freecad_helpers.py

"""
Shared utility functions for FreeCAD operations used across the Nesting Workbench.
Consolidates common logic that was previously duplicated in multiple modules.
"""

import FreeCAD

def get_view_object(obj):
    """
    Returns obj's ViewObject, or None when there is no GUI to attach one to.

    `hasattr(obj, "ViewObject")` is NOT a sufficient test. Under a GUI-less
    FreeCAD (freecadcmd, FreeCADCmd, `FreeCAD.GuiUp == False`) the attribute
    still exists — it is simply None. Guarding on hasattr alone therefore lets
    a None ViewObject through and the following attribute write raises
    AttributeError. That is how a headless nesting run ended up silently
    producing zero parts: every master container raised on
    `ViewObject.Visibility = True` and was skipped.

    Args:
        obj: A FreeCAD document object, or None.

    Returns:
        The ViewObject, or None if obj is None or has no live view.
    """
    if obj is None:
        return None
    view_object = getattr(obj, "ViewObject", None)
    return view_object if view_object is not None else None

def set_visibility(obj, visible):
    """
    Sets obj's Visibility, tolerating a GUI-less document.

    A no-op when there is no ViewObject. Returns True if the visibility was
    actually applied, so callers can distinguish "set" from "headless no-op"
    if they ever need to.

    Args:
        obj: A FreeCAD document object, or None.
        visible: The bool to assign to Visibility.

    Returns:
        bool — whether a ViewObject was present and updated.
    """
    view_object = get_view_object(obj)
    if view_object is None:
        return False
    view_object.Visibility = visible
    return True

def refresh_gui():
    """Processes pending Qt events, tolerating a GUI-less FreeCAD.

    `FreeCADGui` imports fine under freecadcmd but exposes no `updateGui`, so
    an unguarded call raises AttributeError partway through a nest. That is
    what stopped the GA loop running under test at all: GACoordinator.run()
    died on its first redraw.

    The equivalent of get_view_object's reasoning -- a name being importable
    says nothing about the attribute existing. This is a no-op with no GUI.

    Returns:
        bool — whether a GUI was present and the event queue pumped.
    """
    try:
        import FreeCADGui

        if FreeCADGui is None or not hasattr(FreeCADGui, "updateGui"):
            return False
        FreeCADGui.updateGui()
        return True
    except Exception:
        return False

def get_up_direction_rotation(up_direction):
    """
    Returns a FreeCAD.Rotation that transforms the given up_direction to Z+.

    Args:
        up_direction: One of "Z+", "Z-", "Y+", "Y-", "X+", "X-", or None.

    Returns:
        FreeCAD.Rotation to apply to make the given direction point to Z+.
        Returns identity rotation for Z+ or None.
    """
    if up_direction == "Z+" or up_direction is None:
        return FreeCAD.Rotation()  # Identity - no rotation needed
    elif up_direction == "Z-":
        return FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 180)
    elif up_direction == "Y+":
        return FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), -90)
    elif up_direction == "Y-":
        return FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 90)
    elif up_direction == "X+":
        return FreeCAD.Rotation(FreeCAD.Vector(0, 1, 0), 90)
    elif up_direction == "X-":
        return FreeCAD.Rotation(FreeCAD.Vector(0, 1, 0), -90)
    else:
        FreeCAD.Console.PrintWarning(f"Unknown up_direction '{up_direction}', using Z+\n")
        return FreeCAD.Rotation()

def recursive_delete(doc, obj, protected_names=None, perf_stats=None):
    """
    Recursively deletes a FreeCAD object and all its children from the document.
    Children are deleted first since FreeCAD doesn't cascade deletes.

    Args:
        doc: The FreeCAD document.
        obj: The FreeCAD object to delete.
        protected_names: Optional set of object names to skip (not delete).
        perf_stats: Optional measurement dict. When supplied, a 'doc_objects_deleted'
                    counter is incremented per removeObject call. When None the
                    counting branch is skipped entirely.
    """
    if not obj:
        return

    try:
        obj_name = obj.Name
    except Exception:
        return  # Object already deleted or invalid reference

    if protected_names and obj_name in protected_names:
        return

    # Recursively delete all children first (if it's a group-like object)
    if hasattr(obj, "Group"):
        for child in list(obj.Group):  # Copy list to avoid modification during iteration
            recursive_delete(doc, child, protected_names, perf_stats)

    # Delete the object itself
    try:
        if doc.getObject(obj_name):
            doc.removeObject(obj_name)
            if perf_stats is not None:
                perf_stats['doc_objects_deleted'] = \
                    perf_stats.get('doc_objects_deleted', 0) + 1
    except Exception:
        pass  # Already deleted

def get_layout_group(doc):
    """
    Finds the most relevant layout group in the active document.
    Prioritizes the temporary group (__temp_Layout) if it exists,
    otherwise returns the most recently created Layout_* group.

    Args:
        doc: The FreeCAD document.

    Returns:
        The layout group object, or None if not found.
    """
    if not doc:
        return None

    # Prioritize the temporary group as it's the one being actively worked on
    temp_group = doc.getObject("__temp_Layout")
    if temp_group:
        return temp_group

    # Otherwise, find the most recently created final layout group
    groups = [o for o in doc.Objects if o.isDerivedFrom("App::DocumentObjectGroup")]
    packed_groups = sorted(
        [g for g in groups if g.Label.startswith("Layout_")],
        key=lambda x: x.Name
    )
    if packed_groups:
        return packed_groups[-1]

    return None

def get_sheet_groups(layout_group):
    """
    Gets all the direct child Sheet groups from a layout group, sorted numerically.

    Args:
        layout_group: The parent layout group object.

    Returns:
        Sorted list of Sheet_* group objects.
    """
    if not layout_group:
        return []

    sheet_groups = [obj for obj in layout_group.Group if obj.Label.startswith("Sheet_")]
    sheet_groups.sort(key=lambda g: int(g.Label.split('_')[1]))
    return sheet_groups

def get_nested_containers(sheet_group):
    """
    Gets the nested_* App::Part containers holding a sheet's placed parts.

    Containers live in the sheet's Shapes_* subgroup. Documents where they
    sit directly under the sheet group predate that structure and must be
    converted with the MigrateNestingDocuments macro.

    Args:
        sheet_group: A Sheet_* App::DocumentObjectGroup.

    Returns:
        List of nested_* App::Part container objects.
    """
    containers = []
    for sub_group in sheet_group.Group:
        if (sub_group.isDerivedFrom("App::DocumentObjectGroup")
                and sub_group.Label.startswith("Shapes_")):
            containers.extend(
                obj for obj in sub_group.Group
                if obj.TypeId == "App::Part" and obj.Label.startswith("nested_")
            )
    return containers

def get_all_objects_recursive(group):
    """
    Recursively finds all leaf objects within a group and its subgroups.

    Args:
        group: The parent group object.

    Returns:
        List of all non-group objects found recursively.
    """
    all_objects = []
    for obj in group.Group:
        if obj.isDerivedFrom("App::DocumentObjectGroup"):
            all_objects.extend(get_all_objects_recursive(obj))
        else:
            all_objects.append(obj)
    return all_objects

def calculate_label_placement(shapestring_center, container_rotation, label_z_offset=0.1):
    """
    Calculates the local placement for a label centered on a part.
    
    Args:
        shapestring_center (FreeCAD.Vector): The center of the label's bound box.
        container_rotation (FreeCAD.Rotation): The rotation of the parent container.
        label_z_offset (float): Vertical offset above the part.
        
    Returns:
        FreeCAD.Placement: The local placement for the label.
    """
    inverse_rotation = container_rotation.inverted()
    target_label_center = FreeCAD.Vector(0, 0, label_z_offset)
    shapestring_center_rotated = inverse_rotation.multVec(shapestring_center)
    label_placement_base = target_label_center - shapestring_center_rotated
    return FreeCAD.Placement(label_placement_base, inverse_rotation)

def calculate_container_centroid(polygon, sheet_origin):
    """
    Calculates the target world position for a container based on the polygon's centroid.
    
    Args:
        polygon (shapely.geometry.Polygon): The nesting boundary polygon.
        sheet_origin (FreeCAD.Vector): The origin of the sheet.
        
    Returns:
        FreeCAD.Vector: The target world position.
    """
    nested_centroid_shapely = polygon.centroid
    nested_centroid = FreeCAD.Vector(nested_centroid_shapely.x, nested_centroid_shapely.y, 0)
    return sheet_origin + nested_centroid

def create_part_feature(doc, name, shape, group=None, visible=True):
    """
    Standardises Part Feature creation, shape assignment, grouping, and visibility.
    """
    obj = doc.addObject("Part::Feature", name)
    obj.Shape = shape
    if group:
        group.addObject(obj)
    set_visibility(obj, visible)
    return obj


#: Tolerance for `is_rigid_matrix`. Loose enough for the accumulated rounding of
#: a composed matrix, tight enough that a real scale -- the smallest anyone would
#: type into a placement, and `Placement` cannot express one at all -- is many
#: orders of magnitude outside it.
RIGID_TOLERANCE = 1e-9


def is_rigid_matrix(matrix):
    """True if `matrix` is a rotation plus a translation, with no scale or shear.

    Exists because `Shape.transformShape` **requires** a rigid matrix and applies
    a non-rigid one as though it were exact. `transformGeometry` re-fits geometry
    and so tolerated anything -- which is exactly why it was the wrong call
    wherever a rigid motion was wanted, and replacing it makes that assumption
    load-bearing enough to check rather than trust.

    Two parts, and both are needed:

    * `hasScale()` is FreeCAD's own scale flag -- measured 0 for a rigid matrix, 3
      for a uniform scale, and **-1 for a shear**, so a non-zero result covers
      both kinds of distortion. `isOrthogonal()` is deliberately *not* used: it
      returns 0.0 for a rigid, a scaled and a sheared matrix alike in FreeCAD
      26.3, so it would pass everything, and its name invites relying on it.
    * `determinant()` catches a reflection, which preserves lengths and so reports
      no scale, but has determinant -1 and mirrors the part.

    Both predicates were exercised against all three counter-examples -- a scale, a
    shear and a reflection -- before being relied on; see
    `tests/freecad_harness/test_shape_preparer_rigid.py`.
    """
    if matrix.hasScale() != 0:
        return False
    return abs(matrix.determinant() - 1.0) <= RIGID_TOLERANCE


def bake_rigid(shape, placement, what="this geometry"):
    """Return `shape` moved rigidly, without re-fitting its surfaces.

    The one place the workbench applies a rigid motion to geometry for its own
    sake, so the reason is stated once rather than at every call site:

    * `transformGeometry` **re-fits** -- it converts analytic surfaces into
      B-splines, so a `Cylinder` face the user modelled came back approximated.
      Measured on a plate with a hole: every face became a `BSplineSurface`, the
      top face's area moved 921.4602 -> 921.7994 and the cylinder's 188.4956 ->
      189.0014 (+0.2684%, and systematic -- identical at cylinder radius 1 mm and
      50 mm), and the solid's volume moved 5528.7611 -> 5524.6221. CAM offsets a
      toolpath from the real surface, so this makes the machine follow an
      approximation of what was drawn. It is also ~10x slower and makes
      `shape.slice()` ~1.8x slower.
    * `transformShape` moves the geometry and keeps the surfaces. It requires a
      rigid matrix, which is what the check here is for.

    **Why not assign `shape.Placement`**, which is the obvious first answer for a
    rigid motion. Because the transform has to be *baked*: callers reset the
    placement afterwards -- `shape_preparer._handle_new_master` sets it to
    `(0, 0, 0)` plus the up-direction rotation -- and a centring left in the
    placement is discarded by that. Measured: a Placement-assigned centring moves
    5.000 mm when that reset runs, and the baked one does not move at all.

    `placement` may be a `FreeCAD.Placement` or a `FreeCAD.Matrix`; both are
    checked, because `Placement` guarantees rigidity and a `Matrix` does not.

    **The shape is modified in place and returned** -- measured: the input's
    bounding box moves by the full transform and its `Placement` becomes the
    applied one. `transformShape`'s `copy=True` does *not* mean "leave the
    original alone"; `transformGeometry` does leave it alone, which is the
    opposite, and the difference was worth measuring rather than assuming. So
    **pass a copy the caller owns**, which is what every call site does.

    Not copying internally on purpose: all three call sites already hand over a
    shape they made for the purpose, so an internal copy would be a second copy
    of every part in every nest for nothing.
    """
    matrix = placement.toMatrix() if hasattr(placement, "toMatrix") else placement
    if not is_rigid_matrix(matrix):
        raise ValueError(
            "%s needs a rigid transform (rotation and translation only) but was "
            "given a matrix that scales or shears. transformShape would apply it "
            "as though exact and silently distort the geometry." % what)
    return shape.transformShape(matrix, True)


