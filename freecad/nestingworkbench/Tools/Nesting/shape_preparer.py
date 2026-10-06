# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD
import Part
import copy
import Draft
import time
import traceback
from .algorithms import shape_processor
from .algorithms import minkowski_utils
from ...datatypes.shape_object import create_shape_object
from ...datatypes.shape import Shape
from ...freecad_helpers import (
    get_up_direction_rotation,
    create_part_feature,
    get_view_object,
    set_visibility,
)

#: Tolerance for the rigidity test below. Loose enough for the accumulated
#: rounding of a composed matrix, tight enough that a real scale -- the smallest
#: anyone would type into a placement, and `Placement` cannot express one at all
#: -- is many orders of magnitude outside it.
_RIGID_TOLERANCE = 1e-9


def _is_rigid(matrix):
    """True if `matrix` is a rotation plus a translation, with no scale or shear.

    Guards `transformShape`, which **requires** a rigid matrix and applies a
    non-rigid one as though it were exact. `transformGeometry` would have
    re-fitted the geometry and so tolerated anything; that is precisely why it
    was wrong here, and replacing it with `transformShape` makes the assumption
    load-bearing enough to check rather than trust.

    Two parts, and both are needed:

    * `hasScale()` is FreeCAD's own scale flag -- measured 0 for a rigid matrix,
      3 for a uniform scale, and **-1 for a shear**, so a non-zero result covers
      both kinds of distortion. `isOrthogonal()` is *not* used: it returns 0.0
      for a rigid, a scaled and a sheared matrix alike in FreeCAD 26.3, so it
      would pass everything and its name invites relying on it.
    * `determinant()` catches a reflection, which preserves lengths and so
      reports no scale, but has determinant -1 and mirrors the part.

    Both predicates were exercised against all three counter-examples before
    being used; see `tests/test_shape_preparer_rigid/test_shape_preparer_rigid.py`.
    """
    if matrix.hasScale() != 0:
        return False
    return abs(matrix.determinant() - 1.0) <= _RIGID_TOLERANCE


class ShapePreparer:
    """
    Handles the preparation of shapes for nesting.
    - Creates 'Master' FreeCAD objects.
    - Manages the 'MasterShapes' group.
    - Creates the temporary Shape instances used by the algorithm.
    """
    def __init__(self, doc, processed_shape_cache, perf_stats=None,
                 create_doc_objects=True, master_pool=None,
                 shared_master_group=None):
        self.doc = doc
        self.processed_shape_cache = processed_shape_cache
        # Optional run-scoped measurement sink (Performance Logging only).
        self._perf_stats = perf_stats
        # When False, parts are Shapely-only: no Part::Feature is created for
        # them and shape.fc_object stays None. GA layouts use this because the
        # nester only ever reads shape.polygon — it deep-copies parts in
        # non-simulate mode, and Shape.__deepcopy__ drops fc_object anyway,
        # so the per-layout FreeCAD objects are pure overhead. The winning
        # layout is materialized once at commit; see
        # LayoutManager.materialize_layout_objects.
        self.create_doc_objects = create_doc_objects
        # master label -> master Part::Feature, captured during
        # prepare_parts so a headless layout can be materialized later.
        self._last_master_objects = {}
        # cache_key -> (master_shape_obj, temp_shape_wrapper), shared across
        # every layout of a run. Master objects depend only on the source shape
        # and the geometry settings, so they are identical for all layouts and
        # are built once instead of per layout. See LayoutManager.
        self.master_pool = master_pool if master_pool is not None else {}
        # Document-level group the pooled masters are parented into. None only
        # when a caller wants no sharing at all, which the GA never does.
        self.shared_master_group = shared_master_group
        # Pooling is gated on HAVING a shared group, not on create_doc_objects.
        # It used to be `not create_doc_objects`, which excluded simulate mode,
        # and the stated reason was that "simulate must own its own masters --
        # it draws as it nests, so tearing one down must not remove objects
        # another is still using". That is a true statement about *lifetime*,
        # and the shared group satisfies it: a master parented there is
        # unreachable by recursive_delete of any layout group, so no layout's
        # teardown can destroy another's master. Measured on n70 at pop 10 x
        # gen 4, this takes simulate's master_containers from 111 to 3.
        #
        # Sharing the object was never the hazard. `_arrange_masters` runs only
        # for masters this call built (`if newly_built`), so a pooled container
        # keeps the placement it was given when first built, and part features
        # copy `master.Placement` at creation -- every layout agrees.
        self.pool_masters = shared_master_group is not None

    def _perf_add(self, key, seconds):
        stats = self._perf_stats
        if stats is not None:
            stats[key] = stats.get(key, 0.0) + seconds

    def _perf_inc(self, key, count=1):
        stats = self._perf_stats
        if stats is not None:
            stats[key] = stats.get(key, 0) + count

    def prepare_parts(self, ui_global_settings, quantities, master_shapes_map, layout_obj, parts_group):
        """
        Main entry point to prepare parts.
        
        Args:
            ui_global_settings (dict): { 'spacing': float, 'deflection': float, 'simplification': float, 'rotation_steps': int, 'add_labels': bool, 'font_path': str, 'verbose': bool }
            quantities (dict): { label: {'quantity': int, 'rotation_steps': int, 'up_direction': str, 'fill_sheet': bool, 'rotation_steps_override': int, 'rotation_override': bool} }
                `rotation_steps` is the resolved count the nester uses.
                `rotation_steps_override` / `rotation_override` are the raw
                spinbox reading and the checkbox, written to the master
                container as `PartRotationSteps` / `PartRotationOverride` so a
                reopened layout restores the control rather than a number.
                See issues.md NEST-002.
            master_shapes_map (dict): { label: FreeCADObject }
            layout_obj (App::DocumentObjectGroup): The layout group.
            parts_group (App::DocumentObjectGroup): The PartsToPlace group to add temp instances to.
        
        Returns:
            list[Shape]: List of prepared Shape objects for the nester.
        """
        spacing = ui_global_settings['spacing']
        deflection = ui_global_settings.get('deflection', 0.05)
        simplification = ui_global_settings.get('simplification', 0.1)
        verbose = ui_global_settings.get('verbose', False)

        self._last_master_objects = {}
        # Headless: masters are pooled in a document-level group shared by all
        # layouts, so no per-layout group is created. Simulate: masters belong
        # to this layout and go under its own group.
        if self.shared_master_group is not None:
            master_shapes_group = self.shared_master_group
        else:
            master_shapes_group = self._get_or_create_master_group(layout_obj)

        master_shape_obj_map = {} # Maps original FreeCAD object ID to the new master ShapeObject
        master_geometry_cache = {} # Maps original FreeCAD object ID to the processed Shape wrapper
        masters_to_place = []
        # Containers this call is responsible for positioning. Pooled masters
        # are excluded, so _arrange_masters only ever moves objects this layout
        # created.
        newly_built = []

        master_start = time.perf_counter()
        for label, master_obj in master_shapes_map.items():
            try:
                # Get up_direction for cache key
                part_params = quantities.get(label, {'up_direction': 'Z+'})
                if isinstance(part_params, tuple):
                    up_direction = 'Z+'
                else:
                    up_direction = part_params.get('up_direction', 'Z+')
                
                # Cache Key: (Object Name, Spacing, Deflection, Simplification, UpDirection)
                # Updated cache key to respect new parameters
                cache_key = (master_obj.Name, spacing, deflection, simplification, up_direction)
                is_reloading = master_obj.Label.startswith("master_shape_")
                
                # Pooled master hit: the objects already exist and do not
                # depend on this layout, so reuse them wholesale and skip the
                # whole create path below. This is the branch that turns 57
                # master builds per run into 3.
                pooled = self.master_pool.get(cache_key) \
                    if self.pool_masters else None
                if pooled is not None:
                    master_shape_obj, pooled_wrapper = pooled
                    # Only the FreeCAD master object is shared. The geometry
                    # wrapper is copied per layout, exactly as the
                    # processed_shape_cache path below does.
                    #
                    # Measured, not assumed: with the wrapper shared outright,
                    # all 122 part polygons ARE the same objects across
                    # layouts. Nothing leaks, because set_rotation() and
                    # move()/move_to() rebind shape.polygon from
                    # shape.original_polygon rather than mutating in place
                    # (/tmp/opencode/probe_alias.py). So this copy is not
                    # fixing an observed bug -- it is keeping the per-layout
                    # isolation the cache path already provided, for the cost
                    # of one small 2D deepcopy. The saving here is entirely
                    # the avoided master_obj.Shape.copy() on a 192-face
                    # solid, so paying microseconds to not weaken an
                    # invariant is the right trade.
                    temp_shape_wrapper = copy.deepcopy(pooled_wrapper)
                    temp_shape_wrapper.source_freecad_object = master_obj
                    self._perf_inc('lm_masters_pooled')
                    master_shape_obj_map[id(master_obj)] = master_shape_obj
                    master_geometry_cache[id(master_obj)] = temp_shape_wrapper
                    if master_shape_obj.InList:
                        masters_to_place.append(
                            (master_shape_obj.InList[0], temp_shape_wrapper))
                    continue

                temp_shape_wrapper = None
                
                # Check Cache
                if cache_key in self.processed_shape_cache:
                    temp_shape_wrapper = copy.deepcopy(self.processed_shape_cache[cache_key])
                    temp_shape_wrapper.source_freecad_object = master_obj
                
                if is_reloading:
                    master_shape_obj, temp_shape_wrapper = self._create_temp_from_reloading(
                        master_obj, label, quantities, temp_shape_wrapper, spacing, deflection, simplification, cache_key, layout_obj, master_shapes_group, verbose=verbose
                    )
                else:
                    master_shape_obj, temp_shape_wrapper = self._handle_new_master(
                        master_obj, label, quantities, temp_shape_wrapper, spacing, deflection, simplification, cache_key, master_shapes_group, is_reloading, verbose=verbose
                    )

                if master_shape_obj and temp_shape_wrapper:
                    master_shape_obj_map[id(master_obj)] = master_shape_obj
                    master_geometry_cache[id(master_obj)] = temp_shape_wrapper
                    # Hand the wrapper to the next layout that needs this
                    # master, so it can skip the build entirely. Every mode:
                    # the masters live in the shared group, so no layout owns
                    # them and none can destroy another's on teardown.
                    if self.pool_masters:
                        self.master_pool.setdefault(
                            cache_key, (master_shape_obj, temp_shape_wrapper))
                    
                    # Need container for sorting/placing
                    if master_shape_obj.InList:
                        masters_to_place.append((master_shape_obj.InList[0], temp_shape_wrapper))
                        newly_built.append(master_shape_obj.InList[0])

            except Exception as e:
                FreeCAD.Console.PrintError(f"Could not create boundary for '{master_obj.Label}', it will be skipped. Error: {e}\n{traceback.format_exc()}\n")
                continue
        
        # Arrange only the masters this call actually built. Pooled masters were
        # positioned when first built and arranging them again would fight the
        # shared ownership (several layouts each claiming to place the same
        # container), which is a needless write rather than a saving, but it is
        # also wrong to let a no-op layout perturb a container another live
        # layout still points at.
        if newly_built:
            self._arrange_masters(masters_to_place, spacing)
        self._perf_add('lm_master_prepare_s', time.perf_counter() - master_start)
        self._perf_inc('lm_masters_processed', len(master_shapes_map))

        # Whether an interior ring is dead depends on the SMALLEST part in the
        # whole set, which is not known until every master has been prepared --
        # hence publishing here, after the master loop.
        self._publish_dead_ring_profiles(master_geometry_cache)

        instances_start = time.perf_counter()
        parts_to_nest = self._create_nesting_instances(
            master_shapes_map, 
            quantities, 
            master_shape_obj_map, 
            master_geometry_cache, 
            ui_global_settings,
            parts_group
        )
        self._perf_add('lm_part_instances_s', time.perf_counter() - instances_start)

        return parts_to_nest

    def _publish_dead_ring_profiles(self, master_geometry_cache):
        """Hand the part set's extents to the NFP decomposition.

        Deliberately NOT undone at the end of this method. The decomposition
        runs during find_best_placement, long after prepare_parts has returned,
        so clearing here would disable the optimisation for the entire actual
        nesting run.

        Shape.clear_caches() is not an alternative owner, but not for the
        reason once recorded here: it is not called between GA generations at
        all -- NestingController.execute_nesting is its only caller and it runs
        once, before the GA starts, so clearing there would merely be undone by
        the very next prepare_parts. This method's opening clear is what actually
        prevents a run with pruning switched off from inheriting a previous
        run's part set.

        This method also deliberately does NOT reset the counters. It runs once
        per layout while the counting only happens during generation 1 (the NFP
        cache is warm from generation 2 on, so decompose_if_needed is never
        reached). reset_dead_ring_stats, called once per run from
        execute_nesting, is the owner.
        """
        # Unconditional: a run that has pruning switched off must not inherit a
        # previous run's part set.
        minkowski_utils.clear_dead_ring_profiles()
        if not minkowski_utils.dead_ring_pruning_requested():
            return
        if not master_geometry_cache:
            return
        profiles = []
        seen = set()
        for wrapper in master_geometry_cache.values():
            poly = getattr(wrapper, 'original_polygon', None)
            if poly is None or poly.is_empty:
                continue
            minx, miny, maxx, maxy = poly.bounds
            key = (round(maxx - minx, 9), round(maxy - miny, 9), round(poly.area, 9))
            if key in seen:
                continue
            seen.add(key)
            profiles.append(key)
        if not profiles:
            return
        minkowski_utils.set_dead_ring_profiles(profiles)
        FreeCAD.Console.PrintMessage(
            "  -> Dead-ring pruning active for %d distinct part profile(s)\n"
            % len(profiles))

    def _get_or_create_master_group(self, layout_obj):
        master_shapes_group = None
        for child in layout_obj.Group:
             if child.Label == "MasterShapes":
                 master_shapes_group = child
                 break
        
        if not master_shapes_group:
            master_shapes_group = self.doc.addObject("App::DocumentObjectGroup", "MasterShapes")
            master_shapes_group.Label = "MasterShapes"
            layout_obj.addObject(master_shapes_group)
            self._perf_inc('lm_master_group_objects_created')
        
        # Make MasterShapes visible during nesting (will be hidden after commit)
        set_visibility(master_shapes_group, True)
        return master_shapes_group

    def _create_temp_from_reloading(self, master_obj, label, quantities, temp_shape_wrapper, spacing, deflection, simplification, cache_key, layout_obj, master_shapes_group, verbose=False):
        """
        Creates a temporary copy of an existing master shape for use in the sandbox.
        """
        original_label = label.replace("master_shape_", "")
        
        # Find the original container (parent of master_obj)
        original_container = None
        if master_obj.InList:
            for parent in master_obj.InList:
                if hasattr(parent, "SourceCentroid"):
                    original_container = parent
                    break
        
        temp_container = self.doc.addObject("App::Part", f"temp_master_{original_label}")
        master_shapes_group.addObject(temp_container)
        self._perf_inc('lm_master_containers_created')
        
        # *** CLEAN OFFSET DESIGN ***
        temp_container.addProperty("App::PropertyVector", "SourceCentroid", "Nesting", "Original geometry center")
        if original_container and hasattr(original_container, "SourceCentroid"):
            temp_container.SourceCentroid = original_container.SourceCentroid
        else:
            bb = master_obj.Shape.BoundBox
            temp_container.SourceCentroid = FreeCAD.Vector(
                 (bb.XMin + bb.XMax) / 2,
                 (bb.YMin + bb.YMax) / 2,
                 (bb.ZMin + bb.ZMax) / 2
            )
        
        temp_master_obj = create_part_feature(
            self.doc, f"temp_shape_{original_label}", master_obj.Shape.copy(), group=temp_container, visible=True
        )
        self._perf_inc('lm_master_part_features_created')
        temp_master_obj.Label = f"master_shape_{original_label}"
        # Center the shape at the container's origin
        source_centroid = temp_container.SourceCentroid
        temp_master_obj.Placement = FreeCAD.Placement(source_centroid.negative(), FreeCAD.Rotation())
        
        if hasattr(master_obj, "BoundaryObject") and master_obj.BoundaryObject:
            temp_bound = create_part_feature(
                self.doc, f"temp_boundary_{original_label}", master_obj.BoundaryObject.Shape.copy(), group=temp_container, visible=False
            )
            self._perf_inc('lm_master_boundary_features_created')
            
            if not hasattr(temp_master_obj, "BoundaryObject"):
                temp_master_obj.addProperty("App::PropertyLink", "BoundaryObject", "Nesting", "Boundary object")
            temp_master_obj.BoundaryObject = temp_bound
        
        # Shape visible, container visible during nesting
        set_visibility(temp_master_obj, True)
        set_visibility(temp_container, True)

        part_params = quantities.get(original_label, {'quantity': 1})
        if isinstance(part_params, tuple):
            quantity = part_params[0]
        else:
            quantity = part_params.get('quantity', 1)
        temp_container.addProperty("App::PropertyInteger", "Quantity", "Nest", "Number of instances").Quantity = quantity

        temp_shape_wrapper = None
        if hasattr(temp_master_obj, "BoundaryObject") and temp_master_obj.BoundaryObject:
            try:
                bound_shape = temp_master_obj.BoundaryObject.Shape
                wires = bound_shape.Wires
                wires.sort(key=lambda w: w.Length, reverse=True)
                
                final_poly = shape_processor.discretize_wires_to_polygon(wires, deflection)
                if final_poly:
                    temp_shape_wrapper = Shape(temp_master_obj)
                    temp_shape_wrapper.polygon = final_poly
                    temp_shape_wrapper.source_centroid = temp_container.SourceCentroid
                    self.processed_shape_cache[cache_key] = copy.deepcopy(temp_shape_wrapper)
            except Exception as e:
                FreeCAD.Console.PrintWarning(f"Shape reload failed for '{label}': {e}\n{traceback.format_exc()}. Recalculating.\n")
                temp_shape_wrapper = None
        
        if not temp_shape_wrapper:
            temp_shape_wrapper = Shape(temp_master_obj)
            shape_processor.create_single_nesting_part(temp_shape_wrapper, temp_master_obj, spacing, deflection, simplification, verbose=verbose)
            # Update the container's SourceCentroid with the recalculated value
            if temp_shape_wrapper.source_centroid:
                temp_container.SourceCentroid = temp_shape_wrapper.source_centroid
            self.processed_shape_cache[cache_key] = copy.deepcopy(temp_shape_wrapper)

        return temp_master_obj, temp_shape_wrapper

    def _handle_new_master(self, master_obj, label, quantities, temp_shape_wrapper, spacing, deflection, simplification, cache_key, master_shapes_group, is_reloading, verbose=False):
        if not temp_shape_wrapper:
            # Get up_direction for initial processing
            part_params = quantities.get(label, {'up_direction': 'Z+'})
            up_direction = part_params[0] if isinstance(part_params, tuple) else part_params.get('up_direction', 'Z+')
            
            temp_shape_wrapper = Shape(master_obj)
            shape_processor.create_single_nesting_part(temp_shape_wrapper, master_obj, spacing, deflection, simplification, up_direction, verbose=verbose)
            self.processed_shape_cache[cache_key] = copy.deepcopy(temp_shape_wrapper)

        if temp_shape_wrapper.source_centroid is not None:
            source_centroid = temp_shape_wrapper.source_centroid
        else:
            # Fallback: calculate from shape bounding box
            temp_shape = master_obj.Shape.copy()
            if master_obj.Placement and not master_obj.Placement.isIdentity():
                temp_shape.transformShape(master_obj.Placement.Matrix)
            bb = temp_shape.BoundBox
            source_centroid = FreeCAD.Vector((bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2, (bb.ZMin + bb.ZMax) / 2)

        master_container, up_direction = self._create_master_container(label, quantities, source_centroid)
        
        original_shape = master_obj.Shape.copy()
        if verbose:
            FreeCAD.Console.PrintMessage(f"  -> Creating master for '{label}' (type: {master_obj.TypeId}) with up_direction='{up_direction}'\n")
        
        if master_obj.isDerivedFrom("Part::Part2DObject"):
            plc = master_obj.Placement
            offset = FreeCAD.Vector(source_centroid.x, source_centroid.y, source_centroid.z)
            original_shape = self._rebuild_2d_shape(master_obj, original_shape, source_centroid, plc, offset, verbose, label)
        else:
            original_shape = self._center_3d_shape(master_obj, original_shape, source_centroid)
            
        master_shape_obj = create_part_feature(
            self.doc, f"master_shape_{label}", original_shape, group=master_container, visible=True
        )
        self._perf_inc('lm_master_part_features_created')
        if not hasattr(master_shape_obj, "ShowBounds"):
            master_shape_obj.addProperty("App::PropertyBool", "ShowBounds", "Display", "").ShowBounds = False
        if not hasattr(master_shape_obj, "BoundaryObject"):
            master_shape_obj.addProperty("App::PropertyLink", "BoundaryObject", "Nesting", "")
        
        master_shape_obj.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 0), get_up_direction_rotation(up_direction))

        self._create_boundary_object(master_container, master_shape_obj, temp_shape_wrapper, verbose)
        master_shapes_group.addObject(master_container)
        
        return master_shape_obj, temp_shape_wrapper

    def _create_master_container(self, label, quantities, source_centroid):
        """Creates the App::Part container and populates it with metadata properties.

        **Every property written here is one `_load_shapes_from_layout` reads
        back** when the layout is reopened. That pairing used to be incomplete in
        one direction: `PartRotationSteps` and `PartRotationOverride` were read
        and never written, so a per-part rotation override produced a correct nest
        and a layout that silently reverted to the global value on reopen. See
        issues.md NEST-002, and `tests/test_layout_persistence/` for the guard
        that keeps the pairing complete.

        `PartRotationSteps` holds the **raw** spinbox reading, not the resolved
        step count. `part_params['rotation_steps']` is already resolved --
        `rot_val if override else global_rot` -- so persisting it would write the
        global value as though the user had typed it, and an override of 8 could
        not be told from a global of 8 on the way back. The raw pair travels
        beside it as `rotation_steps_override` / `rotation_override`.
        """
        master_container = self.doc.addObject("App::Part", f"master_{label}")
        self._perf_inc('lm_master_containers_created')

        part_params = quantities.get(label, {'quantity': 1, 'up_direction': 'Z+', 'fill_sheet': False})
        if isinstance(part_params, tuple):
            quantity, up_direction, fill_sheet = part_params[0], 'Z+', False
            rotation_steps_override, rotation_override = 0, False
        else:
            quantity = part_params.get('quantity', 1)
            up_direction = part_params.get('up_direction', 'Z+')
            fill_sheet = part_params.get('fill_sheet', False)
            rotation_steps_override = part_params.get('rotation_steps_override', 0)
            rotation_override = part_params.get('rotation_override', False)

        master_container.addProperty("App::PropertyInteger", "Quantity", "Nest", "Number of instances").Quantity = quantity
        master_container.addProperty("App::PropertyString", "UpDirection", "Nest", "Up direction for 2D projection").UpDirection = up_direction
        master_container.addProperty("App::PropertyBool", "FillSheet", "Nest", "Use to fill remaining space").FillSheet = fill_sheet
        master_container.addProperty("App::PropertyVector", "SourceCentroid", "Nesting", "Original geometry center").SourceCentroid = source_centroid
        # 0 means "no per-part override", matching the spinbox's own 0..360 range
        # and its tooltip's "0 or 1 means no rotation". `PartRotationOverride` is
        # a **Bool**: the reload path used to read it with a `[]` default and hand
        # it straight to `add_part_row(override_rotation=...)`, where a list is
        # falsy by accident rather than by being off.
        master_container.addProperty(
            "App::PropertyInteger", "PartRotationSteps", "Nest",
            "Raw rotation steps typed for this part. 0 when not overridden."
        ).PartRotationSteps = int(rotation_steps_override or 0)
        master_container.addProperty(
            "App::PropertyBool", "PartRotationOverride", "Nest",
            "Whether PartRotationSteps overrides the global rotation steps."
        ).PartRotationOverride = bool(rotation_override)

        set_visibility(master_container, True)

        return master_container, up_direction

    def _rebuild_2d_shape(self, master_obj, original_shape, center_point, plc, offset, verbose, label):
        """Rebuilds 2D shape by transforming each edge's curve parameters to preserve smooth curves."""
        try:
            new_edges = []
            for edge in original_shape.Edges:
                curve = edge.Curve
                if hasattr(curve, 'Radius') and hasattr(curve, 'Center'):
                    # Circle or Arc: transform center, preserve radius and smoothness
                    world_center = plc.multVec(curve.Center)
                    new_center = world_center - offset
                    new_axis = plc.Rotation.multVec(curve.Axis)
                    if edge.isClosed():
                        new_edges.append(Part.makeCircle(curve.Radius, new_center, new_axis))
                    else:
                        c = Part.Circle(new_center, new_axis, curve.Radius)
                        new_edges.append(c.toShape(edge.FirstParameter, edge.LastParameter))
                elif len(edge.Vertexes) >= 2:
                    # Line: transform endpoints
                    p1 = plc.multVec(edge.Vertexes[0].Point) - offset
                    p2 = plc.multVec(edge.Vertexes[1].Point) - offset
                    new_edges.append(Part.makeLine(p1, p2))
                else:
                    # Fallback: discretize unknown curve types
                    pts = edge.discretize(Number=72)
                    transformed = [plc.multVec(p) - offset for p in pts]
                    for i in range(len(transformed) - 1):
                        new_edges.append(Part.makeLine(transformed[i], transformed[i + 1]))
            
            if new_edges:
                wire = Part.Wire(new_edges)
                try:
                    rebuilt_shape = Part.Face(wire)
                except Exception:
                    rebuilt_shape = Part.Compound([wire])
                if verbose:
                    FreeCAD.Console.PrintMessage(f"     Rebuilt 2D shape with smooth curves\n")
                return rebuilt_shape
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"     Curve preservation unsuccessful for '{label}': {e}. Using polygon approximation.\n")
            # Fallback: discretize to polygon
            new_wires = []
            for wire in master_obj.Shape.Wires:
                pts = wire.discretize(Number=72)
                if len(pts) > 2:
                    transformed = [plc.multVec(p) - offset for p in pts]
                    if transformed[0] != transformed[-1]:
                        transformed.append(transformed[0])
                    new_wires.append(Part.makePolygon(transformed))
            if new_wires:
                try:
                    return Part.Face(new_wires[0])
                except Exception:
                    return Part.Compound(new_wires)
        return original_shape

    def _center_3d_shape(self, master_obj, original_shape, center_point):
        """Centers a 3D part with one combined rigid transform.

        **A rigid motion, applied as `transformShape` and not
        `transformGeometry`.** The two differ only in whether they re-fit the
        geometry, and re-fitting is not wanted: `transformGeometry` converts
        every analytic surface into a `BSplineSurface`, so a `Cylinder` face the
        user modelled came back approximated. Measured on this function's own
        input, a plate with a hole:

            surface types   transformGeometry -> BSplineSurface x7
                            transformShape    -> Plane x6, Cylinder x1
            face area       top 921.4602 -> 921.7994  (+0.2684%)
                            cylinder 188.4956 -> 189.0014  (+0.2684%)
            volume          5528.7611 -> 5524.6221

        The +0.2684% is systematic, not noise: it is identical at cylinder radius
        1 mm and 50 mm, and it is why CAM then offsets a toolpath from an
        approximation of the surface rather than the surface itself. It also makes
        `shape.slice()` ~1.8x slower, which is most of NEST-008.

        `transformShape` is a drop-in for the geometry: measured against
        `transformGeometry` on the same matrix, bounding boxes agree to 0.000
        (identity placement) and 7.1e-15 (rotated), volume to 2.7e-12, and the
        result survives `_handle_new_master`'s placement reset identically.

        **Why not simply assign a `Placement`.** That looks like the natural
        choice -- a rigid motion needs no scale, and `Placement` cannot carry any
        -- and it was the first thing tried. It does not work here because
        `_handle_new_master` resets the master's placement immediately
        afterwards, to `(0, 0, 0)` plus the up-direction rotation. A centring left
        in the placement is discarded by that reset and the part lands back where
        it started. Measured: the baked result is unchanged by the reset, the
        placement-assigned one moves by the full centring offset. The transform
        has to be baked into the geometry, which is what both of these do and what
        `Placement` alone cannot.

        **The transform is baked, so the shape comes back with its placement
        unchanged** -- identical to what `transformGeometry` did, and the reason
        the next line can reset it without moving the part.

        **Known and separate: this is a no-op when the part arrives with a
        non-identity placement.** The matrix is applied to the local geometry and
        then the object's own placement is applied on top, cancelling it -- input
        and output bounding boxes are identical. That is pre-existing, it affects
        `transformGeometry` and `transformShape` equally, and it is why the
        centring is verified against identity-placement parts only. Tracked
        separately rather than fixed here, because fixing it would move parts and
        that is a behaviour change, not a fidelity one. See issues.md.
        """
        combined_mat = FreeCAD.Matrix()
        combined_mat.move(center_point.negative())
        if master_obj.Placement and not master_obj.Placement.isIdentity():
            combined_mat = combined_mat.multiply(master_obj.Placement.Matrix)

        # The whole point of `transformShape` is that it requires a rigid matrix,
        # and a silently non-rigid one -- a scale or a shear -- would be applied
        # as if it were exact. Nothing here can supply either: `move` is a pure
        # translation and a `Placement` has no scale by construction. Asserted
        # rather than assumed, because the assumption is the entire justification
        # for not using `transformGeometry`.
        if not _is_rigid(combined_mat):
            raise ValueError(
                "_center_3d_shape was handed a non-rigid matrix; transformShape "
                "would apply it as an exact transform and silently distort the "
                "part. Centring only ever needs a translation and a rotation.")

        return original_shape.transformShape(combined_mat, True)

    def _create_boundary_object(self, master_container, master_shape_obj, temp_shape_wrapper, verbose):
        """Creates the boundary shape object from the temp_shape_wrapper and adds it to the master_container."""
        if temp_shape_wrapper.polygon:
            boundary_obj = temp_shape_wrapper.draw_bounds(self.doc, FreeCAD.Vector(0,0,0), None)
            if boundary_obj:
                master_container.addObject(boundary_obj)
                self._perf_inc('lm_master_boundary_features_created')
                # Bounds are centered at origin - no placement needed
                boundary_obj.Placement = FreeCAD.Placement()
                master_shape_obj.BoundaryObject = boundary_obj
                master_shape_obj.ShowBounds = False
                set_visibility(boundary_obj, False)
                if verbose:
                    FreeCAD.Console.PrintMessage(f"     Bounds centroid from polygon: {temp_shape_wrapper.polygon.centroid}\n")

    def _arrange_masters(self, masters_to_place, spacing):
        masters_to_place.sort(key=lambda item: item[1].area, reverse=True)
        
        max_master_height = 0
        if masters_to_place:
            max_master_height = max(item[1].bounding_box()[3] for item in masters_to_place if item[1].polygon)

        # Start cursor at 0 (or slight left offset if desired, but 0 is fine)
        cursor_x = 0
        y_offset = -max_master_height - spacing * 4 
        
        for container, shape_wrapper in masters_to_place:
            # bounds is (min_x, min_y, width, height) of the Shapely polygon (centered at 0,0) as returned by bounding_box()
            # Note: bounding_box() returns (minx, miny, width, height)
            bounds = shape_wrapper.bounding_box()
            width = bounds[2] if bounds else 5
            
            # Fix for Asymmetric Shapes:
            # The shape geometry is inside the container. 
            # The container is placed at `container_pos`.
            # We want the Left Edge of the shape's bounding box to be at `cursor_x`.
            # The local Left Edge is `bounds[0]` (min_x).
            # So: container_pos.x + min_x = cursor_x
            # => container_pos.x = cursor_x - min_x
            
            min_x_val = bounds[0] if bounds else (-width/2.0)
            center_x = cursor_x - min_x_val
            
            container_pos = FreeCAD.Vector(center_x, y_offset, 0)
            container.Placement = FreeCAD.Placement(container_pos, FreeCAD.Rotation())
            
            # Move cursor past this shape
            cursor_x += width + spacing

    def _create_nesting_instances(self, master_shapes_map, quantities, master_shape_obj_map, master_geometry_cache, ui_settings, parts_group):
        parts_to_nest = []
        parts_to_place_group = parts_group
        
        add_labels = ui_settings['add_labels']
        font_path = ui_settings['font_path']
        spacing = ui_settings['spacing']
        # Default global rotation
        global_rotation_steps = ui_settings['rotation_steps']
        verbose = ui_settings.get('verbose', False)

        def _spawn_factory(original_obj, master_wrapper, lookup_label,
                           part_rotation_steps, fill_sheet, up_direction,
                           master_shape_obj):
            """Binds one part type's parameters into a dedicated closure scope.

            make_instance is handed out as spawn_next and called long after the
            master-shapes loop has moved on — defining it directly in the loop
            body would late-bind every spawner to the LAST type's variables.
            Each factory call also gets its own instance counter.
            """
            next_instance_num = [0]

            def make_instance():
                next_instance_num[0] += 1
                i = next_instance_num[0]

                shape_instance = Shape(original_obj)

                # Copy properties
                shape_instance.polygon = master_wrapper.polygon
                shape_instance.original_polygon = master_wrapper.original_polygon
                shape_instance.unbuffered_polygon = master_wrapper.unbuffered_polygon
                shape_instance.source_centroid = master_wrapper.source_centroid
                shape_instance.spacing = spacing

                shape_instance.instance_num = i
                shape_instance.id = f"{lookup_label}_{i}"
                shape_instance.master_label = lookup_label  # type identity — never parse .id
                shape_instance.rotation_steps = part_rotation_steps
                shape_instance.fill_sheet = fill_sheet
                shape_instance.up_direction = up_direction

                if self.create_doc_objects:
                    part_copy = create_part_feature(
                        self.doc, f"part_{shape_instance.id}", master_shape_obj.Shape.copy(), group=parts_to_place_group, visible=False
                    )
                    self._perf_inc('lm_part_features_created')
                    part_copy.Placement = master_shape_obj.Placement

                    # Debug: Check what geometry we're getting
                    if verbose and up_direction != "Z+" and up_direction is not None:
                        FreeCAD.Console.PrintMessage(f"     Part copy {shape_instance.id}: BoundBox={part_copy.Shape.BoundBox}\n")

                    # Copy boundary if exists
                    if hasattr(master_shape_obj, "BoundaryObject") and master_shape_obj.BoundaryObject:
                        boundary_copy = create_part_feature(
                            self.doc, f"boundary_{shape_instance.id}", master_shape_obj.BoundaryObject.Shape.copy(), group=parts_to_place_group, visible=False
                        )
                        self._perf_inc('lm_part_boundary_features_created')
                        part_copy.addProperty("App::PropertyLink", "BoundaryObject", "Nesting", "Boundary object")
                        part_copy.BoundaryObject = boundary_copy

                    shape_instance.fc_object = part_copy
                # Headless: fc_object stays None. materialize_layout_objects()
                # builds the real objects for the winning layout at commit.

                # Do NOT manipulate Placement here.
                # The Sheet.draw method is the sole authority on where this part ends up.

                if add_labels and Draft and font_path:
                    shape_instance.label_text = shape_instance.id

                if fill_sheet:
                    shape_instance.spawn_next = make_instance

                return shape_instance

            return make_instance

        for label, original_obj in master_shapes_map.items():
            # If reloading, label is master_shape_X, handle mapping
            lookup_label = label
            if label.startswith("master_shape_"):
                 lookup_label = label.replace("master_shape_", "")
            
            part_params = quantities.get(lookup_label, {'quantity': 0, 'rotation_steps': global_rotation_steps})
            quantity = part_params.get('quantity', 0)
            part_rotation_steps = part_params.get('rotation_steps', global_rotation_steps)
            fill_sheet = part_params.get('fill_sheet', False)
            up_direction = part_params.get('up_direction', 'Z+')
            
            master_shape_obj = master_shape_obj_map.get(id(original_obj))
            master_wrapper = master_geometry_cache.get(id(original_obj))
            
            if not master_shape_obj or not master_wrapper: continue

            # Recorded under the same normalized label the parts carry, so a
            # headless layout can be materialized from master_label later.
            self._last_master_objects[lookup_label] = master_shape_obj

            # Fill-sheet parts don't know in advance how many copies will fit,
            # so instance creation lives in a closure; fill parts carry a
            # spawn_next handle that mints one more instance on demand.
            if fill_sheet:
                quantity = max(quantity, 1)

            make_instance = _spawn_factory(
                original_obj, master_wrapper, lookup_label, part_rotation_steps,
                fill_sheet, up_direction, master_shape_obj
            )
            for _ in range(quantity):
                parts_to_nest.append(make_instance())

        return parts_to_nest
