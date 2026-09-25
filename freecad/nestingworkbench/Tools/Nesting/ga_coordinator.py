# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Coordinates the Genetic Algorithm nesting loop.
Extracted from NestingController._execute_ga_nesting() to follow SRP.
"""
import FreeCAD
import FreeCADGui
import math
import random
import time
from ...datatypes.shape import Shape
from .layout_manager import LayoutManager
from .algorithms import genetic_utils

# Sub-phase breakdown of layout management. These keys are cumulative for the
# whole run and are only populated when Performance Logging is enabled; the
# dict itself is None otherwise, so every writer below short-circuits to a
# no-op in production runs.
_LAYOUT_PERF_TIMERS = (
    'lm_population_s',       # initial GA population creation
    'lm_next_generation_s',  # whole selection/crossover/mutation/offspring build
    'lm_create_s',           # create_layout total (group + prepare + ordering)
    'lm_group_s',            #   doc groups for the layout
    'lm_prepare_parts_s',    #   ShapePreparer.prepare_parts total
    'lm_master_prepare_s',   #     master shape prep/cache lookup
    'lm_part_instances_s',   #     per-part FreeCAD instance creation
    'lm_ordering_s',         #   chromosome ordering / rotation application
    'lm_delete_s',           # delete_layout / recursive_delete
    'lm_gene_ops_s',         # selection + crossover + mutation + immigrants
    'lm_cleanup_s',          # final discard of non-winning layouts
    'lm_post_nest_rebind_s', # per-part placement rebind after nest() returns
    'lm_efficiency_s',       # calculate_efficiency (fitness/tie-break)
    'lm_fill_s',             # fill_existing_sheets on the winner
    'lm_materialize_s',      # FreeCAD objects built for the winning layout
    'lm_finalize_s',         # _finalize: drawing the winning layout to the doc
    'lm_doc_recompute_s',    # doc.recompute() on the main thread
)

_LAYOUT_PERF_COUNTERS = (
    'lm_populations_created',
    'lm_layouts_created',
    'lm_layouts_deleted',
    'lm_parts_created',
    'lm_masters_processed',
    'lm_master_group_objects_created',
    'lm_group_objects_created',
    'lm_master_containers_created',
    'lm_master_part_features_created',
    'lm_master_boundary_features_created',
    'lm_part_features_created',
    'lm_part_boundary_features_created',
    'doc_objects_deleted',
)


def _new_layout_perf_stats():
    return {key: 0.0 for key in _LAYOUT_PERF_TIMERS} | {
        key: 0 for key in _LAYOUT_PERF_COUNTERS
    }


class GACoordinator:
    """Runs the GA optimization loop and returns the best Layout."""

    def __init__(self, doc, shape_preparer, ui_callbacks=None, draw_callback=None, worker=None):
        """
        Args:
            doc: FreeCAD.ActiveDocument
            shape_preparer: ShapePreparer instance (for processed_shape_cache)
            ui_callbacks: dict with optional keys:
                'set_status': callable(str) — update status label
                'update_progress': callable(current, total, msg) — update progress bar
                'reset_progress': callable() — reset progress bar
                'play_sound': callable() — beep on completion
            draw_callback: callable(payload_dict) — marshal to main thread
            worker: NestingWorker instance — for signal emission
        """
        self.doc = doc
        self.shape_preparer = shape_preparer
        self.ui_callbacks = ui_callbacks or {}
        self.draw_callback = draw_callback
        self.worker = worker
        self.layout_manager = None
        self._pending_layouts = None
        self._ga_perf = None
        self._candidate_geometry_key_tracker = None
        self._candidate_geometry_cache = None
        self._layout_perf = None

    def _record_layout_time(self, key, seconds):
        """Adds to a layout-management sub-phase timer (Performance Logging only)."""
        stats = self._layout_perf
        if stats is not None:
            stats[key] = stats.get(key, 0.0) + seconds

    def _format_layout_generation(self, snapshot):
        """Per-generation layout-management deltas, appended to the [GA PERF] line."""
        return (
            f" lm_create={self._layout_delta(snapshot, 'lm_create_s'):.2f}s"
            f" lm_delete={self._layout_delta(snapshot, 'lm_delete_s'):.2f}s"
            f" lm_gene_ops={self._layout_delta(snapshot, 'lm_gene_ops_s'):.2f}s"
            f" lm_nextgen={self._layout_delta(snapshot, 'lm_next_generation_s'):.2f}s"
            f" lm_rebind={self._layout_delta(snapshot, 'lm_post_nest_rebind_s'):.2f}s"
            f" lm_eff={self._layout_delta(snapshot, 'lm_efficiency_s'):.2f}s"
        )

    def _format_layout_totals(self):
        """Cumulative layout-management breakdown, appended to [GA PERF TOTAL]."""
        if self._layout_perf is None:
            return ""
        lm = self._layout_perf
        return (
            " [LAYOUT PERF] "
            f"population={lm['lm_population_s']:.2f}s "
            f"next_generation={lm['lm_next_generation_s']:.2f}s "
            f"create={lm['lm_create_s']:.2f}s "
            f"(group={lm['lm_group_s']:.2f}s "
            f"prepare={lm['lm_prepare_parts_s']:.2f}s "
            f"masters={lm['lm_master_prepare_s']:.2f}s "
            f"instances={lm['lm_part_instances_s']:.2f}s "
            f"ordering={lm['lm_ordering_s']:.2f}s) "
            f"delete={lm['lm_delete_s']:.2f}s "
            f"gene_ops={lm['lm_gene_ops_s']:.2f}s "
            f"cleanup={lm['lm_cleanup_s']:.2f}s "
            f"post_nest_rebind={lm['lm_post_nest_rebind_s']:.2f}s "
            f"efficiency={lm['lm_efficiency_s']:.2f}s "
            f"fill={lm['lm_fill_s']:.2f}s "
            f"materialize={lm['lm_materialize_s']:.2f}s "
            f"layouts_created={lm['lm_layouts_created']} "
            f"layouts_deleted={lm['lm_layouts_deleted']} "
            f"parts_created={lm['lm_parts_created']} "
            f"part_features={lm['lm_part_features_created']} "
            f"part_boundary_features={lm['lm_part_boundary_features_created']} "
            f"master_containers={lm['lm_master_containers_created']} "
            f"master_part_features={lm['lm_master_part_features_created']} "
            f"master_boundary_features={lm['lm_master_boundary_features_created']} "
            f"master_groups={lm['lm_master_group_objects_created']} "
            f"group_objects={lm['lm_group_objects_created']} "
            f"doc_objects_deleted={lm['doc_objects_deleted']}"
        )

    def _record_doc_recompute(self, seconds):
        """Records main-thread doc.recompute() cost (called by the controller)."""
        self._record_layout_time('lm_doc_recompute_s', seconds)

    def _layout_delta(self, snapshot, key):
        """Per-generation delta for a cumulative layout perf key."""
        return self._get_lm(self._layout_perf, key) - self._get_lm(snapshot, key)

    @staticmethod
    def _get_lm(stats, key):
        return stats.get(key, 0) if stats is not None else 0

    def _record_nest_perf(self, stats, elapsed):
        if self._ga_perf is None:
            return
        self._ga_perf['layout_evaluations'] += 1
        self._ga_perf['nesting_s'] += elapsed
        self._ga_perf['nfp_compute_s'] += stats.get('nfp_nfp_compute_ms', 0.0) / 1000
        self._ga_perf['candidate_validity_s'] += stats.get('candidate_validity_ms', 0.0) / 1000
        self._ga_perf['candidate_score_s'] += stats.get('score_ms', 0.0) / 1000
        self._ga_perf['candidate_geometry_s'] += stats.get('candidate_geometry_ms', 0.0) / 1000
        self._ga_perf['sheet_difference_s'] += stats.get('sheet_difference_ms', 0.0) / 1000
        self._ga_perf['collision_intersection_s'] += stats.get(
            'collision_intersection_ms', 0.0) / 1000
        self._ga_perf['placement_wall_s'] += stats.get('placement_wall_ms', 0.0) / 1000
        self._ga_perf['rotation_wall_s'] += stats.get('rotation_wall_ms', 0.0) / 1000
        self._ga_perf['candidate_evaluation_wall_s'] += stats.get(
            'candidate_evaluation_wall_ms', 0.0) / 1000
        for key in ('rotation_evaluations', 'successful_rotations',
                    'candidate_points', 'valid_candidate_points',
                    'bounds_survivors', 'sheet_candidates', 'sheet_rejections',
                    'sheet_boundary_candidates', 'collision_candidates',
                    'collision_rejections',
                    'bbox_rejections', 'polygon_checks',
                    'candidate_geometries_built', 'candidate_geometry_observations', 'candidate_geometry_cache_hits', 'candidate_geometry_cache_misses', 'candidate_geometry_cache_ms', 'bbox_checks',
                    'bbox_overlap_pairs', 'exact_collision_checks',
                    'candidate_geometry_unique', 'candidate_geometry_repeats'):
            self._ga_perf[key] += stats.get(key, 0)
        self._ga_perf['max_concurrent_rotations'] = max(
            self._ga_perf['max_concurrent_rotations'],
            stats.get('max_concurrent_rotations', 0),
        )
        self._ga_perf['candidate_geometry_cache_entries'] = max(
            self._ga_perf['candidate_geometry_cache_entries'],
            stats.get('candidate_geometry_cache_entries', 0),
        )
        self._ga_perf['nfp_cache_hits'] += stats.get('nfp_cache_hits', 0)
        self._ga_perf['nfp_cache_misses'] += stats.get('nfp_cache_misses', 0)

    def _set_status(self, msg):
        if self.worker:
            self.worker.status_changed.emit(msg)
            return
        callback = self.ui_callbacks.get('set_status')
        if callback:
            try:
                callback(msg)
            except RuntimeError:
                pass  # UI widget deleted (panel closed)

    def _update_progress(self, current, total, msg=None):
        if self.worker:
            self.worker.progress_updated.emit(current, total, msg or "")
            return
        callback = self.ui_callbacks.get('update_progress')
        if callback:
            try:
                callback(current, total, msg)
            except RuntimeError:
                pass  # UI widget deleted (panel closed)

    def _play_sound(self):
        callback = self.ui_callbacks.get('play_sound')
        if callback:
            try:
                callback()
            except RuntimeError:
                pass  # Sound callback failed or widget deleted

    def run(self, target_layout, ui_params, quantities, master_map,
            rotation_params, algo_kwargs, is_simulating, viz_manager=None):
        """
        Execute the GA optimization and return a NestingJob for the winner.

        Returns:
            NestingJob — ready to commit or cancel
        """
        # Keep the Shapely-dependent strategy import lazy so importing the
        # panel still works when the optional nesting dependency is missing.
        from .algorithms.nesting_strategy import (
            CandidateGeometryCache,
            CandidateGeometryKeyTracker,
        )

        generations = algo_kwargs.get('generations', 1)
        population_size = algo_kwargs.get('population_size', 1)
        rotation_steps = ui_params.get('rotation_steps', 1)
        elite_count = max(2, population_size // 5)
        mutation_rate = 0.1
        immigrant_ratio = 0.15
        early_stop_threshold = 5
        verbose = algo_kwargs.get('verbose', False)
        performance_logging = algo_kwargs.get('performance_logging', False)
        cancel_callback = algo_kwargs.get('cancel_callback', lambda: False)

        seed = algo_kwargs.get('random_seed')
        if seed is None:
            seed = random.randrange(2**32)
        # Threading: self.rng is accessed sequentially (either worker or main thread via blocking callbacks), never concurrently.
        self.rng = random.Random(seed)
        self.is_simulating = is_simulating
        FreeCAD.Console.PrintMessage(f"GA random seed: {seed}\n")
        
        if algo_kwargs.pop('clear_nfp_cache', False):
            Shape.clear_nfp_cache()
            FreeCAD.Console.PrintMessage("NFP cache cleared (user request).\n")
        
        if verbose:
            FreeCAD.Console.PrintMessage(f"GA Mode: {generations} generations, {population_size} population\n")
        
        # Layout-management sub-phase instrumentation. Only materialized when
        # Performance Logging is on; LayoutManager/ShapePreparer/recursive_delete
        # all take the dict by reference and no-op when it is None.
        self._layout_perf = _new_layout_perf_stats() if performance_logging else None

        # GA layouts only need Shapely geometry while the search runs. The
        # nester deep-copies parts in non-simulate mode (and Shape.__deepcopy__
        # drops fc_object), so the per-layout FreeCAD part objects are pure
        # overhead: they are only ever consumed at commit, for the winner.
        # Simulate mode draws every layout as it nests, so it must keep them.
        self._headless_layouts = not is_simulating

        self.layout_manager = LayoutManager(self.doc, self.shape_preparer.processed_shape_cache,
                                            rng=self.rng, perf_stats=self._layout_perf,
                                            create_doc_objects=not self._headless_layouts)
        
        self._set_status(f"Creating {population_size} layouts...")
        if self.draw_callback:
            self.draw_callback({'updateGui_only': True})
        else:
            FreeCADGui.updateGui()
        
        if self.draw_callback:
            # Marshal to main thread
            create_payload = {
                'create_population': True,
                'master_map': master_map,
                'quantities': quantities, 
                'ui_params': ui_params,
                'population_size': population_size,
                'rotation_steps': rotation_steps,
                'verbose': verbose,
            }
            self.draw_callback(create_payload)
            layouts = self._pending_layouts
        else:
            layouts = self.layout_manager.create_ga_population(
                master_map, quantities, ui_params, population_size, rotation_steps, verbose=verbose
            )
        
        best_layout = None
        best_efficiency = 0
        generations_without_improvement = 0
        total_nesting_time = 0
        self._ga_perf = {
            'generation_s': 0.0,
            'layout_evaluations': 0,
            'nesting_s': 0.0,
            'nfp_compute_s': 0.0,
            'candidate_validity_s': 0.0,
            'candidate_score_s': 0.0,
            'candidate_geometry_s': 0.0,
            'sheet_difference_s': 0.0,
            'collision_intersection_s': 0.0,
            'placement_wall_s': 0.0,
            'rotation_wall_s': 0.0,
            'candidate_evaluation_wall_s': 0.0,
            'bounds_survivors': 0,
            'sheet_candidates': 0,
            'sheet_rejections': 0,
            'sheet_boundary_candidates': 0,
            'collision_candidates': 0,
            'collision_rejections': 0,
            'bbox_rejections': 0,
            'polygon_checks': 0,
            'candidate_geometries_built': 0,
            'candidate_geometry_observations': 0,
            'candidate_geometry_cache_hits': 0,
            'candidate_geometry_cache_misses': 0,
            'candidate_geometry_cache_ms': 0.0,
            'candidate_geometry_cache_entries': 0,
            'bbox_checks': 0,
            'bbox_overlap_pairs': 0,
            'exact_collision_checks': 0,
            'candidate_geometry_unique': 0,
            'candidate_geometry_repeats': 0,
            'max_concurrent_rotations': 0,
            'candidate_points': 0,
            'valid_candidate_points': 0,
            'rotation_evaluations': 0,
            'successful_rotations': 0,
            'nfp_cache_hits': 0,
            'nfp_cache_misses': 0,
            'layout_management_s': 0.0,
            'visualization_s': 0.0,
            'offspring_layouts': 0,
            'immigrant_layouts': 0,
        }

        # Create run-scoped caches only after setup has completed, so setup
        # failures cannot leave retained candidate geometry on the coordinator.
        # The tracker is diagnostic-only and is absent when Performance Logging
        # is disabled; the production geometry cache remains independent.
        self._candidate_geometry_key_tracker = (
            CandidateGeometryKeyTracker() if performance_logging else None
        )
        self._candidate_geometry_cache = (
            CandidateGeometryCache()
            if algo_kwargs.get('candidate_geometry_cache', False) else None
        )
        if performance_logging:
            FreeCAD.Console.PrintMessage(
                "Candidate Geometry Cache: enabled\n"
                if self._candidate_geometry_cache is not None
                else "Candidate Geometry Cache: disabled\n"
            )

        try:
            for gen in range(generations):
                generation_start = time.perf_counter()
                generation_perf_start = {
                    key: self._ga_perf[key]
                    for key in (
                        'candidate_evaluation_wall_s', 'placement_wall_s',
                        'candidate_geometries_built', 'candidate_geometry_observations', 'candidate_geometry_cache_hits', 'candidate_geometry_cache_misses', 'candidate_geometry_cache_ms', 'bbox_checks',
                        'bbox_overlap_pairs', 'exact_collision_checks',
                        'candidate_geometry_unique', 'candidate_geometry_repeats',
                    )
                }
                layout_perf_start = dict(self._layout_perf) if self._layout_perf else {}
                if cancel_callback():
                    FreeCAD.Console.PrintMessage("Nesting cancelled by user.\n")
                    break

                if verbose:
                    FreeCAD.Console.PrintMessage(f"\n=== Generation {gen+1}/{generations} ===\n")
                self._set_status(f"Generation {gen+1}/{generations}...")
                gui_start = time.perf_counter()
                if self.draw_callback:
                    self.draw_callback({'updateGui_only': True})
                else:
                    FreeCADGui.updateGui()
                self._ga_perf['visualization_s'] += time.perf_counter() - gui_start
                
                gen_time, interrupted = self._run_generation(
                    layouts, gen, generations, ui_params, rotation_steps, 
                    algo_kwargs, is_simulating, cancel_callback, verbose, viz_manager
                )
                total_nesting_time += gen_time
                if interrupted: break

                # Evaluate progress
                layouts.sort(key=lambda l: l.fitness)
                current_best = layouts[0]
                
                if best_layout is None or current_best.fitness < best_layout.fitness:
                    best_layout = current_best
                    best_efficiency = current_best.efficiency
                    generations_without_improvement = 0
                    if verbose:
                        FreeCAD.Console.PrintMessage(f"\n>>> New Best: {best_efficiency:.1f}% efficiency <<<\n")
                else:
                    generations_without_improvement += 1
                    if verbose:
                        FreeCAD.Console.PrintMessage(f"\nNo improvement ({generations_without_improvement}/{early_stop_threshold})\n")
                
                # Early Stopping / Stagnation Check:
                # An early stopping threshold of 5 generations is selected because nesting jobs typically
                # operate with relatively small chromosome/population sizes where genetic diversity
                # converges quickly. If no elite child (improved ordering/rotation layout) is found
                # after 5 generations, the population has converged to a local optimum, and continuing
                # to run more generations would waste CPU/GPU resources without realistic chance of improvement.
                # Decided before building the next generation (a converged run
                # must not pay to breed a population it will discard), but acted
                # on after this generation is accounted for and reported below, so
                # the final generation is not dropped from generation_s.
                early_stop = generations_without_improvement >= early_stop_threshold

                # STEP 2 & 3: Build next generation
                if gen < generations - 1 and not early_stop:
                    layout_management_start = time.perf_counter()
                    actual_elite = min(elite_count, len(layouts))
                    elites = layouts[:actual_elite]
                    
                    if self.draw_callback:
                        next_gen_payload = {
                            'build_next_generation': True,
                            'gen': gen,
                            'layouts': layouts,
                            'elites': elites,
                            'master_map': master_map,
                            'quantities': quantities,
                            'ui_params': ui_params,
                            'rotation_steps': rotation_steps,
                            'mutation_rate': mutation_rate,
                            'immigrant_ratio': immigrant_ratio,
                            'verbose': verbose
                        }
                        self.draw_callback(next_gen_payload)
                        layouts = self._pending_layouts
                    else:
                        layouts = self._build_next_generation(
                            gen, layouts, elites, master_map, quantities, ui_params, 
                            rotation_steps, mutation_rate, immigrant_ratio, verbose
                        )
                    self._ga_perf['layout_management_s'] += (
                        time.perf_counter() - layout_management_start)

                generation_elapsed = time.perf_counter() - generation_start
                self._ga_perf['generation_s'] += generation_elapsed
                if performance_logging:
                    FreeCAD.Console.PrintMessage(
                        f"[GA PERF] generation={gen + 1} total={generation_elapsed:.2f}s "
                        f"layouts={len(layouts)} nesting={gen_time:.2f}s "
                        f"cumulative_nesting={self._ga_perf['nesting_s']:.2f}s "
                        f"layout_management={self._ga_perf['layout_management_s']:.2f}s "
                        f"candidate_wall={self._ga_perf['candidate_evaluation_wall_s'] - generation_perf_start['candidate_evaluation_wall_s']:.2f}s "
                        f"exact_checks={self._ga_perf['exact_collision_checks'] - generation_perf_start['exact_collision_checks']} "
                        f"candidate_geometries={self._ga_perf['candidate_geometries_built'] - generation_perf_start['candidate_geometries_built']} "
                        f"geometry_keys={self._ga_perf['candidate_geometry_observations'] - generation_perf_start['candidate_geometry_observations']} "
                        f"geometry_unique={self._ga_perf['candidate_geometry_unique'] - generation_perf_start['candidate_geometry_unique']} "
                        f"geometry_repeats={self._ga_perf['candidate_geometry_repeats'] - generation_perf_start['candidate_geometry_repeats']} "
                        f"cache_hits={self._ga_perf['candidate_geometry_cache_hits'] - generation_perf_start['candidate_geometry_cache_hits']} "
                        f"cache_misses={self._ga_perf['candidate_geometry_cache_misses'] - generation_perf_start['candidate_geometry_cache_misses']}"
                        + (self._format_layout_generation(layout_perf_start) if self._layout_perf else "")
                        + "\n")

                if early_stop:
                    FreeCAD.Console.PrintMessage(
                        f"Early stopping: no improvement for "
                        f"{early_stop_threshold} generations\n")
                    break

            # Final cleanup: discard every layout except the winner.
            #
            # Deliberately done once after the loop rather than in the last
            # generation: the cancel, interrupt and early-stop exits all leave
            # a full population behind, and leaving those layouts in the
            # document leaks their FreeCAD objects into the commit path.
            # delete_layout is re-entry safe, so layouts already discarded by
            # _build_next_generation are skipped here.
            survivors = [l for l in layouts if l is not best_layout]
            cleanup_start = time.perf_counter()
            if survivors:
                if self.draw_callback:
                    self.draw_callback({
                        'cleanup_layouts': True,
                        'layouts': survivors,
                        'best_layout': best_layout,
                        'verbose': verbose,
                    })
                else:
                    for layout in survivors:
                        self.layout_manager.delete_layout(layout, verbose=verbose)
            self._record_layout_time(
                'lm_cleanup_s', time.perf_counter() - cleanup_start)
            layouts = [best_layout] if best_layout is not None else []

            # Fill phase: generations nested regular parts only — fill the
            # winning layout exactly once (spawns still marshal to the main
            # thread via request_spawn).
            if best_layout is not None and not cancel_callback():
                fill_parts = [p for p in best_layout.parts
                              if getattr(p, 'fill_sheet', False) is True]
                if fill_parts:
                    from .nesting_logic import fill_existing_sheets
                    self._set_status("Filling winning layout...")
                    fill_kwargs = algo_kwargs.copy()
                    fill_kwargs.pop('clear_nfp_cache', None)
                    fill_kwargs['rng'] = self.rng
                    fill_kwargs['spawn_more_callback'] = self.request_spawn
                    fill_kwargs['candidate_geometry_key_tracker'] = self._candidate_geometry_key_tracker
                    fill_kwargs['candidate_geometry_cache'] = self._candidate_geometry_cache
                    fill_kwargs['cancel_callback'] = cancel_callback
                    if self.draw_callback:
                        fill_kwargs.pop('progress_callback', None)
                        fill_kwargs['log_callback'] = (
                            lambda msg, level=None: FreeCAD.Console.PrintMessage(f"{msg}\n"))
                    if best_layout.sheets is None:
                        best_layout.sheets = []
                    pre_counts = [len(s.parts) for s in best_layout.sheets]
                    fill_start = time.perf_counter()
                    _, fill_time = fill_existing_sheets(
                        best_layout.sheets, fill_parts,
                        ui_params['sheet_width'], ui_params['sheet_height'],
                        rotation_steps, simulate=is_simulating,
                        viz_manager=viz_manager, **fill_kwargs)
                    self._record_layout_time(
                        'lm_fill_s', time.perf_counter() - fill_start)
                    total_nesting_time += fill_time
                    if not is_simulating:
                        # Fill parts placed here never went through the
                        # non-sim rebind loop, so sync their doc placement
                        # (mirrors _run_generation's rebind).
                        for sheet, n_before in zip(best_layout.sheets, pre_counts):
                            for placed in sheet.parts[n_before:]:
                                placed.shape.placement = placed.shape.get_final_placement(sheet.get_origin())
                        for sheet in best_layout.sheets[len(pre_counts):]:
                            for placed in sheet.parts:
                                placed.shape.placement = placed.shape.get_final_placement(sheet.get_origin())
                    self.layout_manager.calculate_efficiency(
                        best_layout, ui_params['sheet_width'], ui_params['sheet_height'],
                        ui_params.get('compactness_weight', 0.0))
                    best_efficiency = best_layout.efficiency

            if performance_logging:
                FreeCAD.Console.PrintMessage(
                    "[GA PERF TOTAL] "
                    f"generations={self._ga_perf['generation_s']:.2f}s "
                    f"layouts={self._ga_perf['layout_evaluations']} "
                    f"nesting={self._ga_perf['nesting_s']:.2f}s "
                    f"nfp_compute={self._ga_perf['nfp_compute_s']:.2f}s "
                    # validity_stage is the same window as candidate_wall below
                    # (PlacementOptimizer assigns both from t_validity-t_nfp):
                    # it is the post-NFP stage, which decomposes into
                    # candidate_geometry + sheet_difference + collision.
                    f"validity_stage={self._ga_perf['candidate_validity_s']:.2f}s "
                    f"score={self._ga_perf['candidate_score_s']:.2f}s "
                    f"candidate_geometry={self._ga_perf['candidate_geometry_s']:.2f}s "
                    f"sheet_difference={self._ga_perf['sheet_difference_s']:.2f}s "
                    f"collision_intersection={self._ga_perf['collision_intersection_s']:.2f}s "
                    f"candidate_wall={self._ga_perf['candidate_evaluation_wall_s']:.2f}s "
                    f"placement_wall={self._ga_perf['placement_wall_s']:.2f}s "
                    f"rotation_wall={self._ga_perf['rotation_wall_s']:.2f}s "
                    f"max_concurrent_rotations={self._ga_perf['max_concurrent_rotations']} "
                    f"candidate_geometries={self._ga_perf['candidate_geometries_built']} "
                    f"geometry_key_observations={self._ga_perf['candidate_geometry_observations']} "
                    f"geometry_unique={self._ga_perf['candidate_geometry_unique']} "
                    f"geometry_repeats={self._ga_perf['candidate_geometry_repeats']} "
                    f"cache_hits={self._ga_perf['candidate_geometry_cache_hits']} "
                    f"cache_misses={self._ga_perf['candidate_geometry_cache_misses']} "
                    f"cache_s={self._ga_perf['candidate_geometry_cache_ms'] / 1000:.2f} "
                    f"cache_entries={self._ga_perf['candidate_geometry_cache_entries']} "
                    f"bbox_checks={self._ga_perf['bbox_checks']} "
                    f"bbox_overlap_pairs={self._ga_perf['bbox_overlap_pairs']} "
                    f"exact_collision_checks={self._ga_perf['exact_collision_checks']} "
                    f"layout_management={self._ga_perf['layout_management_s']:.2f}s "
                    f"offspring={self._ga_perf['offspring_layouts']} "
                    f"immigrants={self._ga_perf['immigrant_layouts']} "
                    f"rotations={self._ga_perf['rotation_evaluations']} "
                    f"candidates={self._ga_perf['candidate_points']} "
                    f"valid_candidates={self._ga_perf['valid_candidate_points']} "
                    f"bounds_survivors={self._ga_perf['bounds_survivors']} "
                    f"sheet_candidates={self._ga_perf['sheet_candidates']} "
                    f"sheet_rejections={self._ga_perf['sheet_rejections']} "
                    f"sheet_boundary={self._ga_perf['sheet_boundary_candidates']} "
                    f"collision_candidates={self._ga_perf['collision_candidates']} "
                    f"collision_rejections={self._ga_perf['collision_rejections']} "
                    f"bbox_rejections={self._ga_perf['bbox_rejections']} "
                    f"polygon_checks={self._ga_perf['polygon_checks']} "
                    f"nfp_hits={self._ga_perf['nfp_cache_hits']} "
                    f"nfp_misses={self._ga_perf['nfp_cache_misses']}"
                    + self._format_layout_totals()
                    + "\n")
            
            # STEP 4: Finalize result — dispatch to main thread (ViewObject + recompute)
            finalize_start = time.perf_counter()
            job = self._dispatch_finalize(best_layout, best_efficiency, total_nesting_time, target_layout, ui_params)
            # _finalize runs on the main thread when a worker is active, so its
            # own cost is recorded by the controller; the dispatch wait is
            # charged here.
            self._record_layout_time(
                'lm_finalize_s', time.perf_counter() - finalize_start)
            if performance_logging and self._layout_perf is not None:
                # Recompute happens after the totals line, so report it separately.
                FreeCAD.Console.PrintMessage(
                    "[GA PERF FINALIZE] "
                    f"finalize_dispatch={self._layout_perf['lm_finalize_s']:.2f}s "
                    f"doc_recompute={self._layout_perf['lm_doc_recompute_s']:.2f}s\n")
            return job

        except Exception as e:
            import traceback
            FreeCAD.Console.PrintError(f"GA Nesting Error: {e}\n{traceback.format_exc()}\n")
            self._set_status(f"Error: {e}")
            if 'layouts' in locals():
                for layout in layouts:
                    self.layout_manager.delete_layout(layout)
            self._dispatch_recompute()
            return None
        finally:
            if self._candidate_geometry_cache is not None:
                self._candidate_geometry_cache.clear()
            if self._candidate_geometry_key_tracker is not None:
                self._candidate_geometry_key_tracker.clear()
            self._candidate_geometry_cache = None
            self._candidate_geometry_key_tracker = None

    def _dispatch_finalize(self, best_layout, best_efficiency, total_time, target_layout, ui_params):
        """Runs _finalize() and doc.recompute() on the main thread if using a worker."""
        if self.draw_callback:
            result_holder = [None]
            self.draw_callback({
                'ga_finalize': True,
                'best_layout': best_layout,
                'best_efficiency': best_efficiency,
                'total_time': total_time,
                'target_layout': target_layout,
                'ui_params': ui_params,
                'result_holder': result_holder,
            })
            return result_holder[0]
        else:
            job = self._finalize(best_layout, best_efficiency, total_time, target_layout, ui_params)
            recompute_start = time.perf_counter()
            self.doc.recompute()
            self._record_doc_recompute(time.perf_counter() - recompute_start)
            return job

    def _dispatch_recompute(self):
        """Runs doc.recompute() on the main thread if using a worker."""
        if self.draw_callback:
            self.draw_callback({'doc_recompute_only': True})
        else:
            recompute_start = time.perf_counter()
            self.doc.recompute()
            self._record_doc_recompute(time.perf_counter() - recompute_start)

    def request_spawn(self, spawn_fn):
        """Runs spawn_fn() on the main thread (it creates FreeCAD doc objects)
        and returns the new part. Used by the nester to mint fill-part
        instances on demand."""
        if not self.draw_callback:
            return spawn_fn()
        result_holder = [None]
        self.draw_callback({'spawn_fill_part': True, 'spawn_fn': spawn_fn,
                            'result_holder': result_holder})
        return result_holder[0]

    def _run_generation(self, layouts, gen, generations, ui_params, rotation_steps, algo_kwargs,
                        is_simulating, cancel_callback, verbose, viz_manager=None):
        """Nests each layout in the population and calculates fitness/efficiency."""
        from .nesting_logic import nest
        total_time = 0
        
        for idx, layout in enumerate(layouts):
            if cancel_callback(): return total_time, True

            if verbose:
                FreeCAD.Console.PrintMessage(f"  [Gen {gen+1}] Layout {idx+1}/{len(layouts)}: {layout.name}\n")

            if layout.sheets: continue
            if not layout.parts:
                layout.fitness, layout.efficiency = float('inf'), 0
                continue
            
            # Run nesting
            current_kwargs = algo_kwargs.copy()
            current_kwargs['rng'] = self.rng  # Seeded fallback for search_direction=None
            current_kwargs['spawn_more_callback'] = self.request_spawn
            current_kwargs['candidate_geometry_key_tracker'] = self._candidate_geometry_key_tracker
            current_kwargs['candidate_geometry_cache'] = self._candidate_geometry_cache
            nest_perf = [None]
            current_kwargs['perf_stats_callback'] = lambda stats: nest_perf.__setitem__(0, stats)
            if layout.direction is not None:
                current_kwargs['search_direction'] = layout.direction
            if self.draw_callback:
                # Route through thread-safe signal instead of direct Qt widget calls
                if 'progress_callback' in current_kwargs:
                    current_kwargs['progress_callback'] = self._update_progress
                # The Qt log widget isn't thread-safe from the GA worker, but
                # dropping logs entirely hides the per-part [TIMING] lines —
                # route them to the FreeCAD console instead.
                current_kwargs['log_callback'] = (
                    lambda msg, level=None: FreeCAD.Console.PrintMessage(f"{msg}\n"))
            if len(layouts) > 1 or generations > 1:
                 current_kwargs['quiet'] = True
                 if 'progress_callback' in current_kwargs: del current_kwargs['progress_callback']
            if layout.genes: current_kwargs['sort'] = False
            
            # Fill placement is deferred to the winner (see run(), Phase B) —
            # generations score regular parts only, so the compactness term
            # measures the true free area.
            regular_parts = [p for p in layout.parts
                             if getattr(p, 'fill_sheet', False) is not True]
            sheets, unplaced, _, elapsed = nest(
                regular_parts, ui_params['sheet_width'], ui_params['sheet_height'],
                rotation_steps, is_simulating, algorithm=ui_params.get('algorithm', 'Minkowski'),
                viz_manager=viz_manager, **current_kwargs
            )
            if nest_perf[0] is not None:
                self._record_nest_perf(nest_perf[0], elapsed)

            if not is_simulating:
                 rebind_start = time.perf_counter()
                 original_parts_map = {p.id: p for p in layout.parts}
                 for s in sheets:
                     for i, placed_part in enumerate(s.parts):
                          # Parts spawned mid-nest (fill top-ups) aren't in
                          # layout.parts, but they were created fresh on the main
                          # thread (not deep-copied) so they already carry a live
                          # fc_object — use them as-is.
                          original_part = original_parts_map.get(placed_part.shape.id, placed_part.shape)
                          original_part.placement = placed_part.shape.get_final_placement(s.get_origin())
                          if original_part is not placed_part.shape:
                              # The seed's polygon still sits at the master
                              # location; fitness reads bounding_box() from it
                              # (calculate_efficiency), so sync the placed
                              # geometry across.
                              original_part.polygon = placed_part.shape.polygon
                              original_part._angle = placed_part.shape._angle
                          s.parts[i].shape = original_part
                 self._record_layout_time(
                     'lm_post_nest_rebind_s', time.perf_counter() - rebind_start)
            total_time += elapsed
            layout.sheets, layout.unplaced = sheets, unplaced

            # Capture genes (fill parts are not part of the genotype)
            gene_parts = [p for p in layout.parts if getattr(p, 'fill_sheet', False) is not True]
            if is_simulating:
                layout.genes = [(p.id, getattr(p, '_angle', 0)) for p in gene_parts] if gene_parts else []
            else:
                gene_map = {placed_part.shape.id: getattr(placed_part.shape, '_angle', 0)
                            for s in layout.sheets for placed_part in s.parts}
                layout.genes = [(p.id, gene_map.get(p.id, getattr(p, '_angle', 0)))
                                for p in gene_parts] if gene_parts else []
            
            # Efficiency/Fitness
            efficiency_start = time.perf_counter()
            self.layout_manager.calculate_efficiency(
                layout, ui_params['sheet_width'], ui_params['sheet_height'],
                ui_params.get('compactness_weight', 0.0))
            self._record_layout_time(
                'lm_efficiency_s', time.perf_counter() - efficiency_start)
            unplaced_regular = [p for p in unplaced if getattr(p, 'fill_sheet', False) is not True]
            if unplaced_regular:
                layout.fitness += len(unplaced_regular) * ui_params['sheet_width'] * ui_params['sheet_height'] * 10
            
            # Draw (simulate mode only — non-sim draws just the winner in _finalize)
            if is_simulating:
                draw_payload = {
                    'sheets': sheets,
                    'doc': self.doc,
                    'ui_params': ui_params,
                    'layout_group': layout.layout_group,
                    'parts_group': layout.parts_group,
                    'verbose': verbose,
                    'hide_layout': len(layouts) > 1,
                }

                if self.draw_callback:
                    self.draw_callback(draw_payload)
                else:
                    for sheet in sheets:
                        sheet.draw(self.doc, ui_params, layout.layout_group, 
                                   parts_to_place_group=layout.parts_group, verbose=verbose)
                    
                    if len(layouts) > 1 and layout.layout_group and hasattr(layout.layout_group, "ViewObject"):
                        layout.layout_group.ViewObject.Visibility = False
                    FreeCADGui.updateGui()
            
        return total_time, False

    def _build_next_generation(self, gen, layouts, elites, master_map, quantities, ui_params, 
                               rotation_steps, mutation_rate, immigrant_ratio, verbose):
        """Handles selection, crossover, mutation, and immigrants."""
        from .algorithms import genetic_utils

        next_gen_start = time.perf_counter()
        
        use_random_direction = ui_params.get('use_random_direction', False)
        ranked_pool = [(e.fitness, (e.genes, e.direction)) for e in elites if e.genes]
        new_layouts = [elites[0]] # Champion carries forward

        # Discarding the outgoing layouts is charged to lm_delete_s (measured
        # inside delete_layout); gene bookkeeping starts after it so the
        # sub-phase timers stay disjoint.
        for e in elites[1:]: self.layout_manager.delete_layout(e, verbose=verbose)
        for layout in layouts:
            if layout not in elites: self.layout_manager.delete_layout(layout, verbose=verbose)

        gene_ops_start = time.perf_counter()
        population_size = len(layouts)
        n_immigrants = max(1, int((population_size - 1) * immigrant_ratio))
        n_offspring = max(0, (population_size - 1) - n_immigrants)
        if self._ga_perf is not None:
            self._ga_perf['offspring_layouts'] += n_offspring
            self._ga_perf['immigrant_layouts'] += n_immigrants
        self._record_layout_time('lm_gene_ops_s', time.perf_counter() - gene_ops_start)

        for i in range(n_offspring):
            gene_start = time.perf_counter()
            k = min(3, len(ranked_pool))
            if len(ranked_pool) >= 2:
                winner1 = genetic_utils.tournament_selection(ranked_pool, k=k, rng=self.rng)
                winner2 = genetic_utils.tournament_selection(ranked_pool, k=k, rng=self.rng)
                p1_genes, p1_dir = winner1
                p2_genes, p2_dir = winner2
                child_genes = genetic_utils.crossover_genes(p1_genes, p2_genes, rng=self.rng)
                parent_direction = p1_dir
            else:
                p1_genes, p1_dir = ranked_pool[0][1] if ranked_pool else ([], None)
                child_genes = list(p1_genes)
                parent_direction = p1_dir
            child_genes = genetic_utils.mutate_genes(child_genes, mutation_rate, rotation_steps, rng=self.rng)
            self._record_layout_time('lm_gene_ops_s', time.perf_counter() - gene_start)
            child_layout = self.layout_manager.create_layout(
                f"Layout_GA_{gen+2}_c{i+1}", master_map, quantities, ui_params, chromosome_ordering=child_genes
            )
            # Offspring: inherit parent 1's direction, mutated by a small random rotation:
            if use_random_direction:
                if parent_direction is not None and self.rng.random() < mutation_rate:
                    jitter = self.rng.uniform(-math.pi / 4, math.pi / 4)
                    cur = math.atan2(parent_direction[1], parent_direction[0])
                    parent_direction = (math.cos(cur + jitter), math.sin(cur + jitter))
                child_layout.direction = parent_direction
            else:
                child_layout.direction = None
            new_layouts.append(child_layout)

        for i in range(n_immigrants):
            imm = self.layout_manager.create_layout(f"Layout_GA_{gen+2}_i{i+1}", master_map, quantities, ui_params)
            gene_start = time.perf_counter()
            if imm.parts:
                regular = [p for p in imm.parts if getattr(p, 'fill_sheet', False) is not True]
                fill = [p for p in imm.parts if getattr(p, 'fill_sheet', False) is True]

                # Shuffle the regular parts order; fill parts stay at the tail
                self.rng.shuffle(regular)
                imm.parts = regular + fill

                # Random rotations for regular parts only — fill parts keep their
                # full rotation sweep and never get a gene_angle pin
                if rotation_steps > 1:
                    for part in regular:
                        angle = self.rng.randrange(rotation_steps) * (360.0 / rotation_steps)
                        part.set_rotation(angle)
                        part.gene_angle = angle
                else:
                    for part in regular:
                        part.gene_angle = 0.0
                imm.genes = [(p.id, getattr(p, '_angle', 0)) for p in regular]
                
                # Immigrants get a fresh random direction if enabled
                if use_random_direction:
                    angle_rad = self.rng.uniform(0, 2 * math.pi)
                    imm.direction = (math.cos(angle_rad), math.sin(angle_rad))
                else:
                    imm.direction = None
            self._record_layout_time('lm_gene_ops_s', time.perf_counter() - gene_start)
            new_layouts.append(imm)
        self._record_layout_time('lm_next_generation_s', time.perf_counter() - next_gen_start)
        return new_layouts

    def _finalize(self, best_layout, best_efficiency, total_time, target_layout, ui_params):
        """Prepares the final NestingJob from the best layout."""
        from .nesting_job import NestingJob
        if not best_layout: return None
            
        if best_layout.layout_group and hasattr(best_layout.layout_group, "ViewObject"):
            best_layout.layout_group.ViewObject.Visibility = True
        
        if not getattr(self, 'is_simulating', False):
            # A headless layout has no FreeCAD part objects yet, and
            # Sheet.draw re-parents shape.fc_object into the final nested_*
            # containers. Build them for the winner first — this is the only
            # layout that ever needs them.
            if getattr(self, '_headless_layouts', False):
                materialize_start = time.perf_counter()
                created = self.layout_manager.materialize_layout_objects(
                    best_layout, ui_params)
                self._record_layout_time('lm_materialize_s',
                                         time.perf_counter() - materialize_start)
                if self._layout_perf is not None:
                    FreeCAD.Console.PrintMessage(
                        f"[GA] Materialized {created} part objects for the "
                        f"winning layout\n")
            for sheet in best_layout.sheets:
                sheet.draw(self.doc, ui_params, best_layout.layout_group,
                           parts_to_place_group=best_layout.parts_group)

        if best_layout.layout_group and hasattr(best_layout.layout_group, "Group"):
            for child in best_layout.layout_group.Group:
                if child.Label.startswith("MasterShapes") and hasattr(child, "ViewObject"):
                    child.ViewObject.Visibility = False
        
        best_layout.layout_group.Label = "Layout_temp"
        job = NestingJob.from_ga_result(
            doc=self.doc, target_layout=target_layout, params=ui_params, preparer=self.shape_preparer,
            layout_group=best_layout.layout_group, parts_group=best_layout.parts_group, sheets=best_layout.sheets
        )
        
        unplaced_count = len(getattr(best_layout, 'unplaced', []) or [])
        placed_count = sum(len(s) for s in best_layout.sheets)
        msg = f"GA Complete: {best_efficiency:.1f}% efficiency, {len(best_layout.sheets)} sheets, {placed_count} placed"
        if unplaced_count: msg += f", {unplaced_count} UNPLACED"
        msg += f", Time: {total_time:.2f}s"
        
        self._set_status(msg)
        FreeCAD.Console.PrintMessage(f"{msg}\n")
        if unplaced_count:
            FreeCAD.Console.PrintWarning(f"WARNING: {unplaced_count} part(s) could not be placed: {[p.id for p in best_layout.unplaced]}\n")
        FreeCAD.Console.PrintMessage(f"--- NESTING DONE ---\n")
        self._play_sound()
        return job
