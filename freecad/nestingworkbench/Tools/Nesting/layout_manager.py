# SPDX-License-Identifier: LGPL-2.1-or-later
"""
LayoutManager - Handles creation, cloning, and management of Layout objects.

This class is responsible for:
- Creating new layouts with master shapes and part instances
- Cloning layouts for GA population members
- Deleting layouts and all their child objects
- Calculating layout efficiency

Separates layout management from the nesting algorithm for cleaner architecture.
"""

import FreeCAD
import copy
import random
import math
import time
import traceback
from .shape_preparer import ShapePreparer
from ...datatypes.shape import Shape
from ...freecad_helpers import recursive_delete, create_part_feature

try:
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False

def largest_open_area(parts, sheet_width, sheet_height):
    """
    Area of the largest contiguous free region on a sheet.

    Computed exactly with Shapely: bin polygon minus the union of the placed
    part polygons (sheet-local coordinates); the largest resulting polygon's
    area is returned. Falls back to the full sheet area (i.e. a zero
    compactness deficit) when Shapely is unavailable or geometry is degenerate,
    so the fitness term degrades to a no-op instead of erroring.

    Args:
        parts: iterable of PlacedPart objects (each with .shape.polygon)
        sheet_width: sheet width in mm
        sheet_height: sheet height in mm

    Returns:
        float: area (mm^2) of the largest open region, in
        [0, sheet_width * sheet_height].
    """
    sheet_area = sheet_width * sheet_height
    if not SHAPELY_AVAILABLE:
        return sheet_area

    polygons = []
    for p in parts:
        try:
            poly = p.shape.polygon
            if poly is not None and not poly.is_empty:
                polygons.append(poly.buffer(0))
        except Exception:
            continue
    if not polygons:
        return sheet_area

    bin_polygon = Polygon([(0, 0), (sheet_width, 0),
                           (sheet_width, sheet_height), (0, sheet_height)])
    try:
        free_space = bin_polygon.difference(unary_union(polygons))
    except Exception:
        return sheet_area

    if free_space.is_empty:
        return 0.0
    regions = getattr(free_space, 'geoms', [free_space])
    return max(region.area for region in regions)


class Layout:
    """
    Represents a single layout attempt (population member in GA).
    Contains references to the FreeCAD objects and the parts list.
    
    Attributes:
        genes: List of (part_id, angle) tuples representing the ordering and rotation
               of parts. Can be used to recreate the exact same layout.
    """
    def __init__(self, layout_group, parts_group, parts, master_shapes_group=None,
                 master_objects=None):
        self.layout_group = layout_group  # The Layout_xxx group object
        self.parts_group = parts_group    # The PartsToPlace group
        self.parts = parts                # List of Shape objects for nesting
        self.master_shapes_group = master_shapes_group
        # master label -> master Part::Feature. Lets a headless layout (one
        # built without FreeCAD part objects) be materialized later; see
        # LayoutManager.materialize_layout_objects.
        self.master_objects = master_objects or {}
        self.sheets = []                  # Filled after nesting
        self.fitness = float('inf')
        self.efficiency = 0.0
        self.genes = []                   # (part_id, angle) tuples - the "DNA" of this layout
        self.direction = None             # (dx, dy) unit vector; None = use global search_direction
    
    @property
    def name(self):
        return self.layout_group.Label if self.layout_group else "unknown"

class LayoutManager:
    """
    Manages layout creation, cloning, and deletion.
    Acts as a factory for Layout objects used in nesting.
    """
    
    def __init__(self, doc, processed_shape_cache=None, rng=None, perf_stats=None,
                 create_doc_objects=True):
        self.doc = doc
        self.processed_shape_cache = processed_shape_cache or {}
        self._layout_counter = 0
        self.rng = rng or random
        # Optional run-scoped measurement sink. When None (the default) every
        # hook below is a no-op, so production runs pay nothing for this
        # instrumentation. Only the Performance Logging path passes a dict.
        self._perf_stats = perf_stats
        # When False, layouts are built without FreeCAD part/boundary objects
        # (Shapely only). The GA uses this so that only the winning layout
        # pays for document object creation.
        self.create_doc_objects = create_doc_objects

    def _perf_add(self, key, seconds):
        stats = self._perf_stats
        if stats is not None:
            stats[key] = stats.get(key, 0.0) + seconds

    def _perf_inc(self, key, count=1):
        stats = self._perf_stats
        if stats is not None:
            stats[key] = stats.get(key, 0) + count
    
    def create_layout(self, name, master_shapes_map, quantities, ui_params, 
                      chromosome_ordering=None) -> Layout:
        """
        Creates a new layout with master shapes and part instances.
        
        Args:
            name: Name for the layout (e.g., "Layout_GA_1")
            master_shapes_map: Dict mapping labels to FreeCAD shape objects
            quantities: Dict mapping labels to (quantity, rotation_steps)
            ui_params: UI parameters dict
            chromosome_ordering: Optional list of (part_id, angle) tuples for ordering
            
        Returns:
            Layout object containing the layout group and prepared parts
        """
        create_start = time.perf_counter()

        # Create layout group
        group_start = time.perf_counter()
        layout_group = self.doc.addObject("App::DocumentObjectGroup", name)
        layout_group.Label = name
        if hasattr(layout_group, "ViewObject"):
            layout_group.ViewObject.Visibility = True
        
        # Create parts bin
        parts_group = self.doc.addObject("App::DocumentObjectGroup", "PartsToPlace")
        layout_group.addObject(parts_group)
        self._perf_add('lm_group_s', time.perf_counter() - group_start)
        self._perf_inc('lm_group_objects_created', 2)

        # Create shape preparer for this layout
        preparer = ShapePreparer(self.doc, self.processed_shape_cache,
                                 perf_stats=self._perf_stats,
                                 create_doc_objects=self.create_doc_objects)
        
        # Prepare parts (creates masters and instances)
        prepare_start = time.perf_counter()
        parts = preparer.prepare_parts(
            ui_params, quantities, master_shapes_map, 
            layout_group, parts_group
        )
        self._perf_add('lm_prepare_parts_s', time.perf_counter() - prepare_start)
        self._perf_inc('lm_parts_created', len(parts))

        # Get master shapes group
        master_shapes_group = None
        for child in layout_group.Group:
            if child.Label == "MasterShapes":
                master_shapes_group = child
                break
        
        # Apply chromosome ordering if provided
        order_start = time.perf_counter()
        if chromosome_ordering and parts:
            parts = self._apply_ordering(parts, chromosome_ordering)
        self._perf_add('lm_ordering_s', time.perf_counter() - order_start)
        
        self._layout_counter += 1
        self._perf_inc('lm_layouts_created')
        
        layout = Layout(layout_group, parts_group, parts, master_shapes_group,
                        master_objects=dict(preparer._last_master_objects))
        if chromosome_ordering and parts:
            # Genotype drives nesting: only genes for parts that actually exist,
            # and never for fill parts (they are placed greedily after the genome)
            layout.genes = [(p.id, getattr(p, '_angle', 0)) for p in parts
                            if getattr(p, 'fill_sheet', False) is not True]
        # create_s is the sum of the three sub-phases above plus the small
        # amount of Layout construction that is not separately attributed.
        self._perf_add('lm_create_s', time.perf_counter() - create_start)
        return layout
    
    def materialize_layout_objects(self, layout, ui_params):
        """
        Creates the FreeCAD part/boundary objects for a headless layout.

        Headless layouts (LayoutManager created with
        ``create_doc_objects=False``) nest from Shapely geometry alone, so
        their parts carry ``fc_object = None``. The winning layout must be
        turned back into real document objects before ``Sheet.draw``, which
        re-parents them out of ``parts_group`` into the final ``nested_*``
        containers.

        Master FreeCAD objects are still created per layout, so the geometry
        source is already available and nothing is recomputed here.

        Must be called on the main thread. Idempotent: any shape that already
        has an ``fc_object`` is skipped, so calling it twice is harmless.
        Parts spawned mid-nest (fill top-ups) are not in ``layout.parts``, so
        the placed parts on each sheet are walked as well.

        Returns:
            int: number of parts materialized (0 if already materialized or
            this manager builds document objects itself).
        """
        if self.create_doc_objects or not layout or not layout.parts_group:
            return 0

        parts_group = layout.parts_group
        master_objects = getattr(layout, 'master_objects', None) or {}

        candidates = list(layout.parts)
        for sheet in (layout.sheets or []):
            for placed in sheet.parts:
                shape = getattr(placed, 'shape', None)
                if shape is not None:
                    candidates.append(shape)

        created = 0
        for shape in candidates:
            if getattr(shape, 'fc_object', None) is not None:
                continue
            master = master_objects.get(getattr(shape, 'master_label', None))
            if master is None:
                continue
            try:
                part_copy = create_part_feature(
                    self.doc, f"part_{shape.id}", master.Shape.copy(),
                    group=parts_group, visible=False
                )
                self._perf_inc('lm_part_features_created')
                part_copy.Placement = master.Placement

                if hasattr(master, "BoundaryObject") and master.BoundaryObject:
                    boundary_copy = create_part_feature(
                        self.doc, f"boundary_{shape.id}",
                        master.BoundaryObject.Shape.copy(),
                        group=parts_group, visible=False
                    )
                    self._perf_inc('lm_part_boundary_features_created')
                    part_copy.addProperty("App::PropertyLink", "BoundaryObject",
                                          "Nesting", "Boundary object")
                    part_copy.BoundaryObject = boundary_copy

                shape.fc_object = part_copy
                created += 1
            except Exception:
                FreeCAD.Console.PrintWarning(
                    f"[LayoutManager] Could not materialize part "
                    f"'{getattr(shape, 'id', '?')}': {traceback.format_exc()}\n")
        return created

    def _apply_ordering(self, parts, chromosome_ordering):
        """
        Reorders and rotates parts according to a chromosome.

        Args:
            parts: List of Shape objects
            chromosome_ordering: List of (part_id, angle) tuples
            
        Returns:
            Reordered list of Shape objects with rotations applied
        """
        if not chromosome_ordering:
            return parts
        
        # Build a map of part id -> part
        parts_map = {p.id: p for p in parts}
        
        ordered_parts = []
        for part_id, angle in chromosome_ordering:
            if part_id in parts_map:
                part = parts_map[part_id]
                if angle is not None:
                    part.set_rotation(angle)
                    part.gene_angle = angle
                ordered_parts.append(part)
        
        # Parts not in the chromosome (fill parts) tag along at the end in
        # their original relative order; the nester's fill phase handles them.
        ordered_ids = {p.id for p in ordered_parts}
        ordered_parts.extend(p for p in parts if p.id not in ordered_ids)
        
        return ordered_parts
    
    def delete_layout(self, layout, verbose=False):
        """
        Removes a layout group and ALL its children from the document.
        Must recursively delete children first since FreeCAD doesn't do this automatically.
        
        Args:
            layout: Layout object to delete
            verbose: If True, log deletion
        """
        if not layout:
            return
            
        # Check if already deleted
        if hasattr(layout, '_deleted') and layout._deleted:
            return
        
        # Get the group object before we mark it deleted
        group_obj = None
        try:
            if layout.layout_group:
                group_obj = layout.layout_group
        except Exception:
            pass  # Stale layout_group reference during cleanup
        
        layout_label = layout.name if hasattr(layout, 'name') else "unknown"
        
        # Mark as deleted immediately to prevent re-entry
        layout._deleted = True
        layout.layout_group = None
        layout.sheets = []
        layout.parts = []
        
        # Recursively delete the group and all children
        if group_obj:
            delete_start = time.perf_counter()
            recursive_delete(self.doc, group_obj, perf_stats=self._perf_stats)
            self._perf_add('lm_delete_s', time.perf_counter() - delete_start)
            self._perf_inc('lm_layouts_deleted')
            if verbose:
                FreeCAD.Console.PrintMessage(f"  Deleted: {layout_label}\n")

    

    
    def calculate_efficiency(self, layout, sheet_width, sheet_height,
                             compactness_weight=0.0) -> tuple:
        """
        Calculates the packing efficiency of a layout.
        
        Args:
            layout: Layout object with sheets populated
            sheet_width: Width of each sheet
            sheet_height: Height of each sheet
            compactness_weight: 0 disables the open-area term (default); higher
                                values weight the largest-open-area deficit into the last-sheet
                                tie-break as a blend that never exceeds one sheet's area
            
        Returns:
            (fitness, efficiency_percent) tuple
        """
        if not layout.sheets:
            return float('inf'), 0.0
        
        # Calculate total parts area
        total_parts_area = 0
        for sheet in layout.sheets:
            for part in sheet.parts:
                if hasattr(part, 'shape') and part.shape:
                    total_parts_area += part.shape.area
        
        # Calculate total sheet area
        total_sheet_area = len(layout.sheets) * sheet_width * sheet_height
        
        # Efficiency percentage
        efficiency = (total_parts_area / total_sheet_area) * 100 if total_sheet_area > 0 else 0
        
        # Fitness: lower is better
        # Prioritize fewer sheets, then tighter bounding box
        fitness = len(layout.sheets) * sheet_width * sheet_height
        
        # Add bounding box of last sheet
        last_sheet = layout.sheets[-1]
        if last_sheet.parts:
            min_x, min_y = float('inf'), float('inf')
            max_x, max_y = float('-inf'), float('-inf')
            found_valid = False
            
            for p in last_sheet.parts:
                try:
                    bx, by, bw, bh = p.shape.bounding_box()
                    min_x = min(min_x, bx)
                    min_y = min(min_y, by)
                    max_x = max(max_x, bx + bw)
                    max_y = max(max_y, by + bh)
                    found_valid = True
                except Exception as e:
                    part_id = getattr(p.shape, 'id', 'unknown') if hasattr(p, 'shape') else 'unknown'
                    FreeCAD.Console.PrintWarning(f"[LayoutManager] Bounding box failed for part '{part_id}': {e}\n")
            
            if found_valid:
                bbox_area = (max_x - min_x) * (max_y - min_y)
                tie_break = bbox_area
                if compactness_weight > 0:
                    open_deficit = (sheet_width * sheet_height
                                    - largest_open_area(last_sheet.parts,
                                                        sheet_width, sheet_height))
                    tie_break = ((bbox_area + compactness_weight * open_deficit)
                                 / (1.0 + compactness_weight))
                fitness += tie_break
        
        layout.fitness = fitness
        layout.efficiency = efficiency

        return fitness, efficiency
    
    def create_ga_population(self, master_shapes_map, quantities, ui_params, 
                             population_size, rotation_steps=1, verbose=False) -> list:
        """
        Creates a population of layouts for genetic algorithm.
        
        Args:
            master_shapes_map: Dict mapping labels to FreeCAD shape objects
            quantities: Dict mapping labels to (quantity, rotation_steps)
            ui_params: UI parameters dict
            population_size: Number of layouts to create
            rotation_steps: Number of rotation steps for random rotations
            
        Returns:
            List of Layout objects
        """
        population = []
        population_start = time.perf_counter()
        self._perf_inc('lm_populations_created')
        
        for i in range(population_size):
            name = f"Layout_GA_{i+1}"
            
            # Create the layout
            layout = self.create_layout(name, master_shapes_map, quantities, ui_params)
            
            if layout.parts and i > 0:  # First layout keeps original ordering
                regular = [p for p in layout.parts if getattr(p, 'fill_sheet', False) is not True]
                fill = [p for p in layout.parts if getattr(p, 'fill_sheet', False) is True]

                # Shuffle the regular parts order; fill parts stay at the tail
                self.rng.shuffle(regular)
                layout.parts = regular + fill

                # Apply random rotations (regular parts only — fill parts keep
                # their full rotation sweep and never get a gene_angle pin)
                if rotation_steps > 1:
                    for part in regular:
                        angle = self.rng.randrange(rotation_steps) * (360.0 / rotation_steps)
                        part.set_rotation(angle)
                        part.gene_angle = angle
                else:
                    for part in regular:
                        part.gene_angle = 0.0

                # Record genotype so nest() runs with sort=False and this ordering survives
                layout.genes = [(p.id, getattr(p, '_angle', 0)) for p in regular]

                # Per-layout search direction
                if ui_params.get('use_random_direction', False):
                    angle_rad = self.rng.uniform(0, 2 * math.pi)
                    layout.direction = (math.cos(angle_rad), math.sin(angle_rad))
                else:
                    layout.direction = None
            
            population.append(layout)
            if verbose:
                FreeCAD.Console.PrintMessage(f"Created layout {name} with {len(layout.parts)} parts\n")
        
        self._perf_add('lm_population_s', time.perf_counter() - population_start)
        return population
    
