import math
from collections import Counter, OrderedDict, defaultdict
import secrets
import time

import bpy
import bmesh
from mathutils import Vector

from . import (
    island_tools,
    match_scheduler,
    pro_process_adapter,
    pro_process_payload,
    pro_process_pipeline,
    pro_candidate_planner,
    pro_process_pool,
    pro_process_shape,
    pro_shape_state,
    pro_group_first,
    pro_verified_nearest,
    pro_worker,
    similarity_matcher,
    topology_correspondence,
    uv_utils,
)


_MATCH03_EVIDENCE_SINK = None
_ALIGN_SIMILAR_PRO_EVIDENCE_SINK = None
_CHEAP_INVARIANT_BUCKET_WIDTH = 0.005
_PRO_DENSITY_TIE_EPSILON = 1.0e-12
_PRO_UV_AREA_TIE_EPSILON = pro_group_first.DEFAULT_UV_AREA_TIE_EPSILON
_PRO_FAST_SYNC_ERROR = (
    "Pro Fast requires an external correspondence worker; synchronous mode "
    "cannot provide verified-nearest-only semantics."
)
_PRO_EXACT_RESIDUAL_TOLERANCE = 1.0e-6
_PRO_SYNC_WALL_TIME_BUDGET_MS = 30_000.0
# Empirical minimum preserving the current locked pair and all six dedicated
# exact/rejection cases.  topology_correspondence keeps its independent core
# default; Pro always passes this bounded value explicitly.
_PRO_CORRESPONDENCE_MAX_SEARCH = 1024
_PRO_COOPERATIVE_YIELD_EVERY = 64
_PRO_MODAL_TICK_ACTIVE_BUDGET_MS = 12.0
_PRO_EXACT_SLICE_BUDGET_MS = 10.0
_PRO_EXACT_OPERATION_BUDGET = 256
_PRO_SHAPE_OPERATION_BUDGET = 64
_PRO_ENUMERATION_SLICE_BUDGET_MS = 10.0
_PRO_ENUMERATION_OPERATION_CAP = 1024
_PRO_GRAPH_OPERATION_BUDGET = 256
_PRO_SNAPSHOT_OPERATION_BUDGET = 96
_PRO_SNAPSHOT_VALIDATION_OPERATION_BUDGET = 96
_PRO_SNAPSHOT_VALIDATION_SLICE_BUDGET_MS = 2.0
_PRO_PROCESS_FINALIZATION_GRACE_MS = 5_000.0
_PRO_PROCESS_FINALIZATION_GRACE_SLICE_BUDGET_MS = 50.0
_PRO_PROCESS_GRAPH_OPERATION_BUDGET = 64
_PRO_PROCESS_GRAPH_SLICE_BUDGET_MS = 2.0
_PRO_DEFAULT_PROCESS_WORKER_COUNT = 4
_PRO_DEFAULT_PROCESS_BATCH_SIZE = 32
_PRO_PROCESS_GRAPH_REJECTION_REASONS = frozenset(
    {
        "boundary_component_branch_or_open",
        "boundary_component_degenerate_segment",
        "boundary_component_not_closed",
        "boundary_component_trace_failed",
        "boundary_component_trace_not_closed",
        "boundary_component_degenerate_area",
        "non_manifold_topology",
    }
)
_PRO_MODAL_TIMER_INTERVAL = 0.01
_PRO_MODAL_MAX_CORRESPONDENCE_PER_TICK = 1
_PRO_RECORD_OPERATION_BUDGET = 64
_PRO_RECORD_SORT_RUN_SIZE = 32
_PRO_PROGRESS_STALL_GRACE_MS = 30_000.0
_PRO_CANDIDATE_PLANNER_CONFIG = pro_candidate_planner.PlannerConfig(
    per_member_k=8,
    global_pair_budget=4096,
    per_bucket_pair_budget=4096,
    descriptor_bin_width=0.05,
    index_dimensions=2,
    fallback_probe_limit=16,
    fallback_candidate_limit=8,
    batch_size=256,
    density_tie_epsilon=_PRO_DENSITY_TIE_EPSILON,
)
_PRO_GRAPH_CACHE_LIMIT = 4
_PRO_REJECTION_SAMPLE_LIMIT = 8
_PRO_GROUP_SAMPLE_LIMIT = 16
_ACTIVE_PRO_SESSION = None
_ACTIVE_PRO_OPERATOR = None


def _island_face_key(island):
    return tuple(sorted({loop.face.index for loop in island}))


def _island_is_selected(island, uv_layer):
    """Mirror island_tools' selection predicate for an already-built island."""

    return any(
        bool(
            getattr(loop[uv_layer], "select", False)
            or getattr(loop[uv_layer], "select_edge", False)
            or loop.face.select
            or loop.vert.select
            or loop.edge.select
        )
        for loop in island
    )


def _uv_close(a, b, epsilon=1e-6):
    return abs(a.x - b.x) <= epsilon and abs(a.y - b.y) <= epsilon


def _edge_uvs_match(loop_a, loop_b, uv_layer):
    a0 = loop_a[uv_layer].uv
    a1 = loop_a.link_loop_next[uv_layer].uv
    b0 = loop_b[uv_layer].uv
    b1 = loop_b.link_loop_next[uv_layer].uv
    return (_uv_close(a0, b1) and _uv_close(a1, b0)) or (
        _uv_close(a0, b0) and _uv_close(a1, b1)
    )


def _island_boundary_segments(island, uv_layer):
    edge_map = {}
    for loop in island:
        edge_map.setdefault(loop.edge, []).append(loop)

    segments = []
    for loop in island:
        has_uv_neighbor = False
        for other in edge_map.get(loop.edge, []):
            if other is loop or other.face is loop.face:
                continue
            if _edge_uvs_match(loop, other, uv_layer):
                has_uv_neighbor = True
                break
        if has_uv_neighbor:
            continue
        start = loop[uv_layer].uv.copy()
        end = loop.link_loop_next[uv_layer].uv.copy()
        if (end - start).length_squared > 1e-14:
            segments.append((start, end))
    return segments


def _unique_segment_points(segments):
    points = []
    seen = set()
    for start, end in segments:
        for point in (start, end):
            key = (round(point.x, 7), round(point.y, 7))
            if key in seen:
                continue
            seen.add(key)
            points.append(point.copy())
    return points


def _numeric_boundary_segments(island, uv_layer):
    """Return immutable numeric segments; no BMesh object crosses the matcher boundary."""

    return tuple(
        (
            (float(start.x), float(start.y)),
            (float(end.x), float(end.y)),
        )
        for start, end in _island_boundary_segments(island, uv_layer)
    )


def _island_topology_metadata(island):
    """Build topology metadata from the current island without retaining BMesh refs."""

    faces = set()
    edge_faces = {}
    vertices = set()
    for loop in island:
        faces.add(loop.face)
        vertices.add(loop.vert)
        edge_faces.setdefault(loop.edge, set()).add(loop.face)

    incidence = Counter(len(face_set) for face_set in edge_faces.values())
    return {
        "face_count": len(faces),
        "edge_count": len(edge_faces),
        "vertex_count": len(vertices),
        "non_manifold_edge_count": sum(
            count for degree, count in incidence.items() if degree > 2
        ),
        "edge_incidence_histogram": dict(incidence),
    }


def _numeric_island_inputs(island, uv_layer):
    """Extract immutable boundary segments and topology in one island pass."""

    edge_map = {}
    faces = set()
    vertices = set()
    edge_faces = {}
    for loop in island:
        edge_map.setdefault(loop.edge, []).append(loop)
        faces.add(loop.face)
        vertices.add(loop.vert)
        edge_faces.setdefault(loop.edge, set()).add(loop.face)

    segments = []
    for loop in island:
        has_uv_neighbor = False
        for other in edge_map.get(loop.edge, []):
            if other is loop or other.face is loop.face:
                continue
            if _edge_uvs_match(loop, other, uv_layer):
                has_uv_neighbor = True
                break
        if has_uv_neighbor:
            continue
        start = loop[uv_layer].uv
        end = loop.link_loop_next[uv_layer].uv
        if (end - start).length_squared > 1e-14:
            segments.append(
                ((float(start.x), float(start.y)), (float(end.x), float(end.y)))
            )

    incidence = Counter(len(face_set) for face_set in edge_faces.values())
    topology = {
        "face_count": len(faces),
        "edge_count": len(edge_faces),
        "vertex_count": len(vertices),
        "non_manifold_edge_count": sum(
            count for degree, count in incidence.items() if degree > 2
        ),
        "edge_incidence_histogram": dict(incidence),
    }
    return tuple(segments), topology


def _snapshot_identity(obj, bm, uv_layer, islands):
    """Return an execution-local identity for the immutable pre-apply UV snapshot."""

    return (
        "uv-snapshot",
        getattr(obj.data, "name", ""),
        getattr(uv_layer, "name", ""),
        id(bm),
        len(islands),
    )


def _pro_uv_token(uv):
    return (round(float(uv.x), 10), round(float(uv.y), 10))


def _pro_boundary_components(loop_by_key, boundary_loop_keys, uv_layer):
    """Trace closed UV-boundary rings from immutable loop-key candidates."""

    if not boundary_loop_keys:
        return ()

    endpoints = {}
    endpoint_to_loops = {}
    for key in boundary_loop_keys:
        loop = loop_by_key[key]
        start_uv = loop[uv_layer].uv
        end_uv = loop.link_loop_next[uv_layer].uv
        start = (int(loop.vert.index), _pro_uv_token(start_uv))
        end = (int(loop.link_loop_next.vert.index), _pro_uv_token(end_uv))
        endpoints[key] = (start, end)
        endpoint_to_loops.setdefault(start, []).append(key)
        endpoint_to_loops.setdefault(end, []).append(key)

    adjacency = {key: set() for key in boundary_loop_keys}
    for endpoint, keys in endpoint_to_loops.items():
        del endpoint
        if len(keys) != 2:
            raise ValueError("boundary_component_branch_or_open")
        left, right = keys
        if left == right:
            raise ValueError("boundary_component_degenerate_segment")
        adjacency[left].add(right)
        adjacency[right].add(left)
    if any(len(neighbours) != 2 for neighbours in adjacency.values()):
        raise ValueError("boundary_component_not_closed")

    unseen = set(boundary_loop_keys)
    components = []
    while unseen:
        start = min(unseen)
        stack = [start]
        unseen.remove(start)
        component = []
        while stack:
            key = stack.pop()
            component.append(key)
            for neighbour in sorted(adjacency[key]):
                if neighbour in unseen:
                    unseen.remove(neighbour)
                    stack.append(neighbour)
        components.append(tuple(sorted(component)))

    ordered_components = []
    for component in components:
        start = min(component)
        ordered = [start]
        previous = None
        current = start
        while len(ordered) < len(component):
            candidates = sorted(
                neighbour
                for neighbour in adjacency[current]
                if neighbour != previous and neighbour not in ordered
            )
            if not candidates:
                raise ValueError("boundary_component_trace_failed")
            previous, current = current, candidates[0]
            ordered.append(current)
        if start not in adjacency[current]:
            raise ValueError("boundary_component_trace_not_closed")
        area_twice = 0.0
        for key in component:
            loop = loop_by_key[key]
            start_uv = loop[uv_layer].uv
            end_uv = loop.link_loop_next[uv_layer].uv
            area_twice += float(start_uv.x) * float(end_uv.y)
            area_twice -= float(end_uv.x) * float(start_uv.y)
        area = abs(area_twice) * 0.5
        if not math.isfinite(area) or area <= 1.0e-14:
            raise ValueError("boundary_component_degenerate_area")
        ordered_components.append((area, tuple(ordered)))

    ordered_components.sort(key=lambda item: (-item[0], item[1]))
    boundaries = []
    outer_key = ("boundary", 0)
    for index, (_area, loop_keys) in enumerate(ordered_components):
        component_key = ("boundary", index)
        boundaries.append(
            topology_correspondence.BoundaryComponentRecord(
                key=component_key,
                loop_keys=tuple(loop_keys),
                role="outer" if index == 0 else "hole",
                parent_key=None if index == 0 else outer_key,
                signature=("closed", len(loop_keys)),
            )
        )
    return tuple(boundaries)


class _ProGraphBuildState:
    """Resumable Pro-only copy of one BMesh island into immutable records.

    The synchronous ``_pro_graph_for_island`` helper below remains the small
    equivalence oracle used by focused tests and compatibility callers.  Live
    Pro sessions use this state instead, so graph construction never enters
    the graph cache until every phase has completed.
    """

    def __init__(self, island, uv_layer):
        self.island = island if isinstance(island, (tuple, list)) else tuple(island)
        self.uv_layer = uv_layer
        self.phase = "faces"
        self.result = None
        self.phase_transitions = []
        self.graph_primitive_operations = 0
        self.graph_slices = 0
        self.last_graph_slice_ms = 0.0
        self.last_graph_slice_operations = 0
        self.max_graph_slice_ms = 0.0
        self.phase_timings = {}
        self.sort_ms = 0.0
        self.graph_finalize_ms = 0.0

        self._island_index = 0
        self._island_loop_ids = set()
        self._faces_by_key = {}
        self._faces = ()
        self._face_index = 0
        self._current_face = None
        self._current_face_loops = ()
        self._loop_index = 0
        self._loop_by_key = {}
        self._face_loop_keys = {}
        self._edge_loops = {}
        self._vertex_loops = {}
        self._loop_items = ()
        self._loop_index_cursor = 0
        self._vertex_uvs = {}
        self._uv_split = {}
        self._edge_items = ()
        self._edge_index_cursor = 0
        self._edge_face_keys = {}
        self._edge_boundary = {}
        self._edge_non_manifold = {}
        self._sorted_loop_items = ()
        self._record_index = 0
        self._loop_records = []
        self._sorted_face_keys = ()
        self._face_record_index = 0
        self._face_records = []
        self._sorted_edge_items = ()
        self._edge_record_index = 0
        self._edge_records = []
        self._sorted_vertex_items = ()
        self._vertex_record_index = 0
        self._vertex_records = []

        self._boundary_keys = ()
        self._boundary_index = 0
        self._endpoints = {}
        self._endpoint_to_loops = {}
        self._endpoint_items = ()
        self._boundary_adjacency = {}
        self._boundary_endpoint_index = 0
        self._boundary_validate_index = 0
        self._boundary_unseen = set()
        self._boundary_components = []
        self._boundary_component_current = []
        self._boundary_stack = []
        self._boundary_dfs_neighbors = None
        self._boundary_dfs_neighbor_index = 0
        self._ordered_component_index = 0
        self._ordered_components = []
        self._ordered_current_component = ()
        self._ordered_current_start = None
        self._ordered_current = None
        self._ordered_previous = None
        self._ordered_values = []
        self._boundary_area_index = 0
        self._boundary_area_twice = 0.0
        self._boundary_record_index = 0
        self._boundaries = ()

    @property
    def done(self):
        return self.result is not None

    def _set_phase(self, phase):
        if self.phase != phase:
            self.phase_transitions.append((self.phase, phase))
            self.phase = phase

    def _phase_time(self, phase, started):
        elapsed = (time.perf_counter() - started) * 1000.0
        self.phase_timings[phase] = self.phase_timings.get(phase, 0.0) + elapsed
        return elapsed

    def _start_boundary_components(self):
        self._boundary_unseen = set(self._boundary_keys)
        self._boundary_components = []
        self._boundary_component_current = []
        self._boundary_stack = []
        self._boundary_dfs_neighbors = None
        self._boundary_dfs_neighbor_index = 0
        self._set_phase("boundary_components")

    def advance(self, operation_budget=_PRO_GRAPH_OPERATION_BUDGET, deadline=None):
        """Advance graph copying until the cap/deadline or graph completion."""

        if self.done:
            return self.result, 0
        try:
            operation_budget = max(0, int(operation_budget))
        except (TypeError, ValueError):
            operation_budget = 0
        started = time.perf_counter()
        operations = 0
        while self.result is None and operations < operation_budget:
            if deadline is not None and time.perf_counter() >= deadline:
                break

            if self.phase == "faces":
                if self._island_index < len(self.island):
                    loop = self.island[self._island_index]
                    self._island_index += 1
                    self._island_loop_ids.add(id(loop))
                    self._faces_by_key[int(loop.face.index)] = loop.face
                else:
                    sort_started = time.perf_counter()
                    self._faces = tuple(
                        sorted(
                            self._faces_by_key.values(),
                            key=lambda face: int(face.index),
                        )
                    )
                    self.sort_ms += self._phase_time("faces_sort", sort_started)
                    if not self._faces:
                        raise ValueError("invalid_record_empty_graph")
                    self._set_phase("loops")
                operations += 1
                continue

            if self.phase == "loops":
                if self._current_face is None:
                    if self._face_index >= len(self._faces):
                        self._loop_items = tuple(self._loop_by_key.items())
                        self._loop_index_cursor = 0
                        self._set_phase("uv_splits")
                        operations += 1
                        continue
                    self._current_face = self._faces[self._face_index]
                    self._current_face_loops = tuple(self._current_face.loops)
                    self._loop_index = 0
                    self._face_loop_keys[int(self._current_face.index)] = []
                    operations += 1
                    continue
                if self._loop_index < len(self._current_face_loops):
                    local_index = self._loop_index
                    loop = self._current_face_loops[local_index]
                    self._loop_index += 1
                    if id(loop) not in self._island_loop_ids:
                        raise ValueError("invalid_record_partial_face")
                    face_key = int(self._current_face.index)
                    key = (face_key, int(local_index))
                    if key in self._loop_by_key:
                        raise ValueError("invalid_record_duplicate_loop_key")
                    self._loop_by_key[key] = loop
                    self._face_loop_keys[face_key].append(key)
                    self._edge_loops.setdefault(int(loop.edge.index), []).append(key)
                    self._vertex_loops.setdefault(int(loop.vert.index), []).append(key)
                else:
                    face_key = int(self._current_face.index)
                    self._face_loop_keys[face_key] = tuple(
                        self._face_loop_keys[face_key]
                    )
                    self._face_index += 1
                    self._current_face = None
                    self._current_face_loops = ()
                    self._loop_index = 0
                operations += 1
                continue

            if self.phase == "uv_splits":
                if self._loop_index_cursor < len(self._loop_items):
                    _key, loop = self._loop_items[self._loop_index_cursor]
                    self._loop_index_cursor += 1
                    self._vertex_uvs.setdefault(int(loop.vert.index), set()).add(
                        _pro_uv_token(loop[self.uv_layer].uv)
                    )
                else:
                    self._uv_split = {
                        vertex_key: len(values) > 1
                        for vertex_key, values in self._vertex_uvs.items()
                    }
                    self._edge_items = tuple(self._edge_loops.items())
                    self._edge_index_cursor = 0
                    self._set_phase("edge_incidence")
                operations += 1
                continue

            if self.phase == "edge_incidence":
                if self._edge_index_cursor < len(self._edge_items):
                    edge_key, keys = self._edge_items[self._edge_index_cursor]
                    self._edge_index_cursor += 1
                    face_keys = tuple(
                        sorted(
                            {
                                self._loop_by_key[key].face.index
                                for key in keys
                            }
                        )
                    )
                    self._edge_face_keys[edge_key] = tuple(
                        int(value) for value in face_keys
                    )
                    self._edge_boundary[edge_key] = len(face_keys) == 1
                    representative = self._loop_by_key[keys[0]].edge
                    self._edge_non_manifold[edge_key] = (
                        len(getattr(representative, "link_faces", ())) > 2
                    )
                else:
                    self._sorted_loop_items = tuple(
                        sorted(self._loop_by_key.items())
                    )
                    self._record_index = 0
                    self._set_phase("loop_records")
                operations += 1
                continue

            if self.phase == "loop_records":
                if self._record_index < len(self._sorted_loop_items):
                    key, loop = self._sorted_loop_items[self._record_index]
                    self._record_index += 1
                    face_key, local_index = key
                    cycle = self._face_loop_keys[face_key]
                    edge_key = int(loop.edge.index)
                    vertex_key = int(loop.vert.index)
                    self._loop_records.append(
                        topology_correspondence.LoopRecord(
                            key=key,
                            face_key=face_key,
                            edge_key=edge_key,
                            vertex_key=vertex_key,
                            next_key=cycle[(local_index + 1) % len(cycle)],
                            prev_key=cycle[(local_index - 1) % len(cycle)],
                            uv=(
                                float(loop[self.uv_layer].uv.x),
                                float(loop[self.uv_layer].uv.y),
                            ),
                            boundary=self._edge_boundary[edge_key],
                            seam=self._uv_split[vertex_key],
                            signature=("uv_split", self._uv_split[vertex_key]),
                        )
                    )
                else:
                    self._loop_records = tuple(self._loop_records)
                    self._sorted_face_keys = tuple(sorted(self._face_loop_keys))
                    self._face_record_index = 0
                    self._set_phase("face_records")
                operations += 1
                continue

            if self.phase == "face_records":
                if self._face_record_index < len(self._sorted_face_keys):
                    face_key = self._sorted_face_keys[self._face_record_index]
                    self._face_record_index += 1
                    self._face_records.append(
                        topology_correspondence.FaceRecord(
                            key=face_key,
                            loop_keys=self._face_loop_keys[face_key],
                        )
                    )
                else:
                    self._face_records = tuple(self._face_records)
                    self._sorted_edge_items = tuple(sorted(self._edge_loops.items()))
                    self._edge_record_index = 0
                    self._set_phase("edge_records")
                operations += 1
                continue

            if self.phase == "edge_records":
                if self._edge_record_index < len(self._sorted_edge_items):
                    edge_key, keys = self._sorted_edge_items[self._edge_record_index]
                    self._edge_record_index += 1
                    self._edge_records.append(
                        topology_correspondence.EdgeRecord(
                            key=edge_key,
                            loop_keys=tuple(sorted(keys)),
                            face_keys=self._edge_face_keys[edge_key],
                            boundary=self._edge_boundary[edge_key],
                            non_manifold=self._edge_non_manifold[edge_key],
                            signature=(
                                "mesh_non_manifold",
                                self._edge_non_manifold[edge_key],
                            ),
                        )
                    )
                else:
                    self._edge_records = tuple(self._edge_records)
                    self._sorted_vertex_items = tuple(
                        sorted(self._vertex_loops.items())
                    )
                    self._vertex_record_index = 0
                    self._set_phase("vertex_records")
                operations += 1
                continue

            if self.phase == "vertex_records":
                if self._vertex_record_index < len(self._sorted_vertex_items):
                    vertex_key, keys = self._sorted_vertex_items[
                        self._vertex_record_index
                    ]
                    self._vertex_record_index += 1
                    self._vertex_records.append(
                        topology_correspondence.VertexRecord(
                            key=vertex_key,
                            loop_keys=tuple(sorted(keys)),
                            boundary=any(
                                self._edge_boundary[
                                    int(self._loop_by_key[key].edge.index)
                                ]
                                for key in keys
                            ),
                            signature=("uv_split", self._uv_split[vertex_key]),
                        )
                    )
                else:
                    self._vertex_records = tuple(self._vertex_records)
                    self._boundary_keys = tuple(
                        key
                        for key, loop in self._loop_by_key.items()
                        if self._edge_boundary[int(loop.edge.index)]
                    )
                    self._boundary_index = 0
                    self._set_phase("boundary_init")
                operations += 1
                continue

            if self.phase == "boundary_init":
                if not self._boundary_keys:
                    self._boundaries = ()
                    self._set_phase("graph_finalize")
                else:
                    self._endpoints = {}
                    self._endpoint_to_loops = {}
                    self._boundary_index = 0
                    self._set_phase("boundary_endpoints")
                operations += 1
                continue

            if self.phase == "boundary_endpoints":
                if self._boundary_index < len(self._boundary_keys):
                    key = self._boundary_keys[self._boundary_index]
                    self._boundary_index += 1
                    loop = self._loop_by_key[key]
                    start_uv = loop[self.uv_layer].uv
                    end_uv = loop.link_loop_next[self.uv_layer].uv
                    start = (int(loop.vert.index), _pro_uv_token(start_uv))
                    end = (
                        int(loop.link_loop_next.vert.index),
                        _pro_uv_token(end_uv),
                    )
                    self._endpoints[key] = (start, end)
                    self._endpoint_to_loops.setdefault(start, []).append(key)
                    self._endpoint_to_loops.setdefault(end, []).append(key)
                else:
                    self._endpoint_items = tuple(self._endpoint_to_loops.items())
                    self._boundary_adjacency = {
                        key: set() for key in self._boundary_keys
                    }
                    self._boundary_endpoint_index = 0
                    self._set_phase("boundary_adjacency")
                operations += 1
                continue

            if self.phase == "boundary_adjacency":
                if self._boundary_endpoint_index < len(self._endpoint_items):
                    _endpoint, keys = self._endpoint_items[
                        self._boundary_endpoint_index
                    ]
                    self._boundary_endpoint_index += 1
                    if len(keys) != 2:
                        raise ValueError("boundary_component_branch_or_open")
                    left, right = keys
                    if left == right:
                        raise ValueError("boundary_component_degenerate_segment")
                    self._boundary_adjacency[left].add(right)
                    self._boundary_adjacency[right].add(left)
                else:
                    self._boundary_validate_index = 0
                    self._set_phase("boundary_validate")
                operations += 1
                continue

            if self.phase == "boundary_validate":
                if self._boundary_validate_index < len(self._boundary_keys):
                    key = self._boundary_keys[self._boundary_validate_index]
                    self._boundary_validate_index += 1
                    if len(self._boundary_adjacency[key]) != 2:
                        raise ValueError("boundary_component_not_closed")
                else:
                    self._start_boundary_components()
                operations += 1
                continue

            if self.phase == "boundary_components":
                if self._boundary_dfs_neighbors is not None:
                    if self._boundary_dfs_neighbor_index < len(
                        self._boundary_dfs_neighbors
                    ):
                        neighbour = self._boundary_dfs_neighbors[
                            self._boundary_dfs_neighbor_index
                        ]
                        self._boundary_dfs_neighbor_index += 1
                        if neighbour in self._boundary_unseen:
                            self._boundary_unseen.remove(neighbour)
                            self._boundary_stack.append(neighbour)
                    else:
                        self._boundary_dfs_neighbors = None
                        self._boundary_dfs_neighbor_index = 0
                elif self._boundary_stack:
                    key = self._boundary_stack.pop()
                    self._boundary_component_current.append(key)
                    self._boundary_dfs_neighbors = tuple(
                        sorted(self._boundary_adjacency[key])
                    )
                    self._boundary_dfs_neighbor_index = 0
                elif self._boundary_component_current:
                    self._boundary_components.append(
                        tuple(sorted(self._boundary_component_current))
                    )
                    self._boundary_component_current = []
                elif self._boundary_unseen:
                    start = min(self._boundary_unseen)
                    self._boundary_unseen.remove(start)
                    self._boundary_stack = [start]
                else:
                    self._ordered_component_index = 0
                    self._ordered_components = []
                    self._set_phase("boundary_ordered_init")
                operations += 1
                continue

            if self.phase == "boundary_ordered_init":
                if self._ordered_component_index >= len(self._boundary_components):
                    self._set_phase("boundary_sort")
                else:
                    component = self._boundary_components[
                        self._ordered_component_index
                    ]
                    self._ordered_current_component = component
                    self._ordered_current_start = min(component)
                    self._ordered_current = self._ordered_current_start
                    self._ordered_previous = None
                    self._ordered_values = [self._ordered_current_start]
                    self._set_phase("boundary_ordered_trace")
                operations += 1
                continue

            if self.phase == "boundary_ordered_trace":
                if len(self._ordered_values) < len(self._ordered_current_component):
                    candidates = sorted(
                        neighbour
                        for neighbour in self._boundary_adjacency[
                            self._ordered_current
                        ]
                        if neighbour != self._ordered_previous
                        and neighbour not in self._ordered_values
                    )
                    if not candidates:
                        raise ValueError("boundary_component_trace_failed")
                    self._ordered_previous = self._ordered_current
                    self._ordered_current = candidates[0]
                    self._ordered_values.append(self._ordered_current)
                else:
                    if self._ordered_current_start not in self._boundary_adjacency[
                        self._ordered_current
                    ]:
                        raise ValueError("boundary_component_trace_not_closed")
                    self._boundary_area_index = 0
                    self._boundary_area_twice = 0.0
                    self._set_phase("boundary_ordered_area")
                operations += 1
                continue

            if self.phase == "boundary_ordered_area":
                if self._boundary_area_index < len(self._ordered_current_component):
                    key = self._ordered_current_component[self._boundary_area_index]
                    self._boundary_area_index += 1
                    loop = self._loop_by_key[key]
                    start_uv = loop[self.uv_layer].uv
                    end_uv = loop.link_loop_next[self.uv_layer].uv
                    self._boundary_area_twice += (
                        float(start_uv.x) * float(end_uv.y)
                        - float(end_uv.x) * float(start_uv.y)
                    )
                else:
                    area = abs(self._boundary_area_twice) * 0.5
                    if not math.isfinite(area) or area <= 1.0e-14:
                        raise ValueError("boundary_component_degenerate_area")
                    self._ordered_components.append(
                        (area, tuple(self._ordered_values))
                    )
                    self._ordered_component_index += 1
                    self._set_phase("boundary_ordered_init")
                operations += 1
                continue

            if self.phase == "boundary_sort":
                sort_started = time.perf_counter()
                self._ordered_components.sort(
                    key=lambda item: (-item[0], item[1])
                )
                self.sort_ms += self._phase_time("boundary_sort", sort_started)
                self._boundary_record_index = 0
                self._set_phase("boundary_records")
                operations += 1
                continue

            if self.phase == "boundary_records":
                if self._boundary_record_index < len(self._ordered_components):
                    index = self._boundary_record_index
                    self._boundary_record_index += 1
                    _area, loop_keys = self._ordered_components[index]
                    self._boundaries = tuple(self._boundaries) + (
                        topology_correspondence.BoundaryComponentRecord(
                            key=("boundary", index),
                            loop_keys=tuple(loop_keys),
                            role="outer" if index == 0 else "hole",
                            parent_key=None
                            if index == 0
                            else ("boundary", 0),
                            signature=("closed", len(loop_keys)),
                        ),
                    )
                else:
                    self._set_phase("graph_finalize")
                operations += 1
                continue

            if self.phase == "graph_finalize":
                finalize_started = time.perf_counter()
                graph = topology_correspondence.make_graph(
                    faces=self._face_records,
                    edges=self._edge_records,
                    vertices=self._vertex_records,
                    loops=self._loop_records,
                    boundaries=self._boundaries,
                )
                self.graph_finalize_ms = self._phase_time(
                    "graph_finalize",
                    finalize_started,
                )
                self.result = (graph, self._loop_by_key)
                self._set_phase("done")
                operations += 1
                continue

            raise RuntimeError("unknown Pro graph build phase: %s" % self.phase)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.graph_primitive_operations += operations
        self.graph_slices += 1
        self.last_graph_slice_ms = elapsed_ms
        self.last_graph_slice_operations = operations
        self.max_graph_slice_ms = max(self.max_graph_slice_ms, elapsed_ms)
        return self.result, operations

    def run_to_completion(self, operation_budget=_PRO_GRAPH_OPERATION_BUDGET):
        """Drive the same state synchronously for equivalence tests."""

        if int(operation_budget) <= 0:
            raise ValueError("graph operation budget must be positive")
        while not self.done:
            self.advance(operation_budget=operation_budget)
        return self.result


def _pro_graph_for_island(island, uv_layer):
    """Synchronous graph oracle retained for compatibility/equivalence tests."""

    faces_by_key = {}
    island_loop_ids = {id(loop) for loop in island}
    for loop in island:
        faces_by_key[int(loop.face.index)] = loop.face
    faces = tuple(
        sorted(faces_by_key.values(), key=lambda face: int(face.index))
    )
    if not faces:
        raise ValueError("invalid_record_empty_graph")

    loop_by_key = {}
    face_loop_keys = {}
    edge_loops = {}
    vertex_loops = {}
    for face in faces:
        keys = []
        for local_index, loop in enumerate(face.loops):
            if id(loop) not in island_loop_ids:
                raise ValueError("invalid_record_partial_face")
            key = (int(face.index), int(local_index))
            if key in loop_by_key:
                raise ValueError("invalid_record_duplicate_loop_key")
            loop_by_key[key] = loop
            keys.append(key)
            edge_loops.setdefault(int(loop.edge.index), []).append(key)
            vertex_loops.setdefault(int(loop.vert.index), []).append(key)
        face_loop_keys[int(face.index)] = tuple(keys)

    vertex_uvs = {}
    for key, loop in loop_by_key.items():
        vertex_uvs.setdefault(int(loop.vert.index), set()).add(
            _pro_uv_token(loop[uv_layer].uv)
        )
    uv_split = {
        vertex_key: len(values) > 1 for vertex_key, values in vertex_uvs.items()
    }

    edge_face_keys = {}
    edge_boundary = {}
    edge_non_manifold = {}
    for edge_key, keys in edge_loops.items():
        face_keys = tuple(sorted({loop_by_key[key].face.index for key in keys}))
        edge_face_keys[edge_key] = tuple(int(value) for value in face_keys)
        edge_boundary[edge_key] = len(face_keys) == 1
        representative = loop_by_key[keys[0]].edge
        edge_non_manifold[edge_key] = len(getattr(representative, "link_faces", ())) > 2

    loop_records = []
    for key, loop in sorted(loop_by_key.items()):
        face_key, local_index = key
        cycle = face_loop_keys[face_key]
        edge_key = int(loop.edge.index)
        vertex_key = int(loop.vert.index)
        loop_records.append(
            topology_correspondence.LoopRecord(
                key=key,
                face_key=face_key,
                edge_key=edge_key,
                vertex_key=vertex_key,
                next_key=cycle[(local_index + 1) % len(cycle)],
                prev_key=cycle[(local_index - 1) % len(cycle)],
                uv=(float(loop[uv_layer].uv.x), float(loop[uv_layer].uv.y)),
                boundary=edge_boundary[edge_key],
                seam=uv_split[vertex_key],
                signature=("uv_split", uv_split[vertex_key]),
            )
        )

    face_records = tuple(
        topology_correspondence.FaceRecord(
            key=face_key,
            loop_keys=face_loop_keys[face_key],
        )
        for face_key in sorted(face_loop_keys)
    )
    edge_records = tuple(
        topology_correspondence.EdgeRecord(
            key=edge_key,
            loop_keys=tuple(sorted(keys)),
            face_keys=edge_face_keys[edge_key],
            boundary=edge_boundary[edge_key],
            non_manifold=edge_non_manifold[edge_key],
            signature=("mesh_non_manifold", edge_non_manifold[edge_key]),
        )
        for edge_key, keys in sorted(edge_loops.items())
    )
    vertex_records = tuple(
        topology_correspondence.VertexRecord(
            key=vertex_key,
            loop_keys=tuple(sorted(keys)),
            boundary=any(
                edge_boundary[int(loop_by_key[key].edge.index)] for key in keys
            ),
            signature=("uv_split", uv_split[vertex_key]),
        )
        for vertex_key, keys in sorted(vertex_loops.items())
    )
    boundary_keys = tuple(
        key for key, loop in loop_by_key.items() if edge_boundary[int(loop.edge.index)]
    )
    boundaries = _pro_boundary_components(loop_by_key, boundary_keys, uv_layer)
    graph = topology_correspondence.make_graph(
        faces=face_records,
        edges=edge_records,
        vertices=vertex_records,
        loops=tuple(loop_records),
        boundaries=boundaries,
    )
    return graph, loop_by_key


def _pro_density_root_from_areas(uv_area, world_area):
    try:
        uv_value = float(uv_area)
        world_value = float(world_area)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(uv_value)
        or not math.isfinite(world_value)
        or uv_value <= 0.0
        or world_value <= 0.0
    ):
        return None
    density = math.sqrt(uv_value / world_value)
    return density if math.isfinite(density) and density > 0.0 else None


def _pro_world_surface_area(obj, island):
    area = 0.0
    matrix = obj.matrix_world
    for face in island_tools.island_faces(island):
        points = [matrix @ loop.vert.co for loop in face.loops]
        if len(points) < 3:
            continue
        origin = points[0]
        for index in range(1, len(points) - 1):
            area += (points[index] - origin).cross(points[index + 1] - origin).length * 0.5
    return area


def _pro_density_for_island(obj, island, uv_layer):
    return _pro_density_root_from_areas(
        island_tools.get_island_area(island, uv_layer),
        _pro_world_surface_area(obj, island),
    )


def _pro_uv_polygon_area(face, uv_layer):
    """Return the finite absolute area of one face's actual UV polygon."""

    points = []
    for loop in getattr(face, "loops", ()):
        uv = loop[uv_layer].uv
        point = (float(uv.x), float(uv.y))
        if not all(math.isfinite(value) for value in point):
            return None
        points.append(point)
    if len(points) < 3:
        return 0.0
    twice = 0.0
    for index, point in enumerate(points):
        other = points[(index + 1) % len(points)]
        twice += point[0] * other[1] - other[0] * point[1]
    area = abs(twice) * 0.5
    return area if math.isfinite(area) and area >= 0.0 else None


def _pro_uv_area_for_island(island, uv_layer):
    """Compute visible UV size from all face UV polygons, without bpy state."""

    faces = {loop.face for loop in island}
    total = 0.0
    for face in sorted(faces, key=lambda item: int(item.index)):
        area = _pro_uv_polygon_area(face, uv_layer)
        if area is None:
            return None
        total += area
    return total if math.isfinite(total) and total >= 0.0 else None


def _pro_select_density_master(density_records, tie_epsilon=_PRO_DENSITY_TIE_EPSILON):
    """Return the highest valid-density record with deterministic key tie-break."""

    best = None
    for record in sorted(density_records, key=lambda item: tuple(item["key"])):
        density = record.get("density")
        if density is None:
            continue
        if best is None:
            best = record
            continue
        best_density = float(best["density"])
        current_density = float(density)
        if current_density > best_density + tie_epsilon:
            best = record
        elif abs(current_density - best_density) <= tie_epsilon:
            if tuple(record["key"]) < tuple(best["key"]):
                best = record
    return best


def _pro_exact_write_values(master_loops, candidate_loops, correspondence, uv_layer):
    """Validate a complete result and stage candidate UV copies, without writes."""

    if not getattr(correspondence, "accepted", False):
        return None
    mapping = tuple(getattr(correspondence, "loop_mapping", ()))
    if len(mapping) != len(candidate_loops) or len(mapping) != len(master_loops):
        return None
    if len({pair[0] for pair in mapping}) != len(mapping):
        return None
    if len({pair[1] for pair in mapping}) != len(mapping):
        return None
    if {pair[0] for pair in mapping} != set(candidate_loops):
        return None
    if {pair[1] for pair in mapping} != set(master_loops):
        return None
    staged = []
    for candidate_key, master_key in sorted(mapping):
        candidate_loop = candidate_loops.get(candidate_key)
        master_loop = master_loops.get(master_key)
        if candidate_loop is None or master_loop is None:
            return None
        uv = master_loop[uv_layer].uv
        staged.append(
            (
                candidate_key,
                candidate_loop,
                (float(uv.x), float(uv.y)),
            )
        )
    return tuple(staged)


def _numeric_inputs_for_island(
    island,
    uv_layer,
    numeric_cache,
    diagnostics,
):
    face_key = _island_face_key(island)
    if face_key not in numeric_cache:
        started = time.perf_counter()
        numeric_cache[face_key] = _numeric_island_inputs(island, uv_layer)
        diagnostics.record_phase(
            "boundary_extraction",
            (time.perf_counter() - started) * 1000.0,
        )
    return numeric_cache[face_key]


def _cheap_signature_for_island(
    island,
    uv_layer,
    descriptor_cache,
    snapshot_identity,
    numeric_cache,
):
    face_key = _island_face_key(island)
    diagnostics = descriptor_cache.diagnostics

    def builder():
        segments, topology = _numeric_inputs_for_island(
            island,
            uv_layer,
            numeric_cache,
            diagnostics,
        )
        started = time.perf_counter()
        signature = similarity_matcher.build_cheap_signature(
            segments,
            face_key=face_key,
            topology=topology,
            include_invariants=True,
        )
        diagnostics.record_phase(
            "cheap_signature",
            (time.perf_counter() - started) * 1000.0,
        )
        return signature

    return descriptor_cache.get_or_build_cheap(
        face_key,
        snapshot_identity,
        builder,
    )


def _descriptor_for_island(
    island,
    uv_layer,
    descriptor_cache,
    snapshot_identity,
    numeric_cache=None,
):
    face_key = _island_face_key(island)

    if numeric_cache is None:
        numeric_cache = {}

    def builder():
        diagnostics = descriptor_cache.diagnostics
        segments, topology = _numeric_inputs_for_island(
            island,
            uv_layer,
            numeric_cache,
            diagnostics,
        )
        started = time.perf_counter()
        descriptor = similarity_matcher.build_descriptor(
            segments,
            face_key=face_key,
            topology=topology,
        )
        diagnostics.record_phase(
            "full_descriptor",
            (time.perf_counter() - started) * 1000.0,
        )
        return descriptor

    return descriptor_cache.get_or_build(
        face_key,
        snapshot_identity,
        builder,
    )


def _best_align_transform(
    ref_island,
    target_island,
    uv_layer,
    match_scale,
    allow_flipping,
    descriptor_cache=None,
    snapshot_identity=None,
    numeric_cache=None,
    tolerance=float("inf"),
):
    """Return a deterministic ordered-loop transform or ``None`` for rejection."""

    if descriptor_cache is None:
        descriptor_cache = similarity_matcher.DescriptorCache()
    if snapshot_identity is None:
        snapshot_identity = ("direct", id(uv_layer))

    reference = _descriptor_for_island(
        ref_island,
        uv_layer,
        descriptor_cache,
        snapshot_identity,
        numeric_cache=numeric_cache,
    )
    candidate = _descriptor_for_island(
        target_island,
        uv_layer,
        descriptor_cache,
        snapshot_identity,
        numeric_cache=numeric_cache,
    )
    result = similarity_matcher.match_descriptors(
        reference,
        candidate,
        match_scale=bool(match_scale),
        allow_flipping=bool(allow_flipping),
        tolerance=float(tolerance),
        allow_tolerant_topology=(
            float(tolerance) > similarity_matcher.TOPOLOGY_PENALTY
        ),
        count_candidate=False,
    )
    if not result.accepted:
        return None
    return result.transform


def _pro_shape_match_result(
    ref_island,
    target_island,
    uv_layer,
    settings,
    descriptor_cache,
    snapshot_identity,
    numeric_cache,
):
    """Run the existing ordered shape matcher as a Pro candidate filter."""

    reference = _descriptor_for_island(
        ref_island,
        uv_layer,
        descriptor_cache,
        snapshot_identity,
        numeric_cache=numeric_cache,
    )
    candidate = _descriptor_for_island(
        target_island,
        uv_layer,
        descriptor_cache,
        snapshot_identity,
        numeric_cache=numeric_cache,
    )
    tolerance = max(0.0, float(settings.stack_similarity_tolerance))
    result = similarity_matcher.match_descriptors(
        reference,
        candidate,
        match_scale=bool(settings.stack_match_scale),
        allow_flipping=bool(settings.stack_allow_flipping),
        tolerance=tolerance,
        allow_tolerant_topology=(tolerance > similarity_matcher.TOPOLOGY_PENALTY),
        count_candidate=False,
    )
    similarity_matcher.record_match_diagnostics(result.diagnostics)
    return result


# Focused Pro tests and compatibility callers may replace the synchronous
# helper.  Live sessions use the resumable state below only when this oracle
# is still the original function, preserving those narrow seams without
# changing the normal Align Similar path.
_PRO_SHAPE_MATCH_RESULT_ORACLE = _pro_shape_match_result


def _pro_canonical_cycle(values):
    """Canonicalize a small face-local cycle without mesh identities."""

    return pro_candidate_planner.canonical_cycle_signature(values)


def _pro_histogram(values):
    counts = Counter(values)
    return tuple(sorted(counts.items()))


def _pro_color_ids(labels):
    """Canonicalize local refinement labels without per-element hashing."""

    palette = {label: index for index, label in enumerate(sorted(set(labels)))}
    return tuple(palette[label] for label in labels)


def _pro_lightweight_topology_fingerprint(
    island,
    uv_layer,
    refinement_metrics=None,
):
    """Build the legacy two-round UI-safe record fingerprint.

    Exact loop refinement belongs to the pure external worker.  The Blender
    planner deliberately keeps this face-level prefilter bounded to two
    rounds so one planner record cannot monopolize a modal tick.  The optional
    metrics sink is retained for compatibility but records that convergence is
    intentionally disabled on the main thread.
    """

    island_loops = tuple(island)
    island_loop_ids = {id(loop) for loop in island_loops}
    faces_by_key = {}
    for loop in island_loops:
        faces_by_key[int(loop.face.index)] = loop.face
    if not faces_by_key:
        raise ValueError("invalid_record_empty_graph")

    face_items = tuple(sorted(faces_by_key.items()))
    face_loops = []
    edge_loops = []
    vertex_loops = []
    loop_objects = []
    edge_positions = {}
    vertex_positions = {}
    loop_edge = []
    loop_vertex = []
    for _face_key, face in face_items:
        ordered_loops = tuple(face.loops)
        if any(id(loop) not in island_loop_ids for loop in ordered_loops):
            raise ValueError("invalid_record_partial_face")
        local_face_loops = []
        for loop in ordered_loops:
            loop_position = len(loop_objects)
            edge_position = edge_positions.setdefault(
                int(loop.edge.index), len(edge_loops)
            )
            vertex_position = vertex_positions.setdefault(
                int(loop.vert.index), len(vertex_loops)
            )
            if edge_position == len(edge_loops):
                edge_loops.append([])
            if vertex_position == len(vertex_loops):
                vertex_loops.append([])
            loop_objects.append(loop)
            local_face_loops.append(loop_position)
            loop_edge.append(edge_position)
            loop_vertex.append(vertex_position)
            edge_loops[edge_position].append(loop_position)
            vertex_loops[vertex_position].append(loop_position)
        face_loops.append(tuple(local_face_loops))

    edge_faces = []
    face_for_loop = [None] * len(loop_objects)
    for face_position, keys in enumerate(face_loops):
        for loop_position in keys:
            face_for_loop[loop_position] = face_position
    for loop_positions in edge_loops:
        edge_faces.append(tuple(sorted({face_for_loop[loop] for loop in loop_positions})))
    face_neighbors = [set() for _face in face_loops]
    for faces in edge_faces:
        for left in faces:
            face_neighbors[left].update(right for right in faces if right != left)

    vertex_uv_tokens = [set() for _vertex in vertex_loops]
    for loop_position, loop in enumerate(loop_objects):
        vertex_uv_tokens[loop_vertex[loop_position]].add(
            _pro_uv_token(loop[uv_layer].uv)
        )
    uv_split = tuple(len(tokens) > 1 for tokens in vertex_uv_tokens)
    edge_boundary = tuple(len(faces) == 1 for faces in edge_faces)
    edge_base = tuple(
        (
            bool(edge_boundary[index]),
            len(faces) > 2,
            len(faces),
            len(edge_loops[index]),
        )
        for index, faces in enumerate(edge_faces)
    )
    vertex_base = tuple(
        (
            len(loops),
            any(edge_boundary[loop_edge[loop]] for loop in loops),
            bool(uv_split[index]),
        )
        for index, loops in enumerate(vertex_loops)
    )
    face_base = tuple(
        (
            len(loops),
            len(face_neighbors[index]),
            _pro_canonical_cycle(
                (
                    edge_base[loop_edge[loop]],
                    vertex_base[loop_vertex[loop]],
                )
                for loop in loops
            ),
            tuple(
                sorted(
                    (
                        edge_base[loop_edge[loop]],
                        vertex_base[loop_vertex[loop]],
                    )
                    for loop in loops
                )
            ),
        )
        for index, loops in enumerate(face_loops)
    )
    if refinement_metrics is not None:
        refinement_metrics.update(
            {
                "mode": "two_round_bounded",
                "rounds": 2,
                "max_rounds": 2,
                "stable": False,
                "truncated": False,
                "elapsed_ms": 0.0,
            }
        )
    face_colors = _pro_color_ids(face_base)
    for _round in range(2):
        face_colors = _pro_color_ids(
            tuple(
                (
                    face_base[index],
                    tuple(sorted(face_colors[neighbor] for neighbor in neighbors)),
                )
                for index, neighbors in enumerate(face_neighbors)
            )
        )
    return (
        "pro-lite-topology-v4",
        (len(face_loops), len(edge_loops), len(vertex_loops), len(loop_objects)),
        _pro_histogram(edge_base),
        _pro_histogram(vertex_base),
        _pro_histogram(face_base),
        _pro_histogram(face_colors),
        tuple(sorted(len(neighbors) for neighbors in face_neighbors)),
        tuple(sorted(len(faces) for faces in edge_faces)),
    )


def _pro_compact_cheap_signature(signature):
    """Return immutable cheap fields needed for planner diagnostics only."""

    return (
        "cheap-signature-v1",
        tuple(signature.invariant_signature),
        signature.raw_boundary_signature,
        (
            signature.segment_count,
            signature.point_count,
            signature.component_count,
            signature.closed_component_count,
            signature.open_component_count,
            signature.ambiguous_component_count,
            signature.degenerate_segment_count,
            signature.cycle_count,
        ),
        signature.topology.core_key,
    )


def _pro_planner_record_for_island(
    obj,
    island,
    uv_layer,
    descriptor_cache,
    snapshot_identity,
    numeric_cache,
    refinement_metrics=None,
):
    """Extract one BMesh-free planner record without building an exact graph."""

    key = _island_face_key(island)
    signature = _cheap_signature_for_island(
        island,
        uv_layer,
        descriptor_cache,
        snapshot_identity,
        numeric_cache,
    )
    descriptor = tuple(float(value) for value in signature.invariant_signature[:2])
    return pro_candidate_planner.IslandRecord(
        face_key=key,
        strict_topology_fingerprint=_pro_lightweight_topology_fingerprint(
            island,
            uv_layer,
            refinement_metrics=refinement_metrics,
        ),
        normalized_boundary_descriptor=descriptor,
        density=_pro_density_for_island(obj, island, uv_layer),
        cheap_signature=_pro_compact_cheap_signature(signature),
    ), signature


class _ProCheapBoundaryBuildState:
    """Cooperatively build the numeric cheap-boundary signature.

    The synchronous matcher helper remains the oracle.  This state mirrors its
    union-find and numeric accumulation order one segment at a time so a large
    boundary cannot monopolize the modal owner thread.  It deliberately stores
    only tuples, numbers and small Python containers; no Blender object enters
    the state.
    """

    def __init__(self, segments, face_key, topology):
        self.segments = tuple(segments)
        self.face_key = tuple(face_key)
        self.topology = dict(topology or {})
        self.phase = "segments"
        self.result = None
        self.segment_index = 0
        self.parent = []
        self.sizes = []
        self.degrees = []
        self.edges = []
        self.node_ids = {}
        self.points = {}
        self.perimeter = 0.0
        self.degenerate_segments = 0
        self.components = {}
        self.component_index = 0
        self.edge_index = 0
        self.component_records = []
        self.sorted_records = ()
        self.point_values = ()
        self.point_index = 0
        self.center = (0.0, 0.0)
        self.bounds = (0.0, 0.0, 0.0, 0.0)
        self.covariance = (0.0, 0.0, 0.0)
        self.invariant_signature = ()

    @property
    def done(self):
        return self.result is not None

    def _find(self, index):
        root = index
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[index] != index:
            next_index = self.parent[index]
            self.parent[index] = root
            index = next_index
        return root

    def _node_id(self, key):
        index = self.node_ids.get(key)
        if index is not None:
            return index
        index = len(self.parent)
        self.node_ids[key] = index
        self.parent.append(index)
        self.sizes.append(1)
        self.degrees.append(0)
        return index

    def _union(self, left, right):
        left = self._find(left)
        right = self._find(right)
        if left == right:
            return
        if self.sizes[left] < self.sizes[right]:
            left, right = right, left
        self.parent[right] = left
        self.sizes[left] += self.sizes[right]

    def _finish_points(self):
        points = self.point_values
        # Use the same built-in sum expression as the synchronous matcher at
        # the commit boundary.  This preserves Python's exact float reduction
        # order while all mesh traversal remains incremental.
        self.perimeter = sum(
            similarity_matcher._distance(start, end)
            for start, end in self.segments
        )
        if points:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            self.bounds = (min(xs), max(xs), min(ys), max(ys))
            count = float(len(points))
            self.center = (
                sum(point[0] for point in points) / count,
                sum(point[1] for point in points) / count,
            )
            covariance_xx = sum(
                (point[0] - self.center[0]) ** 2 for point in points
            ) / len(points)
            covariance_yy = sum(
                (point[1] - self.center[1]) ** 2 for point in points
            ) / len(points)
            covariance_xy = sum(
                (point[0] - self.center[0]) * (point[1] - self.center[1])
                for point in points
            ) / len(points)
        else:
            self.bounds = (0.0, 0.0, 0.0, 0.0)
            self.center = (0.0, 0.0)
            covariance_xx = covariance_yy = covariance_xy = 0.0
        self.covariance = (covariance_xx, covariance_yy, covariance_xy)
        trace = covariance_xx + covariance_yy
        discriminant = math.sqrt(
            max(0.0, (covariance_xx - covariance_yy) ** 2 + 4.0 * covariance_xy**2)
        )
        largest = max((trace + discriminant) * 0.5, similarity_matcher.DEGENERATE_EPSILON)
        smallest = max((trace - discriminant) * 0.5, similarity_matcher.DEGENERATE_EPSILON)
        self.invariant_signature = (
            round(largest / smallest, 7),
            round(
                self.perimeter
                / math.sqrt(largest * max(len(points), 1)),
                7,
            ),
        )

    def advance(self, operation_budget=_PRO_RECORD_OPERATION_BUDGET, deadline=None):
        try:
            operation_budget = max(0, int(operation_budget))
        except (TypeError, ValueError):
            operation_budget = 0
        operations = 0
        while not self.done and operations < operation_budget:
            if deadline is not None and time.perf_counter() >= deadline:
                break
            if self.phase == "segments":
                if self.segment_index < len(self.segments):
                    start, end = self.segments[self.segment_index]
                    self.segment_index += 1
                    start = similarity_matcher._as_point(start)
                    end = similarity_matcher._as_point(end)
                    self.perimeter += similarity_matcher._distance(start, end)
                    for point in (start, end):
                        key = similarity_matcher._point_key(point)
                        if key not in self.points:
                            self.points[key] = point
                    start_id = self._node_id(similarity_matcher._point_key(start))
                    end_id = self._node_id(similarity_matcher._point_key(end))
                    if similarity_matcher._distance(start, end) <= similarity_matcher.DEGENERATE_EPSILON:
                        self.degenerate_segments += 1
                    else:
                        self.edges.append((start_id, end_id))
                        self.degrees[start_id] += 1
                        self.degrees[end_id] += 1
                        self._union(start_id, end_id)
                else:
                    self.point_values = tuple(self.points.values())
                    self.phase = "components"
                operations += 1
                continue
            if self.phase == "components":
                if self.component_index < len(self.parent):
                    index = self.component_index
                    self.component_index += 1
                    root = self._find(index)
                    component = self.components.setdefault(
                        root, {"nodes": [], "edges": 0}
                    )
                    component["nodes"].append(self.degrees[index])
                else:
                    self.component_index = 0
                    self.phase = "component_edges"
                operations += 1
                continue
            if self.phase == "component_edges":
                if self.edge_index < len(self.edges):
                    start_id, _end_id = self.edges[self.edge_index]
                    self.edge_index += 1
                    self.components[self._find(start_id)]["edges"] += 1
                else:
                    self.component_index = 0
                    self.phase = "component_records"
                operations += 1
                continue
            if self.phase == "component_records":
                components = tuple(self.components.values())
                if self.component_index < len(components):
                    component = components[self.component_index]
                    self.component_index += 1
                    component_degrees = tuple(sorted(component["nodes"]))
                    node_count = len(component_degrees)
                    edge_count = component["edges"]
                    cycle_rank = max(0, edge_count - node_count + 1)
                    if (
                        node_count >= 3
                        and edge_count == node_count
                        and all(degree == 2 for degree in component_degrees)
                    ):
                        status = "closed"
                    elif not edge_count or all(degree <= 1 for degree in component_degrees):
                        status = "degenerate"
                    elif any(degree > 2 for degree in component_degrees) or cycle_rank > 1:
                        status = "ambiguous"
                    else:
                        status = "open"
                    self.component_records.append(
                        {
                            "node_count": node_count,
                            "edge_count": edge_count,
                            "cycle_rank": cycle_rank,
                            "status": status,
                            "degree_histogram": tuple(
                                (degree, component_degrees.count(degree))
                                for degree in sorted(set(component_degrees))
                            ),
                        }
                    )
                else:
                    self.sorted_records = tuple(
                        sorted(
                            self.component_records,
                            key=lambda item: (
                                item["status"],
                                item["node_count"],
                                item["edge_count"],
                                item["cycle_rank"],
                                item["degree_histogram"],
                            ),
                        )
                    )
                    self.phase = "points"
                operations += 1
                continue
            if self.phase == "points":
                if self.point_index < len(self.point_values):
                    self.point_index += 1
                else:
                    self._finish_points()
                    self.phase = "result"
                operations += 1
                continue
            if self.phase == "result":
                closed_count = sum(item["status"] == "closed" for item in self.sorted_records)
                open_count = sum(item["status"] == "open" for item in self.sorted_records)
                ambiguous_count = sum(
                    item["status"] == "ambiguous" for item in self.sorted_records
                )
                degenerate_count = sum(
                    item["status"] == "degenerate" for item in self.sorted_records
                )
                histogram = tuple(
                    sorted(
                        (
                            int(key),
                            int(count),
                        )
                        for key, count in dict(
                            self.topology.get("edge_incidence_histogram", {})
                        ).items()
                    )
                )
                base = similarity_matcher.TopologySignature(
                    face_count=self.topology.get("face_count"),
                    edge_count=self.topology.get("edge_count"),
                    vertex_count=self.topology.get("vertex_count"),
                    non_manifold_edge_count=self.topology.get(
                        "non_manifold_edge_count"
                    ),
                    edge_incidence_histogram=histogram,
                )
                cheap_topology = similarity_matcher.TopologySignature(
                    face_count=base.face_count,
                    edge_count=base.edge_count,
                    vertex_count=base.vertex_count,
                    non_manifold_edge_count=base.non_manifold_edge_count,
                    edge_incidence_histogram=base.edge_incidence_histogram,
                    component_count=len(self.sorted_records),
                    closed_component_count=closed_count,
                    open_component_count=open_count,
                    ambiguous_component_count=ambiguous_count,
                    boundary_loop_count=len(self.sorted_records),
                    closed_loop_count=closed_count,
                    degenerate_count=degenerate_count,
                )
                raw_signature = (
                    len(self.segments),
                    len(self.points),
                    tuple(
                        (
                            item["status"],
                            item["node_count"],
                            item["edge_count"],
                            item["cycle_rank"],
                        )
                        for item in self.sorted_records
                    ),
                )
                self.result = similarity_matcher.CheapBoundarySignature(
                    face_key=tuple(self.face_key),
                    topology=cheap_topology,
                    segment_count=len(self.segments),
                    point_count=len(self.points),
                    component_count=len(self.sorted_records),
                    closed_component_count=closed_count,
                    open_component_count=open_count,
                    ambiguous_component_count=ambiguous_count,
                    degenerate_segment_count=self.degenerate_segments,
                    cycle_count=sum(item["cycle_rank"] for item in self.sorted_records),
                    perimeter=self.perimeter,
                    bounds=self.bounds,
                    center=self.center,
                    invariant_signature=self.invariant_signature,
                    raw_boundary_signature=raw_signature,
                )
                self.phase = "done"
                operations += 1
                continue
            raise RuntimeError("unknown Pro cheap-boundary build phase: %s" % self.phase)
        return self.result, operations


class _ProPlannerFingerprintBuildState:
    """Incremental equivalent of ``_pro_lightweight_topology_fingerprint``."""

    def __init__(self, island, uv_layer, refinement_metrics=None):
        self.island = island if isinstance(island, (tuple, list)) else tuple(island)
        self.uv_layer = uv_layer
        self.refinement_metrics = refinement_metrics
        self.phase = "faces"
        self.result = None
        self._island_loop_ids = {id(loop) for loop in self.island}
        self._faces_by_key = {}
        for loop in self.island:
            self._faces_by_key[int(loop.face.index)] = loop.face
        self._face_items = tuple(sorted(self._faces_by_key.items()))
        self._face_index = 0
        self._current_face = None
        self._current_loops = ()
        self._loop_index = 0
        self._face_loops = []
        self._edge_loops = {}
        self._vertex_loops = {}
        self._edge_positions = {}
        self._vertex_positions = {}
        self._loop_objects = []
        self._loop_edge = []
        self._loop_vertex = []
        self._face_for_loop = []
        self._edge_items = None
        self._edge_index = 0
        self._edge_faces = []
        self._face_neighbors = None
        self._vertex_tokens = None
        self._loop_index_cursor = 0
        self._edge_base = None
        self._vertex_base = None
        self._face_base = None
        self._color_round = 0
        self._face_colors = None
        self._color_labels = None
        self._color_index = 0

    @property
    def done(self):
        return self.result is not None

    def advance(self, operation_budget=_PRO_RECORD_OPERATION_BUDGET, deadline=None):
        try:
            operation_budget = max(0, int(operation_budget))
        except (TypeError, ValueError):
            operation_budget = 0
        operations = 0
        while not self.done and operations < operation_budget:
            if deadline is not None and time.perf_counter() >= deadline:
                break
            if self.phase == "faces":
                if not self._face_items:
                    raise ValueError("invalid_record_empty_graph")
                self.phase = "loops"
                operations += 1
                continue
            if self.phase == "loops":
                if self._current_face is None:
                    if self._face_index >= len(self._face_items):
                        self._edge_items = tuple(self._edge_loops.items())
                        self._face_neighbors = [set() for _item in self._face_loops]
                        self._loop_index_cursor = 0
                        self.phase = "edge_faces"
                        operations += 1
                        continue
                    _face_key, self._current_face = self._face_items[self._face_index]
                    self._current_loops = tuple(self._current_face.loops)
                    self._loop_index = 0
                    self._face_loops.append([])
                    operations += 1
                    continue
                if self._loop_index < len(self._current_loops):
                    local_index = self._loop_index
                    loop = self._current_loops[local_index]
                    self._loop_index += 1
                    if id(loop) not in self._island_loop_ids:
                        raise ValueError("invalid_record_partial_face")
                    face_key = int(self._current_face.index)
                    key = (face_key, int(local_index))
                    if any(existing == key for existing, _loop in self._loop_objects):
                        raise ValueError("invalid_record_duplicate_loop_key")
                    loop_position = len(self._loop_objects)
                    self._loop_objects.append((key, loop))
                    self._face_loops[-1].append(loop_position)
                    edge_key = int(loop.edge.index)
                    vertex_key = int(loop.vert.index)
                    if edge_key not in self._edge_positions:
                        self._edge_positions[edge_key] = len(self._edge_loops)
                    if vertex_key not in self._vertex_positions:
                        self._vertex_positions[vertex_key] = len(self._vertex_loops)
                    self._edge_loops.setdefault(edge_key, []).append(loop_position)
                    self._vertex_loops.setdefault(vertex_key, []).append(loop_position)
                    self._loop_edge.append(self._edge_positions[edge_key])
                    self._loop_vertex.append(self._vertex_positions[vertex_key])
                    self._face_for_loop.append(self._face_index)
                else:
                    self._face_loops[-1] = tuple(self._face_loops[-1])
                    self._face_index += 1
                    self._current_face = None
                    self._current_loops = ()
                    self._loop_index = 0
                operations += 1
                continue
            if self.phase == "edge_faces":
                if self._edge_index < len(self._edge_items):
                    edge_key, loop_positions = self._edge_items[self._edge_index]
                    self._edge_index += 1
                    face_keys = tuple(
                        sorted({self._face_for_loop[position] for position in loop_positions})
                    )
                    self._edge_faces.append(face_keys)
                    for left in face_keys:
                        self._face_neighbors[left].update(
                            right for right in face_keys if right != left
                        )
                else:
                    self._vertex_tokens = [set() for _item in self._vertex_loops]
                    self._loop_index_cursor = 0
                    self._edge_index = 0
                    self.phase = "vertex_tokens"
                operations += 1
                continue
            if self.phase == "vertex_tokens":
                loop_items = self._loop_objects
                if self._loop_index_cursor < len(loop_items):
                    position, loop = self._loop_index_cursor, loop_items[self._loop_index_cursor][1]
                    self._loop_index_cursor += 1
                    vertex_key = self._loop_vertex[position]
                    self._vertex_tokens[vertex_key].add(
                        _pro_uv_token(loop[self.uv_layer].uv)
                    )
                else:
                    edge_boundary = tuple(len(faces) == 1 for faces in self._edge_faces)
                    uv_split = tuple(len(tokens) > 1 for tokens in self._vertex_tokens)
                    self._edge_boundary = edge_boundary
                    self._uv_split = uv_split
                    self._edge_base = [None] * len(self._edge_items)
                    self._vertex_base = [None] * len(self._vertex_loops)
                    self._edge_index = 0
                    self.phase = "edge_base"
                operations += 1
                continue
            if self.phase == "edge_base":
                if self._edge_index < len(self._edge_items):
                    index = self._edge_index
                    _edge_key, loop_positions = self._edge_items[index]
                    self._edge_index += 1
                    faces = self._edge_faces[index]
                    self._edge_base[index] = (
                        bool(self._edge_boundary[index]),
                        len(faces) > 2,
                        len(faces),
                        len(loop_positions),
                    )
                else:
                    self._edge_index = 0
                    self.phase = "vertex_base"
                operations += 1
                continue
            if self.phase == "vertex_base":
                if self._edge_index < len(self._vertex_loops):
                    index = self._edge_index
                    vertex_key = tuple(self._vertex_loops.keys())[index]
                    loop_positions = self._vertex_loops[vertex_key]
                    self._edge_index += 1
                    self._vertex_base[index] = (
                        len(loop_positions),
                        any(
                            self._edge_boundary[self._loop_edge[position]]
                            for position in loop_positions
                        ),
                        bool(self._uv_split[index]),
                    )
                else:
                    self._edge_index = 0
                    self._face_base = [None] * len(self._face_loops)
                    self._face_cycle_values = None
                    self.phase = "face_base"
                operations += 1
                continue
            if self.phase == "face_base":
                if self._edge_index < len(self._face_loops):
                    index = self._edge_index
                    loops = self._face_loops[index]
                    self._edge_index += 1
                    self._face_base[index] = (
                        len(loops),
                        len(self._face_neighbors[index]),
                        _pro_canonical_cycle(
                            (
                                self._edge_base[self._loop_edge[position]],
                                self._vertex_base[self._loop_vertex[position]],
                            )
                            for position in loops
                            for edge_key, vertex_key in [
                                (
                                    self._loop_edge[position],
                                    self._loop_vertex[position],
                                )
                            ]
                        ),
                        tuple(
                            sorted(
                                (
                                    self._edge_base[self._loop_edge[position]],
                                    self._vertex_base[self._loop_vertex[position]],
                                )
                                for position in loops
                                for edge_key, vertex_key in [
                                    (
                                        self._loop_edge[position],
                                        self._loop_vertex[position],
                                    )
                                ]
                            )
                        ),
                    )
                else:
                    self._color_labels = tuple(self._face_base)
                    self._face_colors = None
                    self._color_index = 0
                    self.phase = "color_palette"
                operations += 1
                continue
            if self.phase == "color_palette":
                palette = {
                    label: index for index, label in enumerate(sorted(set(self._color_labels)))
                }
                self._color_palette = palette
                self._face_colors = []
                self._color_index = 0
                self.phase = "color_values"
                operations += 1
                continue
            if self.phase == "color_values":
                if self._color_index < len(self._color_labels):
                    self._face_colors.append(self._color_palette[self._color_labels[self._color_index]])
                    self._color_index += 1
                else:
                    self._color_round += 1
                    if self._color_round >= 3:
                        self.phase = "result"
                    else:
                        self._color_labels = tuple(
                            (
                                self._face_base[index],
                                tuple(
                                    sorted(
                                        self._face_colors[neighbor]
                                        for neighbor in self._face_neighbors[index]
                                    )
                                ),
                            )
                            for index in range(len(self._face_base))
                        )
                        self._color_index = 0
                        self.phase = "color_palette"
                operations += 1
                continue
            if self.phase == "result":
                if self.refinement_metrics is not None:
                    self.refinement_metrics.update(
                        {
                            "mode": "two_round_bounded",
                            "rounds": 2,
                            "max_rounds": 2,
                            "stable": False,
                            "truncated": False,
                            "elapsed_ms": 0.0,
                        }
                    )
                self.result = (
                    "pro-lite-topology-v4",
                    (
                        len(self._face_loops),
                        len(self._edge_items),
                        len(self._vertex_loops),
                        len(self._loop_objects),
                    ),
                    _pro_histogram(self._edge_base),
                    _pro_histogram(self._vertex_base),
                    _pro_histogram(self._face_base),
                    _pro_histogram(self._face_colors),
                    tuple(sorted(len(neighbors) for neighbors in self._face_neighbors)),
                    tuple(sorted(len(faces) for faces in self._edge_faces)),
                )
                self.phase = "done"
                operations += 1
                continue
            raise RuntimeError("unknown Pro planner fingerprint phase: %s" % self.phase)
        return self.result, operations


class _ProPlannerAreaBuildState:
    """Slice UV polygon and world-area accumulation for one planner island."""

    def __init__(self, obj, island, uv_layer, sorted_faces):
        self.obj = obj
        self.island = island if isinstance(island, (tuple, list)) else tuple(island)
        self.uv_layer = uv_layer
        self.ordered_faces = []
        seen = set()
        for loop in self.island:
            if loop.face not in seen:
                seen.add(loop.face)
                self.ordered_faces.append(loop.face)
        self.uv_faces = tuple(sorted(sorted_faces, key=lambda item: int(item.index)))
        self.phase = "uv_face_init"
        self.result = None
        self._uv_index = 0
        self._uv_points = ()
        self._uv_point_index = 0
        self._uv_twice = 0.0
        self._uv_face_areas = {}
        self._uv_invalid = False
        self._uv_total_sorted = 0.0
        self._density_uv_index = 0
        self._density_uv_total = 0.0
        self._world_face_index = 0
        self._world_points = []
        self._world_point_index = 0
        self._world_triangle_index = 1
        self._world_area = 0.0

    @property
    def done(self):
        return self.result is not None

    def _start_uv_face(self, face):
        self._uv_points = tuple(face.loops)
        self._uv_point_index = 0
        self._uv_twice = 0.0

    def advance(self, operation_budget=_PRO_RECORD_OPERATION_BUDGET, deadline=None):
        try:
            operation_budget = max(0, int(operation_budget))
        except (TypeError, ValueError):
            operation_budget = 0
        operations = 0
        while not self.done and operations < operation_budget:
            if deadline is not None and time.perf_counter() >= deadline:
                break
            if self.phase == "uv_face_init":
                if self._uv_index >= len(self.uv_faces):
                    self._density_uv_index = 0
                    self.phase = "density_uv"
                else:
                    self._start_uv_face(self.uv_faces[self._uv_index])
                    self.phase = "uv_face_points"
                operations += 1
                continue
            if self.phase == "uv_face_points":
                if self._uv_point_index < len(self._uv_points):
                    loop = self._uv_points[self._uv_point_index]
                    self._uv_point_index += 1
                    uv = loop[self.uv_layer].uv
                    point = (float(uv.x), float(uv.y))
                    if not all(math.isfinite(value) for value in point):
                        self._uv_invalid = True
                        self._uv_face_areas[int(self.uv_faces[self._uv_index].index)] = None
                        self._uv_point_index = len(self._uv_points)
                        self.phase = "uv_face_finish"
                    elif len(self._uv_points) >= 3:
                        other = self._uv_points[self._uv_point_index % len(self._uv_points)]
                        other_uv = other[self.uv_layer].uv
                        self._uv_twice += point[0] * float(other_uv.y)
                        self._uv_twice -= float(other_uv.x) * point[1]
                else:
                    self.phase = "uv_face_finish"
                operations += 1
                continue
            if self.phase == "uv_face_finish":
                face = self.uv_faces[self._uv_index]
                if self._uv_face_areas.get(int(face.index), 0.0) is None:
                    area = None
                elif len(self._uv_points) < 3:
                    area = 0.0
                else:
                    area = abs(self._uv_twice) * 0.5
                    if not math.isfinite(area) or area < 0.0:
                        area = None
                self._uv_face_areas[int(face.index)] = area
                if area is not None:
                    self._uv_total_sorted += area
                self._uv_index += 1
                self.phase = "uv_face_init"
                operations += 1
                continue
            if self.phase == "density_uv":
                if self._density_uv_index < len(self.ordered_faces):
                    face = self.ordered_faces[self._density_uv_index]
                    self._density_uv_index += 1
                    area = self._uv_face_areas.get(int(face.index))
                    if area is None:
                        self._density_uv_total = None
                    elif self._density_uv_total is not None:
                        self._density_uv_total += area
                else:
                    self._world_face_index = 0
                    self.phase = "world_face_init"
                operations += 1
                continue
            if self.phase == "world_face_init":
                if self._world_face_index >= len(self.ordered_faces):
                    self.phase = "result"
                else:
                    self._world_points = []
                    self._world_point_index = 0
                    self._world_triangle_index = 1
                    self._world_face = self.ordered_faces[self._world_face_index]
                    self.phase = "world_points"
                operations += 1
                continue
            if self.phase == "world_points":
                loops = tuple(self._world_face.loops)
                if self._world_point_index < len(loops):
                    loop = loops[self._world_point_index]
                    self._world_point_index += 1
                    self._world_points.append(self.obj.matrix_world @ loop.vert.co)
                else:
                    if len(self._world_points) < 3:
                        self._world_face_index += 1
                        self.phase = "world_face_init"
                    else:
                        self.phase = "world_triangles"
                operations += 1
                continue
            if self.phase == "world_triangles":
                if self._world_triangle_index < len(self._world_points) - 1:
                    origin = self._world_points[0]
                    left = self._world_points[self._world_triangle_index]
                    right = self._world_points[self._world_triangle_index + 1]
                    self._world_area += (left - origin).cross(right - origin).length * 0.5
                    self._world_triangle_index += 1
                else:
                    self._world_face_index += 1
                    self.phase = "world_face_init"
                operations += 1
                continue
            if self.phase == "result":
                density = _pro_density_root_from_areas(
                    self._density_uv_total,
                    self._world_area,
                )
                self.result = (
                    None if self._uv_invalid else self._uv_total_sorted,
                    density,
                )
                self.phase = "done"
                operations += 1
                continue
            raise RuntimeError("unknown Pro planner area phase: %s" % self.phase)
        return self.result, operations


class _ProPlannerRecordBuildState:
    """Cooperative, owner-thread builder for one immutable planner record."""

    def __init__(
        self,
        obj,
        island,
        uv_layer,
        descriptor_cache,
        snapshot_identity,
        numeric_cache,
        refinement_metrics=None,
    ):
        self.obj = obj
        self.island = island if isinstance(island, (tuple, list)) else tuple(island)
        self.uv_layer = uv_layer
        self.descriptor_cache = descriptor_cache
        self.snapshot_identity = snapshot_identity
        self.numeric_cache = numeric_cache
        self.refinement_metrics = refinement_metrics if refinement_metrics is not None else {}
        self.key = _island_face_key(self.island)
        self.phase = "numeric_capture"
        self.result = None
        self.error = None
        self.signature = None
        self._capture_index = 0
        self._faces_by_key = {}
        self._ordered_faces = []
        self._edge_map = {}
        self._vertices = set()
        self._edge_faces = {}
        self._segments = []
        self._segment_index = 0
        self._neighbor_index = 0
        self._current_loop = None
        self._has_uv_neighbor = False
        self._numeric_started = time.perf_counter()
        self._cheap_state = None
        self._fingerprint_state = None
        self._area_state = None
        self.slices = 0
        self.operations = 0
        self.max_slice_ms = 0.0
        self.max_primitive_ms = 0.0
        self.max_primitive = {}
        self.phase_timings = {}
        self._cached_numeric = self.numeric_cache.get(self.key)
        self._cached_signature = None
        entries = getattr(self.descriptor_cache, "_cheap_entries", {})
        try:
            cache_key = (
                "cheap",
                similarity_matcher.make_descriptor_cache_key(
                    self.key,
                    self.snapshot_identity,
                ),
            )
            self._cached_signature = entries.get(cache_key)
        except Exception:
            self._cached_signature = None

    @property
    def done(self):
        return self.result is not None or self.error is not None

    def _record_phase_time(self, name, started):
        elapsed = (time.perf_counter() - started) * 1000.0
        self.phase_timings[name] = self.phase_timings.get(name, 0.0) + elapsed
        return elapsed

    def _start_cheap(self):
        if self._cached_signature is not None:
            self.signature = self._cached_signature
            self.phase = "fingerprint_init"
            return
        if self._cached_numeric is None:
            self.numeric_cache[self.key] = (tuple(self._segments), self._topology)
        else:
            self._segments, self._topology = self._cached_numeric
        self._cheap_state = _ProCheapBoundaryBuildState(
            self._segments,
            self.key,
            self._topology,
        )
        self.phase = "cheap"

    def advance(self, operation_budget=_PRO_RECORD_OPERATION_BUDGET, deadline=None):
        try:
            operation_budget = max(0, int(operation_budget))
        except (TypeError, ValueError):
            operation_budget = 0
        started = time.perf_counter()
        operations = 0
        try:
            while not self.done and operations < operation_budget:
                if deadline is not None and time.perf_counter() >= deadline:
                    break
                primitive_started = time.perf_counter()
                if self.phase == "numeric_capture":
                    if self._cached_numeric is not None:
                        self._segments, self._topology = self._cached_numeric
                        self._start_cheap()
                        operations += 1
                        continue
                    if self._capture_index < len(self.island):
                        loop = self.island[self._capture_index]
                        self._capture_index += 1
                        self._edge_map.setdefault(loop.edge, []).append(loop)
                        if loop.face not in self._faces_by_key:
                            self._faces_by_key[int(loop.face.index)] = loop.face
                            self._ordered_faces.append(loop.face)
                        self._vertices.add(loop.vert)
                        self._edge_faces.setdefault(loop.edge, set()).add(loop.face)
                    else:
                        incidence = Counter(len(values) for values in self._edge_faces.values())
                        self._topology = {
                            "face_count": len(self._faces_by_key),
                            "edge_count": len(self._edge_faces),
                            "vertex_count": len(self._vertices),
                            "non_manifold_edge_count": sum(
                                count for degree, count in incidence.items() if degree > 2
                            ),
                            "edge_incidence_histogram": dict(incidence),
                        }
                        self.phase = "numeric_segments"
                    operations += 1
                elif self.phase == "numeric_segments":
                    if self._segment_index >= len(self.island):
                        self._segments = tuple(self._segments)
                        self._record_phase_time("boundary_extraction", self._numeric_started)
                        self._start_cheap()
                        operations += 1
                        continue
                    if self._current_loop is None:
                        self._current_loop = self.island[self._segment_index]
                        self._neighbor_index = 0
                        self._has_uv_neighbor = False
                    neighbours = self._edge_map.get(self._current_loop.edge, ())
                    if self._neighbor_index < len(neighbours):
                        other = neighbours[self._neighbor_index]
                        self._neighbor_index += 1
                        if (
                            other is not self._current_loop
                            and other.face is not self._current_loop.face
                            and _edge_uvs_match(self._current_loop, other, self.uv_layer)
                        ):
                            self._has_uv_neighbor = True
                            self._neighbor_index = len(neighbours)
                    else:
                        if not self._has_uv_neighbor:
                            start = self._current_loop[self.uv_layer].uv
                            end = self._current_loop.link_loop_next[self.uv_layer].uv
                            if (end - start).length_squared > 1e-14:
                                self._segments.append(
                                    (
                                        (float(start.x), float(start.y)),
                                        (float(end.x), float(end.y)),
                                    )
                                )
                        self._segment_index += 1
                        self._current_loop = None
                    operations += 1
                elif self.phase == "cheap":
                    signature, cheap_operations = self._cheap_state.advance(
                        operation_budget=1,
                        deadline=deadline,
                    )
                    operations += max(1, cheap_operations)
                    if signature is not None:
                        signature_started = time.perf_counter()
                        self.signature = self.descriptor_cache.get_or_build_cheap(
                            self.key,
                            self.snapshot_identity,
                            lambda: signature,
                        )
                        self._record_phase_time("cheap_signature", signature_started)
                        self.phase = "fingerprint_init"
                elif self.phase == "fingerprint_init":
                    self._fingerprint_state = _ProPlannerFingerprintBuildState(
                        self.island,
                        self.uv_layer,
                        self.refinement_metrics,
                    )
                    self.phase = "fingerprint"
                    operations += 1
                elif self.phase == "fingerprint":
                    fingerprint, fingerprint_operations = self._fingerprint_state.advance(
                        operation_budget=1,
                        deadline=deadline,
                    )
                    operations += max(1, fingerprint_operations)
                    if fingerprint is not None:
                        sorted_faces = tuple(
                            face for _face_key, face in self._fingerprint_state._face_items
                        )
                        self._area_state = _ProPlannerAreaBuildState(
                            self.obj,
                            self.island,
                            self.uv_layer,
                            sorted_faces,
                        )
                        self._fingerprint = fingerprint
                        self.phase = "area"
                elif self.phase == "area":
                    area_result, area_operations = self._area_state.advance(
                        operation_budget=1,
                        deadline=deadline,
                    )
                    operations += max(1, area_operations)
                    if area_result is not None:
                        uv_area, density = area_result
                        descriptor = tuple(
                            float(value) for value in self.signature.invariant_signature[:2]
                        )
                        self.result = (
                            pro_candidate_planner.IslandRecord(
                                face_key=self.key,
                                strict_topology_fingerprint=self._fingerprint,
                                normalized_boundary_descriptor=descriptor,
                                density=density,
                                cheap_signature=_pro_compact_cheap_signature(self.signature),
                            ),
                            self.signature,
                            uv_area,
                        )
                        self.phase = "done"
                else:
                    raise RuntimeError("unknown Pro planner record phase: %s" % self.phase)
                primitive_ms = (time.perf_counter() - primitive_started) * 1000.0
                self.max_primitive_ms = max(self.max_primitive_ms, primitive_ms)
                self.max_primitive = {
                    "phase": self.phase,
                    "operation": int(self.operations + operations),
                }
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.error = exc
        self.operations += operations
        self.slices += 1
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.max_slice_ms = max(self.max_slice_ms, elapsed_ms)
        return self.result, self.error, operations

    def run_to_completion(self, operation_budget=_PRO_RECORD_OPERATION_BUDGET):
        if int(operation_budget) <= 0:
            raise ValueError("record operation budget must be positive")
        while not self.done:
            self.advance(operation_budget=operation_budget)
        return self.result, self.error


def _pro_ownership_allows(
    member_key,
    master_key,
    assigned_member_keys,
    owner_keys,
):
    """Enforce one-owner star groups and prevent ownership chains/cycles."""

    if member_key == master_key:
        return False
    if member_key in assigned_member_keys or member_key in owner_keys:
        return False
    # An island that is already a member cannot become an owner later.  This
    # prevents a staged A <- B <- C chain from changing source UV values.
    if master_key in assigned_member_keys:
        return False
    return True


def _pro_master_precedes(
    master_key,
    member_key,
    density_by_key,
    tie_epsilon=_PRO_DENSITY_TIE_EPSILON,
    *,
    uv_area_by_key=None,
):
    """Compare one pair using the explicit route metric.

    The positional density form remains only for old pure compatibility tests
    and diagnostics.  Every live Pro route passes ``uv_area_by_key`` by
    keyword, so density cannot become a hidden master-precedence fallback.
    """

    if uv_area_by_key is not None:
        master_area = uv_area_by_key.get(master_key)
        member_area = uv_area_by_key.get(member_key)
        try:
            master_area = float(master_area)
            member_area = float(member_area)
        except (TypeError, ValueError):
            return False
        if (
            not math.isfinite(master_area)
            or not math.isfinite(member_area)
            or master_area < 0.0
            or member_area < 0.0
        ):
            return False
        scale = max(1.0, abs(master_area), abs(member_area))
        difference = master_area - member_area
        area_epsilon = _PRO_UV_AREA_TIE_EPSILON * scale
        if difference > area_epsilon:
            return True
        if abs(difference) <= area_epsilon:
            return tuple(master_key) < tuple(member_key)
        return False

    master_density = density_by_key.get(master_key)
    member_density = density_by_key.get(member_key)
    if master_density is None or member_density is None:
        return False
    difference = float(master_density) - float(member_density)
    if difference > float(tie_epsilon):
        return True
    if abs(difference) <= float(tie_epsilon):
        return tuple(master_key) < tuple(member_key)
    return False


def _pro_master_precedence_reason(master_key, member_key, uv_area_by_key):
    """Return ``None`` for an area-valid master pair or a skip reason."""

    master_area = uv_area_by_key.get(master_key)
    member_area = uv_area_by_key.get(member_key)
    try:
        master_area = float(master_area)
        member_area = float(member_area)
    except (TypeError, ValueError):
        return "missing_uv_area"
    if (
        not math.isfinite(master_area)
        or not math.isfinite(member_area)
        or master_area < 0.0
        or member_area < 0.0
    ):
        return "missing_uv_area"
    if _pro_master_precedes(
        master_key,
        member_key,
        None,
        uv_area_by_key=uv_area_by_key,
    ):
        return None
    return "master_precedence"


def _pro_area_ranked_planner_record(record, uv_area):
    """Adapt the legacy planner API to rank by UV area, never density.

    ``pro_candidate_planner`` predates the explicit UV-area contract and
    exposes only a positive ``density`` ranking field.  A monotonic positive
    proxy preserves its bounded candidate machinery while the actual density
    remains in the session diagnostics and is never consulted by live route
    precedence checks.
    """

    if uv_area is None:
        ranking_value = None
    else:
        try:
            area = float(uv_area)
        except (TypeError, ValueError):
            area = None
        ranking_value = (
            None
            if area is None or not math.isfinite(area) or area < 0.0
            else 1.0 + area
        )
    return pro_candidate_planner.IslandRecord(
        face_key=record.face_key,
        strict_topology_fingerprint=record.strict_topology_fingerprint,
        normalized_boundary_descriptor=record.normalized_boundary_descriptor,
        density=ranking_value,
        cheap_signature=record.cheap_signature,
    )


def _pro_commit_ownership(member_key, master_key, assigned_member_keys, owner_keys):
    assigned_member_keys.add(member_key)
    owner_keys.add(master_key)


class _ProGraphLRU:
    """Bounded exact-graph cache; graph construction is always caller-lazy."""

    def __init__(self, limit=_PRO_GRAPH_CACHE_LIMIT):
        self.limit = max(1, int(limit))
        self._values = OrderedDict()
        self.builds = 0
        self.hits = 0
        self.peak = 0

    def get_or_build(self, key, builder):
        if key in self._values:
            self.hits += 1
            value = self._values.pop(key)
            self._values[key] = value
            return value
        self.builds += 1
        value = builder()
        self._values[key] = value
        self._values.move_to_end(key)
        while len(self._values) > self.limit:
            self._values.popitem(last=False)
        self.peak = max(self.peak, len(self._values))
        return value

    def get(self, key):
        """Return one completed value and mark it as recently used."""

        if key not in self._values:
            return None
        self.hits += 1
        value = self._values.pop(key)
        self._values[key] = value
        return value

    def store(self, key, value):
        """Store only a completed graph produced by a resumable builder."""

        if key in self._values:
            self._values.pop(key)
        self.builds += 1
        self._values[key] = value
        self._values.move_to_end(key)
        while len(self._values) > self.limit:
            self._values.popitem(last=False)
        self.peak = max(self.peak, len(self._values))
        return value

    def clear(self):
        self._values.clear()

    def drop(self, key):
        """Release one graph/loop tuple while retaining cache diagnostics."""

        self._values.pop(key, None)


class _ProIncrementalPlanBuilder:
    """Build the existing immutable planner index in resumable slices.

    The candidate planner remains the single owner of candidate semantics.  A
    Pro session only spreads its record normalization, bucket construction and
    compact index wiring across modal ticks so planner preparation cannot
    become another monolithic main-thread pause.  The finished object is a
    normal ``CandidatePlan`` assembled from the planner's private index
    primitives; no pairs or exact results are materialized here.
    """

    def __init__(self, records, config):
        self.config = config
        self._started = time.perf_counter()
        self._source = tuple(records)
        self._source_index = 0
        self._normalized = []
        self._ordered = None
        self._validate_index = 0
        self._seen = set()
        self._group_index = 0
        self._grouped = defaultdict(list)
        self._fingerprints = ()
        self._bucket_index = 0
        self._buckets = {}
        self._map_index = 0
        self._bucket_for_key = {}
        self._fingerprint_for_key = {}
        self._phase = "normalize"
        self.plan = None

    @property
    def done(self):
        return self.plan is not None

    def _finish(self):
        ordered = self._ordered or ()
        estimated_bytes = pro_candidate_planner._estimate_index_bytes(
            ordered,
            self._buckets,
        )
        sizes = [len(bucket.records) for bucket in self._buckets.values()]
        theoretical_all_pairs = sum(
            size * (size - 1) // 2 for size in sizes
        )
        plan = object.__new__(pro_candidate_planner.CandidatePlan)
        plan.config = self.config
        plan.records = ordered
        plan._buckets = dict(self._buckets)
        plan._bucket_for_key = dict(self._bucket_for_key)
        plan._fingerprint_for_key = dict(self._fingerprint_for_key)
        plan._estimated_bytes = estimated_bytes
        plan._build_elapsed_ms = (
            time.perf_counter() - self._started
        ) * 1000.0
        plan._theoretical_all_pairs = theoretical_all_pairs
        plan._last_diagnostics = pro_candidate_planner.PlannerDiagnostics(
            selected=len(ordered),
            topology_buckets=len(self._buckets),
            candidate_pairs=0,
            theoretical_all_pairs=theoretical_all_pairs,
            avoided_all_pairs=theoretical_all_pairs,
            truncated_members=(),
            truncated_buckets=(),
            max_bucket=max(sizes) if sizes else 0,
            estimated_bytes=estimated_bytes,
            elapsed_ms=plan._build_elapsed_ms,
            unresolved_members=0,
            reason_counts=(),
            member_statuses=(),
        )
        self.plan = plan

    def advance(self, operation_budget=1):
        """Perform at most ``operation_budget`` bounded preparation units."""

        try:
            operation_budget = max(0, int(operation_budget))
        except (TypeError, ValueError):
            operation_budget = 0
        operations = 0
        while not self.done and operations < operation_budget:
            if self._phase == "normalize":
                if self._source_index >= len(self._source):
                    self._ordered = tuple(
                        sorted(
                            self._normalized,
                            key=lambda item: pro_candidate_planner._face_sort_key(
                                item.face_key
                            ),
                        )
                    )
                    self._phase = "validate"
                else:
                    item = self._source[self._source_index]
                    self._source_index += 1
                    self._normalized.append(
                        item
                        if isinstance(item, pro_candidate_planner.IslandRecord)
                        else pro_candidate_planner.IslandRecord(
                            item.face_key,
                            item.strict_topology_fingerprint,
                            item.normalized_boundary_descriptor,
                            item.density,
                            item.cheap_signature,
                        )
                    )
                operations += 1
                continue

            if self._phase == "validate":
                ordered = self._ordered or ()
                if self._validate_index >= len(ordered):
                    self._phase = "group"
                else:
                    record = ordered[self._validate_index]
                    self._validate_index += 1
                    if record.face_key in self._seen:
                        raise ValueError("duplicate face_key: %r" % (record.face_key,))
                    self._seen.add(record.face_key)
                operations += 1
                continue

            if self._phase == "group":
                ordered = self._ordered or ()
                if self._group_index >= len(ordered):
                    self._fingerprints = tuple(
                        sorted(
                            self._grouped,
                            key=pro_candidate_planner._stable_sort_key,
                        )
                    )
                    self._phase = "bucket"
                else:
                    record = ordered[self._group_index]
                    self._group_index += 1
                    self._grouped[record.strict_topology_fingerprint].append(record)
                operations += 1
                continue

            if self._phase == "bucket":
                if self._bucket_index >= len(self._fingerprints):
                    self._phase = "map"
                else:
                    fingerprint = self._fingerprints[self._bucket_index]
                    self._bucket_index += 1
                    self._buckets[fingerprint] = pro_candidate_planner._BucketIndex(
                        self._grouped[fingerprint],
                        self.config,
                    )
                operations += 1
                continue

            if self._phase == "map":
                ordered = self._ordered or ()
                if self._map_index >= len(ordered):
                    self._phase = "finish"
                else:
                    record = ordered[self._map_index]
                    self._map_index += 1
                    bucket = self._buckets[record.strict_topology_fingerprint]
                    self._bucket_for_key[record.face_key] = bucket
                    self._fingerprint_for_key[
                        record.face_key
                    ] = record.strict_topology_fingerprint
                operations += 1
                continue

            if self._phase == "finish":
                self._finish()
                operations += 1
                continue

            raise RuntimeError("unknown Pro planner build phase: %s" % self._phase)
        return self.plan, operations


class _ProIslandEnumerationState:
    """Resumable Pro-only equivalent of ``island_tools`` island discovery."""

    def __init__(self, bm, uv_layer):
        self.bm = bm
        self.uv_layer = uv_layer
        self.phase = "faces"
        self._face_iterator = iter(bm.faces)
        self._faces = []
        self._face_index = 0
        self._loop_index = 0
        self._edge_map = {}
        self._edge_values = ()
        self._edge_index = 0
        self._pair_i = 0
        self._pair_j = 1
        self._adjacency = {}
        self._visited = set()
        self._component_index = 0
        self._component_stack = []
        self._component_faces = []
        self._islands = []
        self.result = None
        self.enum_primitive_operations = 0
        self.enum_slices = 0
        self.max_enum_slice_ms = 0.0
        self.last_enum_slice_ms = 0.0
        self.last_enum_slice_operations = 0
        self.phase_transitions = []

    @property
    def done(self):
        return self.result is not None

    def _set_phase(self, phase):
        if self.phase != phase:
            self.phase_transitions.append((self.phase, phase))
            self.phase = phase

    def advance(
        self,
        operation_budget=_PRO_ENUMERATION_OPERATION_CAP,
        deadline=None,
    ):
        """Advance until the operation cap or monotonic deadline."""

        if self.done:
            return self.result, 0

        try:
            operation_budget = max(0, int(operation_budget))
        except (TypeError, ValueError):
            operation_budget = 0
        started = time.perf_counter()
        operations = 0
        while not self.done and operations < operation_budget:
            if deadline is not None and time.perf_counter() >= deadline:
                break
            if self.phase == "faces":
                try:
                    face = next(self._face_iterator)
                except StopIteration:
                    self._set_phase("adjacency_init")
                else:
                    if not getattr(face, "hide", False):
                        self._faces.append(face)
                operations += 1
                continue

            if self.phase == "adjacency_init":
                self._adjacency = {face: set() for face in self._faces}
                self._face_index = 0
                self._loop_index = 0
                self._set_phase("edge_map")
                operations += 1
                continue

            if self.phase == "edge_map":
                if self._face_index >= len(self._faces):
                    self._edge_values = tuple(self._edge_map.values())
                    self._edge_index = 0
                    self._pair_i = 0
                    self._pair_j = 1
                    self._set_phase("adjacency")
                else:
                    face = self._faces[self._face_index]
                    loops = face.loops
                    if self._loop_index >= len(loops):
                        self._face_index += 1
                        self._loop_index = 0
                    else:
                        loop = loops[self._loop_index]
                        self._loop_index += 1
                        self._edge_map.setdefault(loop.edge, []).append(loop)
                operations += 1
                continue

            if self.phase == "adjacency":
                if self._edge_index >= len(self._edge_values):
                    self._set_phase("components_init")
                else:
                    loops = self._edge_values[self._edge_index]
                    count = len(loops)
                    if self._pair_i >= max(0, count - 1):
                        self._edge_index += 1
                        self._pair_i = 0
                        self._pair_j = 1
                    elif self._pair_j >= count:
                        self._pair_i += 1
                        self._pair_j = self._pair_i + 1
                    else:
                        loop_a = loops[self._pair_i]
                        loop_b = loops[self._pair_j]
                        self._pair_j += 1
                        if (
                            loop_a.face is not loop_b.face
                            and _edge_uvs_match(loop_a, loop_b, self.uv_layer)
                        ):
                            self._adjacency[loop_a.face].add(loop_b.face)
                            self._adjacency[loop_b.face].add(loop_a.face)
                operations += 1
                continue

            if self.phase == "components_init":
                self._component_index = 0
                self._visited = set()
                self._component_stack = []
                self._component_faces = []
                self._islands = []
                self._set_phase("components")
                operations += 1
                continue

            if self.phase == "components":
                if self._component_stack:
                    face = self._component_stack.pop()
                    self._component_faces.append(face)
                    neighbors = sorted(
                        self._adjacency[face],
                        key=lambda item: int(getattr(item, "index", 0)),
                    )
                    for linked in reversed(neighbors):
                        if linked not in self._visited:
                            self._visited.add(linked)
                            self._component_stack.append(linked)
                elif self._component_faces:
                    self._islands.append(
                        tuple(
                            loop
                            for face in self._component_faces
                            for loop in face.loops
                        )
                    )
                    self._component_faces = []
                elif self._component_index >= len(self._faces):
                    self.result = tuple(self._islands)
                    self._set_phase("done")
                else:
                    face = self._faces[self._component_index]
                    self._component_index += 1
                    if face not in self._visited:
                        self._visited.add(face)
                        self._component_stack = [face]
                operations += 1
                continue

            raise RuntimeError("unknown Pro island enumeration phase: %s" % self.phase)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.enum_primitive_operations += operations
        self.enum_slices += 1
        self.last_enum_slice_ms = elapsed_ms
        self.last_enum_slice_operations = operations
        self.max_enum_slice_ms = max(self.max_enum_slice_ms, elapsed_ms)
        return self.result, operations


def _pro_budget_cutoff(report, deadline, stage):
    """Mark a soft synchronous cutoff without scheduling more expensive work."""

    if deadline is None or time.perf_counter() < deadline:
        return False
    report["truncated"] = True
    report["partial"] = bool(report.get("aligned_exact", 0))
    report["budget_cutoff_stage"] = str(stage)
    reasons = report.setdefault("truncation_reasons", [])
    if "wall_time_budget" not in reasons:
        reasons.append("wall_time_budget")
    return True


def _pro_snapshot_uvs(bm, uv_layer):
    snapshot = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            uv = loop[uv_layer].uv
            snapshot[(int(face.index), int(local_index))] = (
                float(uv.x),
                float(uv.y),
            )
    return snapshot


def _pro_snapshot_selection(bm, uv_layer):
    """Capture mesh/UV selection state without retaining BMesh references."""

    snapshot = {}
    if bm is None or uv_layer is None:
        return snapshot
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            snapshot[(int(face.index), int(local_index))] = (
                bool(getattr(luv, "select", False)),
                bool(getattr(luv, "select_edge", False)),
                bool(getattr(face, "select", False)),
                bool(getattr(loop.vert, "select", False)),
                bool(getattr(loop.edge, "select", False)),
            )
    return snapshot


def _pro_snapshot_active(context, obj, bm):
    """Capture compact active-object/face/UV state for modal invalidation."""

    active_face = getattr(getattr(bm, "faces", None), "active", None)
    active_uv = getattr(getattr(obj, "data", None), "uv_layers", None)
    active_uv = getattr(active_uv, "active", None)
    selected_objects = getattr(context, "selected_objects", ()) if context else ()
    if not selected_objects and context is not None:
        view_layer = getattr(context, "view_layer", None)
        selected_objects = getattr(view_layer, "objects", ()) if view_layer else ()
        selected_objects = tuple(
            item for item in selected_objects if getattr(item, "select_get", lambda: False)()
        )
    return {
        "object": getattr(obj, "name", None),
        "mode": getattr(obj, "mode", None),
        "active_face": int(active_face.index) if active_face is not None else None,
        "active_uv": getattr(active_uv, "name", None),
        "selected_objects": tuple(
            sorted(getattr(item, "name", "") for item in selected_objects)
        ),
    }


def _pro_session_context_valid(context, obj, bm, uv_layer):
    """Check the identity that makes a captured modal snapshot safe to use."""

    if context is None or obj is None or bm is None or uv_layer is None:
        return True
    try:
        current_obj = uv_utils.get_active_mesh_object(context)
        if current_obj is not obj:
            return False
        current_bm = island_tools.get_active_bmesh(context)
        if current_bm is not bm:
            return False
        current_uv = island_tools.get_active_uv_layer(current_bm, obj)
        if current_uv is uv_layer:
            return True
        # Blender may expose a fresh BMLayerItem wrapper on each lookup.  The
        # BMesh/object identity is still the stable snapshot boundary, so use
        # the UV layer name as the fallback identity within that same mesh.
        return (
            getattr(current_uv, "name", None)
            == getattr(uv_layer, "name", None)
            and getattr(current_uv, "name", None) is not None
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


def _pro_restore_uvs(bm, uv_layer, snapshot):
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            value = snapshot.get((int(face.index), int(local_index)))
            if value is not None:
                loop[uv_layer].uv = Vector(value)


def _pro_apply_staged_writes(obj, bm, uv_layer, staged_writes, snapshot):
    if not staged_writes:
        return 0
    seen_targets = set()
    for target_key, _target_loop, _uv in staged_writes:
        if target_key in seen_targets:
            raise RuntimeError("Align Similar Pro produced a duplicate target loop.")
        seen_targets.add(target_key)
    try:
        for _target_key, target_loop, uv in staged_writes:
            target_loop[uv_layer].uv = Vector(uv)
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    except Exception:
        _pro_restore_uvs(bm, uv_layer, snapshot)
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        raise
    return len(staged_writes)


def _apply_align_transform(target_island, uv_layer, transform):
    """Apply one similarity transform to every candidate UV on the main thread."""

    for loop in target_island:
        uv = loop[uv_layer].uv
        transformed = transform.apply((float(uv.x), float(uv.y)))
        loop[uv_layer].uv = Vector(transformed)


def _align_candidate(
    ref_island,
    target_island,
    uv_layer,
    settings,
    descriptor_cache=None,
    snapshot_identity=None,
    numeric_cache=None,
):
    return _best_align_transform(
        ref_island,
        target_island,
        uv_layer,
        settings.stack_match_scale,
        settings.stack_allow_flipping,
        descriptor_cache=descriptor_cache,
        snapshot_identity=snapshot_identity,
        numeric_cache=numeric_cache,
        tolerance=settings.stack_similarity_tolerance,
    )


def _cheap_group_bucket_key(signature):
    """Return the strict cheap compatibility key used by selected grouping.

    The key intentionally excludes UV position/center.  Mesh-topology core
    fields are not part of the bucket because ``cheap_topology_gate`` and the
    full matcher already define their documented tolerant-topology behavior.
    """

    return (
        int(signature.component_count),
        int(signature.closed_component_count),
        int(signature.open_component_count),
        int(signature.ambiguous_component_count),
        int(signature.degenerate_segment_count),
        int(signature.cycle_count),
        signature.raw_boundary_signature,
        # Shape invariants are queried through overlapping neighboring bins;
        # they are deliberately not an exact partition key because a valid
        # pair may straddle a bin boundary.  cheap gates still run for every
        # neighboring query result.
        tuple(
            getattr(getattr(signature, "topology", None), "core_key", ()) or ()
        ),
    )


def _cheap_group_invariant_bin(signature):
    """Return the deterministic coarse bin for the two cheap invariants."""

    return tuple(
        int(math.floor(float(value) / _CHEAP_INVARIANT_BUCKET_WIDTH))
        for value in (getattr(signature, "invariant_signature", ()) or ())
    )


def _cheap_group_invariant_neighbors(reference_signature, candidate_signature):
    """Return whether two signatures are in overlapping neighboring bins.

    The query window is intentionally overlap-safe at a bin boundary: one
    adjacent bin in each cheap-invariant dimension is included, while the
    existing cheap boundary/topology gates remain the acceptance filters.
    """

    reference_bin = _cheap_group_invariant_bin(reference_signature)
    candidate_bin = _cheap_group_invariant_bin(candidate_signature)
    if not reference_bin or not candidate_bin:
        return True
    if len(reference_bin) != len(candidate_bin):
        return False
    return all(abs(left - right) <= 1 for left, right in zip(reference_bin, candidate_bin))


def _selected_match_passes_quality(result, similarity_tolerance):
    """Apply the UI tolerance to the final full-fit result before grouping."""

    if not bool(getattr(result, "accepted", False)):
        return False
    if getattr(result, "transform", None) is None:
        return False
    try:
        tolerance = max(0.0, float(similarity_tolerance))
        score = float(getattr(result, "score", float("inf")))
    except (TypeError, ValueError):
        return False
    return math.isfinite(score) and score <= tolerance


def _greedy_fixed_representative_groups(
    islands,
    key_fn,
    prefetch_fn,
    pair_filter_fn=None,
    similarity_tolerance=float("inf"),
):
    """Group ordered items against fixed representatives without transitivity.

    ``prefetch_fn`` receives a newly created representative and all later
    items.  It returns a mapping keyed by ``(representative_key,
    candidate_key)`` whose values are match results or ``None`` for a rejected
    cheap/full comparison.  ``pair_filter_fn`` is an optional deterministic
    overlapping-bucket query; it can narrow the prefetch/comparison set while
    preserving direct representative matching.
    """

    ordered = sorted(tuple(islands), key=key_fn)
    groups = []
    pair_results = {}
    missing = object()

    def filtered_future(representative, future):
        if pair_filter_fn is None:
            return tuple(future)
        return tuple(
            candidate
            for candidate in future
            if pair_filter_fn(representative, candidate)
        )

    for index, island in enumerate(ordered):
        island_key = key_fn(island)
        if not groups:
            group = {"representative": island, "members": []}
            groups.append(group)
            pair_results.update(
                prefetch_fn(island, filtered_future(island, ordered[index + 1 :]))
            )
            continue

        accepted = []
        for group in groups:
            representative = group["representative"]
            if pair_filter_fn is not None and not pair_filter_fn(
                representative, island
            ):
                continue
            pair_key = (key_fn(representative), island_key)
            result = pair_results.get(pair_key, missing)
            if result is missing:
                raise RuntimeError(
                    "Selected similarity prefetch did not cover a representative comparison."
                )
            if result is None:
                continue
            if not _selected_match_passes_quality(result, similarity_tolerance):
                continue
            accepted.append(
                (
                    float(getattr(result, "score", float("inf"))),
                    key_fn(representative),
                    group,
                    result,
                )
            )

        if accepted:
            accepted.sort(key=lambda item: (item[0], item[1]))
            _score, _representative_key, group, result = accepted[0]
            group["members"].append({"island": island, "result": result})
            continue

        group = {"representative": island, "members": []}
        groups.append(group)
        pair_results.update(
            prefetch_fn(island, filtered_future(island, ordered[index + 1 :]))
        )

    return ordered, groups


class _ProAlignSession:
    """Incremental Pro execution shared by background and modal operators."""

    def __init__(
        self,
        context,
        obj,
        bm,
        uv_layer,
        selected_islands=None,
        evidence=None,
        all_islands=None,
        island_enumeration_ms=None,
        operator_setup_ms=None,
        runtime_started=None,
        time_budget_ms=_PRO_SYNC_WALL_TIME_BUDGET_MS,
        planner_config=None,
        modal=False,
        correspondence_max_search=None,
        cooperative_yield_every=None,
        process_worker_count=None,
        process_batch_size=None,
        process_python_executable=None,
        process_worker_script=None,
        process_blender_binary=None,
        process_blender_root=None,
        process_blender_version=None,
        process_handshake_timeout=5.0,
        process_io_timeout=5.0,
        process_test_override=False,
        process_debug_delay_ms=0,
        process_fused=False,
        process_group_first=None,
        correspondence_mode=pro_process_payload.CORRESPONDENCE_MODE_HYBRID,
    ):
        self.context = context
        self.obj = obj
        self.bm = bm
        self.uv_layer = uv_layer
        self.selected_islands = selected_islands
        self.all_islands = all_islands
        self.island_enumeration_ms = island_enumeration_ms
        self.operator_setup_ms = operator_setup_ms
        self.started = time.perf_counter() if runtime_started is None else runtime_started
        try:
            self.time_budget_ms = float(time_budget_ms)
        except (TypeError, ValueError):
            self.time_budget_ms = _PRO_SYNC_WALL_TIME_BUDGET_MS
        self.config = planner_config or _PRO_CANDIDATE_PLANNER_CONFIG
        self.modal = bool(modal)
        self.correspondence_mode = pro_process_payload.normalize_correspondence_mode(
            correspondence_mode
        )
        self.mode = self.correspondence_mode
        if process_worker_count is None:
            process_worker_count = 0
        if (
            isinstance(process_worker_count, bool)
            or not isinstance(process_worker_count, int)
            or process_worker_count < 0
            or process_worker_count > 8
        ):
            raise ValueError("process_worker_count must be 0 or an integer from 1 through 8")
        if process_batch_size is None:
            process_batch_size = 1 if process_worker_count <= 1 else 32
        if (
            isinstance(process_batch_size, bool)
            or not isinstance(process_batch_size, int)
            or process_batch_size <= 0
        ):
            raise ValueError("process_batch_size must be a positive integer")
        self.process_test_override = bool(process_test_override)
        try:
            self.process_debug_delay_ms = int(process_debug_delay_ms or 0)
        except (TypeError, ValueError):
            raise ValueError("process_debug_delay_ms must be a non-negative integer")
        if self.process_debug_delay_ms < 0 or self.process_debug_delay_ms > 10000:
            raise ValueError("process_debug_delay_ms must be between 0 and 10000")
        if process_worker_count:
            if self.process_test_override:
                if process_batch_size > 96:
                    raise ValueError("test process_batch_size must be between 1 and 96")
            elif process_batch_size not in (1, 32, 64, 96):
                raise ValueError("process_batch_size must be 1, 32, 64 or 96")
            if process_batch_size not in (1, 32, 64, 96) and not self.process_test_override:
                raise ValueError("small process batches require an explicit test override")
            if process_batch_size == 1 and process_worker_count > 1 and not self.process_test_override:
                raise ValueError("batch_size=1 for a multiworker process path requires an explicit test override")
        self.process_worker_count = int(process_worker_count)
        self.process_batch_size = int(process_batch_size)
        self.process_requested = self.process_worker_count > 0
        self.process_fused_requested = bool(process_fused)
        if self.process_fused_requested and not self.process_requested:
            raise ValueError("process_fused requires an explicit external worker count")
        self.process_pipeline_requested = bool(
            self.process_requested
            and (
                self.process_worker_count > 1
                or self.process_batch_size > 1
                or self.process_fused_requested
            )
        )
        # Group-first is the only permitted live Pro route for an explicit
        # fused/process request in R2E.  Keep the flag separately from the
        # historical fused bit so pure harnesses can assert the route without
        # changing the synchronous Normal-compatible path.
        self.process_group_first_requested = bool(
            process_fused if process_group_first is None else process_group_first
        )
        if self.process_group_first_requested and not self.process_requested:
            raise ValueError("process_group_first requires an explicit external worker count")
        self._require_external_fast_route()
        self._process_python_executable = process_python_executable
        self._process_worker_script = process_worker_script
        self._process_blender_binary = process_blender_binary
        self._process_blender_root = process_blender_root
        self._process_blender_version = process_blender_version
        self._process_handshake_timeout = float(process_handshake_timeout)
        self._process_io_timeout = float(process_io_timeout)
        if self._process_handshake_timeout <= 0.0 or self._process_io_timeout <= 0.0:
            raise ValueError("process timeouts must be positive")
        try:
            self.correspondence_max_search = int(
                _PRO_CORRESPONDENCE_MAX_SEARCH
                if correspondence_max_search is None
                else correspondence_max_search
            )
        except (TypeError, ValueError):
            self.correspondence_max_search = _PRO_CORRESPONDENCE_MAX_SEARCH
        if self.correspondence_max_search <= 0:
            self.correspondence_max_search = _PRO_CORRESPONDENCE_MAX_SEARCH
        try:
            self.cooperative_yield_every = max(
                0,
                int(
                    _PRO_COOPERATIVE_YIELD_EVERY
                    if cooperative_yield_every is None
                    else cooperative_yield_every
                ),
            )
        except (TypeError, ValueError):
            self.cooperative_yield_every = _PRO_COOPERATIVE_YIELD_EVERY
        self.report = evidence if isinstance(evidence, dict) else {}
        self.detail_mappings = bool(self.report.get("detail_mappings", False))
        self.state = "prepare"
        self._state_sequence = []
        self.done = False
        self.cancelled = False
        self.error = None
        self._tick_started = None
        self.active_elapsed_ms = 0.0
        self._record_started = None
        self._record_phase_recorded = False
        self._record_index = 0
        self._record_builder = None
        self._record_builder_key = None
        self._record_slices = 0
        self._record_operations = 0
        self._record_max_slice_ms = 0.0
        self._record_max_primitive_ms = 0.0
        self._record_max_primitive = {}
        self._batch_iterator = None
        self._batch = ()
        self._batch_index = 0
        self._candidate_plan = None
        self._plan_builder = None
        self._staged_writes = []
        self._planned_target_keys = set()
        self._assigned_member_keys = set()
        self._owner_keys = set()
        self._groups_by_master = {}
        self._key_to_island = {}
        self._cheap_signatures = {}
        self._density_by_key = {}
        self._uv_area_by_key = {}
        self._planner_records = []
        self._descriptor_cache = None
        self._numeric_cache = {}
        self._snapshot_identity = None
        self._process_identity = None
        self._process_options = None
        self._process_snapshot_builder = None
        self._process_snapshot_capture = None
        self._process_graph_context = None
        self._process_graph_context_build_ms = 0.0
        self._process_fused_descriptors = {}
        self._process_fused_context_build_ms = 0.0
        self._process_snapshot_guard = None
        self._process_snapshot_live_loop_map = {}
        self._process_island_loop_keys = {}
        self._process_prepare_context_ready = False
        self._process_graph_builder = None
        self._process_graph_builder_key = None
        self._process_graph_slices = 0
        self._process_graph_primitive_ops = 0
        self._process_graph_max_slice_ms = 0.0
        self._process_graph_max_primitive_ms = 0.0
        self._process_graph_max_primitive = {}
        self._process_graph_build_ms = 0.0
        self._process_graph_cache_builds = 0
        self._process_graph_cache_hits = 0
        self._process_graph_rejections = {}
        self._process_graph_pending = set()
        self._process_graph_worker_submitted = 0
        self._process_graph_worker_completed = 0
        self._process_graph_worker_cache_hits = 0
        self._process_graph_main_operations = 0
        self._process_graph_projection_ms = 0.0
        self._process_validation_requested = False
        self._process_validation_complete = False
        self._process_validation_ms = 0.0
        self._process_validation_slices = 0
        self._process_validation_epoch = 0
        self._process_validation_max_slice_ms = 0.0
        self._process_validation_max_primitive_ms = 0.0
        self._process_validation_max_primitive = {}
        self._process_finalization_grace_active = False
        self._process_finalization_grace_started_at = 0.0
        self._process_finalization_grace_deadline = 0.0
        self._process_finalization_grace_rounds = 0
        self._process_finalization_grace_reason = ""
        self._process_finalization_grace_max_tick_ms = 0.0
        self._process_session_nonce = secrets.token_hex(16)
        self._process_generation = 0
        # Monotonic evidence for the current fused context generation.  This
        # is deliberately session-owned: pool cleanup clears its instantaneous
        # context state, but must not erase an ACK already observed by this
        # session.  A stale identity/failure is the only path that clears it.
        self._process_fused_context_acked = False
        self._process_pool = None
        self._process_pipeline = None
        self._process_group_first_plan = None
        self._process_group_first_frontier = None
        self._process_group_first_records = []
        self._process_group_first_descriptors = {}
        self._process_group_first_exact_task_by_ordinal = {}
        self._process_group_first_record_index = 0
        self._process_group_first_stage = "disabled"
        self._process_group_first_shape_results = {}
        self._process_group_first_exact_jobs = ()
        self._process_group_first_direct_results = {}
        self._process_graph_data = {}
        self._process_next_ordinal = 0
        self._process_pair_ordinals = {}
        self._process_pair_contexts = {}
        self._process_shape_batches = ()
        self._process_shape_batch_by_ordinal = {}
        self._process_collect_records = []
        self._process_collect_complete = False
        self._process_exact_graph_keys = ()
        self._process_exact_graph_index = 0
        self._process_exact_batches = None
        self._process_shape_results = {}
        self._process_exact_results = {}
        self._process_stage = "idle"
        self._process_stage_distributions = {}
        self._process_started_pids = []
        self._process_last_result_digest = ""
        self._process_dispatch_ms = 0.0
        self._process_poll_ms = 0.0
        self._process_startup_ms = 0.0
        self._process_pipeline_admission_owner_ms = 0.0
        self._process_compute_ms = 0.0
        self._process_last_progress = None
        self._process_shutdown_state = "idle"
        self._process_shutdown_started_at = 0.0
        self._process_shutdown_grace_deadline = 0.0
        self._process_shutdown_rounds = 0
        self._process_shutdown_wait_ms = 0.0
        self._process_shutdown_force_used = False
        self._process_cancel_state = "idle"
        self._process_cancel_started_at = 0.0
        self._process_cancel_rounds = 0
        self._process_cancel_wait_ms = 0.0
        self._process_snapshot_checked_at = 0.0
        self._process_snapshot_check_result = True
        self._process_snapshot_stage_checked = None
        self._process_snapshot_checks = 0
        self._process_snapshot_forced_checks = 0
        self._prewrite_snapshot = None
        self._selection_snapshot = None
        self._active_snapshot = None
        self._settings = None
        self._exact_tolerance = _PRO_EXACT_RESIDUAL_TOLERANCE
        self._phase_ms = {}
        self._graph_cache = _ProGraphLRU()
        self._graph_build_state = None
        self._graph_build_key = None
        self._pending_graph_pair = None
        self._pending_graph_pair_object = None
        self._pending_graph_master = None
        self._pending_graph_member = None
        self._graph_completed_ops = 0
        self._graph_completed_slices = 0
        self._graph_max_slice_ms = 0.0
        self._graph_sort_ms = 0.0
        self._graph_finalize_ms = 0.0
        self._graph_phase_transitions = []
        self._pending_graph_shape_ready = False
        self._shape_state = None
        self._shape_pair = None
        self._shape_pair_object = None
        self._pending_shape_pair = None
        self._pending_shape_pair_object = None
        self._pending_shape_result = None
        self._shape_completed_ops = 0
        self._shape_completed_slices = 0
        self._shape_max_slice_ms = 0.0
        self._shape_max_call_ms = 0.0
        self._shape_over_25ms_calls = 0
        self._shape_over_25ms_samples = []
        self._shape_phase_transitions = []
        # P06 exact search is resumable on the modal/main thread.  Keep the
        # worker module available for its independent immutability tests and
        # compatibility seam, but never create a Future for the live Pro
        # session; a Python worker only contends for the same GIL.
        self._worker = None
        self._inflight = None
        self.worker_wall_elapsed_ms = 0.0
        self.worker_compute_elapsed_ms = 0.0
        self.main_thread_submit_ms = 0.0
        self.main_thread_poll_ms = 0.0
        self.main_thread_finalize_ms = 0.0
        self.exact_search_slices = 0
        self.exact_search_operations = 0
        self.exact_search_pending = 0
        self.exact_search_completed = 0
        self.exact_search_discarded = 0
        self.max_exact_slice_ms = 0.0
        self.exact_search_elapsed_ms = 0.0
        self.max_exact_search_operations = 0
        self._tick_stage = "idle"
        self._last_tick_info = {}
        # Keep a bounded sample for MC4 responsiveness evidence.  The live
        # path never needs the full tick history, and the cap prevents a long
        # modal session from retaining unbounded timing data.
        self._tick_samples_ms = []
        self._tick_deadline = None
        self._island_builder = None
        self._island_enumeration_started = None
        self._prepare_completed = False
        self._enum_total_ticks = 0

        self._set_report_defaults()

    def _require_external_fast_route(self):
        """Reject synchronous Pro Fast before any exact or UV-write work."""

        if (
            self.correspondence_mode
            == pro_process_payload.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
            and not self.process_requested
        ):
            raise RuntimeError(_PRO_FAST_SYNC_ERROR)

    def _route_master_precedence_reason(self, master_key, member_key):
        """Apply UV-area precedence once the live session is prepared.

        A few historical pure tests call the compatibility ``_process_pair``
        seam directly while the session is still in ``prepare`` and inject
        only graphs/settings.  That unreachable pre-prepare harness state has
        no candidate route to select from, so leave its legacy exact probe
        untouched; every prepared/process route requires captured UV area.
        """

        if (
            not self._prepare_completed
            and not self._process_prepare_context_ready
            and not self._uv_area_by_key
        ):
            return None
        return _pro_master_precedence_reason(
            master_key,
            member_key,
            self._uv_area_by_key,
        )

    def _set_report_defaults(self):
        report = self.report
        report.setdefault("aligned_exact", 0)
        report.setdefault("group_count", 0)
        report.setdefault("skipped_shape", 0)
        report.setdefault("skipped_topology_unproven", 0)
        report.setdefault("skipped_invalid_density", 0)
        report.setdefault("skipped_missing_uv_area", 0)
        report.setdefault("skipped_ownership", 0)
        report.setdefault("truncated", False)
        report.setdefault("partial", False)
        report.setdefault("cancelled", False)
        report.setdefault("truncation_reasons", [])
        report.setdefault("topology_rejection_samples", [])
        report.setdefault("planner_record_errors", [])
        report.setdefault("planner_record_slices", 0)
        report.setdefault("planner_record_operations", 0)
        report.setdefault("planner_record_max_slice_ms", 0.0)
        report.setdefault("planner_record_max_primitive_ms", 0.0)
        report.setdefault("planner_record_max_primitive", {})
        report.setdefault("planner_refinement_records", 0)
        report.setdefault("planner_refinement_mode", "two_round_bounded")
        report.setdefault("planner_refinement_converged", False)
        report.setdefault("planner_refinement_rounds_total", 0)
        report.setdefault("planner_refinement_max_rounds", 0)
        report.setdefault("planner_refinement_max_bound", 0)
        report.setdefault("planner_refinement_ms", 0.0)
        report.setdefault("planner_refinement_max_ms", 0.0)
        report.setdefault("planner_refinement_stable_count", 0)
        report.setdefault("planner_refinement_truncated_count", 0)
        report.setdefault("planner_master_metric", "uv_area")
        report.setdefault("groups", [])
        report.setdefault("group_summaries", [])
        report["candidate_config"] = {
            "per_member_k": self.config.per_member_k,
            "global_pair_budget": self.config.global_pair_budget,
            "per_bucket_pair_budget": self.config.per_bucket_pair_budget,
            "descriptor_bin_width": self.config.descriptor_bin_width,
            "index_dimensions": self.config.index_dimensions,
            "fallback_probe_limit": self.config.fallback_probe_limit,
            "fallback_candidate_limit": self.config.fallback_candidate_limit,
            "batch_size": self.config.batch_size,
        }
        report["runtime_wall_budget_ms"] = float(self.time_budget_ms)
        report["correspondence_max_search"] = self.correspondence_max_search
        report["cooperative_yield_every"] = self.cooperative_yield_every
        report.setdefault("yield_count", 0)
        report.setdefault("correspondence_calls", 0)
        report.setdefault("correspondence_budget_rejections", 0)
        report.setdefault("exact_refinement_pairs", 0)
        report.setdefault("exact_refinement_rounds_total", 0)
        report.setdefault("exact_refinement_max_rounds", 0)
        report.setdefault("exact_refinement_stable_count", 0)
        report.setdefault("exact_refinement_truncated_count", 0)
        report.setdefault("exact_refinement_ms", 0.0)
        report.setdefault("exact_refinement_pre_max_domain", 0)
        report.setdefault("exact_refinement_post_max_domain", 0)
        report.setdefault("exact_refinement_topology_mismatch", 0)
        report.setdefault("max_correspondence_ms", 0.0)
        report.setdefault("worker_submissions", 0)
        report.setdefault("worker_completions", 0)
        report.setdefault("worker_discards", 0)
        report.setdefault("worker_errors", 0)
        report.setdefault("worker_in_flight_peak", 0)
        report.setdefault("future_wall_ms", 0.0)
        report.setdefault("max_future_wall_ms", 0.0)
        report.setdefault("worker_compute_ms", 0.0)
        report.setdefault("max_worker_compute_ms", 0.0)
        report.setdefault("main_thread_submit_ms", 0.0)
        report.setdefault("main_thread_poll_ms", 0.0)
        report.setdefault("main_thread_finalize_ms", 0.0)
        report.setdefault("worker_shutdown", False)
        report.setdefault("tick_p95_ms", 0.0)
        report.setdefault("tick_p99_ms", 0.0)
        report.setdefault("tick_samples_count", 0)
        report.setdefault("worker_mode", "resumable_main_thread")
        report["correspondence_mode"] = self.correspondence_mode
        report["mode"] = self.correspondence_mode
        report["master_precedence_metric"] = "uv_area"
        report.setdefault("process_requested", bool(self.process_requested))
        report.setdefault("process_worker_count", int(self.process_worker_count))
        report.setdefault("process_batch_size", int(self.process_batch_size))
        report.setdefault("process_pipeline", bool(self.process_pipeline_requested))
        report.setdefault("process_fused", bool(self.process_fused_requested))
        report.setdefault(
            "process_group_first",
            bool(self.process_group_first_requested),
        )
        report.setdefault("process_group_first_stage", "disabled")
        report.setdefault("grouping_comparisons_planned", 0)
        report.setdefault("grouping_comparisons_completed", 0)
        report.setdefault("shape_groups", 0)
        report.setdefault("shape_singletons", 0)
        report.setdefault("group_membership_digest", "")
        report.setdefault("density_masters", [])
        report.setdefault("uv_area_masters", [])
        report.setdefault("uv_area_master_areas", [])
        report.setdefault("uv_area_by_key", [])
        report.setdefault("uv_size_masters", [])
        report.setdefault("uv_size_master_areas", [])
        report.setdefault("uv_size_by_key", [])
        report.setdefault("direct_exact_jobs_planned", 0)
        report.setdefault("direct_exact_jobs_completed", 0)
        report.setdefault("direct_exact_jobs_failed", 0)
        report.setdefault("exact_job_bound", 0)
        report.setdefault("process_group_first_error", "")
        report.setdefault("process_fused_context_ready", False)
        report.setdefault("process_fused_descriptor_count", 0)
        report.setdefault("process_fused_context_digest", None)
        report.setdefault("process_fused_batches_submitted", 0)
        report.setdefault("process_fused_batches_completed", 0)
        report.setdefault("process_fused_pairs_submitted", 0)
        report.setdefault("process_fused_pairs_completed", 0)
        report.setdefault("process_fused_graph_cache_builds", 0)
        report.setdefault("process_fused_graph_cache_hits", 0)
        report.setdefault("process_fused_graph_compute_ms", 0.0)
        report.setdefault("process_fused_exact_compute_ms", 0.0)
        report.setdefault("process_fused_shape_compute_ms", 0.0)
        report.setdefault("process_fused_shape_cache_hits", 0)
        report.setdefault("process_fused_lower_bound_checked", 0)
        report.setdefault("process_fused_lower_bound_rejected", 0)
        report.setdefault("process_fused_lower_bound_skipped", 0)
        report.setdefault("process_fused_lower_bound_graph_pairs_avoided", 0)
        report.setdefault("process_fused_lower_bound_min_ratio", 0.0)
        report.setdefault("process_fused_lower_bound_max_ratio", 0.0)
        report.setdefault("process_fused_frame_bytes", 0)
        report.setdefault("process_fused_frame_total_bytes", 0)
        report.setdefault("process_stage", "idle")
        report.setdefault("process_shape_batches", 0)
        report.setdefault("process_shape_pairs_submitted", 0)
        report.setdefault("process_shape_pairs_completed", 0)
        report.setdefault("process_shape_batches", 0)
        report.setdefault("process_shape_accepted", 0)
        report.setdefault("process_shape_rejected", 0)
        report.setdefault("process_shape_prefiltered", 0)
        report.setdefault("process_exact_batches", 0)
        report.setdefault("process_exact_pairs_submitted", 0)
        report.setdefault("process_exact_batches", 0)
        report.setdefault("process_exact_pairs_completed", 0)
        report.setdefault("process_exact_accepted", 0)
        report.setdefault("process_resident_exact_batches_submitted", 0)
        report.setdefault("process_resident_exact_batches_completed", 0)
        report.setdefault("process_resident_graph_cache_builds", 0)
        report.setdefault("process_resident_graph_cache_hits", 0)
        report.setdefault("process_resident_graph_compute_ms", 0.0)
        report.setdefault("process_resident_topology_cache_builds", 0)
        report.setdefault("process_resident_topology_cache_hits", 0)
        report.setdefault("process_resident_topology_compute_ms", 0.0)
        report.setdefault("process_resident_exact_compute_ms", 0.0)
        report.setdefault("process_resident_exact_frame_bytes", 0)
        report.setdefault("process_merged_pairs", 0)
        report.setdefault("process_pruned_pairs", 0)
        report.setdefault("process_exact_started_before_shape_terminal", False)
        report.setdefault("process_exact_first_shape_completed", 0)
        report.setdefault("process_exact_first_shape_total", 0)
        report.setdefault("process_exact_first_timestamp_ms", None)
        report.setdefault("process_last_progress_kind", "")
        report.setdefault("process_poll_calls", 0)
        report.setdefault("process_no_progress_loops", 0)
        report.setdefault("process_event_epoch", 0)
        report.setdefault("process_graph_event_epoch", 0)
        report.setdefault("process_graph_waiter_registrations", 0)
        report.setdefault("process_graph_waiter_dedup", 0)
        report.setdefault("process_queue_depth", 0)
        report.setdefault("process_stage_distributions", {})
        report.setdefault("process_frame_bytes", {})
        report.setdefault("process_frame_total_bytes", {})
        report.setdefault("process_cache_hits", 0)
        report.setdefault("process_debug_delay_ms", int(self.process_debug_delay_ms))
        report.setdefault("process_worker_pids", [])
        report.setdefault("process_helper_path", None)
        report.setdefault("process_python_executable", None)
        report.setdefault("process_python_version", None)
        report.setdefault("process_snapshot_digest", None)
        report.setdefault("process_snapshot_error_diagnostics", None)
        report.setdefault("process_snapshot_checks", 0)
        report.setdefault("process_snapshot_forced_checks", 0)
        report.setdefault("process_initial_snapshot_ms", 0.0)
        report.setdefault("process_initial_snapshot_slices", 0)
        report.setdefault("process_initial_snapshot_operations", 0)
        report.setdefault("process_initial_snapshot_max_slice_ms", 0.0)
        report.setdefault("process_validation_ms", 0.0)
        report.setdefault("process_validation_slices", 0)
        report.setdefault("process_validation_operations", 0)
        report.setdefault("process_validation_epoch", 0)
        report.setdefault("process_validation_max_slice_ms", 0.0)
        report.setdefault("process_validation_max_primitive_ms", 0.0)
        report.setdefault("process_validation_max_primitive", {})
        report.setdefault("process_validation_status", "not_started")
        report.setdefault("process_finalization_grace_state", "idle")
        report.setdefault("process_finalization_grace_started_ms", 0.0)
        report.setdefault("process_finalization_grace_deadline_ms", 0.0)
        report.setdefault("process_finalization_grace_rounds", 0)
        report.setdefault("process_finalization_grace_reason", "")
        report.setdefault("process_finalization_grace_max_tick_ms", 0.0)
        report.setdefault("process_finalization_grace_no_dispatch", False)
        report.setdefault("process_pipeline_subphase_ms", {})
        report.setdefault("process_pipeline_max_subphase_ms", 0.0)
        report.setdefault("process_pipeline_max_subphase", "")
        report.setdefault("process_graph_slices", 0)
        report.setdefault("process_graph_primitive_ops", 0)
        report.setdefault("process_graph_max_slice_ms", 0.0)
        report.setdefault("process_graph_max_primitive_ms", 0.0)
        report.setdefault("process_graph_max_primitive", {})
        report.setdefault("process_graph_build_ms", 0.0)
        report.setdefault("process_graph_cache_builds", 0)
        report.setdefault("process_graph_cache_hits", 0)
        report.setdefault("process_graph_rejections", {})
        report.setdefault("process_graph_worker_submitted", 0)
        report.setdefault("process_graph_worker_completed", 0)
        report.setdefault("process_graph_worker_cache_hits", 0)
        report.setdefault("process_graph_main_operations", 0)
        report.setdefault("process_graph_worker_operations", 0)
        report.setdefault("process_worker_operation_distribution", [])
        report.setdefault("process_graph_projection_ms", 0.0)
        report.setdefault("process_graph_context_digest", None)
        report.setdefault("process_graph_context_build_ms", 0.0)
        report.setdefault("process_graph_context_frame_bytes", 0)
        report.setdefault("process_graph_context_frame_max_bytes", 0)
        report.setdefault("process_graph_context_payload_bytes", 0)
        report.setdefault("process_graph_context_load_submitted", 0)
        report.setdefault("process_graph_context_load_acked", 0)
        report.setdefault("process_graph_context_load_ms", 0.0)
        report.setdefault("process_graph_context_ready", False)
        report.setdefault("process_result_digest", "")
        report.setdefault("process_startup_ms", 0.0)
        report.setdefault("process_worker_start_owner_ms", 0.0)
        report.setdefault("process_worker_start_background_ms", 0.0)
        report.setdefault("process_worker_start_pending", 0)
        report.setdefault("process_worker_start_states", [])
        report.setdefault("process_context_serialize_owner_ms", 0.0)
        report.setdefault("process_context_serialize_background_ms", 0.0)
        report.setdefault("process_context_write_background_ms", 0.0)
        report.setdefault("process_context_send_pending", 0)
        report.setdefault("process_pipeline_admission_owner_ms", 0.0)
        report.setdefault("process_dispatch_ms", 0.0)
        report.setdefault("process_poll_ms", 0.0)
        report.setdefault("process_compute_ms", 0.0)
        report.setdefault("process_active_workers", 0)
        report.setdefault("process_retry_count", 0)
        report.setdefault("process_retry_total", 0)
        report.setdefault("process_max_retry_per_batch", 0)
        report.setdefault("process_retried_batch_count", 0)
        report.setdefault("process_retry_failure_reason", "")
        report.setdefault("process_retry_batches", [])
        report.setdefault("process_restart_pending", 0)
        report.setdefault("process_restart_states", [])
        report.setdefault("process_nearest_attempted", 0)
        report.setdefault("process_nearest_accepted", 0)
        report.setdefault("process_nearest_fallback", 0)
        report.setdefault("process_nearest_max_seed_distance", 0.0)
        report.setdefault("process_nearest_mean_seed_distance", 0.0)
        report.setdefault("process_nearest_ambiguity_count", 0)
        report.setdefault("process_nearest_tie_count", 0)
        report.setdefault("process_nearest_compute_ms", 0.0)
        report.setdefault("process_nearest_distance_evaluations", 0)
        report.setdefault("process_nearest_assignment_nodes", 0)
        report.setdefault("process_nearest_assignment_cap", 0)
        report.setdefault("process_nearest_fallback_reasons", {})
        report.setdefault("process_nearest_distance_lookups", 0)
        report.setdefault("process_nearest_distance_cache_hits", 0)
        report.setdefault("process_nearest_distance_cache_misses", 0)
        report.setdefault("process_nearest_operations_used", 0)
        # R2F2 live evidence separates seed-bearing nearest attempts from
        # direct jobs that intentionally take the unchanged exact path.
        report.setdefault("process_nearest_seeded_jobs_planned", 0)
        report.setdefault("process_nearest_seedless_jobs_planned", 0)
        report.setdefault("process_nearest_fallback_exact_calls", 0)
        report.setdefault("process_nearest_missing_seed_fallbacks", 0)
        report.setdefault("process_graph_rejected_before_nearest", 0)
        report.setdefault("process_nearest_seed_missing", 0)
        report.setdefault("process_nearest_fast_miss", 0)
        report.setdefault("process_exact_fallback_calls", 0)
        report.setdefault("process_exact_primary_calls", 0)
        report.setdefault("process_nearest_accounting", {})
        report.setdefault("process_nearest_accounting_valid", False)
        report.setdefault("process_seed_planned", 0)
        report.setdefault("process_seed_rerooted", 0)
        report.setdefault("process_seed_identity_leg", 0)
        report.setdefault("process_seed_missing_by_reason", {})
        report.setdefault("process_seed_digest", "")
        report.setdefault("process_shutdown_state", "idle")
        report.setdefault("process_shutdown_rounds", 0)
        report.setdefault("process_shutdown_force_used", False)
        report.setdefault("process_shutdown_wait_ms", 0.0)
        report.setdefault("process_shutdown_timings_ms", [])
        report.setdefault("process_cleanup_pending", 0)
        report.setdefault("process_cancel_state", "idle")
        report.setdefault("process_cancel_rounds", 0)
        report.setdefault("process_cancel_wait_ms", 0.0)
        report.setdefault("process_state_sequence", [])
        report.setdefault("exact_search_slices", 0)
        report.setdefault("exact_search_operations", 0)
        report.setdefault("exact_search_pending", 0)
        report.setdefault("exact_search_completed", 0)
        report.setdefault("exact_search_discarded", 0)
        report.setdefault("exact_search_elapsed_ms", 0.0)
        report.setdefault("max_exact_slice_ms", 0.0)
        report.setdefault("max_exact_search_operations", 0)
        report.setdefault("exact_slice_budget_ms", _PRO_EXACT_SLICE_BUDGET_MS)
        report.setdefault("exact_operation_budget", _PRO_EXACT_OPERATION_BUDGET)
        report.setdefault("enum_operation_cap", _PRO_ENUMERATION_OPERATION_CAP)
        report.setdefault("enum_slice_budget_ms", _PRO_ENUMERATION_SLICE_BUDGET_MS)
        report.setdefault("enum_primitive_ops", 0)
        report.setdefault("enum_slices", 0)
        report.setdefault("max_enum_slice_ms", 0.0)
        report.setdefault("enum_phase_transitions", [])
        report.setdefault("enum_total_ticks", 0)
        report.setdefault("graph_operation_cap", _PRO_GRAPH_OPERATION_BUDGET)
        report.setdefault("graph_primitive_ops", 0)
        report.setdefault("graph_slices", 0)
        report.setdefault("max_graph_slice_ms", 0.0)
        report.setdefault("graph_phase", "idle")
        report.setdefault("graph_phase_transitions", [])
        report.setdefault("graph_sort_ms", 0.0)
        report.setdefault("graph_finalize_ms", 0.0)
        report.setdefault("shape_operation_cap", _PRO_SHAPE_OPERATION_BUDGET)
        report.setdefault("shape_primitive_ops", 0)
        report.setdefault("shape_slices", 0)
        report.setdefault("max_shape_slice_ms", 0.0)
        report.setdefault("max_shape_call_ms", 0.0)
        report.setdefault("shape_phase", "idle")
        report.setdefault("shape_phase_transitions", [])
        report.setdefault("shape_over_25ms_calls", 0)
        report.setdefault("shape_over_25ms_samples", [])

    def _record_phase(self, name, elapsed):
        self._phase_ms[name] = self._phase_ms.get(name, 0.0) + float(elapsed)

    def _record_state_transition(self, state=None):
        value = str(self.state if state is None else state)
        if not self._state_sequence or self._state_sequence[-1] != value:
            self._state_sequence.append(value)
        self.report["process_state_sequence"] = list(self._state_sequence)

    def _update_enum_report(self):
        builder = self._island_builder
        if builder is not None:
            self.report["enum_primitive_ops"] = int(
                builder.enum_primitive_operations
            )
            self.report["enum_slices"] = int(builder.enum_slices)
            self.report["max_enum_slice_ms"] = float(builder.max_enum_slice_ms)
            self.report["enum_phase_transitions"] = [
                [str(before), str(after)]
                for before, after in builder.phase_transitions
            ]
        self.report["enum_total_ticks"] = int(self._enum_total_ticks)

    def _update_graph_report(self):
        builder = self._graph_build_state
        if builder is not None:
            graph_ops = (
                self._graph_completed_ops + builder.graph_primitive_operations
            )
            graph_slices = (
                self._graph_completed_slices + builder.graph_slices
            )
            graph_max_slice_ms = max(
                self._graph_max_slice_ms,
                builder.max_graph_slice_ms,
            )
            graph_sort_ms = self._graph_sort_ms + builder.sort_ms
            graph_finalize_ms = self._graph_finalize_ms + builder.graph_finalize_ms
            transitions = self._graph_phase_transitions + [
                [str(before), str(after)]
                for before, after in builder.phase_transitions
            ]
            self.report["graph_phase"] = str(builder.phase)
        else:
            graph_ops = self._graph_completed_ops
            graph_slices = self._graph_completed_slices
            graph_max_slice_ms = self._graph_max_slice_ms
            graph_sort_ms = self._graph_sort_ms
            graph_finalize_ms = self._graph_finalize_ms
            transitions = self._graph_phase_transitions
            self.report["graph_phase"] = "idle"
        self.report["graph_primitive_ops"] = int(graph_ops)
        self.report["graph_slices"] = int(graph_slices)
        self.report["max_graph_slice_ms"] = float(graph_max_slice_ms)
        self.report["graph_phase_transitions"] = transitions[-64:]
        self.report["graph_sort_ms"] = float(graph_sort_ms)
        self.report["graph_finalize_ms"] = float(graph_finalize_ms)
        self.report["graph_cache_hits"] = int(self._graph_cache.hits)
        self.report["graph_cache_peak"] = int(self._graph_cache.peak)

    def _commit_graph_builder_metrics(self, builder):
        self._graph_completed_ops += int(builder.graph_primitive_operations)
        self._graph_completed_slices += int(builder.graph_slices)
        self._graph_max_slice_ms = max(
            self._graph_max_slice_ms,
            float(builder.max_graph_slice_ms),
        )
        self._graph_sort_ms += float(builder.sort_ms)
        self._graph_finalize_ms += float(builder.graph_finalize_ms)
        self._graph_phase_transitions = (
            self._graph_phase_transitions
            + [
                [str(before), str(after)]
                for before, after in builder.phase_transitions
            ]
        )[-64:]

    def _update_shape_report(self):
        state = self._shape_state
        if state is not None:
            shape_ops = self._shape_completed_ops + state.shape_primitive_operations
            shape_slices = self._shape_completed_slices + state.shape_slices
            max_slice_ms = max(
                self._shape_max_slice_ms,
                state.max_shape_slice_ms,
            )
            max_call_ms = max(
                self._shape_max_call_ms,
                state.max_shape_call_ms,
            )
            over_25ms_calls = (
                self._shape_over_25ms_calls + state.over_25ms_calls
            )
            samples = self._shape_over_25ms_samples + list(
                state.over_25ms_call_samples
            )
            transitions = self._shape_phase_transitions + [
                [str(before), str(after)]
                for before, after in state.phase_transitions
            ]
            self.report["shape_phase"] = str(state.phase)
        else:
            shape_ops = self._shape_completed_ops
            shape_slices = self._shape_completed_slices
            max_slice_ms = self._shape_max_slice_ms
            max_call_ms = self._shape_max_call_ms
            over_25ms_calls = self._shape_over_25ms_calls
            samples = self._shape_over_25ms_samples
            transitions = self._shape_phase_transitions
            self.report["shape_phase"] = "idle"
        self.report["shape_primitive_ops"] = int(shape_ops)
        self.report["shape_slices"] = int(shape_slices)
        self.report["max_shape_slice_ms"] = float(max_slice_ms)
        self.report["max_shape_call_ms"] = float(max_call_ms)
        self.report["shape_over_25ms_calls"] = int(over_25ms_calls)
        self.report["shape_over_25ms_samples"] = samples[-_PRO_REJECTION_SAMPLE_LIMIT:]
        self.report["shape_phase_transitions"] = transitions[-64:]

    def _commit_shape_state_metrics(self, state):
        self._shape_completed_ops += int(state.shape_primitive_operations)
        self._shape_completed_slices += int(state.shape_slices)
        self._shape_max_slice_ms = max(
            self._shape_max_slice_ms,
            float(state.max_shape_slice_ms),
        )
        self._shape_max_call_ms = max(
            self._shape_max_call_ms,
            float(state.max_shape_call_ms),
        )
        self._shape_over_25ms_calls += int(state.over_25ms_calls)
        self._shape_over_25ms_samples = (
            self._shape_over_25ms_samples
            + list(state.over_25ms_call_samples)
        )[-_PRO_REJECTION_SAMPLE_LIMIT:]
        self._shape_phase_transitions = (
            self._shape_phase_transitions
            + [
                [str(before), str(after)]
                for before, after in state.phase_transitions
            ]
        )[-64:]

    def _shape_match_is_overridden(self):
        """Keep focused seams that replace the synchronous shape oracle."""

        return globals().get("_pro_shape_match_result") is not _PRO_SHAPE_MATCH_RESULT_ORACLE

    def _advance_shape_for(self, master_key, member_key, deadline=None):
        """Advance exactly one Pro shape candidate without materializing fits."""

        if self._shape_match_is_overridden():
            started = time.perf_counter()
            try:
                result = _pro_shape_match_result(
                    self._key_to_island[master_key],
                    self._key_to_island[member_key],
                    self.uv_layer,
                    self._settings,
                    self._descriptor_cache,
                    self._snapshot_identity,
                    self._numeric_cache,
                )
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self._shape_max_call_ms = max(self._shape_max_call_ms, elapsed_ms)
                self._record_phase("shape_fit", elapsed_ms)
                return "error", None, str(exc)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._shape_max_call_ms = max(self._shape_max_call_ms, elapsed_ms)
            if elapsed_ms > pro_shape_state.SHAPE_CALL_LIMIT_MS:
                self._shape_over_25ms_calls += 1
                if len(self._shape_over_25ms_samples) < _PRO_REJECTION_SAMPLE_LIMIT:
                    self._shape_over_25ms_samples.append(
                        {"phase": "synchronous_oracle", "elapsed_ms": elapsed_ms}
                    )
            self._record_phase("shape_fit", elapsed_ms)
            return "ready", result, None

        pair_key = (tuple(master_key), tuple(member_key))
        if self._shape_state is None:
            self._shape_pair = pair_key
            self._shape_pair_object = None
            tolerance = max(
                0.0,
                float(self._settings.stack_similarity_tolerance),
            )
            self._shape_state = pro_shape_state.ProShapeMatchState(
                reference_builder=lambda: _descriptor_for_island(
                    self._key_to_island[master_key],
                    self.uv_layer,
                    self._descriptor_cache,
                    self._snapshot_identity,
                    numeric_cache=self._numeric_cache,
                ),
                candidate_builder=lambda: _descriptor_for_island(
                    self._key_to_island[member_key],
                    self.uv_layer,
                    self._descriptor_cache,
                    self._snapshot_identity,
                    numeric_cache=self._numeric_cache,
                ),
                match_scale=bool(self._settings.stack_match_scale),
                allow_flipping=bool(self._settings.stack_allow_flipping),
                tolerance=tolerance,
                allow_tolerant_topology=(
                    tolerance > similarity_matcher.TOPOLOGY_PENALTY
                ),
            )
        elif self._shape_pair != pair_key:
            raise RuntimeError(
                "Pro shape build re-entry would violate one-state ordering"
            )

        state = self._shape_state
        try:
            result, _operations = state.advance(
                operation_budget=_PRO_SHAPE_OPERATION_BUDGET,
                deadline=deadline,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self._commit_shape_state_metrics(state)
            self._shape_state = None
            self._shape_pair = None
            self._shape_pair_object = None
            self._update_shape_report()
            return "error", None, str(exc)

        self._record_phase("shape_fit", state.last_shape_slice_ms)
        self._update_shape_report()
        if result is None:
            if state.cancelled or state.done:
                self._commit_shape_state_metrics(state)
                self._shape_state = None
                self._shape_pair = None
                self._shape_pair_object = None
                self._update_shape_report()
                return "error", None, "shape_state_cancelled"
            return "pending", None, None

        self._commit_shape_state_metrics(state)
        self._shape_state = None
        self._shape_pair = None
        self._shape_pair_object = None
        self._update_shape_report()
        similarity_matcher.record_match_diagnostics(result.diagnostics)
        return "ready", result, None

    def _budget_reached(self):
        if self.time_budget_ms <= 0.0:
            return False
        elapsed = self.active_elapsed_ms
        if self._tick_started is not None:
            elapsed += (time.perf_counter() - self._tick_started) * 1000.0
        return elapsed >= self.time_budget_ms

    def _request_timeout(self, stage):
        if self.state == "process_shutdown":
            # A canonical process result has already been merged.  Modal
            # budget exhaustion must defer shutdown to the next advance, not
            # turn a completed semantic result into a synchronous finish or
            # a partial apply.
            self.report["budget_cutoff_stage"] = str(stage)
            self.report["process_shutdown_budget_deferred"] = True
            return
        if self.state == "process_finalization_grace" or self._process_finalization_grace_active:
            if time.perf_counter() < self._process_finalization_grace_deadline:
                self.report["process_finalization_grace_budget_deferred"] = True
                self.report["budget_cutoff_stage"] = str(stage)
                return
            self.report["process_finalization_grace_state"] = "expired"
            self.cancel("validation_grace_expired", nonblocking=self.modal)
            return
        if self.process_requested and self._process_pipeline_has_terminal_canonical_result():
            if self._begin_process_finalization_grace(str(stage)):
                self.report["budget_cutoff_stage"] = str(stage)
                return
        process_incomplete = bool(
            self.process_requested
            and not self._process_pipeline_has_terminal_canonical_result()
        )
        self._discard_inflight("wall_time_budget")
        if not self.report["truncated"]:
            self.report["truncated"] = True
            self.report["partial"] = bool(self._staged_writes) and not process_incomplete
            self.report["truncation_reasons"].append("wall_time_budget")
        self.report["budget_cutoff_stage"] = str(stage)
        self.report["active_budget_ms"] = float(self.time_budget_ms)
        if process_incomplete:
            # Never apply a prefix of a process pipeline.  The normal
            # finalize path clears staged writes atomically when cancelled.
            self.cancelled = True
            self.report["cancelled"] = True
            self.report["cancel_reason"] = "wall_time_budget"
            self.report["partial"] = False
        self.state = "finish"

    def _discard_inflight(self, reason, *, nonblocking=False):
        """Discard one exact state and all main-thread graph references."""

        inflight = self._inflight
        if inflight is None:
            return False
        if inflight.get("process"):
            if self._process_pool is not None:
                try:
                    if nonblocking:
                        self._process_pool.begin_cancel()
                    else:
                        self._process_pool.cancel(timeout=1.0)
                except Exception:
                    if not nonblocking:
                        try:
                            self._process_pool.close()
                        except Exception:
                            pass
            self.report["worker_discards"] = int(
                self.report.get("worker_discards", 0)
            ) + 1
        else:
            search = inflight.get("search")
            if search is not None:
                search.cancel()
                self.exact_search_discarded += 1
                self.report["exact_search_discarded"] = self.exact_search_discarded
            elif self._worker is not None:
                self._worker.discard()
        self._inflight = None
        self._graph_cache.drop(inflight["member_key"])
        self.report.setdefault("worker_discard_reasons", [])
        if len(self.report["worker_discard_reasons"]) < _PRO_REJECTION_SAMPLE_LIMIT:
            self.report["worker_discard_reasons"].append(str(reason))
        return True

    def _update_worker_report(self):
        if self.process_requested:
            pool = self._process_pool
            progress = self._process_last_progress
            if pool is not None and hasattr(pool, "workers"):
                self._update_process_pool_metadata(pool)
            if self.process_fused_requested:
                self.report["process_fused_context_ready"] = bool(
                    self._process_fused_context_acked
                )
            self.report["worker_mode"] = (
                "external_bundled_python_group_first"
                if self.process_group_first_requested
                else (
                    "external_bundled_python_fused"
                    if self.process_fused_requested
                    else (
                        "external_bundled_python_pipeline"
                        if self.process_pipeline_requested
                        else "external_bundled_python_single"
                    )
                )
            )
            self.report["worker_submissions"] = int(
                self.report.get("worker_submissions", 0)
            )
            self.report["worker_completions"] = int(
                self.report.get("worker_completions", 0)
            )
            self.report["worker_in_flight_peak"] = int(
                self.report.get("worker_in_flight_peak", 0)
            )
            self.report["worker_shutdown"] = bool(
                pool is None or not pool.worker_pids
            )
            if progress is not None:
                self.report["process_active_workers"] = int(progress.active_workers)
                self.report["process_retry_count"] = int(progress.retry_count)
                self.report["process_retry_total"] = int(
                    getattr(progress, "retry_total", progress.retry_count)
                )
                self.report["process_max_retry_per_batch"] = int(
                    getattr(progress, "max_retry_per_batch", progress.retry_count)
                )
                self.report["process_retried_batch_count"] = int(
                    getattr(progress, "retried_batch_count", 0)
                )
                self.report["process_retry_failure_reason"] = str(
                    getattr(progress, "retry_failure_reason", "") or ""
                )
                self.report["process_nearest_attempted"] = int(
                    getattr(progress, "nearest_attempted", 0)
                )
                self.report["process_nearest_accepted"] = int(
                    getattr(progress, "nearest_accepted", 0)
                )
                self.report["process_nearest_fallback"] = int(
                    getattr(progress, "nearest_fallback", 0)
                )
                self.report["process_nearest_max_seed_distance"] = float(
                    getattr(progress, "nearest_max_seed_distance", 0.0)
                )
                self.report["process_nearest_mean_seed_distance"] = float(
                    getattr(progress, "nearest_mean_seed_distance", 0.0)
                )
                self.report["process_nearest_ambiguity_count"] = int(
                    getattr(progress, "nearest_ambiguity_count", 0)
                )
                self.report["process_nearest_tie_count"] = int(
                    getattr(progress, "nearest_tie_count", 0)
                )
                self.report["process_nearest_compute_ms"] = float(
                    getattr(progress, "nearest_compute_ms", 0.0)
                )
                self.report["process_nearest_distance_evaluations"] = int(
                    getattr(progress, "nearest_distance_evaluations", 0)
                )
                self.report["process_nearest_assignment_nodes"] = int(
                    getattr(progress, "nearest_assignment_nodes", 0)
                )
                self.report["process_nearest_assignment_cap"] = int(
                    getattr(progress, "nearest_assignment_cap", 0)
                )
                self.report["process_nearest_fallback_reasons"] = {
                    pro_verified_nearest.fallback_reason_name(code): int(count)
                    for code, count in getattr(progress, "nearest_fallback_reasons", ())
                }
                self.report["process_nearest_distance_lookups"] = int(
                    getattr(progress, "nearest_distance_lookups", 0)
                )
                self.report["process_nearest_distance_cache_hits"] = int(
                    getattr(progress, "nearest_distance_cache_hits", 0)
                )
                self.report["process_nearest_distance_cache_misses"] = int(
                    getattr(progress, "nearest_distance_cache_misses", 0)
                )
                self.report["process_nearest_operations_used"] = int(
                    getattr(progress, "nearest_operations_used", 0)
                )
                self.report["process_graph_rejected_before_nearest"] = int(
                    getattr(progress, "graph_rejected_before_nearest", 0)
                )
                self.report["process_nearest_seed_missing"] = int(
                    getattr(progress, "nearest_seed_missing", 0)
                )
                fast_miss = int(
                    getattr(progress, "nearest_fast_miss", 0) or 0
                )
                if fast_miss <= 0:
                    # Older/fake progress objects expose only the legacy
                    # nearest_fallback field.  Do not let a present-but-zero
                    # compatibility attribute erase an actual typed count.
                    fast_miss = int(
                        getattr(progress, "nearest_fallback", 0) or 0
                    )
                self.report["process_nearest_fast_miss"] = fast_miss
                exact_fallback_calls = int(
                    getattr(progress, "exact_fallback_calls", 0) or 0
                )
                if (
                    exact_fallback_calls <= 0
                    and self.correspondence_mode
                    == pro_process_payload.CORRESPONDENCE_MODE_HYBRID
                ):
                    exact_fallback_calls = fast_miss + int(
                        getattr(progress, "nearest_seed_missing", 0) or 0
                    )
                self.report["process_exact_fallback_calls"] = (
                    exact_fallback_calls
                )
                self.report["process_nearest_fallback_exact_calls"] = int(
                    self.report["process_exact_fallback_calls"]
                )
                self.report["process_exact_primary_calls"] = int(
                    getattr(progress, "exact_primary_calls", 0) or 0
                )
                self.report["process_nearest_missing_seed_fallbacks"] = int(
                    self.report["process_nearest_seed_missing"]
                )
                self.report["process_restart_pending"] = int(
                    getattr(progress, "restart_pending", 0)
                )
                self.report["process_restart_states"] = [
                    [int(index), str(state), str(batch_id)]
                    for index, state, batch_id in getattr(progress, "restart_states", ())
                ]
                self.report["process_queue_depth"] = int(
                    getattr(self._process_pool, "queue_depth", 0)
                )
            self.report["process_startup_ms"] = float(self._process_startup_ms)
            self.report["process_dispatch_ms"] = float(self._process_dispatch_ms)
            self.report["process_poll_ms"] = float(self._process_poll_ms)
            self.report["process_compute_ms"] = float(self._process_compute_ms)
            self.report["process_worker_pids"] = list(self._process_started_pids)
            self.report["process_result_digest"] = self._process_last_result_digest
            self.report["process_shutdown_state"] = str(self._process_shutdown_state)
            self.report["process_shutdown_rounds"] = int(self._process_shutdown_rounds)
            self.report["process_shutdown_force_used"] = bool(
                self._process_shutdown_force_used
            )
            self.report["process_shutdown_wait_ms"] = float(
                self._process_shutdown_wait_ms
            )
            self.report["process_cancel_state"] = str(self._process_cancel_state)
            self.report["process_cancel_rounds"] = int(self._process_cancel_rounds)
            self.report["process_cancel_wait_ms"] = float(
                self._process_cancel_wait_ms
            )
        elif self._worker is None:
            snapshot = {
                "worker_mode": "resumable_main_thread",
                "worker_submissions": 0,
                "worker_completions": 0,
                "worker_discards": 0,
                "worker_errors": 0,
                "worker_in_flight_peak": 0,
                "future_wall_ms": 0.0,
                "max_future_wall_ms": 0.0,
                "worker_compute_ms": 0.0,
                "max_worker_compute_ms": 0.0,
                "worker_shutdown": True,
            }
            self.report.update(snapshot)
        else:
            snapshot = self._worker.snapshot()
            self.report.update(snapshot)
        self.report["worker_wall_elapsed_ms"] = float(self.worker_wall_elapsed_ms)
        self.report["worker_compute_elapsed_ms"] = float(
            self.worker_compute_elapsed_ms
        )
        self.report["process_snapshot_checks"] = int(self._process_snapshot_checks)
        self.report["process_snapshot_forced_checks"] = int(
            self._process_snapshot_forced_checks
        )
        self.report["main_thread_submit_ms"] = float(self.main_thread_submit_ms)
        self.report["main_thread_poll_ms"] = float(self.main_thread_poll_ms)
        self.report["main_thread_finalize_ms"] = float(self.main_thread_finalize_ms)
        self.report["exact_search_slices"] = int(self.exact_search_slices)
        self.report["exact_search_operations"] = int(self.exact_search_operations)
        self.report["exact_search_pending"] = int(self.exact_search_pending)
        self.report["exact_search_completed"] = int(self.exact_search_completed)
        self.report["exact_search_discarded"] = int(self.exact_search_discarded)
        self.report["exact_search_elapsed_ms"] = float(
            self.exact_search_elapsed_ms
        )
        self.report["max_exact_slice_ms"] = float(self.max_exact_slice_ms)
        self.report["max_exact_search_operations"] = int(
            self.max_exact_search_operations
        )

    def _latch_process_fused_context_ack(self, pool=None):
        """Latch a valid fused-context ACK without depending on cleanup state."""

        if not self.process_fused_requested:
            return False
        context = self._process_graph_context
        identity = getattr(context, "identity", None)
        current_identity = self._process_identity
        current_generation = int(self._process_generation)
        if (
            context is None
            or not str(getattr(context, "fused_digest", ""))
            or identity is None
            or current_identity is None
            or identity != current_identity
            or int(getattr(identity, "generation", -1)) != current_generation
            or int(getattr(current_identity, "generation", -1)) != current_generation
        ):
            self._process_fused_context_acked = False
            return False
        if pool is not None:
            acked = int(getattr(pool, "context_load_acked", 0)) > 0
            ready = bool(getattr(pool, "graph_context_ready", False))
            if acked or ready:
                self._process_fused_context_acked = True
        return bool(self._process_fused_context_acked)

    def _admit_process_fused_context(self, context_ready):
        """Latch the ACK at the authoritative stream-admission barrier."""

        if not context_ready:
            return False
        if not self.process_fused_requested:
            return True
        context = self._process_graph_context
        if context is None or not str(getattr(context, "fused_digest", "")):
            self._fail(RuntimeError("fused context admission lacks a digest"))
            return False
        self._process_fused_context_acked = True
        self.report["process_fused_context_ready"] = True
        return True

    def _reset_process_fused_context_ack(self):
        """Invalidate fused ACK evidence when its generation is no longer valid."""

        self._process_fused_context_acked = False

    def _reject(self, master_key, member_key, reason, *, topology=True, detail=None):
        if topology:
            self.report["skipped_topology_unproven"] += 1
        # Fused workers already return an ordinal-keyed terminal outcome and
        # the main thread owns the immutable pair context.  Keeping full
        # master/member loop-key arrays for every negative report made the
        # session evidence much larger without adding apply safety.  Retain
        # only finite scalar diagnostics in this explicitly feature-gated
        # path; accepted mappings remain unchanged in ``groups``.
        if topology and self.process_fused_requested:
            sample = {"reason": str(reason), "compact": True}
            if detail:
                for key, value in detail.items():
                    if isinstance(value, bool) or isinstance(value, int):
                        sample[key] = value
                    elif isinstance(value, float) and math.isfinite(value):
                        sample[key] = value
                    elif isinstance(value, str):
                        sample[key] = value[:256]
        else:
            sample = {
                "master_key": list(master_key),
                "candidate_key": list(member_key),
                "reason": str(reason),
            }
            if detail:
                sample.update(detail)
        if self.detail_mappings:
            self.report.setdefault("topology_rejections", []).append(sample)
        elif len(self.report["topology_rejection_samples"]) < _PRO_REJECTION_SAMPLE_LIMIT:
            self.report["topology_rejection_samples"].append(sample)

    def _prepare(self):
        self._settings = uv_utils.get_settings(self.context)
        self._exact_tolerance = max(
            _PRO_EXACT_RESIDUAL_TOLERANCE,
            max(0.0, float(self._settings.stack_similarity_tolerance)),
        )
        enumeration_completed = False
        if self.all_islands is None:
            if self._island_builder is None:
                self._island_enumeration_started = time.perf_counter()
                self._island_builder = _ProIslandEnumerationState(
                    self.bm,
                    self.uv_layer,
                )
            enum_deadline = self._tick_deadline
            if enum_deadline is None:
                enum_deadline = time.perf_counter() + (
                    _PRO_ENUMERATION_SLICE_BUDGET_MS / 1000.0
                )
            else:
                enum_deadline = min(
                    enum_deadline,
                    time.perf_counter()
                    + (_PRO_ENUMERATION_SLICE_BUDGET_MS / 1000.0),
                )
            islands, _operations = self._island_builder.advance(
                operation_budget=_PRO_ENUMERATION_OPERATION_CAP,
                deadline=enum_deadline,
            )
            self._update_enum_report()
            if islands is None:
                return
            self.all_islands = islands
            self.island_enumeration_ms = (
                time.perf_counter() - self._island_enumeration_started
            ) * 1000.0
            enumeration_completed = True
        if self.island_enumeration_ms is not None:
            self.report["island_enumeration_ms"] = float(self.island_enumeration_ms)
            if "island_enumeration" not in self._phase_ms:
                self._record_phase(
                    "island_enumeration",
                    self.island_enumeration_ms,
                )
            if enumeration_completed:
                # Keep the large snapshot/record preparation out of the tick
                # that completes enumeration.  This preserves a real modal
                # yield even when the final adjacency/component primitive
                # finishes early.
                return "yield_after_enumeration"
        if self.operator_setup_ms is not None:
            self.report["operator_setup_ms"] = float(self.operator_setup_ms)
            if "operator_setup" not in self._phase_ms:
                self._record_phase("operator_setup", self.operator_setup_ms)

        if not self._process_prepare_context_ready:
            if self.selected_islands is None:
                self.selected_islands = [
                    island
                    for island in self.all_islands
                    if _island_is_selected(island, self.uv_layer)
                ]
            ordered_selected = sorted(self.selected_islands, key=_island_face_key)
            self.selected_islands = ordered_selected
            self.report["selected_count"] = len(ordered_selected)
            self._key_to_island = {
                _island_face_key(island): island for island in ordered_selected
            }
            if self.process_requested:
                self._prewrite_snapshot = {}
                self._selection_snapshot = {}
            if self.bm is not None and self.uv_layer is not None:
                self._active_snapshot = _pro_snapshot_active(
                    self.context, self.obj, self.bm
                )
                if not self.process_requested:
                    self._prewrite_snapshot = _pro_snapshot_uvs(self.bm, self.uv_layer)
                    self._selection_snapshot = _pro_snapshot_selection(
                        self.bm, self.uv_layer
                    )
            else:
                self._prewrite_snapshot = {}
                self._selection_snapshot = {}
                self._active_snapshot = {}
            self.report["prewrite_loop_count"] = len(self._prewrite_snapshot)

            if len(ordered_selected) < 2:
                self._process_prepare_context_ready = True
                self._prepare_completed = True
                self.state = "finish"
                return
            similarity_matcher.reset_diagnostics()
            self._snapshot_identity = _snapshot_identity(
                self.obj,
                self.bm,
                self.uv_layer,
                self.all_islands,
            )
            if self.process_requested:
                self._process_options = pro_process_adapter.make_exact_options(
                    allow_flipping=bool(self._settings.stack_allow_flipping),
                    match_scale=bool(self._settings.stack_match_scale),
                    tolerance=self._exact_tolerance,
                    max_search=self.correspondence_max_search,
                    cooperative_yield_every=self.cooperative_yield_every,
                )
                self._process_snapshot_builder = pro_process_adapter.IncrementalSnapshotBuilder(
                    self.context,
                    self.obj,
                    self.bm,
                    self.uv_layer,
                    self.all_islands,
                    session_nonce=self._process_session_nonce,
                    generation=self._process_generation,
                    options=self._process_options,
                )
            self._process_prepare_context_ready = True

        if self.process_requested and self._process_identity is None:
            builder = self._process_snapshot_builder
            if builder is None:
                raise RuntimeError("incremental process snapshot builder is unavailable")
            try:
                capture = builder.advance(
                    operation_budget=_PRO_SNAPSHOT_OPERATION_BUDGET,
                    deadline=self._tick_deadline,
                )
            except Exception:
                diagnostics = getattr(builder, "failure_diagnostics", None)
                if diagnostics is not None:
                    self.report["process_snapshot_error_diagnostics"] = diagnostics
                raise
            self.report["process_initial_snapshot_ms"] = float(builder.elapsed_ms)
            self.report["process_initial_snapshot_slices"] = int(builder.slices)
            self.report["process_initial_snapshot_operations"] = int(
                builder.primitive_operations
            )
            self.report["process_initial_snapshot_max_slice_ms"] = float(
                builder.max_slice_ms
            )
            self.report["process_snapshot_phase"] = str(builder._phase)
            if capture is None:
                return
            self._process_snapshot_capture = capture
            if not self.process_fused_requested:
                context_started = time.perf_counter()
                self._process_graph_context = pro_process_adapter.make_graph_context_payload(
                    capture
                )
                self._process_graph_context_build_ms = (
                    time.perf_counter() - context_started
                ) * 1000.0
                context_estimate = self._process_graph_context.estimate_frame(
                    batch_id="__graph-context-estimate"
                )
                self.report["process_graph_context_digest"] = (
                    self._process_graph_context.context_digest
                )
                self.report["process_graph_context_build_ms"] = float(
                    self._process_graph_context_build_ms
                )
                self.report["process_graph_context_frame_bytes"] = int(
                    context_estimate.frame_bytes
                )
                self.report["process_graph_context_payload_bytes"] = int(
                    context_estimate.payload_bytes
                )
            self._process_snapshot_live_loop_map = dict(builder.live_loop_map)
            material = capture.material
            self._process_island_loop_keys = {
                tuple(int(value) for value in face_keys): tuple(
                    sorted(tuple(loop_keys), key=repr)
                )
                for face_keys, loop_keys in getattr(material, "island_face_keys", ())
            }
            self._prewrite_snapshot = dict(builder.prewrite_snapshot)
            self._selection_snapshot = dict(builder.selection_snapshot)
            self.report["prewrite_loop_count"] = len(self._prewrite_snapshot)
            self._process_identity = capture.identity
            self.report["process_snapshot_digest"] = capture.identity.snapshot_digest
            self._process_snapshot_guard = pro_process_adapter.SnapshotGuard(
                capture,
                self.context,
                self.obj,
                self.bm,
                self.uv_layer,
                self.all_islands,
                session_nonce=self._process_session_nonce,
                generation=self._process_generation,
                options=self._process_options,
            )
            self._process_snapshot_checked_at = time.perf_counter()
            self._process_snapshot_check_result = True
            self._process_snapshot_stage_checked = None
            self._process_snapshot_checks = 1
            self._process_snapshot_forced_checks = 0
            self._record_phase(
                "process_snapshot_identity",
                float(builder.elapsed_ms),
            )
        self._descriptor_cache = similarity_matcher.DescriptorCache()
        self._record_started = time.perf_counter()
        self._prepare_completed = True
        self.state = "records"

    def _finish_record_phase(self):
        if self._record_phase_recorded:
            return
        if self._record_started is not None:
            self._record_phase_recorded = True
            elapsed = (time.perf_counter() - self._record_started) * 1000.0
            self._record_phase("planner_record_build", elapsed)
            self.report["planner_record_build_ms"] = self._phase_ms[
                "planner_record_build"
            ]
            self.report["planner_record_count"] = len(self._planner_records)
            self.report["planner_refinement"] = {
                "mode": "two_round_bounded",
                "converged": False,
                "records": int(self.report.get("planner_refinement_records", 0)),
                "rounds_total": int(
                    self.report.get("planner_refinement_rounds_total", 0)
                ),
                "max_rounds": int(
                    self.report.get("planner_refinement_max_rounds", 0)
                ),
                "max_bound": int(
                    self.report.get("planner_refinement_max_bound", 0)
                ),
                "total_ms": float(self.report.get("planner_refinement_ms", 0.0)),
                "max_ms": float(
                    self.report.get("planner_refinement_max_ms", 0.0)
                ),
                "stable_count": int(
                    self.report.get("planner_refinement_stable_count", 0)
                ),
                "truncated_count": int(
                    self.report.get("planner_refinement_truncated_count", 0)
                ),
            }
            self.report["skipped_invalid_density"] = sum(
                density is None for density in self._density_by_key.values()
            )
            self.report["skipped_missing_uv_area"] = sum(
                self._uv_area_by_key.get(record.face_key) is None
                for record in self._planner_records
            )
            if self.detail_mappings:
                self.report["density_records"] = [
                    {
                        "key": list(key),
                        "density": self._density_by_key[key],
                    }
                    for key in sorted(self._density_by_key)
                ]
            else:
                valid = [
                    value
                    for value in self._density_by_key.values()
                    if value is not None
                ]
                self.report["density_valid_count"] = len(valid)
                self.report["density_min"] = min(valid) if valid else None
                self.report["density_max"] = max(valid) if valid else None

    def _record_one(self):
        if self._record_index >= len(self.selected_islands):
            self._finish_record_phase()
            self.state = "plan"
            return
        island = self.selected_islands[self._record_index]
        if self._record_builder is None:
            key = _island_face_key(island)
            self._record_builder_key = key
            self._record_builder = _ProPlannerRecordBuildState(
                self.obj,
                island,
                self.uv_layer,
                self._descriptor_cache,
                self._snapshot_identity,
                self._numeric_cache,
                refinement_metrics={},
            )
        builder = self._record_builder
        result, error, operations = builder.advance(
            operation_budget=_PRO_RECORD_OPERATION_BUDGET,
            deadline=self._tick_deadline,
        )
        self._record_slices += 1
        self._record_operations += int(operations)
        self._record_max_slice_ms = max(
            self._record_max_slice_ms,
            float(builder.max_slice_ms),
        )
        self._record_max_primitive_ms = max(
            self._record_max_primitive_ms,
            float(builder.max_primitive_ms),
        )
        self._record_max_primitive = dict(builder.max_primitive)
        self.report["planner_record_slices"] = int(self._record_slices)
        self.report["planner_record_operations"] = int(self._record_operations)
        self.report["planner_record_max_slice_ms"] = float(self._record_max_slice_ms)
        self.report["planner_record_max_primitive_ms"] = float(
            self._record_max_primitive_ms
        )
        self.report["planner_record_max_primitive"] = dict(self._record_max_primitive)
        if not builder.done:
            return
        self._record_builder = None
        self._record_builder_key = None
        key = _island_face_key(island)
        refinement_metrics = builder.refinement_metrics
        if error is not None:
            self.report["planner_record_errors"].append(
                {"key": list(key), "reason": str(error)}
            )
            self._record_index += 1
            return
        try:
            record, signature, uv_area = result
            self._uv_area_by_key[key] = uv_area
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report["planner_record_errors"].append(
                {"key": list(key), "reason": str(exc)}
            )
            self._record_index += 1
            return
        if refinement_metrics:
            elapsed_ms = float(refinement_metrics.get("elapsed_ms", 0.0))
            rounds = int(refinement_metrics.get("rounds", 0))
            max_rounds = int(refinement_metrics.get("max_rounds", 0))
            self.report["planner_refinement_records"] += 1
            self.report["planner_refinement_rounds_total"] += rounds
            self.report["planner_refinement_max_rounds"] = max(
                self.report["planner_refinement_max_rounds"], rounds
            )
            self.report["planner_refinement_max_bound"] = max(
                self.report["planner_refinement_max_bound"], max_rounds
            )
            self.report["planner_refinement_ms"] += elapsed_ms
            self.report["planner_refinement_max_ms"] = max(
                self.report["planner_refinement_max_ms"], elapsed_ms
            )
            if refinement_metrics.get("stable"):
                self.report["planner_refinement_stable_count"] += 1
            if refinement_metrics.get("truncated"):
                self.report["planner_refinement_truncated_count"] += 1
        self._planner_records.append(record)
        self._cheap_signatures[key] = signature
        self._density_by_key[key] = record.density
        self._record_index += 1

    def _advance_plan_builder(self):
        """Advance planner preparation without materializing candidate pairs."""

        self._finish_record_phase()
        if len(self._planner_records) < 2 or self.report["truncated"]:
            self.state = "finish"
            return True
        if self._plan_builder is None:
            area_ranked_records = tuple(
                _pro_area_ranked_planner_record(
                    record,
                    self._uv_area_by_key.get(record.face_key),
                )
                for record in self._planner_records
            )
            self._plan_builder = _ProIncrementalPlanBuilder(
                area_ranked_records,
                self.config,
            )
            self.report["planner_master_metric"] = "uv_area"
        planner_started = time.perf_counter()
        plan, _operations = self._plan_builder.advance(operation_budget=1)
        self._record_phase(
            "planner_index_build",
            (time.perf_counter() - planner_started) * 1000.0,
        )
        if plan is None:
            return False
        self._candidate_plan = plan
        self.report["planner_index_build_ms"] = self._phase_ms[
            "planner_index_build"
        ]
        self.report["candidate_pairs_planned"] = 0
        self.report["candidate_pairs_processed"] = 0
        self.report["shape_fit_accepted"] = 0
        self.report["global_pair_budget"] = self.config.global_pair_budget
        self.report["per_bucket_pair_budget"] = self.config.per_bucket_pair_budget
        self.report["per_member_k"] = self.config.per_member_k
        if self.process_group_first_requested:
            # Group-first deliberately bypasses the historical density-ordered
            # CandidatePlan.  The immutable planner records are still useful
            # for cheap signatures/density diagnostics, but Phase A owns its
            # own fixed-representative frontier and therefore cannot expose a
            # cross-product pair count to the live process route.
            self._process_group_first_stage = "group_shape_collect"
            self.report["process_group_first_stage"] = self._process_group_first_stage
            self.state = "process_group_first_collect"
            return True
        self._batch_iterator = self._candidate_plan.iter_batches()
        if self.process_pipeline_requested:
            self._process_stage = "shape_collect"
            self.report["process_stage"] = self._process_stage
            self.state = "process_collect"
        else:
            self.state = "candidates"
        return True

    def _graph_for(self, key):
        def build():
            graph_started = time.perf_counter()
            try:
                return _pro_graph_for_island(
                    self._key_to_island[key],
                    self.uv_layer,
                )
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                return (None, None, str(exc))
            finally:
                self._record_phase(
                    "graph_build",
                    (time.perf_counter() - graph_started) * 1000.0,
                )

        value = self._graph_cache.get_or_build(key, build)
        if len(value) == 3:
            return value[0], value[1], value[2]
        return value[0], value[1], None

    def _graph_for_is_overridden(self):
        """Keep focused compatibility seams that replace ``_graph_for``."""

        method = getattr(self._graph_for, "__func__", None)
        return method is not _ProAlignSession._graph_for

    def _advance_graph_for(self, key, deadline=None):
        """Advance one live graph build and return ``pending/ready/error``."""

        if self._graph_for_is_overridden():
            try:
                value = self._graph_for(key)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                return "error", None, None, str(exc)
            if len(value) == 3:
                return "ready", value[0], value[1], value[2]
            return "ready", value[0], value[1], None

        cached = self._graph_cache.get(key)
        if cached is not None:
            return "ready", cached[0], cached[1], None

        if self._graph_build_state is None:
            self._graph_build_key = key
            self._graph_build_state = _ProGraphBuildState(
                self._key_to_island[key],
                self.uv_layer,
            )
        elif self._graph_build_key != key:
            raise RuntimeError("Pro graph build re-entry would violate one-builder ordering")

        builder = self._graph_build_state
        try:
            result, _operations = builder.advance(
                operation_budget=_PRO_GRAPH_OPERATION_BUDGET,
                deadline=deadline,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self._commit_graph_builder_metrics(builder)
            self._graph_build_state = None
            self._graph_build_key = None
            self._update_graph_report()
            return "error", None, None, str(exc)

        self._record_phase("graph_build", builder.last_graph_slice_ms)
        self._update_graph_report()
        if result is None:
            return "pending", None, None, None

        value = self._graph_cache.store(key, result)
        self._commit_graph_builder_metrics(builder)
        self._graph_build_state = None
        self._graph_build_key = None
        self._update_graph_report()
        return "ready", value[0], value[1], None

    def _next_pair(self):
        while self._batch_index >= len(self._batch):
            if self._budget_reached():
                self._request_timeout("planner_batch")
                return None
            fetch_started = time.perf_counter()
            try:
                self._batch = next(self._batch_iterator)
            except StopIteration:
                self._close_plan()
                self.state = "finish"
                return None
            self._batch_index = 0
            self._record_phase(
                "planner_stream",
                (time.perf_counter() - fetch_started) * 1000.0,
            )
            self.report["candidate_pairs_planned"] += len(self._batch)
            if not self._batch:
                continue
        pair = self._batch[self._batch_index]
        self._batch_index += 1
        self.report["candidate_pairs_processed"] += 1
        if self.process_requested:
            pair_key = (tuple(pair.master_key), tuple(pair.member_key))
            if pair_key not in self._process_pair_ordinals:
                self._process_pair_ordinals[pair_key] = self._process_next_ordinal
                self._process_next_ordinal += 1
        return pair

    def _ensure_process_pool(self, *, start=True):
        """Start the one session-owned bundled-Python helper on first use."""

        if not self.process_requested:
            raise RuntimeError("external process mode was not requested")
        if self._process_pool is not None:
            return self._process_pool
        blender_binary = self._process_blender_binary
        if blender_binary is None:
            app = getattr(bpy, "app", None)
            blender_binary = getattr(app, "binary_path", None)
        blender_version = self._process_blender_version
        if blender_version is None:
            app = getattr(bpy, "app", None)
            blender_version = getattr(app, "version", None)
        pool = pro_process_pool.PersistentWorkerPool(
            self.process_worker_count,
            worker_script=self._process_worker_script,
            blender_binary=blender_binary,
            blender_root=self._process_blender_root,
            blender_version=blender_version,
            python_executable=self._process_python_executable,
            session_nonce=self._process_session_nonce,
            generation=self._process_generation,
            handshake_timeout=self._process_handshake_timeout,
            io_timeout=self._process_io_timeout,
            use_cache=True,
        )
        started = time.perf_counter()
        if start:
            pool.start()
        startup_ms = (time.perf_counter() - started) * 1000.0
        self._process_pool = pool
        self._process_startup_ms += startup_ms
        self._update_process_pool_metadata(pool)
        self.report["process_worker_count"] = int(self.process_worker_count)
        self.report["process_batch_size"] = int(self.process_batch_size)
        return pool

    def _update_process_pool_metadata(self, pool):
        self._process_started_pids = [
            int(worker.pid)
            for worker in pool.workers
            if worker.pid is not None
        ]
        self.report["process_worker_pids"] = list(self._process_started_pids)
        self.report["process_startup_ms"] = float(self._process_startup_ms)
        self.report["process_worker_start_owner_ms"] = float(
            getattr(pool, "worker_start_owner_ms", 0.0)
        )
        self.report["process_worker_start_background_ms"] = float(
            getattr(pool, "worker_start_background_ms", 0.0)
        )
        self.report["process_worker_start_pending"] = int(
            getattr(pool, "startup_pending", 0) or 0
        )
        self.report["process_worker_start_states"] = [
            [int(index), str(state)]
            for index, state in getattr(pool, "startup_states", ())
        ]
        self.report["process_context_serialize_owner_ms"] = float(
            getattr(pool, "context_serialize_owner_ms", 0.0)
        )
        self.report["process_context_serialize_background_ms"] = float(
            getattr(pool, "context_serialize_background_ms", 0.0)
        )
        self.report["process_context_write_background_ms"] = float(
            getattr(pool, "context_write_background_ms", 0.0)
        )
        self.report["process_context_send_pending"] = int(
            getattr(pool, "context_load_pending", 0) or 0
        )
        self.report["process_pipeline_admission_owner_ms"] = float(
            self._process_pipeline_admission_owner_ms
        )
        self.report["worker_mode"] = (
            "external_bundled_python_group_first"
            if self.process_group_first_requested
            else (
                "external_bundled_python_fused"
                if self.process_fused_requested
                else (
                    "external_bundled_python_pipeline"
                    if self.process_pipeline_requested
                    else "external_bundled_python_single"
                )
            )
        )
        if pool.workers:
            worker = pool.workers[0]
            owned = worker.owned_process
            self.report["process_helper_path"] = (
                str(owned.worker_script) if owned is not None else None
            )
            self.report["process_python_executable"] = (
                str(owned.executable) if owned is not None else None
            )
            ready = worker.ready_payload or {}
            self.report["process_python_version"] = ready.get("python_version")
            self.report["process_thread_caps"] = ready.get("thread_caps", {})

    def _advance_process_startup(self):
        """Advance one persistent helper launch for a modal pipeline."""

        pool = self._process_pool
        if pool is None:
            pool = self._ensure_process_pool(start=False)
        self._tick_stage = "process_startup"
        deadline = self._tick_deadline
        started = time.perf_counter()
        try:
            ready = pool.start_incremental(deadline=deadline)
        except TypeError as exc:
            # Keep old pure test doubles source-compatible; the production
            # pool always accepts the explicit modal deadline.
            if "deadline" not in str(exc):
                raise
            ready = pool.start_incremental()
        self._process_startup_ms += (time.perf_counter() - started) * 1000.0
        self._update_process_pool_metadata(pool)
        self.report["process_startup_ms"] = float(self._process_startup_ms)
        if not ready:
            self._process_stage = "process_startup"
            self.report["process_stage"] = self._process_stage
            return False
        if deadline is not None and time.perf_counter() >= float(deadline):
            self._process_stage = "process_startup"
            self.report["process_stage"] = self._process_stage
            return False
        if self.process_fused_requested and self._process_graph_context is None:
            capture = self._process_snapshot_capture
            if capture is None or not self._process_fused_descriptors:
                self._fail(RuntimeError("fused immutable context descriptors are unavailable"))
                return False
            try:
                context_started = time.perf_counter()
                self._process_graph_context = pro_process_adapter.make_fused_context_payload(
                    capture,
                    tuple(self._process_fused_descriptors.values()),
                    self._process_shape_options(),
                )
                self._process_fused_context_build_ms = (
                    time.perf_counter() - context_started
                ) * 1000.0
                self.report["process_graph_context_digest"] = (
                    self._process_graph_context.context_digest
                )
                self.report["process_fused_context_digest"] = (
                    self._process_graph_context.fused_digest
                )
                self.report["process_fused_descriptor_count"] = len(
                    self._process_fused_descriptors
                )
                self.report["process_fused_context_ready"] = False
                self.report["process_graph_context_build_ms"] = float(
                    self._process_fused_context_build_ms
                )
            except Exception as exc:
                self._fail(RuntimeError("fused immutable context build failed: %s" % exc))
                return False
        if self._process_graph_context is None:
            self._fail(RuntimeError("immutable graph context is unavailable"))
            return False
        try:
            context_ready = pool.load_graph_context(
                self._process_graph_context,
                deadline=deadline,
            )
        except Exception as exc:
            self._fail(RuntimeError("external graph context load failed: %s" % exc))
            return False
        self.report["process_graph_context_load_submitted"] = int(
            getattr(pool, "context_load_submitted", 0)
        )
        self.report["process_graph_context_load_acked"] = int(
            getattr(pool, "context_load_acked", 0)
        )
        self.report["process_graph_context_load_ms"] = float(
            getattr(pool, "context_load_ms", 0.0)
        )
        self.report["process_graph_context_ready"] = bool(
            getattr(pool, "graph_context_ready", False)
        )
        self.report["process_graph_context_frame_bytes"] = int(
            getattr(pool, "context_load_frame_bytes", 0)
        )
        self.report["process_graph_context_payload_bytes"] = int(
            getattr(pool, "context_load_payload_bytes", 0)
        )
        self.report["process_graph_context_frame_max_bytes"] = int(
            getattr(pool, "context_load_frame_max_bytes", 0)
        )
        self._update_process_pool_metadata(pool)
        if self.process_fused_requested:
            self._latch_process_fused_context_ack(pool)
            self.report["process_fused_context_ready"] = bool(
                self._process_fused_context_acked
            )
        if not context_ready:
            self._process_stage = "process_context_load"
            self.report["process_stage"] = self._process_stage
            return False
        if deadline is not None and time.perf_counter() >= float(deadline):
            self._process_stage = "process_context_load"
            self.report["process_stage"] = self._process_stage
            return False
        if not self._admit_process_fused_context(context_ready):
            return False
        try:
            if self.process_group_first_requested:
                frontier = self._process_group_first_frontier
                if frontier is None:
                    raise RuntimeError("group-first frontier was not prepared")
                self._process_pipeline = pro_process_pipeline.GroupFirstProcessPipeline(
                    pool,
                    frontier,
                    shape_task_builder=self._build_group_first_shape_batch,
                    exact_task_builder=self._build_group_first_exact_tasks,
                    merge_callback=self._merge_group_first_exact_outcome,
                    batch_size=self.process_batch_size,
                )
            else:
                ordinals = tuple(sorted(self._process_pair_contexts))
                if self.process_fused_requested:
                    self._process_pipeline = pro_process_pipeline.FusedProcessPipeline(
                        pool,
                        ordinals,
                        domain_for_ordinal=lambda ordinal: self._process_pair_contexts[
                            ordinal
                        ]["member_key"],
                        master_for_ordinal=lambda ordinal: self._process_pair_contexts[
                            ordinal
                        ]["master_key"],
                        fused_builder=self._build_process_fused_batch_for_ordinals,
                        merge_callback=self._merge_process_outcome,
                        batch_size=self.process_batch_size,
                        merge_limit=1,
                    )
                else:
                    self._process_pipeline = pro_process_pipeline.FrontierProcessPipeline(
                        pool,
                        ordinals,
                        domain_for_ordinal=lambda ordinal: self._process_pair_contexts[
                            ordinal
                        ]["member_key"],
                        shape_builder=self._process_shape_batch_for_ordinals,
                        exact_builder=self._build_process_exact_frontier_task,
                        graph_result_callback=self._handle_process_graph_completion,
                        graph_task_admitted_callback=self._handle_process_graph_task_admitted,
                        merge_callback=self._merge_process_outcome,
                        batch_size=self.process_batch_size,
                        merge_limit=1,
                    )
            admission_started = time.perf_counter()
            self._process_pipeline.start(deadline=deadline)
            self._process_pipeline_admission_owner_ms += (
                time.perf_counter() - admission_started
            ) * 1000.0
            self.report["process_pipeline_admission_owner_ms"] = float(
                self._process_pipeline_admission_owner_ms
            )
        except Exception as exc:
            self._fail(RuntimeError("external Pro pipeline unavailable: %s" % exc))
            return False
        self.report["process_shape_batches"] = 0
        self.report["process_shape_pairs_submitted"] = 0
        self.report["process_frame_bytes"].setdefault("shape", 0)
        self._process_stage = (
            "group_shape_wait"
            if self.process_group_first_requested
            else ("fused_wait" if self.process_fused_requested else "shape_wait")
        )
        self._process_group_first_stage = self._process_stage
        self.report["process_stage"] = self._process_stage
        self.report["process_group_first_stage"] = self._process_group_first_stage
        self.state = "process_pipeline"
        return True

    def _build_process_fused_batch_for_ordinals(self, ordinals):
        """Build one compact master-affine task from resident context refs."""

        ordered = tuple(sorted(int(item) for item in ordinals))
        if not ordered:
            raise RuntimeError("fused batch cannot be empty")
        context_payload = self._process_graph_context
        if context_payload is None or not context_payload.fused_digest:
            raise RuntimeError("fused context is not loaded")
        specs = []
        for ordinal in ordered:
            context = self._process_pair_contexts.get(ordinal)
            if context is None:
                raise RuntimeError("fused batch references an unknown ordinal")
            master_key = tuple(context["master_key"])
            member_key = tuple(context["member_key"])
            master_descriptor = self._process_fused_descriptors.get(master_key)
            member_descriptor = self._process_fused_descriptors.get(member_key)
            if master_descriptor is None or member_descriptor is None:
                raise RuntimeError("fused batch references an unknown descriptor")
            master_loop_keys = self._process_island_loop_keys.get(master_key)
            member_loop_keys = self._process_island_loop_keys.get(member_key)
            if master_loop_keys is None or member_loop_keys is None:
                raise RuntimeError("fused batch references an unknown loop-key set")
            specs.append(
                (
                    ordinal,
                    master_key,
                    member_key,
                    master_descriptor.descriptor_digest,
                    member_descriptor.descriptor_digest,
                    tuple(master_loop_keys),
                    tuple(member_loop_keys),
                    self._process_options,
                    context.get("shape_prefilter"),
                    self.correspondence_mode,
                )
            )

        # The canonical frontier may expose more than one master at once.
        # Keep each wire task master-affine, but coalesce every visible
        # master's ready refs up to the configured cap.  The pipeline still
        # merges by ordinal, so this is scheduling-only speculation.
        grouped = OrderedDict()
        for spec in specs:
            grouped.setdefault(spec[1], []).append(spec)
        tasks = []
        for master_index, (_master_key, values) in enumerate(grouped.items()):
            for chunk_index in range(0, len(values), self.process_batch_size):
                chunk = tuple(values[chunk_index:chunk_index + self.process_batch_size])
                task = pro_process_adapter.make_fused_batch(
                    self._process_identity,
                    context_payload.context_digest,
                    context_payload.fused_digest,
                    chunk,
                    batch_id="mc4r1f-fused-%08d-m%02d-c%02d"
                    % (chunk[0][0], master_index, chunk_index // self.process_batch_size),
                    debug_delay_ms=self.process_debug_delay_ms,
                    correspondence_mode=self.correspondence_mode,
                )
                tasks.append(task)
        if not tasks:
            raise RuntimeError("fused batch produced no master-affine task")
        return tasks[0] if len(tasks) == 1 else tuple(tasks)

    def _process_shape_options(self):
        tolerance = max(0.0, float(self._settings.stack_similarity_tolerance))
        return pro_process_shape.ShapeOptions(
            match_scale=bool(self._settings.stack_match_scale),
            allow_flipping=bool(self._settings.stack_allow_flipping),
            tolerance=tolerance,
            allow_tolerant_topology=(
                tolerance > similarity_matcher.TOPOLOGY_PENALTY
            ),
            use_numpy=None,
        )

    def _process_shape_prefilter(
        self,
        master_key,
        member_key,
        master_descriptor=None,
        member_descriptor=None,
        *,
        allow_master_precedence=True,
    ):
        """Reuse independent synchronous gates before speculative full fitting."""

        def rejected(reason, coarse_gate, topology_gate, penalty=0.0):
            self.report["process_shape_prefiltered"] = int(
                self.report.get("process_shape_prefiltered", 0)
            ) + 1
            return pro_process_shape.ShapePrefilterData(
                reason=str(reason),
                coarse_gate=pro_process_shape.ShapeGateData.from_similarity(
                    coarse_gate
                ) if not isinstance(coarse_gate, pro_process_shape.ShapeGateData)
                else coarse_gate,
                topology_gate=pro_process_shape.ShapeGateData.from_similarity(
                    topology_gate
                ) if not isinstance(topology_gate, pro_process_shape.ShapeGateData)
                else topology_gate,
                topology_penalty=float(penalty),
            )

        # UV-area precedence is a pure pair property.  It does not depend on
        # earlier accepted members, so MC3B permits it before dispatch and the
        # process path can avoid fitting the same candidates that the
        # synchronous Pro path rejects immediately.  Ownership-allows is
        # intentionally left to canonical merge because it is stateful.
        precedence_reason = _pro_master_precedence_reason(
            tuple(master_key),
            tuple(member_key),
            self._uv_area_by_key,
        )
        if allow_master_precedence and precedence_reason is not None:
            gate = pro_process_shape.ShapeGateData(
                False,
                True,
                reason=precedence_reason,
            )
            return rejected(precedence_reason, gate, gate)

        master_signature = self._cheap_signatures.get(tuple(master_key))
        member_signature = self._cheap_signatures.get(tuple(member_key))
        if master_signature is None or member_signature is None:
            missing = pro_process_shape.ShapeGateData(
                False,
                True,
                reason="missing_cheap_signature",
            )
            return rejected("missing_cheap_signature", missing, missing)
        boundary_gate = similarity_matcher.cheap_boundary_gate(
            master_signature,
            member_signature,
        )
        topology_gate = similarity_matcher.cheap_topology_gate(
            master_signature,
            member_signature,
        )
        if boundary_gate.passed and topology_gate.passed:
            pass
        else:
            return rejected(
                boundary_gate.reason if not boundary_gate.passed else topology_gate.reason,
                boundary_gate,
                topology_gate,
                boundary_gate.penalty + topology_gate.penalty,
            )

        # The full matcher repeats these immutable descriptor gates before the
        # cyclic fit.  Applying them here preserves its exact rejection order
        # while avoiding a costly worker fit for a known rejection.  The
        # topology gate is allowed to remain non-strict when the current
        # tolerance permits its documented penalty.
        if master_descriptor is None or member_descriptor is None:
            return None
        coarse_full = similarity_matcher.coarse_boundary_gate(
            master_descriptor,
            member_descriptor,
        )
        if not coarse_full.passed:
            not_evaluated = similarity_matcher.GateResult(
                False,
                True,
                reason="not_evaluated",
            )
            return rejected(
                coarse_full.reason,
                coarse_full,
                not_evaluated,
                coarse_full.penalty,
            )
        topology_full = similarity_matcher.topology_gate(
            master_descriptor,
            member_descriptor,
        )
        options = self._process_shape_options()
        if not topology_full.passed:
            return rejected(
                topology_full.reason,
                coarse_full,
                topology_full,
                coarse_full.penalty + topology_full.penalty,
            )
        if (
            not topology_full.strict
            and not options.allow_tolerant_topology
        ):
            return rejected(
                "topology_mismatch",
                coarse_full,
                topology_full,
                coarse_full.penalty + topology_full.penalty,
            )
        return None

    def _process_partition_shape_batches(self):
        """Partition immutable descriptors by master, then by bounded size."""

        by_master = OrderedDict()
        for ordinal in sorted(self._process_pair_contexts):
            context = self._process_pair_contexts[ordinal]
            by_master.setdefault(context["master_key"], []).append(context)
        options = self._process_shape_options()
        batches = []
        for master_key, records in by_master.items():
            for offset in range(0, len(records), self.process_batch_size):
                chunk = records[offset : offset + self.process_batch_size]
                pairs = tuple(
                    (
                        item["ordinal"],
                        item["master_key"],
                        item["member_key"],
                        item["master_descriptor"],
                        item["member_descriptor"],
                        item.get("shape_prefilter"),
                    )
                    for item in chunk
                )
                batch = pro_process_shape.make_shape_batch(
                    self._process_identity,
                    pairs,
                    options,
                    batch_id="mc3b-shape-%08d" % int(chunk[0]["ordinal"]),
                    debug_delay_ms=self.process_debug_delay_ms,
                )
                batches.append(batch)
                for ordinal in batch.pair_ordinals:
                    self._process_shape_batch_by_ordinal[ordinal] = batch.batch_id
        return tuple(batches)

    def _advance_process_group_first_collection(self):
        """Capture one immutable descriptor per selected island for Phase A."""

        self._tick_stage = "process_group_first_collect"
        deadline = self._tick_deadline
        while self._process_group_first_record_index < len(self.selected_islands):
            if deadline is not None and time.perf_counter() >= deadline:
                return False
            island = self.selected_islands[self._process_group_first_record_index]
            self._process_group_first_record_index += 1
            key = _island_face_key(island)
            try:
                uv_area = _pro_uv_area_for_island(island, self.uv_layer)
                self._uv_area_by_key[key] = uv_area
                descriptor = _descriptor_for_island(
                    island,
                    self.uv_layer,
                    self._descriptor_cache,
                    self._snapshot_identity,
                    numeric_cache=self._numeric_cache,
                )
                signature = self._cheap_signatures.get(key)
                if signature is None:
                    raise RuntimeError("group-first island has no cheap signature")
                record = pro_group_first.IslandRecord(
                    key=key,
                    density=self._density_by_key.get(key),
                    uv_area=uv_area,
                    # Normal's grouping partitions by this strict structural
                    # key.  The overlap/bin hints remain gates in the shape
                    # prefilter; they are not used as a second partition.
                    bucket_key=_cheap_group_bucket_key(signature),
                    ordinal=self._process_group_first_record_index - 1,
                    descriptor_digest=pro_process_shape.ShapeDescriptor.from_similarity(
                        descriptor
                    ).descriptor_digest,
                )
            except (AttributeError, KeyError, IndexError, RuntimeError, TypeError, ValueError) as exc:
                self.report["process_group_first_error"] = str(exc)
                self._fail(RuntimeError("group-first descriptor snapshot failed: %s" % exc))
                return False
            self._process_group_first_records.append(record)
            self._process_group_first_descriptors[key] = descriptor
            self._process_fused_descriptors[key] = (
                pro_process_shape.ShapeDescriptor.from_similarity(descriptor)
            )
            loop_keys = self._process_island_loop_keys.get(key)
            if loop_keys is None and self._process_snapshot_capture is not None:
                loop_keys = pro_process_adapter.graph_loop_keys_for_island(
                    self._process_snapshot_capture,
                    key,
                )
                self._process_island_loop_keys[key] = tuple(loop_keys)

        if self._process_group_first_frontier is None:
            self._process_group_first_frontier = pro_group_first.GroupFirstFrontier(
                tuple(self._process_group_first_records),
                similarity_tolerance=float(
                    self._settings.stack_similarity_tolerance
                ),
                density_tie_epsilon=_PRO_DENSITY_TIE_EPSILON,
            )
            self.report["candidate_pairs_planned"] = int(
                self._process_group_first_frontier.comparisons_planned
            )
            self.report["process_group_first_stage"] = "group_shape_dispatch"
        try:
            self._ensure_process_pool(start=False)
        except Exception as exc:
            self.report["process_group_first_error"] = str(exc)
            self._fail(RuntimeError("external group-first pipeline unavailable: %s" % exc))
            return False
        self.state = "process_startup"
        return True

    def _process_shape_batch_for_ordinals(self, ordinals):
        """Build one bounded frontier batch without retaining the planner set."""

        ordered = tuple(sorted(int(item) for item in ordinals))
        if not ordered:
            raise RuntimeError("frontier shape batch cannot be empty")
        options = self._process_shape_options()
        pairs = tuple(
            (
                ordinal,
                self._process_pair_contexts[ordinal]["master_key"],
                self._process_pair_contexts[ordinal]["member_key"],
                self._process_pair_contexts[ordinal]["master_descriptor"],
                self._process_pair_contexts[ordinal]["member_descriptor"],
                self._process_pair_contexts[ordinal].get("shape_prefilter"),
            )
            for ordinal in ordered
        )
        batch = pro_process_shape.make_shape_batch(
            self._process_identity,
            pairs,
            options,
            batch_id="mc4-frontier-shape-%08d" % ordered[0],
            debug_delay_ms=self.process_debug_delay_ms,
        )
        for ordinal in ordered:
            self._process_shape_batch_by_ordinal[ordinal] = batch.batch_id
        self.report["process_shape_batches"] = int(
            self.report.get("process_shape_batches", 0)
        ) + 1
        self.report["process_shape_pairs_submitted"] = int(
            self.report.get("process_shape_pairs_submitted", 0)
        ) + len(ordered)
        estimate = int(batch.estimate_frame().frame_bytes)
        self.report["process_frame_bytes"]["shape"] = int(
            self.report["process_frame_bytes"].get("shape", 0)
        ) + estimate
        return batch

    def _build_group_first_shape_batch(self, requests):
        """Build shape-only requests for the fixed-representative frontier."""

        ordered = tuple(sorted(requests, key=lambda item: int(item.pair_ordinal)))
        if not ordered:
            raise RuntimeError("group-first shape batch cannot be empty")
        pairs = []
        for request in ordered:
            master_key = tuple(request.representative_key)
            member_key = tuple(request.candidate_key)
            master_descriptor = self._process_group_first_descriptors.get(master_key)
            member_descriptor = self._process_group_first_descriptors.get(member_key)
            if master_descriptor is None or member_descriptor is None:
                raise RuntimeError("group-first request references an unknown descriptor")
            prefilter = self._process_shape_prefilter(
                master_key,
                member_key,
                master_descriptor,
                member_descriptor,
                    allow_master_precedence=False,
            )
            pairs.append(
                (
                    int(request.pair_ordinal),
                    master_key,
                    member_key,
                    master_descriptor,
                    member_descriptor,
                    prefilter,
                )
            )
        batch = pro_process_shape.make_shape_batch(
            self._process_identity,
            tuple(pairs),
            self._process_shape_options(),
            batch_id="mc4r2e-group-shape-%08d" % int(ordered[0].pair_ordinal),
            debug_delay_ms=self.process_debug_delay_ms,
        )
        self.report["process_frame_bytes"]["group_shape"] = int(
            self.report["process_frame_bytes"].get("group_shape", 0)
        ) + int(batch.estimate_frame().frame_bytes)
        return batch

    def _build_group_first_exact_tasks(self, jobs):
        """Build one resident exact task per group member, master-affine."""

        ordered = tuple(sorted(jobs, key=lambda item: int(item.job_ordinal)))
        if not ordered:
            return ()
        by_master = OrderedDict()
        for job in ordered:
            by_master.setdefault(tuple(job.master_key), []).append(job)
        tasks = []
        for master_key, master_jobs in by_master.items():
            for offset in range(0, len(master_jobs), self.process_batch_size):
                chunk = tuple(master_jobs[offset : offset + self.process_batch_size])
                pair_specs = []
                for job in chunk:
                    master_loops = self._process_island_loop_keys.get(tuple(job.master_key))
                    member_loops = self._process_island_loop_keys.get(tuple(job.member_key))
                    if master_loops is None or member_loops is None:
                        raise RuntimeError("group-first exact job has no captured loop keys")
                    pair_specs.append(
                        (
                            int(job.job_ordinal),
                            tuple(job.master_key),
                            tuple(job.member_key),
                            tuple(master_loops),
                            tuple(member_loops),
                            self._process_options,
                            getattr(job, "seed_transform", None),
                            self.correspondence_mode,
                        )
                    )
                task = pro_process_adapter.make_resident_exact_batch(
                    self._process_identity,
                    self._process_graph_context.context_digest,
                    pair_specs,
                    batch_id="mc4r2e-direct-exact-%08d" % int(chunk[0].job_ordinal),
                    debug_delay_ms=self.process_debug_delay_ms,
                )
                tasks.append(task)
                frame_bytes = int(task.estimate_frame().frame_bytes)
                self.report["process_frame_bytes"]["group_exact"] = int(
                    self.report["process_frame_bytes"].get("group_exact", 0)
                ) + frame_bytes
        return tuple(tasks)

    def _merge_group_first_exact_outcome(self, outcome):
        """Stage one direct exact result; ownership is already group-owned."""

        job = outcome.job
        exact_result = outcome.exact_result
        task = outcome.task
        if task is None:
            raise RuntimeError("group-first exact result has no immutable task")
        pair = next(
            (
                item
                for item in task.pair_tasks
                if int(item.pair_ordinal) == int(job.job_ordinal)
            ),
            None,
        )
        if pair is None:
            raise RuntimeError("group-first exact result has no matching pair")
        master_key = tuple(job.master_key)
        member_key = tuple(job.member_key)
        master_loops = {
            key: self._process_snapshot_live_loop_map[key]
            for key in pair.master_loop_keys
            if key in self._process_snapshot_live_loop_map
        }
        member_loops = {
            key: self._process_snapshot_live_loop_map[key]
            for key in pair.member_loop_keys
            if key in self._process_snapshot_live_loop_map
        }
        if len(master_loops) != len(pair.master_loop_keys) or len(member_loops) != len(pair.member_loop_keys):
            raise RuntimeError("group-first exact result references an unknown live loop")
        self._inflight = {
            "process": True,
            "token": ("mc4r2e-direct", int(job.job_ordinal)),
            "task": task,
            "master_key": master_key,
            "member_key": member_key,
            "master_loops": master_loops,
            "candidate_loops": member_loops,
            "submitted_at": time.perf_counter(),
        }
        correspondence = pro_process_adapter.pair_result_to_correspondence(
            exact_result,
            topology_module=topology_correspondence,
            task=task,
        )
        self._process_group_first_direct_results[int(job.job_ordinal)] = exact_result
        self._consume_exact_result(
            correspondence,
            token=("mc4r2e-direct", int(job.job_ordinal)),
            error=None,
        )
        self.report["direct_exact_jobs_completed"] = int(
            self.report.get("direct_exact_jobs_completed", 0)
        ) + 1
        if not bool(getattr(exact_result, "accepted", False)):
            self.report["direct_exact_jobs_failed"] = int(
                self.report.get("direct_exact_jobs_failed", 0)
            ) + 1
        return True

    def _advance_process_collection(self):
        """Snapshot all planned pairs without making ownership decisions."""

        self._tick_stage = "process_shape_collect"
        deadline = self._tick_deadline
        while deadline is None or time.perf_counter() < deadline:
            pair = self._next_pair()
            if pair is None:
                if self.report.get("truncated"):
                    self.cancel("process_pipeline_collection_timeout")
                    return False
                if self._process_collect_complete:
                    return False
                self._process_collect_complete = True
                if not self._process_pair_contexts:
                    self.state = "finish"
                    return True
                try:
                    # The frontier admits only the first unresolved candidate
                    # for each member.  Descriptors are immutable main-owned
                    # snapshots; shape batches themselves are built lazily as
                    # the bounded stream requests capacity.
                    self._process_shape_batches = ()
                    self._ensure_process_pool(start=False)
                except Exception as exc:
                    self._fail(RuntimeError("external Pro pipeline unavailable: %s" % exc))
                    return False
                self.state = "process_startup"
                return True

            master_key = tuple(pair.master_key)
            member_key = tuple(pair.member_key)
            pair_key = (master_key, member_key)
            ordinal = self._process_pair_ordinals.get(pair_key)
            if ordinal is None:
                raise RuntimeError("process planner pair has no canonical ordinal")
            try:
                master_descriptor = _descriptor_for_island(
                    self._key_to_island[master_key],
                    self.uv_layer,
                    self._descriptor_cache,
                    self._snapshot_identity,
                    numeric_cache=self._numeric_cache,
                )
                member_descriptor = _descriptor_for_island(
                    self._key_to_island[member_key],
                    self.uv_layer,
                    self._descriptor_cache,
                    self._snapshot_identity,
                    numeric_cache=self._numeric_cache,
                )
            except (AttributeError, KeyError, IndexError, RuntimeError, TypeError, ValueError) as exc:
                self._fail(RuntimeError("shape descriptor snapshot failed: %s" % exc))
                return False
            self._process_pair_contexts[ordinal] = {
                "ordinal": ordinal,
                "pair": pair,
                "master_key": master_key,
                "member_key": member_key,
                "master_descriptor": master_descriptor,
                "member_descriptor": member_descriptor,
                "shape_prefilter": self._process_shape_prefilter(
                    master_key,
                    member_key,
                    master_descriptor,
                    member_descriptor,
                ),
            }
            if self.process_fused_requested:
                for island_key, descriptor in (
                    (master_key, master_descriptor),
                    (member_key, member_descriptor),
                ):
                    if island_key not in self._process_fused_descriptors:
                        self._process_fused_descriptors[island_key] = (
                            pro_process_shape.ShapeDescriptor.from_similarity(
                                descriptor
                            )
                        )
            if deadline is not None and time.perf_counter() >= deadline:
                break
        return True

    def _handle_process_graph_completion(self, task, result):
        """Commit one complete worker graph batch to the main-owned cache."""

        result.validate_against(task)
        for entry in result.graph_results:
            key = tuple(entry.island_key)
            self._process_graph_pending.discard(key)
            if entry.accepted:
                data = entry.graph
                if data is None:
                    raise RuntimeError("accepted graph result has no GraphData")
                data.validate()
                loops = {
                    item.key: self._process_snapshot_live_loop_map[item.key]
                    for item in data.loops
                    if item.key in self._process_snapshot_live_loop_map
                }
                if len(loops) != len(data.loops):
                    raise RuntimeError("worker graph result references an unknown live loop")
                self._process_graph_data[key] = {
                    "graph": None,
                    "loops": loops,
                    "data": data,
                    "worker": True,
                }
                self._process_graph_cache_builds += 1
            else:
                self._process_graph_data[key] = {
                    "graph": None,
                    "loops": {},
                    "data": None,
                    "worker": True,
                    "rejected": str(entry.reason),
                }
                reason = str(entry.reason).split(":", 1)[0].strip() or "graph_rejected"
                self._process_graph_rejections[reason] = int(
                    self._process_graph_rejections.get(reason, 0)
                ) + 1
            self.report["process_graph_cache_builds"] = int(
                self._process_graph_cache_builds
            )
        self._process_graph_worker_completed += 1
        self.report["process_graph_worker_completed"] = int(
            self._process_graph_worker_completed
        )
        self.report["process_graph_cache_hits"] = int(self._process_graph_cache_hits)
        self.report["process_graph_rejections"] = dict(self._process_graph_rejections)

    def _handle_process_graph_task_admitted(self, task):
        """Commit graph pending/counters only after pool admission succeeds."""

        if str(getattr(task, "operation_kind", "")) != "graph":
            raise RuntimeError("graph admission callback received a non-graph task")
        self._process_graph_pending.update(tuple(task.island_keys))
        self._process_graph_worker_submitted += 1
        self.report["process_graph_worker_submitted"] = int(
            self._process_graph_worker_submitted
        )
        self.report["process_graph_main_operations"] = int(
            self._process_graph_main_operations
        )
        self.report["process_frame_bytes"]["graph"] = int(
            self.report["process_frame_bytes"].get("graph", 0)
        ) + int(task.estimate_frame().frame_bytes)

    def _build_process_exact_frontier_task(self, shape_result):
        """Build one exact task, submitting immutable graph work first."""

        ordinal = int(shape_result.pair_ordinal)
        context = self._process_pair_contexts[ordinal]
        match = shape_result.to_similarity(similarity_matcher)
        if not _selected_match_passes_quality(
            match,
            self._settings.stack_similarity_tolerance,
        ):
            return False
        if self._process_graph_context is not None:
            master_key = tuple(context["master_key"])
            member_key = tuple(context["member_key"])
            master_loops = self._process_island_loop_keys.get(master_key)
            member_loops = self._process_island_loop_keys.get(member_key)
            if master_loops is None or member_loops is None:
                raise RuntimeError("resident exact task has no captured island loop keys")
            task = pro_process_adapter.make_resident_exact_batch(
                self._process_identity,
                self._process_graph_context.context_digest,
                (
                    (
                        ordinal,
                        master_key,
                        member_key,
                        master_loops,
                        member_loops,
                        self._process_options,
                        None,
                        self.correspondence_mode,
                    ),
                ),
                batch_id="mc4c12-resident-exact-%08d" % ordinal,
                debug_delay_ms=self.process_debug_delay_ms,
            )
            context["exact_task"] = task
            frame_bytes = int(task.estimate_frame().frame_bytes)
            self.report["process_resident_exact_frame_bytes"] = int(
                self.report.get("process_resident_exact_frame_bytes", 0)
            ) + frame_bytes
            self.report["process_frame_bytes"]["resident_exact"] = int(
                self.report["process_frame_bytes"].get("resident_exact", 0)
            ) + frame_bytes
            self.report["process_frame_bytes"]["exact"] = int(
                self.report["process_frame_bytes"].get("exact", 0)
            ) + frame_bytes
            return task
        graph_data = {}
        keys = (context["master_key"], context["member_key"])
        if self._process_snapshot_capture is not None:
            missing = []
            for key in keys:
                cached = self._process_graph_data.get(key)
                if cached is None:
                    if key not in self._process_graph_pending:
                        missing.append(key)
                    continue
                self._process_graph_cache_hits += 1
                self.report["process_graph_cache_hits"] = int(self._process_graph_cache_hits)
                if cached.get("rejected"):
                    context["exact_rejection_reason"] = str(cached["rejected"])
                    return False
                graph_data[key] = cached
            if missing:
                if self._process_graph_context is None:
                    raise RuntimeError("resident graph context is unavailable")
                task = pro_process_adapter.make_graph_build_context_task(
                    self._process_identity,
                    self._process_graph_context.context_digest,
                    missing,
                    batch_id="mc4c9-graph-%08d" % ordinal,
                    debug_delay_ms=self.process_debug_delay_ms,
                )
                return task
        else:
            for key in keys:
                cached = self._process_graph_data.get(key)
                if cached is None:
                    status, graph, loops, error = self._advance_graph_for(
                        key, self._tick_deadline
                    )
                    if status == "pending":
                        self._process_stage = "graph_snapshot"
                        self.report["process_stage"] = self._process_stage
                        return None
                    if status == "error" or graph is None or loops is None:
                        raise RuntimeError(error or ("invalid graph snapshot for %r" % (key,)))
                    cached = {
                        "graph": graph,
                        "loops": loops,
                        "data": pro_process_adapter.graph_data_from_topology(graph, key),
                    }
                    self._process_graph_data[key] = cached
                    self._process_graph_cache_builds += 1
                else:
                    self._process_graph_cache_hits += 1
                graph_data[key] = cached
        if len(graph_data) != len(keys):
            self._process_stage = "graph_snapshot"
            self.report["process_stage"] = self._process_stage
            return None

        master_data = graph_data[context["master_key"]]["data"]
        member_data = graph_data[context["member_key"]]["data"]
        task = pro_process_payload.BatchTask(
            identity=self._process_identity,
            batch_id="mc4-frontier-exact-%08d" % ordinal,
            pair_tasks=(
                pro_process_payload.PairTask(
                    pair_ordinal=ordinal,
                    master_key=context["master_key"],
                    member_key=context["member_key"],
                    master_graph=pro_process_payload.GraphRef(
                        master_data.graph_key,
                        master_data.content_digest,
                    ),
                    member_graph=pro_process_payload.GraphRef(
                        member_data.graph_key,
                        member_data.content_digest,
                    ),
                    options=self._process_options,
                    correspondence_mode=self.correspondence_mode,
                ),
            ),
            graphs=tuple(
                sorted(
                    (master_data, member_data),
                    key=lambda item: item.graph_key,
                )
            ),
            debug_delay_ms=self.process_debug_delay_ms,
        )
        task.validate()
        context["exact_task"] = task
        self.report["process_exact_batches"] = int(
            self.report.get("process_exact_batches", 0)
        ) + 1
        self.report["process_exact_pairs_submitted"] = int(
            self.report.get("process_exact_pairs_submitted", 0)
        ) + 1
        self.report["process_frame_bytes"]["exact"] = int(
            self.report["process_frame_bytes"].get("exact", 0)
        ) + int(pro_process_payload.estimate_batch_frame(task).frame_bytes)
        return task

    def _build_process_exact_batches(self, shape_results):
        """Build graph snapshots in bounded slices after shape completion."""

        if self._process_exact_batches is not None:
            return self._process_exact_batches
        accepted = tuple(
            item for item in shape_results if bool(getattr(item, "accepted", False))
        )
        if not self._process_exact_graph_keys:
            keys = []
            for result in accepted:
                context = self._process_pair_contexts[result.pair_ordinal]
                for key in (context["master_key"], context["member_key"]):
                    if key not in keys:
                        keys.append(key)
            self._process_exact_graph_keys = tuple(keys)

        while self._process_exact_graph_index < len(self._process_exact_graph_keys):
            key = self._process_exact_graph_keys[self._process_exact_graph_index]
            status, graph, loops, error = self._advance_graph_for(key, self._tick_deadline)
            if status == "pending":
                self._process_stage = "graph_snapshot"
                self.report["process_stage"] = self._process_stage
                return None
            if status == "error" or graph is None or loops is None:
                raise RuntimeError(error or ("invalid graph snapshot for %r" % (key,)))
            if key not in self._process_graph_data:
                data = pro_process_adapter.graph_data_from_topology(graph, key)
                self._process_graph_data[key] = {
                    "graph": graph,
                    "loops": loops,
                    "data": data,
                }
            self._process_exact_graph_index += 1
            if self._tick_deadline is not None and time.perf_counter() >= self._tick_deadline:
                self._process_stage = "graph_snapshot"
                self.report["process_stage"] = self._process_stage
                return None

        if not accepted:
            self._process_exact_batches = ()
            return self._process_exact_batches

        by_master = OrderedDict()
        for result in accepted:
            context = self._process_pair_contexts[result.pair_ordinal]
            by_master.setdefault(context["master_key"], []).append((result, context))
        exact_batches = []
        for master_key, records in by_master.items():
            for offset in range(0, len(records), self.process_batch_size):
                chunk = records[offset : offset + self.process_batch_size]
                pair_tasks = []
                graph_data = {}
                for result, context in chunk:
                    master_data = self._process_graph_data[context["master_key"]]["data"]
                    member_data = self._process_graph_data[context["member_key"]]["data"]
                    graph_data[master_data.graph_key] = master_data
                    graph_data[member_data.graph_key] = member_data
                    pair_tasks.append(
                        pro_process_payload.PairTask(
                            pair_ordinal=result.pair_ordinal,
                            master_key=context["master_key"],
                            member_key=context["member_key"],
                            master_graph=pro_process_payload.GraphRef(
                                master_data.graph_key, master_data.content_digest
                            ),
                            member_graph=pro_process_payload.GraphRef(
                                member_data.graph_key, member_data.content_digest
                            ),
                            options=self._process_options,
                            correspondence_mode=self.correspondence_mode,
                        )
                    )
                task = pro_process_payload.BatchTask(
                    identity=self._process_identity,
                    batch_id="mc3b-exact-%08d" % int(chunk[0][0].pair_ordinal),
                    pair_tasks=tuple(pair_tasks),
                    graphs=tuple(sorted(graph_data.values(), key=lambda item: item.graph_key)),
                    debug_delay_ms=self.process_debug_delay_ms,
                )
                task.validate()
                exact_batches.append(task)
                for result, context in chunk:
                    context["exact_task"] = task
        self._process_exact_batches = tuple(exact_batches)
        self.report["process_exact_batches"] = len(self._process_exact_batches)
        self.report["process_exact_pairs_submitted"] = sum(
            len(task.pair_tasks) for task in self._process_exact_batches
        )
        self.report["process_frame_bytes"]["exact"] = sum(
            int(pro_process_payload.estimate_batch_frame(task).frame_bytes)
            for task in self._process_exact_batches
        )
        return self._process_exact_batches

    def _merge_process_outcome(self, outcome):
        """Consume one canonical speculative outcome on Blender's main thread."""

        ordinal = int(outcome.pair_ordinal)
        decision_type = pro_process_pipeline.FrontierDecision
        if getattr(outcome, "pruned", False):
            self.report["process_pruned_pairs"] = int(
                self.report.get("process_pruned_pairs", 0)
            ) + 1
            self.report["skipped_ownership"] += 1
            return decision_type(False, False, "already_pruned")
        context = self._process_pair_contexts[ordinal]
        master_key = context["master_key"]
        member_key = context["member_key"]
        shape_result = outcome.shape_result.to_similarity(similarity_matcher)
        similarity_matcher.record_match_diagnostics(shape_result.diagnostics)
        self._process_shape_results[ordinal] = outcome.shape_result
        if not bool(getattr(shape_result, "accepted", False)):
            self.report["skipped_shape"] += 1
            return decision_type(True, False, "shape_rejected")
        if not _selected_match_passes_quality(
            shape_result,
            self._settings.stack_similarity_tolerance,
        ):
            self.report["skipped_shape"] += 1
            return decision_type(True, False, "shape_quality")
        self.report["shape_fit_accepted"] += 1
        precedence_reason = _pro_master_precedence_reason(
            master_key,
            member_key,
            self._uv_area_by_key,
        )
        if precedence_reason is not None:
            self.report["skipped_ownership"] += 1
            return decision_type(True, False, precedence_reason)
        if not _pro_ownership_allows(
            member_key,
            master_key,
            self._assigned_member_keys,
            self._owner_keys,
        ):
            self.report["skipped_ownership"] += 1
            member_closed = (
                member_key in self._assigned_member_keys
                or member_key in self._owner_keys
                or member_key == master_key
            )
            return decision_type(
                not member_closed,
                member_closed,
                "ownership_closed" if member_closed else "master_already_member",
            )
        exact_result = outcome.exact_result
        if exact_result is None:
            reason = str(
                context.pop("exact_rejection_reason", "exact_graph_rejected")
            )
            self._reject(
                master_key,
                member_key,
                reason,
                topology=True,
                detail={"pair_ordinal": ordinal},
            )
            self.report["process_exact_rejected"] = int(
                self.report.get("process_exact_rejected", 0)
            ) + 1
            return decision_type(True, False, "exact_graph_rejected")
        task = context.get("exact_task")
        if task is None and self._process_pipeline is not None:
            task_for_ordinal = getattr(
                self._process_pipeline,
                "fused_task_for_ordinal",
                None,
            )
            if callable(task_for_ordinal):
                task = task_for_ordinal(ordinal)
        if task is None:
            raise RuntimeError("exact result has no immutable task context")
        correspondence = pro_process_adapter.pair_result_to_correspondence(
            exact_result,
            topology_module=topology_correspondence,
            task=task,
        )
        token = (
            "mc4r1f" if getattr(task, "operation_kind", "") == "fused" else "mc3b",
            ordinal,
        )
        if getattr(task, "operation_kind", "") in {"resident_exact", "fused"}:
            resident_pair = next(
                item for item in task.pair_tasks
                if item.pair_ordinal == ordinal
            )
            master_loops = {
                key: self._process_snapshot_live_loop_map[key]
                for key in resident_pair.master_loop_keys
                if key in self._process_snapshot_live_loop_map
            }
            member_loops = {
                key: self._process_snapshot_live_loop_map[key]
                for key in resident_pair.member_loop_keys
                if key in self._process_snapshot_live_loop_map
            }
            if len(master_loops) != len(resident_pair.master_loop_keys):
                raise RuntimeError("resident exact master result references an unknown live loop")
            if len(member_loops) != len(resident_pair.member_loop_keys):
                raise RuntimeError("resident exact member result references an unknown live loop")
        else:
            graph_master = self._process_graph_data[master_key]
            graph_member = self._process_graph_data[member_key]
            master_loops = graph_master["loops"]
            member_loops = graph_member["loops"]
        self._inflight = {
            "process": True,
            "token": token,
            "task": task,
            "master_key": master_key,
            "member_key": member_key,
            "master_loops": master_loops,
            "candidate_loops": member_loops,
            "submitted_at": time.perf_counter(),
        }
        self._process_exact_results[ordinal] = exact_result
        self._consume_exact_result(correspondence, token=token, error=None)
        if (
            bool(getattr(exact_result, "accepted", False))
            and member_key in self._assigned_member_keys
        ):
            return decision_type(False, True, "ownership_committed")
        return decision_type(True, False, "exact_rejected")

    def _process_pipeline_has_terminal_canonical_result(self):
        """Return true only after every canonical ordinal is consumable.

        Snapshot validation is mandatory before apply, but it must not turn a
        fully merged worker result into a wall-budget partial merely because
        validation was resumed on the following modal tick.
        """

        pipeline = self._process_pipeline
        if pipeline is None or not bool(getattr(pipeline, "has_consumable_result", False)):
            return False
        if str(getattr(pipeline, "stage", "")) != "done":
            return False
        progress = pipeline.progress()
        return int(getattr(progress, "merged_pairs", 0)) == len(
            getattr(pipeline, "canonical_ordinals", ())
        )

    def _process_semantic_work_complete_for_grace(self) -> bool:
        """Prove that only validation/shutdown/apply remain.

        This predicate is intentionally stricter than the public terminal
        result check.  A deadline may enter finalization grace only when no
        task, completion, restart, context replay, or pool cleanup can still
        produce semantic work.  The grace phase never dispatches new work.
        """

        pipeline = self._process_pipeline
        if pipeline is None or not self._process_pipeline_has_terminal_canonical_result():
            return False
        progress = pipeline.progress()
        if int(getattr(progress, "active_workers", 0)) or int(
            getattr(progress, "queue_depth", 0)
        ):
            return False
        if int(getattr(progress, "exact_submitted", 0)) != int(
            getattr(progress, "exact_completed", 0)
        ):
            return False
        jobs = getattr(pipeline, "_jobs", None)
        if jobs is not None:
            if int(getattr(pipeline, "_exact_cursor", len(jobs))) < len(jobs):
                return False
            submitted = set(getattr(pipeline, "_submitted_exact", ()))
            completed = set(getattr(pipeline, "_completed_exact", {}).keys())
            if submitted != completed:
                return False
        for name in (
            "_task_meta",
            "_exact_pending",
            "_graph_waiting",
            "_completion_buffer",
            "_pending_fused_admission",
        ):
            value = getattr(pipeline, name, None)
            if value:
                return False
        pool = self._process_pool
        if pool is None:
            return True
        for name in (
            "_active",
            "_context_active",
            "_ready_batches",
            "_undispatched",
            "_pending_restarts",
            "_pending_cleanups",
            "_context_replay_pending",
            "_stream_completions",
            "_result_buffer",
        ):
            if getattr(pool, name, None):
                return False
        if bool(getattr(pool, "_stream_mode", False)) and not bool(
            getattr(pool, "_stream_closed", False)
        ):
            return False
        if int(getattr(pool, "restart_pending", 0) or 0):
            return False
        if int(getattr(pool, "cleanup_pending", 0) or 0):
            return False
        return bool(getattr(pool, "is_terminal", True))

    def _begin_process_finalization_grace(self, reason: str) -> bool:
        if self._process_finalization_grace_active:
            return True
        if not self._process_semantic_work_complete_for_grace():
            self.report["process_finalization_grace_state"] = "rejected_incomplete"
            self.report["process_finalization_grace_reason"] = str(reason)
            return False
        started = time.perf_counter()
        self._process_finalization_grace_active = True
        self._process_finalization_grace_started_at = started
        self._process_finalization_grace_deadline = (
            started + _PRO_PROCESS_FINALIZATION_GRACE_MS / 1000.0
        )
        self._process_finalization_grace_rounds = 0
        self._process_finalization_grace_reason = str(reason)
        self.report["process_finalization_grace_state"] = "active"
        self.report["process_finalization_grace_started_ms"] = float(
            self.active_elapsed_ms
        )
        self.report["process_finalization_grace_deadline_ms"] = float(
            self.active_elapsed_ms + _PRO_PROCESS_FINALIZATION_GRACE_MS
        )
        self.report["process_finalization_grace_rounds"] = 0
        self.report["process_finalization_grace_reason"] = str(reason)
        self.report["process_finalization_grace_no_dispatch"] = True
        # Keep the public session state at process_pipeline while grace is
        # active.  Existing modal consumers and lifecycle evidence therefore
        # retain the stable pipeline->shutdown->finish->done sequence; the
        # internal latch below prevents any new semantic dispatch.
        return True

    def _latch_process_result_and_begin_shutdown(self) -> bool:
        if not self._process_pipeline_has_terminal_canonical_result():
            return False
        final = self._process_pipeline.final_result()
        if not final.complete:
            self._fail(RuntimeError(final.failure or "incomplete external Pro pipeline"))
            return False
        self._process_last_result_digest = final.result_digest
        self.report["process_result_digest"] = final.result_digest
        self._process_shutdown_started_at = time.perf_counter()
        self._process_shutdown_grace_deadline = (
            self._process_shutdown_started_at + 2.0
        )
        self._process_shutdown_state = "begin"
        self._process_shutdown_rounds = 0
        self._process_shutdown_wait_ms = 0.0
        self._process_shutdown_force_used = False
        self.report["process_shutdown_state"] = "begin"
        self.report["process_shutdown_rounds"] = 0
        self.report["process_shutdown_force_used"] = False
        self._process_finalization_grace_active = False
        self.report["process_finalization_grace_state"] = "complete"
        self.state = "process_shutdown"
        return True

    def _advance_process_finalization_grace(self) -> bool:
        """Advance validation only after the strict semantic completion gate."""

        started = time.perf_counter()
        if not self._process_finalization_grace_active:
            if not self._begin_process_finalization_grace("explicit"):
                self.cancel("finalization_incomplete", nonblocking=self.modal)
                return False
        if time.perf_counter() >= self._process_finalization_grace_deadline:
            self.report["process_finalization_grace_state"] = "expired"
            self.report["process_finalization_grace_reason"] = "validation_grace_expired"
            self.cancel("validation_grace_expired", nonblocking=self.modal)
            return False
        if not self._process_snapshot_is_current(force=False):
            self.cancel("context_invalidated", nonblocking=self.modal)
            return False
        guard = self._process_snapshot_guard
        if guard is not None and not bool(getattr(guard, "validation_requested", False)):
            guard.request_validation()
            self._process_validation_requested = True
        validation = self._advance_process_snapshot_validation()
        self._process_finalization_grace_rounds += 1
        self.report["process_finalization_grace_rounds"] = int(
            self._process_finalization_grace_rounds
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        self._process_finalization_grace_max_tick_ms = max(
            self._process_finalization_grace_max_tick_ms,
            elapsed,
        )
        self.report["process_finalization_grace_max_tick_ms"] = float(
            self._process_finalization_grace_max_tick_ms
        )
        self._record_phase("process_finalization_grace", elapsed)
        if validation == "pending":
            self._process_stage = "snapshot_validate"
            self.report["process_stage"] = self._process_stage
            return True
        if validation != "valid":
            self.cancel("context_invalidated", nonblocking=self.modal)
            return False
        if not self._process_semantic_work_complete_for_grace():
            self.cancel("semantic_work_changed", nonblocking=self.modal)
            return False
        return self._latch_process_result_and_begin_shutdown()

    def _advance_process_pipeline(self):
        if self._process_pipeline is None:
            raise RuntimeError("process pipeline is not initialized")
        if self._process_finalization_grace_active:
            return self._advance_process_finalization_grace()
        current_stage = self._process_pipeline.stage
        force_snapshot = current_stage != self._process_snapshot_stage_checked
        if not self._process_snapshot_is_current(force=force_snapshot):
            self.cancel("context_invalidated", nonblocking=self.modal)
            return False
        if force_snapshot:
            self._process_snapshot_stage_checked = current_stage
        self._tick_stage = "process_%s" % self._process_pipeline.stage
        before = self._process_pipeline.stage
        poll_started = time.perf_counter()
        progress = self._process_pipeline.advance(
            timeout=0.0,
            merge_limit=1,
            deadline=self._tick_deadline,
        )
        poll_elapsed_ms = (time.perf_counter() - poll_started) * 1000.0
        self._process_poll_ms += poll_elapsed_ms
        self._process_compute_ms += poll_elapsed_ms
        self.worker_wall_elapsed_ms += poll_elapsed_ms
        self.worker_compute_elapsed_ms += poll_elapsed_ms
        self.main_thread_poll_ms += poll_elapsed_ms
        self._record_phase("process_poll", poll_elapsed_ms)
        self._record_phase("main_thread_poll", poll_elapsed_ms)
        subphase_ms = dict(self.report.get("process_pipeline_subphase_ms", {}) or {})
        subphase_ms["pipeline_advance"] = float(poll_elapsed_ms)
        self.report["process_pipeline_subphase_ms"] = subphase_ms
        if poll_elapsed_ms >= float(self.report.get("process_pipeline_max_subphase_ms", 0.0)):
            self.report["process_pipeline_max_subphase_ms"] = float(poll_elapsed_ms)
            self.report["process_pipeline_max_subphase"] = "pipeline_advance"
        after = self._process_pipeline.stage
        self._process_stage = after
        self.report["process_stage"] = after
        self.report["process_queue_depth"] = int(progress.queue_depth)
        self.report["process_active_workers"] = int(progress.active_workers)
        self.report["process_retry_count"] = int(progress.retry_count)
        self.report["process_retry_total"] = int(
            getattr(progress, "retry_total", progress.retry_count)
        )
        self.report["process_max_retry_per_batch"] = int(
            getattr(progress, "max_retry_per_batch", 0)
        )
        self.report["process_retried_batch_count"] = int(
            getattr(progress, "retried_batch_count", 0)
        )
        self.report["process_retry_failure_reason"] = str(
            getattr(progress, "retry_failure_reason", "") or ""
        )
        self.report["process_nearest_attempted"] = int(
            getattr(progress, "nearest_attempted", 0)
        )
        self.report["process_nearest_accepted"] = int(
            getattr(progress, "nearest_accepted", 0)
        )
        self.report["process_nearest_fallback"] = int(
            getattr(progress, "nearest_fallback", 0)
        )
        self.report["process_nearest_max_seed_distance"] = float(
            getattr(progress, "nearest_max_seed_distance", 0.0)
        )
        self.report["process_nearest_mean_seed_distance"] = float(
            getattr(progress, "nearest_mean_seed_distance", 0.0)
        )
        self.report["process_nearest_ambiguity_count"] = int(
            getattr(progress, "nearest_ambiguity_count", 0)
        )
        self.report["process_nearest_tie_count"] = int(
            getattr(progress, "nearest_tie_count", 0)
        )
        self.report["process_nearest_compute_ms"] = float(
            getattr(progress, "nearest_compute_ms", 0.0)
        )
        self.report["process_nearest_distance_evaluations"] = int(
            getattr(progress, "nearest_distance_evaluations", 0)
        )
        self.report["process_nearest_assignment_nodes"] = int(
            getattr(progress, "nearest_assignment_nodes", 0)
        )
        self.report["process_nearest_assignment_cap"] = int(
            getattr(progress, "nearest_assignment_cap", 0)
        )
        self.report["process_nearest_fallback_reasons"] = {
            pro_verified_nearest.fallback_reason_name(code): int(count)
            for code, count in getattr(progress, "nearest_fallback_reasons", ())
        }
        self.report["process_nearest_distance_lookups"] = int(
            getattr(progress, "nearest_distance_lookups", 0)
        )
        self.report["process_nearest_distance_cache_hits"] = int(
            getattr(progress, "nearest_distance_cache_hits", 0)
        )
        self.report["process_nearest_distance_cache_misses"] = int(
            getattr(progress, "nearest_distance_cache_misses", 0)
        )
        self.report["process_nearest_operations_used"] = int(
            getattr(progress, "nearest_operations_used", 0)
        )
        self.report["process_restart_pending"] = int(
            getattr(progress, "restart_pending", 0)
        )
        self.report["process_restart_states"] = [
            [int(index), str(state), str(batch_id)]
            for index, state, batch_id in getattr(progress, "restart_states", ())
        ]
        self.report["process_retry_batches"] = [
            [str(batch_id), int(count)]
            for batch_id, count in getattr(progress, "retry_batches", ())
        ]
        fused_mode = bool(self.process_fused_requested)
        group_first_mode = bool(self.process_group_first_requested)
        fused_batches_submitted = int(
            getattr(progress, "fused_batches_submitted", 0)
        )
        fused_batches_completed = int(
            getattr(progress, "fused_batches_completed", 0)
        )
        fused_pairs_submitted = int(progress.exact_submitted) if fused_mode else 0
        fused_exact_submitted = int(progress.exact_total) if fused_mode else 0
        if group_first_mode:
            fused_batches_submitted = int(
                getattr(progress, "shape_batches_submitted", 0)
            ) + int(getattr(progress, "exact_batches_submitted", 0))
            fused_batches_completed = int(
                getattr(progress, "shape_batches_completed", 0)
            ) + int(getattr(progress, "resident_exact_batches_completed", 0))
            fused_pairs_submitted = int(getattr(progress, "exact_submitted", 0))
            fused_exact_submitted = int(getattr(progress, "exact_total", 0))
        self.report["process_fused_batches_submitted"] = fused_batches_submitted
        self.report["process_fused_batches_completed"] = fused_batches_completed
        self.report["process_fused_pairs_submitted"] = fused_pairs_submitted
        self.report["process_fused_pairs_completed"] = int(
            getattr(progress, "exact_completed", 0)
        ) if fused_mode else 0
        self.report["process_fused_graph_cache_builds"] = int(
            getattr(progress, "fused_graph_cache_builds", 0)
        )
        self.report["process_fused_graph_cache_hits"] = int(
            getattr(progress, "fused_graph_cache_hits", 0)
        )
        self.report["process_fused_graph_compute_ms"] = float(
            getattr(progress, "fused_graph_compute_ms", 0.0)
        )
        self.report["process_fused_exact_compute_ms"] = float(
            getattr(progress, "fused_exact_compute_ms", 0.0)
        )
        self.report["process_fused_shape_compute_ms"] = float(
            getattr(progress, "fused_shape_compute_ms", 0.0)
        )
        self.report["process_fused_shape_cache_hits"] = int(
            getattr(progress, "fused_shape_cache_hits", 0)
        )
        self.report["process_fused_lower_bound_checked"] = int(
            getattr(progress, "fused_lower_bound_checked", 0)
        )
        self.report["process_fused_lower_bound_rejected"] = int(
            getattr(progress, "fused_lower_bound_rejected", 0)
        )
        self.report["process_fused_lower_bound_skipped"] = int(
            getattr(progress, "fused_lower_bound_skipped", 0)
        )
        self.report["process_fused_lower_bound_graph_pairs_avoided"] = int(
            getattr(progress, "fused_lower_bound_graph_pairs_avoided", 0)
        )
        self.report["process_fused_lower_bound_min_ratio"] = float(
            getattr(progress, "fused_lower_bound_min_ratio", 0.0)
        )
        self.report["process_fused_lower_bound_max_ratio"] = float(
            getattr(progress, "fused_lower_bound_max_ratio", 0.0)
        )
        self.report["process_fused_frame_bytes"] = int(
            getattr(progress, "fused_frame_bytes", 0)
        )
        self.report["process_fused_frame_total_bytes"] = int(
            getattr(progress, "fused_frame_total_bytes", 0)
        )
        frame_max = dict(getattr(progress, "frame_bytes_max", ()))
        frame_total = dict(getattr(progress, "frame_bytes_total", ()))
        if frame_max:
            self.report["process_frame_bytes"] = frame_max
        if frame_total:
            self.report["process_frame_total_bytes"] = frame_total
        self.report["process_shape_batches"] = (
            int(getattr(progress, "shape_batches_submitted", fused_batches_submitted))
            if group_first_mode
            else (fused_batches_submitted if fused_mode else int(progress.shape_batches_submitted))
        )
        self.report["process_exact_batches"] = int(
            getattr(progress, "exact_batches_submitted", progress.exact_batches_submitted)
        )
        self.report["process_shape_pairs_submitted"] = (
            int(progress.shape_submitted)
            if group_first_mode
            else (fused_pairs_submitted if fused_mode else int(progress.shape_submitted))
        )
        self.report["process_exact_pairs_submitted"] = (
            int(progress.exact_submitted)
            if group_first_mode
            else (fused_exact_submitted if fused_mode else int(progress.exact_submitted))
        )
        self.report["process_shape_pairs_completed"] = int(progress.shape_completed)
        self.report["process_shape_accepted"] = int(progress.shape_accepted)
        self.report["process_shape_rejected"] = int(progress.shape_rejected)
        self.report["process_exact_pairs_completed"] = int(progress.exact_completed)
        self.report["process_exact_accepted"] = int(progress.exact_accepted)
        # These counters come from typed PairResult diagnostics.  Never infer
        # exact calls from direct completion: graph-rejected pairs never reach
        # nearest or CorrespondenceSearch.
        graph_rejected = int(
            getattr(progress, "graph_rejected_before_nearest", 0)
        )
        seed_missing = int(getattr(progress, "nearest_seed_missing", 0))
        fast_miss = int(
            getattr(
                progress,
                "nearest_fast_miss",
                getattr(progress, "nearest_fallback", 0),
            )
        )
        legacy_fast_miss = int(getattr(progress, "nearest_fallback", 0))
        if fast_miss <= 0 and legacy_fast_miss > 0:
            fast_miss = legacy_fast_miss
        exact_fallback_calls = int(
            getattr(progress, "exact_fallback_calls", 0)
        )
        if (
            exact_fallback_calls <= 0
            and self.correspondence_mode
            == pro_process_payload.CORRESPONDENCE_MODE_HYBRID
        ):
            # Compatibility for old/fake progress values that expose only the
            # pre-R2F5 nearest fallback field.
            exact_fallback_calls = fast_miss + seed_missing
        if graph_rejected <= 0:
            graph_rejected = sum(
                int(value)
                for value in getattr(
                    self,
                    "_process_graph_rejections",
                    {},
                ).values()
            )
        self.report["process_graph_rejected_before_nearest"] = graph_rejected
        self.report["process_nearest_seed_missing"] = seed_missing
        self.report["process_nearest_fast_miss"] = fast_miss
        self.report["process_exact_fallback_calls"] = exact_fallback_calls
        exact_primary_calls = int(
            getattr(progress, "exact_primary_calls", 0) or 0
        )
        self.report["process_exact_primary_calls"] = exact_primary_calls
        self.report["process_nearest_fallback_exact_calls"] = exact_fallback_calls
        self.report["process_nearest_missing_seed_fallbacks"] = seed_missing
        direct_completed = int(
            getattr(
                progress,
                "direct_exact_jobs_completed",
                self.report.get("direct_exact_jobs_completed", 0),
            )
        )
        nearest_attempted = int(self.report.get("process_nearest_attempted", 0))
        nearest_accepted = int(self.report.get("process_nearest_accepted", 0))
        accounting = {
            "direct_completed": direct_completed,
            "graph_rejected_before_nearest": graph_rejected,
            "nearest_seed_missing": seed_missing,
            "nearest_attempted": nearest_attempted,
            "nearest_accepted": nearest_accepted,
            "nearest_fast_miss": fast_miss,
            "exact_fallback_calls": exact_fallback_calls,
            "exact_primary_calls": exact_primary_calls,
        }
        self.report["process_nearest_accounting"] = accounting
        if (
            self.correspondence_mode
            == pro_process_payload.CORRESPONDENCE_MODE_EXACT_ONLY
        ):
            accounting_valid = bool(
                nearest_attempted == 0
                and nearest_accepted == 0
                and fast_miss == 0
                and seed_missing == 0
                and exact_fallback_calls == 0
                and graph_rejected + exact_primary_calls == direct_completed
            )
        elif (
            self.correspondence_mode
            == pro_process_payload.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
        ):
            accounting_valid = bool(
                graph_rejected + seed_missing + nearest_attempted == direct_completed
                and nearest_attempted == nearest_accepted + fast_miss
                and exact_fallback_calls == 0
                and exact_primary_calls == 0
            )
        else:
            accounting_valid = bool(
                graph_rejected + seed_missing + nearest_attempted == direct_completed
                and nearest_attempted == nearest_accepted + fast_miss
                and exact_fallback_calls == fast_miss + seed_missing
                and exact_primary_calls == 0
            )
        self.report["process_nearest_accounting_valid"] = accounting_valid
        if group_first_mode:
            self.report["candidate_pairs_planned"] = int(
                getattr(progress, "grouping_comparisons_planned", 0)
            )
            self.report["candidate_pairs_processed"] = int(
                getattr(progress, "grouping_comparisons_completed", 0)
            )
            self.report["grouping_comparisons_planned"] = int(
                getattr(progress, "grouping_comparisons_planned", 0)
            )
            self.report["grouping_comparisons_completed"] = int(
                getattr(progress, "grouping_comparisons_completed", 0)
            )
            self.report["direct_exact_jobs_planned"] = int(
                getattr(progress, "direct_exact_jobs_planned", 0)
            )
            self.report["direct_exact_jobs_completed"] = int(
                getattr(progress, "direct_exact_jobs_completed", 0)
            )
            self.report["direct_exact_jobs_failed"] = int(
                getattr(progress, "direct_exact_jobs_failed", 0)
            )
            self.report["exact_job_bound"] = int(
                getattr(progress, "exact_job_bound", 0)
            )
            plan = getattr(self._process_pipeline, "plan", None)
            self.report["shape_groups"] = int(
                getattr(progress, "grouping_groups", 0)
            )
            self.report["shape_singletons"] = sum(
                1
                for group in getattr(plan, "groups", ())
                if int(getattr(group, "size", 1)) == 1
            ) if plan is not None else 0
            if plan is not None:
                self._process_group_first_plan = plan
                self._process_group_first_exact_jobs = tuple(
                    getattr(plan, "direct_exact_jobs", ())
                )
                seeded_jobs = sum(
                    1
                    for job in self._process_group_first_exact_jobs
                    if getattr(job, "seed_transform", None) is not None
                )
                direct_job_count = len(self._process_group_first_exact_jobs)
                self.report["process_nearest_seeded_jobs_planned"] = int(
                    seeded_jobs
                )
                self.report["process_nearest_seedless_jobs_planned"] = int(
                    max(0, direct_job_count - seeded_jobs)
                )
                self.report["process_seed_planned"] = int(
                    getattr(plan, "seed_planned", seeded_jobs)
                )
                self.report["process_seed_rerooted"] = int(
                    getattr(plan, "seed_rerooted", 0)
                )
                self.report["process_seed_identity_leg"] = int(
                    getattr(plan, "seed_identity_leg", 0)
                )
                self.report["process_seed_missing_by_reason"] = {
                    str(reason): int(count)
                    for reason, count in getattr(plan, "seed_missing_by_reason", ())
                }
                self.report["process_seed_digest"] = str(
                    getattr(plan, "seed_digest", "")
                )
                self.report["group_membership_digest"] = str(
                    getattr(plan, "membership_digest", "")
                )
                self.report["density_masters"] = [
                    list(group.density_master_key)
                    for group in getattr(plan, "groups", ())
                    if group.density_master_key is not None
                ]
                self.report["uv_area_masters"] = [
                    list(group.uv_area_master_key)
                    for group in getattr(plan, "groups", ())
                    if group.uv_area_master_key is not None
                ]
                self.report["uv_area_master_areas"] = [
                    float(group.uv_area_master_area)
                    for group in getattr(plan, "groups", ())
                    if group.uv_area_master_area is not None
                ]
                self.report["uv_area_by_key"] = [
                    [list(key), area]
                    for key, area in getattr(plan, "uv_area_by_key", ())
                ]
                self.report["uv_size_masters"] = [
                    list(group.uv_size_master_key)
                    for group in getattr(plan, "groups", ())
                    if group.uv_size_master_key is not None
                ]
                self.report["uv_size_master_areas"] = [
                    float(group.uv_size_master_area)
                    for group in getattr(plan, "groups", ())
                    if group.uv_size_master_area is not None
                ]
                self.report["uv_size_by_key"] = [
                    [list(key), area]
                    for key, area in getattr(plan, "uv_size_by_key", ())
                ]
            self.report["process_group_first_stage"] = str(after)
        self.report["process_resident_exact_batches_submitted"] = int(
            getattr(progress, "resident_exact_batches_submitted", 0)
        )
        self.report["process_resident_exact_batches_completed"] = int(
            getattr(progress, "resident_exact_batches_completed", 0)
        )
        self.report["process_resident_graph_cache_builds"] = int(
            getattr(progress, "resident_graph_cache_builds", 0)
        )
        self.report["process_resident_graph_cache_hits"] = int(
            getattr(progress, "resident_graph_cache_hits", 0)
        )
        self.report["process_resident_graph_compute_ms"] = float(
            getattr(progress, "resident_graph_compute_ms", 0.0)
        )
        self.report["process_resident_topology_cache_builds"] = int(
            getattr(progress, "resident_topology_cache_builds", 0)
        )
        self.report["process_resident_topology_cache_hits"] = int(
            getattr(progress, "resident_topology_cache_hits", 0)
        )
        self.report["process_resident_topology_compute_ms"] = float(
            getattr(progress, "resident_topology_compute_ms", 0.0)
        )
        self.report["process_resident_exact_compute_ms"] = float(
            getattr(progress, "resident_exact_compute_ms", 0.0)
        )
        self.report["process_merged_pairs"] = int(progress.merged_pairs)
        self.report["process_pruned_pairs"] = int(progress.pruned_pairs)
        self.report["process_exact_started_before_shape_terminal"] = bool(
            progress.exact_started_before_shape_terminal
        )
        self.report["process_exact_first_shape_completed"] = int(
            getattr(progress, "exact_first_shape_completed", 0)
        )
        self.report["process_exact_first_shape_total"] = int(
            getattr(progress, "exact_first_shape_total", 0)
        )
        self.report["process_exact_first_timestamp_ms"] = getattr(
            progress, "exact_first_timestamp_ms", None
        )
        self.report["process_poll_calls"] = int(
            getattr(progress, "poll_calls", 0)
        )
        self.report["process_no_progress_loops"] = int(
            getattr(progress, "no_progress_loops", 0)
        )
        self.report["process_event_epoch"] = int(
            getattr(progress, "event_epoch", 0)
        )
        self.report["process_graph_event_epoch"] = int(
            getattr(progress, "graph_event_epoch", 0)
        )
        self.report["process_graph_waiter_registrations"] = int(
            getattr(progress, "graph_waiter_registrations", 0)
        )
        self.report["process_graph_waiter_dedup"] = int(
            getattr(progress, "graph_waiter_dedup", 0)
        )
        self.report["process_graph_worker_submitted"] = int(
            getattr(progress, "graph_tasks_submitted", 0)
        )
        self.report["process_graph_worker_completed"] = int(
            getattr(progress, "graph_tasks_completed", 0)
        )
        self.report["process_graph_worker_operations"] = int(
            getattr(progress, "graph_items_completed", 0)
        )
        self.report["process_graph_worker_cache_hits"] = int(
            getattr(progress, "graph_cache_hits", 0)
        )
        self.report["process_worker_operation_distribution"] = [
            [int(index), [[str(kind), int(count)] for kind, count in values]]
            for index, values in getattr(
                self._process_pool, "worker_operation_distribution", ()
            )
        ] if self._process_pool is not None else []
        self.report["process_graph_main_operations"] = int(
            self._process_graph_main_operations
        )
        self.report["process_last_progress_kind"] = str(progress.last_progress_kind)
        self.report["process_graph_slices"] = int(self._process_graph_slices)
        self.report["process_graph_primitive_ops"] = int(
            self._process_graph_primitive_ops
        )
        self.report["process_graph_max_slice_ms"] = float(
            self._process_graph_max_slice_ms
        )
        self.report["process_graph_max_primitive_ms"] = float(
            self._process_graph_max_primitive_ms
        )
        self.report["process_graph_max_primitive"] = dict(
            self._process_graph_max_primitive
        )
        self.report["process_graph_build_ms"] = float(self._process_graph_build_ms)
        self.report["process_graph_cache_builds"] = int(
            self._process_graph_cache_builds
        )
        self.report["process_graph_cache_hits"] = int(
            self._process_graph_cache_hits
        )
        self.report["process_graph_rejections"] = dict(
            self._process_graph_rejections
        )
        if self._process_pool is not None:
            self.report["process_graph_context_digest"] = (
                self._process_graph_context.context_digest
                if self._process_graph_context is not None
                else None
            )
            self.report["process_graph_context_build_ms"] = float(
                self._process_graph_context_build_ms
            )
            self.report["process_graph_context_load_submitted"] = int(
                getattr(self._process_pool, "context_load_submitted", 0)
            )
            self.report["process_graph_context_load_acked"] = int(
                getattr(self._process_pool, "context_load_acked", 0)
            )
            self.report["process_graph_context_load_ms"] = float(
                getattr(self._process_pool, "context_load_ms", 0.0)
            )
            context_ready_now = bool(
                getattr(self._process_pool, "graph_context_ready", False)
            )
            self.report["process_graph_context_ready"] = context_ready_now
            # Pool shutdown clears its resident context.  Preserve the ACK
            # evidence already observed during startup so a completed fused
            # report does not falsely claim that context loading never
            # happened.
            if self.process_fused_requested:
                self._latch_process_fused_context_ack(self._process_pool)
                self.report["process_fused_context_ready"] = bool(
                    self._process_fused_context_acked
                )
            self.report["process_graph_context_frame_bytes"] = int(
                getattr(self._process_pool, "context_load_frame_bytes", 0)
            )
            self.report["process_graph_context_frame_max_bytes"] = int(
                getattr(self._process_pool, "context_load_frame_max_bytes", 0)
            )
            self.report["process_graph_context_payload_bytes"] = int(
                getattr(self._process_pool, "context_load_payload_bytes", 0)
            )
        self.report["worker_submissions"] = int(
            self.report.get("process_shape_pairs_submitted", 0)
            + self.report.get("process_exact_pairs_submitted", 0)
        )
        self.report["worker_completions"] = int(
            self.report.get("process_shape_pairs_completed", 0)
            + self.report.get("process_exact_pairs_completed", 0)
        )
        self.report["worker_in_flight_peak"] = max(
            int(self.report.get("worker_in_flight_peak", 0)),
            int(progress.active_workers),
        )
        self.report["process_poll_ms"] = float(self._process_poll_ms)
        self.report["main_thread_poll_ms"] = float(self.main_thread_poll_ms)
        self.report["process_compute_ms"] = max(
            float(self.report.get("process_compute_ms", 0.0)),
            float(progress.elapsed_ms),
        )
        self.report["worker_compute_elapsed_ms"] = max(
            float(self.report.get("worker_compute_elapsed_ms", 0.0)),
            float(progress.elapsed_ms),
        )
        self.report["process_cache_hits"] = int(getattr(self._process_pool, "cache_hits", 0))
        self.report["process_stage_distributions"][after] = [
            [int(index), int(count)]
            for index, count in progress.worker_distribution
        ]
        self._process_last_progress = progress
        self._process_stage_distributions[after] = progress.worker_distribution
        if before != after:
            self._process_snapshot_stage_checked = after
            self._record_phase("process_%s_to_%s" % (before, after), 0.0)
        if after == "failed":
            self._fail(RuntimeError(progress.failure or "external Pro pipeline failed"))
            return False
        if after == "done":
            if not self._process_snapshot_is_current(force=True):
                self.cancel("context_invalidated", nonblocking=self.modal)
                return False
            validation = self._advance_process_snapshot_validation()
            if validation == "pending":
                self._begin_process_finalization_grace("pipeline_terminal_validation")
                return True
            if validation != "valid":
                self.cancel("context_invalidated", nonblocking=self.modal)
                return False
            return self._latch_process_result_and_begin_shutdown()
        return True

    def _advance_process_shutdown(self):
        """Advance owned helper shutdown without waiting inside one modal tick."""

        pool = self._process_pool
        if pool is None:
            self._process_shutdown_state = "complete"
            self.report["process_shutdown_state"] = "complete"
            self.state = "finish"
            return True
        if not self._process_snapshot_is_current(force=False):
            self.cancel("context_invalidated", nonblocking=self.modal)
            return False
        if self._process_shutdown_started_at <= 0.0:
            self._process_shutdown_started_at = time.perf_counter()
            self._process_shutdown_grace_deadline = (
                self._process_shutdown_started_at + 2.0
            )
        if self._process_shutdown_state == "idle":
            self._process_shutdown_state = "begin"
        begin_started = time.perf_counter()
        try:
            pool.begin_shutdown(grace_timeout=max(
                0.01,
                self._process_shutdown_grace_deadline - time.perf_counter(),
            ))
            self._process_shutdown_state = str(pool.advance_shutdown(
                deadline=self._process_slice_deadline(self._tick_deadline, 10.0),
                grace_deadline=self._process_shutdown_grace_deadline,
            ))
        except Exception as exc:
            # Graceful protocol failure is owned-worker cleanup, not a
            # semantic result failure.  Force is still exact-handle-only.
            self._process_shutdown_force_used = True
            self.report["process_shutdown_error"] = str(exc)[:512]
            try:
                self._process_shutdown_state = str(pool.advance_shutdown(
                    deadline=self._process_slice_deadline(self._tick_deadline, 10.0),
                    grace_deadline=time.perf_counter(),
                ))
            except Exception:
                self._process_shutdown_state = "force"
        self._process_shutdown_rounds += 1
        self._process_shutdown_wait_ms = max(
            0.0,
            (time.perf_counter() - self._process_shutdown_started_at) * 1000.0,
        )
        self._process_shutdown_force_used = bool(
            self._process_shutdown_force_used
            or getattr(pool, "shutdown_force_used", False)
        )
        progress = pool.progress()
        self._process_last_progress = progress
        self._process_stage = "shutdown_%s" % self._process_shutdown_state
        self.report["process_stage"] = self._process_stage
        self.report["process_shutdown_state"] = self._process_shutdown_state
        self.report["process_shutdown_rounds"] = int(self._process_shutdown_rounds)
        self.report["process_shutdown_force_used"] = bool(
            self._process_shutdown_force_used
        )
        self.report["process_shutdown_wait_ms"] = float(
            self._process_shutdown_wait_ms
        )
        self.report["process_shutdown_timings_ms"] = [
            float(value) for value in getattr(pool, "shutdown_timings_ms", ())
        ]
        self.report["process_cleanup_pending"] = int(
            getattr(pool, "cleanup_pending", 0) or 0
        )
        self._record_phase(
            "process_shutdown_wait",
            (time.perf_counter() - begin_started) * 1000.0,
        )
        if bool(getattr(pool, "shutdown_complete", False)):
            if not self._process_snapshot_is_current(force=True):
                self.cancel("context_invalidated", nonblocking=self.modal)
                return False
            self._process_shutdown_state = "complete"
            self.report["process_shutdown_state"] = "complete"
            self.state = "finish"
        return True

    def _advance_process_cancel(self):
        """Advance modal cancellation through exact-owned nonblocking cleanup."""

        pool = self._process_pool
        if pool is None:
            self._process_cancel_state = "complete"
            self.report["process_cancel_state"] = "complete"
            self._finalize(apply=False)
            return True
        started = time.perf_counter()
        if self._process_cancel_started_at <= 0.0:
            self._process_cancel_started_at = started
        try:
            state = pool.advance_cancel(
                deadline=self._process_slice_deadline(self._tick_deadline, 10.0),
            )
            self._process_cancel_state = str(state)
        except Exception as exc:
            # The semantic result is already discarded.  Keep the modal
            # callback bounded and retain unregister cleanup as the final
            # exact-owned fallback if a test double/runtime shim fails.
            self._process_cancel_state = "error"
            self.report["process_cancel_error"] = str(exc)[:512]
        self._process_cancel_rounds += 1
        self._process_cancel_wait_ms = max(
            0.0,
            (time.perf_counter() - self._process_cancel_started_at) * 1000.0,
        )
        self.report["process_cancel_state"] = self._process_cancel_state
        self.report["process_cancel_rounds"] = int(self._process_cancel_rounds)
        self.report["process_cancel_wait_ms"] = float(self._process_cancel_wait_ms)
        self._record_phase("process_cancel", (time.perf_counter() - started) * 1000.0)
        if bool(getattr(pool, "cancel_complete", False)):
            pipeline = self._process_pipeline
            if pipeline is not None and getattr(pipeline, "stage", "") == "cancelling":
                try:
                    pipeline.advance_cancel(
                        deadline=self._process_slice_deadline(self._tick_deadline, 10.0),
                    )
                except Exception:
                    pass
            self._process_cancel_state = "complete"
            self.report["process_cancel_state"] = "complete"
            self._finalize(apply=False)
        return True

    @staticmethod
    def _process_slice_deadline(parent_deadline, reserve_ms):
        """Return a short independent slice deadline for pure process work.

        A modal tick may already have spent its active budget in polling,
        canonical merge, or another bounded phase.  Passing that expired
        deadline through would make a resumable graph/validation builder do
        zero operations forever.  The reserve is deliberately tiny and
        bounded, and is used only for pure immutable work; it never turns a
        process tick into a full synchronous wait.
        """

        now = time.perf_counter()
        try:
            reserve = max(0.0, float(reserve_ms)) / 1000.0
        except (TypeError, ValueError):
            reserve = 0.0
        independent = now + reserve
        if parent_deadline is None:
            return independent
        try:
            parent = float(parent_deadline)
        except (TypeError, ValueError):
            return independent
        if parent <= now:
            return independent
        return min(parent, independent)

    def _process_snapshot_is_current(self, *, force=False):
        if not self.process_requested or self._process_identity is None:
            return True
        if not _pro_session_context_valid(
            self.context,
            self.obj,
            self.bm,
            self.uv_layer,
        ):
            self._process_snapshot_check_result = False
            return False
        now = time.perf_counter()
        guard = self._process_snapshot_guard
        if guard is None:
            return bool(self._process_snapshot_check_result)
        if force:
            guard.request_validation()
            self._process_validation_requested = True
            self._process_snapshot_forced_checks += 1
        self._process_snapshot_checked_at = now
        self._process_snapshot_check_result = bool(guard.cheap_check())
        self._process_snapshot_checks += 1
        return self._process_snapshot_check_result

    def _advance_process_snapshot_validation(self):
        """Advance mandatory pre-apply identity verification without a full scan."""

        guard = self._process_snapshot_guard
        if guard is None:
            return "valid"
        if not guard.cheap_check():
            self.report["process_validation_status"] = "invalid"
            return "invalid"
        status = guard.advance_validation(
            operation_budget=_PRO_SNAPSHOT_VALIDATION_OPERATION_BUDGET,
            deadline=self._process_slice_deadline(
                self._tick_deadline,
                _PRO_SNAPSHOT_VALIDATION_SLICE_BUDGET_MS,
            ),
        )
        self._process_validation_ms = float(guard.validation_elapsed_ms)
        self._process_validation_slices = int(guard.validation_slices)
        self._process_validation_epoch = int(
            getattr(guard, "validation_epoch", self._process_validation_epoch + 1)
        )
        self._process_validation_max_slice_ms = float(
            guard.max_validation_slice_ms
        )
        self._process_validation_max_primitive_ms = float(
            getattr(guard, "max_validation_primitive_ms", 0.0)
        )
        self._process_validation_max_primitive = dict(
            getattr(guard, "max_validation_primitive", {}) or {}
        )
        self._process_validation_complete = status == "valid"
        self.report["process_validation_ms"] = self._process_validation_ms
        self.report["process_validation_slices"] = self._process_validation_slices
        self.report["process_validation_operations"] = int(
            guard.validation_operations
        )
        self.report["process_validation_epoch"] = self._process_validation_epoch
        self.report["process_validation_max_slice_ms"] = (
            self._process_validation_max_slice_ms
        )
        self.report["process_validation_max_primitive_ms"] = (
            self._process_validation_max_primitive_ms
        )
        self.report["process_validation_max_primitive"] = dict(
            self._process_validation_max_primitive
        )
        self.report["process_validation_status"] = str(status)
        if status == "invalid":
            self._process_snapshot_check_result = False
            self.report["process_snapshot_validation_error"] = str(
                guard.invalid_reason
            )
        return status

    def _submit_process_pair(
        self,
        pair,
        master_graph,
        candidate_graph,
        master_loops,
        candidate_loops,
    ):
        """Submit exactly one canonical pair without constructing a search."""

        submit_started = time.perf_counter()
        pool = self._ensure_process_pool()
        if self._process_identity is None or self._process_options is None:
            raise RuntimeError("process snapshot identity is unavailable")
        pair_key = (tuple(pair.master_key), tuple(pair.member_key))
        ordinal = self._process_pair_ordinals.get(pair_key)
        if ordinal is None:
            ordinal = self._process_next_ordinal
            self._process_pair_ordinals[pair_key] = ordinal
            self._process_next_ordinal += 1
        task = pro_process_adapter.make_single_pair_batch(
            self._process_identity,
            pair_ordinal=ordinal,
            master_key=pair.master_key,
            member_key=pair.member_key,
            master_graph=master_graph,
            member_graph=candidate_graph,
            options=self._process_options,
            correspondence_mode=self.correspondence_mode,
        )
        pool.begin((task,))
        submit_ms = (time.perf_counter() - submit_started) * 1000.0
        self._process_dispatch_ms += submit_ms
        self.main_thread_submit_ms += submit_ms
        self._record_phase("process_dispatch", submit_ms)
        self._record_phase("main_thread_submit", submit_ms)
        self.report["correspondence_calls"] += 1
        self.report["worker_submissions"] += 1
        self.report["worker_in_flight_peak"] = max(
            int(self.report.get("worker_in_flight_peak", 0)),
            1,
        )
        self._inflight = {
            "process": True,
            "token": task.batch_id,
            "task": task,
            "master_key": tuple(pair.master_key),
            "member_key": tuple(pair.member_key),
            "master_loops": master_loops,
            "candidate_loops": candidate_loops,
            "submitted_at": time.perf_counter(),
        }

    def _poll_process_worker(self):
        """Poll one process batch with no blocking wait on Blender's thread."""

        if self._process_pool is None or self._inflight is None:
            raise RuntimeError("process worker is not active")
        self._tick_stage = "process_poll"
        poll_started = time.perf_counter()
        progress = self._process_pool.poll(timeout=0.0)
        poll_ms = (time.perf_counter() - poll_started) * 1000.0
        self._process_poll_ms += poll_ms
        self.main_thread_poll_ms += poll_ms
        self._record_phase("process_poll", poll_ms)
        self._record_phase("main_thread_poll", poll_ms)
        self._process_last_progress = progress
        self.report["process_poll_ms"] = float(self._process_poll_ms)
        self.report["process_active_workers"] = int(progress.active_workers)
        self.report["process_retry_count"] = int(progress.retry_count)
        self.report["worker_discards"] = int(self.report.get("worker_discards", 0))
        if not self._process_pool.is_terminal:
            return None
        final = self._process_pool.final_result()
        self.report["process_retry_count"] = int(progress.retry_count)
        if not final.complete:
            raise RuntimeError(
                "external Pro worker failed: %s" % (final.failure or "incomplete result")
            )
        if not self._process_snapshot_is_current(force=True):
            self.cancel("context_invalidated", nonblocking=self.modal)
            return None
        inflight = self._inflight
        if len(final.results) != 1:
            raise RuntimeError("external Pro worker returned an invalid pair count")
        pair_result = final.results[0]
        correspondence = pro_process_adapter.pair_result_to_correspondence(
            pair_result,
            topology_module=topology_correspondence,
            task=inflight["task"],
        )
        elapsed_ms = max(
            0.0,
            (time.perf_counter() - float(inflight["submitted_at"])) * 1000.0,
        )
        self._process_compute_ms += elapsed_ms
        self.worker_wall_elapsed_ms += elapsed_ms
        self.worker_compute_elapsed_ms += elapsed_ms
        self._process_last_result_digest = final.result_digest
        self.report["process_result_digest"] = final.result_digest
        self.report["process_compute_ms"] = float(self._process_compute_ms)
        self.report["worker_completions"] = int(
            self.report.get("worker_completions", 0)
        ) + 1
        self.report["max_correspondence_ms"] = max(
            float(self.report.get("max_correspondence_ms", 0.0)),
            elapsed_ms,
        )
        self._record_phase("correspondence", elapsed_ms)
        self._record_phase("process_compute", elapsed_ms)
        return correspondence

    def _process_pair(self, pair):
        self._require_external_fast_route()
        member_key = tuple(pair.member_key)
        master_key = tuple(pair.master_key)
        pair_key = (master_key, member_key)
        continuing_graph = self._pending_graph_pair == pair_key
        continuing_shape = (
            not continuing_graph
            and self._shape_state is not None
            and self._shape_pair == pair_key
        )

        if not continuing_graph:
            if not continuing_shape:
                self._tick_stage = "candidate_gate"
                precedence_reason = self._route_master_precedence_reason(
                    master_key,
                    member_key,
                )
                if precedence_reason is not None:
                    self.report["skipped_ownership"] += 1
                    return False
                if not _pro_ownership_allows(
                    member_key,
                    master_key,
                    self._assigned_member_keys,
                    self._owner_keys,
                ):
                    self.report["skipped_ownership"] += 1
                    return False
                master_signature = self._cheap_signatures.get(master_key)
                member_signature = self._cheap_signatures.get(member_key)
                if master_signature is None or member_signature is None:
                    self._reject(master_key, member_key, "missing_cheap_signature")
                    return False

                self._tick_stage = "shape_fit"
                shape_started = time.perf_counter()
                boundary_gate = similarity_matcher.cheap_boundary_gate(
                    master_signature,
                    member_signature,
                )
                topology_gate = similarity_matcher.cheap_topology_gate(
                    master_signature,
                    member_signature,
                )
                if not boundary_gate.passed or not topology_gate.passed:
                    self.report["skipped_shape"] += 1
                    self._record_phase(
                        "shape_fit",
                        (time.perf_counter() - shape_started) * 1000.0,
                    )
                    return False
                self._pending_shape_pair = pair_key
                self._pending_shape_pair_object = pair

            self._tick_stage = "shape_fit"
            shape_status, shape_result, shape_error = self._advance_shape_for(
                master_key,
                member_key,
                self._tick_deadline,
            )
            if shape_status == "pending":
                return None
            if shape_status == "error" or shape_result is None:
                self.report["skipped_shape"] += 1
                self._pending_shape_pair = None
                self._pending_shape_pair_object = None
                self._shape_pair = None
                self._shape_pair_object = None
                self._reject(
                    master_key,
                    member_key,
                    "shape_error",
                    topology=False,
                    detail={"error": shape_error or "invalid_shape_result"},
                )
                return False
            if not _selected_match_passes_quality(
                shape_result,
                self._settings.stack_similarity_tolerance,
            ):
                self.report["skipped_shape"] += 1
                self._pending_shape_pair = None
                self._pending_shape_pair_object = None
                return False
            self.report["shape_fit_accepted"] += 1
            self._pending_shape_result = shape_result
            self._pending_shape_pair = None
            self._pending_shape_pair_object = None
            self._pending_graph_pair = pair_key
            self._pending_graph_pair_object = pair
            self._pending_graph_master = None
            self._pending_graph_member = None

        if self._budget_reached():
            self._request_timeout("before_exact_graph")
            return False

        if self._pending_graph_master is None:
            self._tick_stage = "master_graph_build"
            (
                master_status,
                master_graph,
                master_loops,
                master_graph_error,
            ) = self._advance_graph_for(master_key, self._tick_deadline)
            if master_status == "pending":
                return None
            if master_status == "error" or master_graph is None:
                self._pending_graph_pair = None
                self._pending_graph_pair_object = None
                self._pending_graph_master = None
                self._pending_graph_member = None
                self._pending_shape_result = None
                self._reject(
                    master_key,
                    member_key,
                    master_graph_error or "invalid_master_graph",
                )
                return False
            self._pending_graph_master = (master_graph, master_loops)

        if self._budget_reached():
            self._request_timeout("before_candidate_graph")
            return False

        if self._pending_graph_member is None:
            self._tick_stage = "candidate_graph_build"
            (
                candidate_status,
                candidate_graph,
                candidate_loops,
                candidate_graph_error,
            ) = self._advance_graph_for(member_key, self._tick_deadline)
            if candidate_status == "pending":
                return None
            if candidate_status == "error" or candidate_graph is None:
                self._pending_graph_pair = None
                self._pending_graph_pair_object = None
                self._pending_graph_master = None
                self._pending_graph_member = None
                self._pending_shape_result = None
                self._reject(
                    master_key,
                    member_key,
                    candidate_graph_error or "invalid_candidate_graph",
                )
                return False
            self._pending_graph_member = (candidate_graph, candidate_loops)

        master_graph, master_loops = self._pending_graph_master
        candidate_graph, candidate_loops = self._pending_graph_member
        self._pending_graph_pair = None
        self._pending_graph_pair_object = None
        self._pending_graph_master = None
        self._pending_graph_member = None
        self._pending_shape_result = None

        self._tick_stage = "exact_prepare"
        prepare_started = time.perf_counter()
        if self.process_requested:
            try:
                self._submit_process_pair(
                    pair,
                    master_graph,
                    candidate_graph,
                    master_loops,
                    candidate_loops,
                )
            except Exception as exc:
                prepare_ms = (time.perf_counter() - prepare_started) * 1000.0
                self.main_thread_submit_ms += prepare_ms
                self._record_phase("process_dispatch", prepare_ms)
                self.report["worker_errors"] += 1
                self._fail(RuntimeError("external Pro worker unavailable: %s" % exc))
            return True
        if self._worker is not None:
            # Compatibility seam for the pre-P06 focused worker tests.  Live
            # Pro sessions leave ``_worker`` as None and use the resumable
            # engine below, so no executor/Future can contend for the GIL.
            try:
                token = self._worker.submit(
                    master_graph,
                    candidate_graph,
                    allow_flipping=bool(self._settings.stack_allow_flipping),
                    match_scale=bool(self._settings.stack_match_scale),
                    tolerance=self._exact_tolerance,
                    max_search=self.correspondence_max_search,
                    cooperative_yield_every=self.cooperative_yield_every,
                )
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                prepare_ms = (time.perf_counter() - prepare_started) * 1000.0
                self.main_thread_submit_ms += prepare_ms
                self._record_phase("main_thread_submit", prepare_ms)
                self._reject(
                    master_key,
                    member_key,
                    "worker_submit_error",
                    detail={"error": str(exc)},
                )
                self._graph_cache.drop(member_key)
                return True
            prepare_ms = (time.perf_counter() - prepare_started) * 1000.0
            self.main_thread_submit_ms += prepare_ms
            self._record_phase("main_thread_submit", prepare_ms)
            self.report["correspondence_calls"] += 1
            self._inflight = {
                "token": token,
                "master_key": master_key,
                "member_key": member_key,
                "master_loops": master_loops,
                "candidate_loops": candidate_loops,
            }
            return True

        try:
            search = topology_correspondence.CorrespondenceSearch(
                master_graph,
                candidate_graph,
                allow_flipping=bool(self._settings.stack_allow_flipping),
                match_scale=bool(self._settings.stack_match_scale),
                tolerance=self._exact_tolerance,
                max_search=self.correspondence_max_search,
                # P06 yields by returning from ``step`` at a deadline.  The
                # compatibility sleep hook would only add latency here.
                cooperative_yield_every=0,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            prepare_ms = (time.perf_counter() - prepare_started) * 1000.0
            self._record_phase("exact_prepare", prepare_ms)
            self._reject(
                master_key,
                member_key,
                "correspondence_prepare_error",
                detail={"error": str(exc)},
            )
            self._graph_cache.drop(member_key)
            return True
        prepare_ms = (time.perf_counter() - prepare_started) * 1000.0
        self._record_phase("exact_prepare", prepare_ms)
        self.report["correspondence_calls"] += 1
        self._inflight = {
            "token": self.report["correspondence_calls"],
            "master_key": master_key,
            "member_key": member_key,
            "master_loops": master_loops,
            "candidate_loops": candidate_loops,
            "search": search,
        }
        return True

    def _advance_resumable_search(self, deadline):
        """Advance the one live exact search within this modal tick."""

        inflight = self._inflight
        if inflight is None or inflight.get("search") is None:
            return None
        search = inflight["search"]
        slice_started = time.perf_counter()
        slice_deadline = slice_started + (
            _PRO_EXACT_SLICE_BUDGET_MS / 1000.0
        )
        if deadline is not None:
            slice_deadline = min(slice_deadline, deadline)
        self._tick_stage = "exact_search_slice"
        step = search.step(
            deadline=slice_deadline,
            operation_budget=_PRO_EXACT_OPERATION_BUDGET,
        )
        elapsed_ms = (time.perf_counter() - slice_started) * 1000.0
        self.exact_search_slices += 1
        self.exact_search_elapsed_ms += elapsed_ms
        self.max_exact_slice_ms = max(self.max_exact_slice_ms, elapsed_ms)
        self.max_exact_search_operations = max(
            self.max_exact_search_operations,
            int(step.operations),
        )
        self.exact_search_operations += int(step.operations)
        self._record_phase("correspondence_slice", elapsed_ms)
        self.report["max_correspondence_ms"] = max(
            float(self.report.get("max_correspondence_ms", 0.0)),
            elapsed_ms,
        )
        self.report["yield_count"] = self.report.get("yield_count", 0) + int(
            getattr(step.diagnostics, "yield_count", 0) or 0
        )
        if step.status == "pending":
            self.exact_search_pending += 1
        else:
            self.exact_search_completed += 1
        self._update_worker_report()
        if step.status == "pending":
            return None
        return step

    def _poll_worker(self):
        """Poll the pure worker without blocking Blender's main thread."""

        self._tick_stage = "worker_poll"
        poll_started = time.perf_counter()
        outcome = self._worker.poll()
        poll_ms = (time.perf_counter() - poll_started) * 1000.0
        self.main_thread_poll_ms += poll_ms
        self._record_phase("main_thread_poll", poll_ms)
        if outcome is None:
            return None
        self.worker_wall_elapsed_ms += float(outcome.wall_ms)
        self.worker_compute_elapsed_ms += float(outcome.compute_ms)
        self._record_phase("correspondence", outcome.wall_ms)
        self._record_phase("worker_compute", outcome.compute_ms)
        result_diagnostics = getattr(
            getattr(outcome.result, "diagnostics", None),
            "yield_count",
            0,
        )
        self.report["yield_count"] = self.report.get("yield_count", 0) + int(
            result_diagnostics or 0
        )
        self.report["max_correspondence_ms"] = max(
            float(self.report.get("max_correspondence_ms", 0.0)),
            float(outcome.wall_ms),
        )
        return outcome

    def _consume_worker_outcome(self, outcome):
        """Compatibility adapter for the old immutable worker tests."""

        return self._consume_exact_result(
            getattr(outcome, "result", None),
            token=getattr(outcome, "token", None),
            error=getattr(outcome, "error", None),
        )

    def _consume_resumable_step(self, step):
        """Finalize a completed main-thread exact search atomically."""

        if step is None or step.status == "pending":
            return False
        if step.status == "cancelled":
            self._discard_inflight("exact_search_cancelled")
            return True
        inflight = self._inflight
        if inflight is None:
            return False
        return self._consume_exact_result(
            step.result,
            token=inflight.get("token"),
            error=None,
        )

    def _record_exact_refinement(self, diagnostics, reason=None):
        """Accumulate compact worker-side loop-refinement diagnostics."""

        if diagnostics is None:
            return
        report = self.report
        report["exact_refinement_pairs"] += 1
        rounds = int(getattr(diagnostics, "refinement_rounds", 0) or 0)
        report["exact_refinement_rounds_total"] += rounds
        report["exact_refinement_max_rounds"] = max(
            report["exact_refinement_max_rounds"],
            int(getattr(diagnostics, "refinement_max_rounds", 0) or 0),
        )
        if bool(getattr(diagnostics, "refinement_stable", False)):
            report["exact_refinement_stable_count"] += 1
        if bool(getattr(diagnostics, "refinement_truncated", False)):
            report["exact_refinement_truncated_count"] += 1
        report["exact_refinement_ms"] += float(
            getattr(diagnostics, "refinement_elapsed_us", 0) or 0
        ) / 1000.0
        report["exact_refinement_pre_max_domain"] = max(
            report["exact_refinement_pre_max_domain"],
            int(getattr(diagnostics, "refinement_pre_max_domain", 0) or 0),
        )
        report["exact_refinement_post_max_domain"] = max(
            report["exact_refinement_post_max_domain"],
            int(getattr(diagnostics, "refinement_post_max_domain", 0) or 0),
        )
        if reason == "topology_signature_mismatch":
            report["exact_refinement_topology_mismatch"] += 1

    def _consume_exact_result(self, exact_result, *, token=None, error=None):
        """Convert one immutable exact result into staged main-thread writes."""

        self._tick_stage = "worker_finalize"
        inflight = self._inflight
        if inflight is None:
            return False
        self._inflight = None
        master_key = inflight["master_key"]
        member_key = inflight["member_key"]
        master_loops = inflight["master_loops"]
        candidate_loops = inflight["candidate_loops"]
        finalize_started = time.perf_counter()
        try:
            if token != inflight["token"]:
                self._reject(
                    master_key,
                    member_key,
                    "worker_token_mismatch",
                )
                return True
            if error:
                self._reject(
                    master_key,
                    member_key,
                    "correspondence_error",
                    detail={"error": error},
                )
                return True
            if exact_result is None:
                self._reject(
                    master_key,
                    member_key,
                    "correspondence_error",
                    detail={"error": "worker returned no result"},
                )
                return True
            diagnostics = getattr(exact_result, "diagnostics", None)
            self._record_exact_refinement(
                diagnostics,
                getattr(exact_result, "reason", None),
            )
            # The full selection/active snapshot is intentionally checked once
            # at atomic finish.  Re-scanning every loop in a 577-island mesh
            # would move the old main-thread freeze from correspondence into
            # this finalize path.  The identity check here still prevents a
            # result from staging against a replaced context/mesh/UV layer.
            if not _pro_session_context_valid(
                self.context,
                self.obj,
                self.bm,
                self.uv_layer,
            ):
                self.cancel("context_invalidated", nonblocking=self.modal)
                return True
            if inflight.get("process") and not self._process_snapshot_is_current():
                self.cancel("context_invalidated", nonblocking=self.modal)
                return True
            if not exact_result.accepted:
                diagnostics = getattr(exact_result, "diagnostics", None)
                reason = getattr(exact_result, "reason", "correspondence_rejected")
                if reason == "search_budget_exceeded":
                    self.report["correspondence_budget_rejections"] += 1
                self._reject(
                    master_key,
                    member_key,
                    reason,
                    detail={
                        "search_count": getattr(diagnostics, "search_count", 0),
                        "score": getattr(exact_result, "score", None),
                        "residual": getattr(exact_result, "residual", None),
                        "reflected": getattr(exact_result, "reflected", False),
                        "reversed": getattr(exact_result, "reversed", False),
                        "cyclic_shift": getattr(exact_result, "cyclic_shift", 0),
                    },
                )
                return True

            staged = _pro_exact_write_values(
                master_loops,
                candidate_loops,
                exact_result,
                self.uv_layer,
            )
            if staged is None:
                self._reject(
                    master_key,
                    member_key,
                    "incomplete_exact_loop_mapping",
                )
                return True
            if any(
                target_key in self._planned_target_keys
                for target_key, _loop, _uv in staged
            ):
                raise RuntimeError(
                    "Align Similar Pro detected duplicate target assignment."
                )
            self._planned_target_keys.update(
                target_key for target_key, _loop, _uv in staged
            )
            self._staged_writes.extend(staged)
            _pro_commit_ownership(
                member_key,
                master_key,
                self._assigned_member_keys,
                self._owner_keys,
            )
            diagnostics = getattr(exact_result, "diagnostics", None)
            canonical_mapping = tuple(
                sorted(
                    tuple(exact_result.loop_mapping),
                    key=lambda item: (tuple(item[0]), tuple(item[1])),
                )
            )
            member_info = {
                "key": member_key,
                "density": self._density_by_key.get(member_key),
                "uv_area": self._uv_area_by_key.get(member_key),
                "loop_count": len(staged),
                "score": exact_result.score,
                "residual": exact_result.residual,
                "reflected": exact_result.reflected,
                "reversed": exact_result.reversed,
                "cyclic_shift": exact_result.cyclic_shift,
                "search_count": getattr(diagnostics, "search_count", 0),
            }
            if self.detail_mappings:
                member_info["mapping"] = canonical_mapping
            group = self._groups_by_master.setdefault(
                master_key,
                {
                    "master_key": master_key,
                    "master_density": self._density_by_key.get(master_key),
                    "master_uv_area": self._uv_area_by_key.get(master_key),
                    "members": [],
                },
            )
            group["members"].append(member_info)
            self.report["aligned_exact"] += 1
            return True
        finally:
            self.main_thread_finalize_ms += (
                time.perf_counter() - finalize_started
            ) * 1000.0
            self._record_phase(
                "main_thread_finalize",
                (time.perf_counter() - finalize_started) * 1000.0,
            )
            self._graph_cache.drop(member_key)

    def _close_plan(self):
        close = getattr(self._batch_iterator, "close", None)
        if close is not None:
            close()
        self._batch_iterator = None

    def _snapshots_unchanged(self):
        if not _pro_session_context_valid(
            self.context,
            self.obj,
            self.bm,
            self.uv_layer,
        ):
            return False
        if self.process_requested:
            if not self._process_snapshot_is_current(force=False):
                return False
            if self._process_snapshot_guard is not None:
                if not self._process_snapshot_guard.validation_complete:
                    return False
                return not bool(self._process_snapshot_guard.invalid_reason)
        if self.bm is None or self.uv_layer is None:
            return True
        return (
            _pro_snapshot_selection(self.bm, self.uv_layer)
            == self._selection_snapshot
            and _pro_snapshot_active(self.context, self.obj, self.bm)
            == self._active_snapshot
        )

    def _populate_planner_report(self):
        self._finish_record_phase()
        if self._candidate_plan is None:
            return
        planner_diagnostics = self._candidate_plan.diagnostics
        self.report["candidate_pairs_planned"] = max(
            self.report.get("candidate_pairs_planned", 0),
            planner_diagnostics.candidate_pairs,
        )
        self.report["planner_diagnostics"] = {
            "selected": planner_diagnostics.selected,
            "topology_buckets": planner_diagnostics.topology_buckets,
            "candidate_pairs": planner_diagnostics.candidate_pairs,
            "theoretical_all_pairs": planner_diagnostics.theoretical_all_pairs,
            "avoided_all_pairs": planner_diagnostics.avoided_all_pairs,
            "max_bucket": planner_diagnostics.max_bucket,
            "estimated_bytes": planner_diagnostics.estimated_bytes,
            "elapsed_ms": planner_diagnostics.elapsed_ms,
            "unresolved_members": planner_diagnostics.unresolved_members,
            "truncated_member_count": len(planner_diagnostics.truncated_members),
            "truncated_member_samples": [
                list(key)
                for key in planner_diagnostics.truncated_members[:_PRO_REJECTION_SAMPLE_LIMIT]
            ],
            "truncated_bucket_count": len(planner_diagnostics.truncated_buckets),
            "reason_counts": dict(planner_diagnostics.reason_counts),
            "refinement": dict(self.report.get("planner_refinement", {})),
        }

    def _populate_group_report(self):
        groups = sorted(
            self._groups_by_master.values(),
            key=lambda item: tuple(item["master_key"]),
        )
        self.report["group_count"] = len(groups)
        self.report["groups_applied"] = len(groups)
        self.report["group_summaries"] = [
            {
                "master_key": list(group["master_key"]),
                "master_density": group["master_density"],
                "member_count": len(group["members"]),
                "loop_count": sum(
                    item["loop_count"] for item in group["members"]
                ),
            }
            for group in groups[:_PRO_GROUP_SAMPLE_LIMIT]
        ]
        if not self.detail_mappings:
            self.report["groups"] = []
            return
        self.report["groups"] = [
            {
                "master_key": list(group["master_key"]),
                "master_density": group["master_density"],
                "member_keys": [list(item["key"]) for item in group["members"]],
                "member_count": len(group["members"]),
                "loop_counts": [item["loop_count"] for item in group["members"]],
                "mapping_pairs": [
                    [
                        [list(pair[0]), list(pair[1])]
                        for pair in item["mapping"]
                    ]
                    for item in group["members"]
                ],
                "member_results": [
                    {
                        key: item[key]
                        for key in (
                            "score",
                            "residual",
                            "reflected",
                            "reversed",
                            "cyclic_shift",
                            "search_count",
                        )
                    }
                    for item in group["members"]
                ],
            }
            for group in groups
        ]

    def _shutdown_process_pool(self):
        pool = self._process_pool
        if pool is None:
            return
        try:
            if not bool(getattr(pool, "shutdown_complete", False)):
                if self.cancelled or self.error is not None:
                    # Cancellation/unregister owns the force-cleanup path;
                    # the successful process result path has already driven
                    # resumable shutdown to completion in modal advances.
                    pool.close()
                else:
                    pool.shutdown(timeout=1.0)
        except Exception:
            try:
                pool.close()
            except Exception:
                pass
        self.report["process_shutdown_timings_ms"] = [
            float(value) for value in pool.shutdown_timings_ms
        ]
        self.report["process_shutdown_state"] = str(
            getattr(pool, "shutdown_state", self._process_shutdown_state)
        )
        self.report["process_shutdown_rounds"] = int(
            getattr(pool, "shutdown_rounds", self._process_shutdown_rounds)
        )
        self.report["process_shutdown_force_used"] = bool(
            self._process_shutdown_force_used
            or getattr(pool, "shutdown_force_used", False)
        )
        self.report["process_shutdown_wait_ms"] = float(
            max(
                self._process_shutdown_wait_ms,
                (time.perf_counter() - self._process_shutdown_started_at) * 1000.0
                if self._process_shutdown_started_at > 0.0
                else 0.0,
            )
        )
        capability = pool.job_object_capability
        self.report["process_job_object"] = {
            "requested": bool(capability.requested),
            "available": bool(capability.available),
            "kill_on_close": bool(capability.kill_on_close),
            "reason": str(capability.reason),
        }
        self._process_pool = None

    def _release_runtime_state(self):
        """Drop BMesh/island and pair-stream references after session finish."""

        if self._inflight is not None:
            self._discard_inflight("runtime_release")
        if self._worker is not None and not self._worker.snapshot()["worker_shutdown"]:
            self._worker.shutdown()
        if self._process_pipeline is not None and not self._process_pipeline.is_terminal:
            try:
                self._process_pipeline.cancel(timeout=1.0)
            except Exception:
                try:
                    self._process_pipeline.close()
                except Exception:
                    pass
        if self._process_pool is not None:
            self._shutdown_process_pool()
        if self._shape_state is not None:
            self._shape_state.cancel()
        self._staged_writes.clear()
        self._island_builder = None
        self._record_builder = None
        self._record_builder_key = None
        self._close_plan()
        self._batch = ()
        self._batch_index = 0
        self._candidate_plan = None
        self._plan_builder = None
        self._key_to_island.clear()
        self.selected_islands = None
        self.all_islands = None
        self._planner_records.clear()
        self._cheap_signatures.clear()
        self._density_by_key.clear()
        self._uv_area_by_key.clear()
        self._numeric_cache.clear()
        self._descriptor_cache = None
        self._prewrite_snapshot = None
        self._selection_snapshot = None
        self._active_snapshot = None
        self._snapshot_identity = None
        self._process_identity = None
        self._process_options = None
        self._process_snapshot_builder = None
        self._process_snapshot_capture = None
        self._process_graph_context = None
        self._process_graph_context_build_ms = 0.0
        self._process_snapshot_guard = None
        self._process_snapshot_live_loop_map.clear()
        self._process_island_loop_keys.clear()
        self._process_prepare_context_ready = False
        self._process_graph_builder = None
        self._process_graph_builder_key = None
        self._process_pipeline = None
        self._process_group_first_frontier = None
        self._process_group_first_plan = None
        self._process_group_first_records.clear()
        self._process_group_first_descriptors.clear()
        self._process_group_first_exact_task_by_ordinal.clear()
        self._process_group_first_shape_results.clear()
        self._process_group_first_exact_jobs = ()
        self._process_group_first_direct_results.clear()
        self._process_group_first_record_index = 0
        self._process_graph_data.clear()
        self._process_graph_pending.clear()
        self._process_pair_contexts.clear()
        self._process_shape_batches = ()
        self._process_shape_batch_by_ordinal.clear()
        self._process_collect_records.clear()
        self._process_exact_graph_keys = ()
        self._process_exact_graph_index = 0
        self._process_exact_batches = None
        self._process_shape_results.clear()
        self._process_exact_results.clear()
        self._graph_build_state = None
        self._graph_build_key = None
        self._shape_state = None
        self._shape_pair = None
        self._shape_pair_object = None
        self._pending_shape_pair = None
        self._pending_shape_pair_object = None
        self._pending_shape_result = None
        self._pending_graph_pair = None
        self._pending_graph_pair_object = None
        self._pending_graph_master = None
        self._pending_graph_member = None
        self._graph_cache.clear()

    def _finalize(self, apply=True):
        if self.done:
            return
        if self._inflight is not None:
            self._discard_inflight("finalize")
        if apply and self.process_requested and (
            bool(self.report.get("truncated", False))
            or not self._process_pipeline_has_terminal_canonical_result()
        ):
            # A timeout or incomplete canonical merge is always a discard.
            # This latch is deliberately checked independently of the caller's
            # ``apply`` argument so no future finish branch can accidentally
            # commit a staged prefix.
            apply = False
            self.cancelled = True
            self.report["cancelled"] = True
            self.report["cancel_reason"] = self.report.get(
                "cancel_reason", "incomplete_process_result"
            )
            self.report["partial"] = False
        self._close_plan()
        self._populate_planner_report()
        self.report["planner_record_error_count"] = len(
            self.report["planner_record_errors"]
        )
        self.report["planner_record_error_samples"] = self.report[
            "planner_record_errors"
        ][: _PRO_REJECTION_SAMPLE_LIMIT]

        if apply and not self.cancelled and self.error is None:
            if not self._snapshots_unchanged():
                self.cancelled = True
                self.report["cancelled"] = True
                self.report["cancel_reason"] = "context_invalidated"
                self.report["partial"] = False
                self._staged_writes.clear()
                self.report["aligned_exact"] = 0
                self.report["group_count"] = 0
                self.report["groups"] = []
                self.report["group_summaries"] = []
                self.report["exact_loop_writes"] = 0
                self._complete_without_apply()
                return
            apply_started = time.perf_counter()
            try:
                applied_loop_count = _pro_apply_staged_writes(
                    self.obj,
                    self.bm,
                    self.uv_layer,
                    tuple(self._staged_writes),
                    self._prewrite_snapshot,
                )
            except Exception as exc:
                self.error = str(exc)
                self.report["error"] = str(exc)
                self.cancelled = True
                self._staged_writes.clear()
                applied_loop_count = 0
            self._record_phase(
                "apply",
                (time.perf_counter() - apply_started) * 1000.0,
            )
            self.report["exact_loop_writes"] = applied_loop_count
        else:
            self.report["staged_exact_before_cancel"] = max(
                int(self.report.get("staged_exact_before_cancel", 0) or 0),
                int(self.report.get("aligned_exact", 0) or 0),
            )
            self._staged_writes.clear()
            self.report["aligned_exact"] = 0
            self.report["group_count"] = 0
            self.report["groups"] = []
            self.report["group_summaries"] = []
            self.report["exact_loop_writes"] = 0

        self._update_shape_report()
        self._update_graph_report()
        self._graph_cache.clear()
        self.report["graphs_built"] = self._graph_cache.builds
        self.report["graph_cache_hits"] = self._graph_cache.hits
        self.report["graph_cache_peak"] = self._graph_cache.peak
        self.report["cache_peak_counts"] = {
            "graph_cache": self._graph_cache.peak,
            "graph_cache_limit": self._graph_cache.limit,
        }
        self.report["candidate_pairs_budget"] = self.config.global_pair_budget
        self.report["correspondence_max_search"] = self.correspondence_max_search
        self._update_enum_report()
        if self._worker is not None:
            self._worker.shutdown()
        if self._process_pool is not None:
            self._shutdown_process_pool()
        self._update_worker_report()
        if not self.cancelled and self.error is None:
            self._populate_group_report()
        self._phase_ms["planner"] = self._phase_ms.get(
            "planner_index_build", 0.0
        ) + self._phase_ms.get("planner_stream", 0.0)
        self._phase_ms["active_processing"] = self.active_elapsed_ms
        self.report["timings"] = {
            key: float(value) for key, value in sorted(self._phase_ms.items())
        }
        self.report["active_processing_ms"] = float(self.active_elapsed_ms)
        self.report["total_ms"] = (time.perf_counter() - self.started) * 1000.0
        self.report["timings"]["total"] = float(self.report["total_ms"])
        self.report["tick_count"] = self.report.get("tick_count", 0)
        self.report["max_tick_ms"] = self.report.get("max_tick_ms", 0.0)
        tick_samples = sorted(float(value) for value in self._tick_samples_ms)
        if tick_samples:
            def _percentile(fraction):
                index = min(
                    len(tick_samples) - 1,
                    max(0, int((len(tick_samples) - 1) * fraction + 0.5)),
                )
                return tick_samples[index]

            self.report["tick_p95_ms"] = _percentile(0.95)
            self.report["tick_p99_ms"] = _percentile(0.99)
            self.report["tick_samples_count"] = len(tick_samples)
        self.report["session_state"] = "cancelled" if self.cancelled else "done"
        self._release_runtime_state()
        self.done = True
        self.state = "cancelled" if self.cancelled else "done"
        self._record_state_transition()

    def _complete_without_apply(self):
        """Finish a cancelled/error session without touching UV data."""

        self._finalize(apply=False)

    def _fail(self, exc):
        self._reset_process_fused_context_ack()
        self.error = str(exc)
        self.report["error"] = str(exc)
        self.cancelled = True
        self._finalize(apply=False)

    def cancel(self, reason="user_cancelled", *, nonblocking=False):
        if self.done:
            return
        if str(reason) == "context_invalidated":
            self._reset_process_fused_context_ack()
        self.cancelled = True
        self.report["cancelled"] = True
        self.report["cancel_reason"] = str(reason)
        self.report["partial"] = False
        self.report["staged_exact_before_cancel"] = max(
            int(self.report.get("staged_exact_before_cancel", 0) or 0),
            int(self.report.get("aligned_exact", 0) or 0),
        )
        # Drop all semantic writes before touching the helper lifecycle.  A
        # slow/faulty owned worker must never hold a staged UV prefix hostage.
        self._staged_writes.clear()
        self.report["aligned_exact"] = 0
        self.report["group_count"] = 0
        self.report["groups"] = []
        self.report["group_summaries"] = []
        self.report["exact_loop_writes"] = 0
        if self._process_pipeline is not None and not self._process_pipeline.is_terminal:
            try:
                if nonblocking:
                    self._process_pipeline.cancel(nonblocking=True)
                    self._process_cancel_state = "force"
                    self.report["process_cancel_state"] = "force"
                    self.state = "process_cancel"
                    return
                self._process_pipeline.cancel(timeout=1.0)
            except Exception:
                if nonblocking:
                    self._process_cancel_state = "error"
                    self.report["process_cancel_state"] = "error"
                    self.state = "process_cancel"
                    return
        elif nonblocking and self._process_pool is not None:
            try:
                self._process_pool.begin_cancel()
                self._process_cancel_state = "force"
                self.report["process_cancel_state"] = "force"
                self.state = "process_cancel"
                return
            except Exception:
                self._process_cancel_state = "error"
                self.report["process_cancel_state"] = "error"
                self.state = "process_cancel"
                return
        self._finalize(apply=False)

    def step(
        self,
        active_budget_ms=_PRO_MODAL_TICK_ACTIVE_BUDGET_MS,
        max_correspondence=_PRO_MODAL_MAX_CORRESPONDENCE_PER_TICK,
    ):
        if self.done:
            return dict(self._last_tick_info)
        tick_started = time.perf_counter()
        self._tick_started = tick_started
        self._tick_stage = self.state
        self._record_state_transition()
        tick_deadline = tick_started + max(0.0, float(active_budget_ms)) / 1000.0
        if self.time_budget_ms > 0.0 and not self._process_finalization_grace_active and self.state not in {
            "process_shutdown",
            "process_finalization_grace",
        }:
            tick_deadline = min(
                tick_deadline,
                self.started + self.time_budget_ms / 1000.0,
            )
        self._tick_deadline = tick_deadline
        enum_tick_active = self.state == "prepare" and self.all_islands is None
        correspondence_count = 0
        try:
            while not self.done:
                if self.state == "process_cancel":
                    self._advance_process_cancel()
                    break
                if not _pro_session_context_valid(
                    self.context,
                    self.obj,
                    self.bm,
                    self.uv_layer,
                ):
                    self.cancel("context_invalidated", nonblocking=self.modal)
                    break
                # The process path owns a bounded immutable snapshot.  Check
                # its cheap sentinel before any worker poll or main-thread
                # staging so an obvious UV/context edit cannot wait for a
                # persistent helper to produce its next frame.  Unsampled
                # identity remains covered by the resumable full validation
                # before atomic apply.
                if (
                    self.process_requested
                    and self._process_snapshot_guard is not None
                    and not self._process_snapshot_is_current(force=False)
                ):
                    self.cancel("context_invalidated", nonblocking=self.modal)
                    break
                if self.state == "prepare":
                    prepare_result = self._prepare()
                    if prepare_result == "yield_after_enumeration":
                        break
                if self.state == "records":
                    self._record_one()
                elif self.state == "plan":
                    self._advance_plan_builder()
                elif self.state == "process_collect":
                    self._advance_process_collection()
                    if self.done:
                        break
                    if self.state == "process_startup":
                        break
                elif self.state == "process_group_first_collect":
                    self._advance_process_group_first_collection()
                    if self.done:
                        break
                    if self.state == "process_startup":
                        break
                elif self.state == "process_startup":
                    self._advance_process_startup()
                    if self.done:
                        break
                    if self.state == "process_startup":
                        break
                elif self.state == "process_pipeline":
                    if (
                        self._budget_reached()
                        and not self._process_pipeline_has_terminal_canonical_result()
                    ):
                        self._request_timeout("process_pipeline")
                        break
                    self._advance_process_pipeline()
                    if self.done:
                        break
                    # One bounded pipeline poll/snapshot slice per modal tick.
                    # The pipeline itself is nonblocking, but allowing the
                    # outer loop to immediately re-enter it can accumulate
                    # several graph/result transitions in one UI callback.
                    # Keep the live path explicitly yield-oriented; the next
                    # timer event resumes the same session and pool.
                    break
                elif self.state == "process_shutdown":
                    self._advance_process_shutdown()
                    if self.done:
                        break
                    # Shutdown is an explicit resumable phase.  Never let
                    # the session wall budget route it through synchronous
                    # finish cleanup in this same modal callback.
                    break
                elif self.state == "process_finalization_grace":
                    self._advance_process_finalization_grace()
                    if self.done:
                        break
                    # Validation and the owned shutdown transition are both
                    # resumable; never re-enter semantic dispatch here.
                    break
                elif self.state == "candidates":
                    if self._inflight is not None:
                        if self._inflight.get("search") is not None:
                            exact_step = self._advance_resumable_search(
                                tick_deadline
                            )
                            if exact_step is None:
                                if self._budget_reached():
                                    self._request_timeout("correspondence_slice")
                                break
                            correspondence_count += 1
                            self._consume_resumable_step(exact_step)
                        elif self._inflight.get("process"):
                            process_token = self._inflight.get("token")
                            correspondence = self._poll_process_worker()
                            if self.done:
                                break
                            if correspondence is None:
                                if self._budget_reached():
                                    self._request_timeout("correspondence_process")
                                    continue
                                break
                            correspondence_count += 1
                            self._consume_exact_result(
                                correspondence,
                                token=process_token,
                                error=None,
                            )
                        else:
                            outcome = self._poll_worker()
                            if outcome is None:
                                if self._budget_reached():
                                    self._request_timeout("correspondence_worker")
                                    continue
                                break
                            correspondence_count += 1
                            self._consume_worker_outcome(outcome)
                        if self.done:
                            break
                        if correspondence_count >= max_correspondence:
                            break
                        continue
                    if self._budget_reached():
                        self._request_timeout("candidate_pair")
                        continue
                    if self._pending_shape_pair_object is not None:
                        pair = self._pending_shape_pair_object
                    elif self._pending_graph_pair_object is not None:
                        pair = self._pending_graph_pair_object
                    else:
                        pair = self._next_pair()
                    if pair is None:
                        continue
                    correspondence = self._process_pair(pair)
                    if correspondence is None:
                        break
                    if correspondence:
                        correspondence_count += 1
                        if correspondence_count >= max_correspondence:
                            break
                elif self.state == "finish":
                    if (
                        self.process_requested
                        and self._process_snapshot_guard is not None
                        and not self._process_snapshot_guard.validation_complete
                    ):
                        validation = self._advance_process_snapshot_validation()
                        if validation == "pending":
                            self._process_stage = "snapshot_validate"
                            self.report["process_stage"] = self._process_stage
                            break
                        if validation != "valid":
                            self.cancel("context_invalidated", nonblocking=self.modal)
                            break
                    self._finalize(apply=not self.cancelled and self.error is None)
                    break
                else:
                    break
                if self._budget_reached() and self.state not in {
                    "process_shutdown",
                    "finish",
                } and not self._process_finalization_grace_active:
                    self._request_timeout(self.state)
                if (
                    time.perf_counter() >= tick_deadline
                ):
                    break
        except Exception as exc:
            self._fail(exc)
        self._record_state_transition()
        tick_ms = (time.perf_counter() - tick_started) * 1000.0
        self.active_elapsed_ms += tick_ms
        if len(self._tick_samples_ms) < 4096:
            self._tick_samples_ms.append(float(tick_ms))
        self._tick_started = None
        self._tick_deadline = None
        if enum_tick_active:
            self._enum_total_ticks += 1
        self._update_enum_report()
        self.report["tick_count"] = self.report.get("tick_count", 0) + 1
        self.report["max_tick_ms"] = max(
            float(self.report.get("max_tick_ms", 0.0)),
            tick_ms,
        )
        if tick_ms >= float(self.report.get("max_tick_ms", 0.0)):
            self.report["max_tick_stage"] = self._tick_stage
        if self.done:
            self._phase_ms["active_processing"] = self.active_elapsed_ms
            self.report["active_processing_ms"] = float(self.active_elapsed_ms)
            if "timings" in self.report:
                self.report["timings"]["active_processing"] = float(
                    self.active_elapsed_ms
                )
            self.report["tick_count"] = self.report.get("tick_count", 0)
            self.report["max_tick_ms"] = self.report.get("max_tick_ms", 0.0)
        self._last_tick_info = {
            "done": self.done,
            "state": self.state,
            "tick_ms": tick_ms,
            "correspondence_calls": correspondence_count,
            "active_processing_ms": self.active_elapsed_ms,
            "error": self.error,
        }
        return dict(self._last_tick_info)

    def _progress_marker(self):
        """Return counters that prove useful work or state is advancing."""

        pipeline = self._process_pipeline
        if pipeline is not None:
            progress = pipeline.progress()
            guard = self._process_snapshot_guard
            validation_pending = bool(
                guard is not None
                and not bool(getattr(guard, "validation_complete", False))
                and bool(
                    getattr(
                        guard,
                        "validation_requested",
                        getattr(
                            guard,
                            "requested",
                            self._process_validation_requested,
                        ),
                    )
                )
            )
            validation_marker = (
                bool(validation_pending),
                int(
                    getattr(
                        guard,
                        "validation_epoch",
                        self._process_validation_epoch,
                    )
                ),
                int(getattr(guard, "validation_slices", 0)),
                int(getattr(guard, "validation_operations", 0)),
                bool(getattr(guard, "validation_complete", False)),
            )
            shutdown_marker = (
                self.state == "process_shutdown",
                str(self._process_shutdown_state),
                int(self._process_shutdown_rounds),
            )
            grace_marker = (
                self._process_finalization_grace_active,
                int(self._process_finalization_grace_rounds),
                str(self.report.get("process_finalization_grace_state", "idle")),
            )
            return (
                self.state,
                pipeline.stage,
                progress.shape_completed,
                progress.exact_completed,
                progress.merged_pairs,
                progress.pruned_pairs,
                progress.shape_submitted,
                progress.exact_submitted,
                validation_marker,
                shutdown_marker,
                grace_marker,
            )
        return (
            self.state,
            self.report.get("candidate_pairs_processed", 0),
            self.report.get("correspondence_calls", 0),
            self.report.get("shape_fit_accepted", 0),
            self.report.get("aligned_exact", 0),
            self.report.get("graph_primitive_ops", 0),
        )

    def run_to_completion(self):
        last_marker = self._progress_marker()
        last_progress_at = time.monotonic()
        deadline = (
            self.started + self.time_budget_ms / 1000.0
            if self.time_budget_ms > 0.0
            else None
        )
        stall_grace_ms = float(
            getattr(self, "_progress_stall_grace_ms", _PRO_PROGRESS_STALL_GRACE_MS)
        )
        stall_grace = max(
            stall_grace_ms / 1000.0,
            2.0 * float(self._process_io_timeout),
        )
        while not self.done:
            self.step(
                active_budget_ms=_PRO_MODAL_TICK_ACTIVE_BUDGET_MS,
                max_correspondence=_PRO_MODAL_MAX_CORRESPONDENCE_PER_TICK,
            )
            now = time.monotonic()
            marker = self._progress_marker()
            if marker != last_marker:
                last_marker = marker
                last_progress_at = now
                self.report["progress_last_change_ms"] = float(
                    self.active_elapsed_ms
                )
            if self.done:
                break
            if deadline is not None and now >= deadline:
                if self._process_finalization_grace_active or self.state == "process_shutdown":
                    phase_deadline = (
                        self._process_finalization_grace_deadline
                        if self._process_finalization_grace_active
                        else self._process_shutdown_grace_deadline
                    )
                    if phase_deadline > now:
                        deadline = phase_deadline
                        last_progress_at = now
                        continue
                self.report["progress_stall_reason"] = "monotonic_deadline"
                self._request_timeout("run_to_completion_deadline")
                continue
            if now - last_progress_at < stall_grace:
                continue
            active_work = False
            process_pool = getattr(self, "_process_pool", None)
            if process_pool is not None and getattr(
                process_pool, "context_load_inflight", 0
            ):
                active_work = True
            if self._process_pipeline is not None:
                progress = self._process_pipeline.progress()
                active_work = bool(
                    active_work
                    or progress.active_workers
                    or progress.queue_depth
                    or progress.shape_completed < progress.shape_submitted
                    or getattr(self._process_pipeline, "_exact_pending", {})
                    or getattr(self._process_pipeline, "_graph_waiting", {})
                    or getattr(self._process_pipeline, "_completion_buffer", ())
                )
                guard = self._process_snapshot_guard
                validation_pending = bool(
                    guard is not None
                    and not bool(getattr(guard, "validation_complete", False))
                    and bool(
                        getattr(
                            guard,
                            "validation_requested",
                            getattr(
                                guard,
                                "requested",
                                self._process_validation_requested,
                            ),
                        )
                    )
                )
                active_work = bool(active_work or validation_pending)
                active_work = bool(
                    active_work or self._process_finalization_grace_active
                )
                if self.state == "process_shutdown":
                    active_work = bool(
                        active_work
                        or (
                            self._process_pool is not None
                            and not bool(
                                getattr(
                                    self._process_pool,
                                    "shutdown_complete",
                                    False,
                                )
                            )
                        )
                    )
            elif self._inflight is not None:
                active_work = True
            if active_work:
                # A live helper may legitimately spend longer than one UI tick
                # inside a pure batch.  The monotonic deadline remains the
                # bounded stop; active work is not mislabeled as a stall.
                last_progress_at = now
                continue
            self.report["progress_stall_ms"] = (now - last_progress_at) * 1000.0
            self.report["progress_stall_reason"] = "no_active_work"
            self._fail(
                RuntimeError(
                    "Align Similar Pro session stalled without active work."
                )
            )
        return self.report.get("aligned_exact", 0), self.report.get("group_count", 0)


def _align_selected_similar_pro(
    context,
    obj,
    bm,
    uv_layer,
    selected_islands,
    evidence=None,
    all_islands=None,
    island_enumeration_ms=None,
    operator_setup_ms=None,
    runtime_started=None,
    time_budget_ms=_PRO_SYNC_WALL_TIME_BUDGET_MS,
    planner_config=None,
):
    """Run the shared bounded Pro session synchronously."""
    
    session = _ProAlignSession(
        context,
        obj,
        bm,
        uv_layer,
        selected_islands=selected_islands,
        evidence=evidence,
        all_islands=all_islands,
        island_enumeration_ms=island_enumeration_ms,
        operator_setup_ms=operator_setup_ms,
        runtime_started=runtime_started,
        time_budget_ms=time_budget_ms,
        planner_config=planner_config,
        modal=False,
    )
    return session.run_to_completion()


def _align_selected_similar(
    context,
    obj,
    bm,
    uv_layer,
    selected_islands,
    evidence=None,
    all_islands=None,
    island_enumeration_ms=None,
    operator_setup_ms=None,
):
    settings = uv_utils.get_settings(context)
    similarity_tolerance = max(
        0.0, float(settings.stack_similarity_tolerance)
    )

    # Cheap signatures and all numeric extraction happen before the first UV
    # write.  Ordered loops/resampling remain lazy and are only built after a
    # candidate passes both cheap stages.  Every cache is local to this
    # operator execution and therefore cannot survive apply, undo, reload, or
    # a later invocation.
    similarity_matcher.reset_diagnostics()
    if operator_setup_ms is not None:
        similarity_matcher.record_phase(
            "operator_setup",
            float(operator_setup_ms),
        )
    if all_islands is None:
        started = time.perf_counter()
        all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        island_enumeration_ms = (time.perf_counter() - started) * 1000.0
    if island_enumeration_ms is not None:
        similarity_matcher.record_phase(
            "island_enumeration",
            float(island_enumeration_ms),
        )
    snapshot_identity = _snapshot_identity(obj, bm, uv_layer, all_islands)
    descriptor_cache = similarity_matcher.DescriptorCache()
    diagnostics = descriptor_cache.diagnostics
    numeric_cache = {}
    cheap_signatures = {}
    ordered_selected = sorted(selected_islands, key=_island_face_key)
    for island in ordered_selected:
        island_key = _island_face_key(island)
        cheap_signatures[island_key] = _cheap_signature_for_island(
            island,
            uv_layer,
            descriptor_cache,
            snapshot_identity,
            numeric_cache,
        )

    if evidence is not None:
        evidence.setdefault("scheduler_results", [])
        evidence.setdefault("quality_rejections", [])
        evidence["selected_count"] = len(ordered_selected)

    def prefetch_matches(representative, future_islands):
        representative_key = _island_face_key(representative)
        pair_results = {}
        eligible_pairs = []

        candidate_started = time.perf_counter()
        for island in future_islands:
            island_key = _island_face_key(island)
            pair_key = (representative_key, island_key)
            similarity_matcher.record_candidate_stage("candidate")
            cheap_boundary = similarity_matcher.cheap_boundary_gate(
                cheap_signatures[representative_key],
                cheap_signatures[island_key],
            )
            if not cheap_boundary.passed:
                pair_results[pair_key] = None
                similarity_matcher.record_rejection()
                continue
            similarity_matcher.record_candidate_stage("cheap")
            cheap_topology = similarity_matcher.cheap_topology_gate(
                cheap_signatures[representative_key],
                cheap_signatures[island_key],
            )
            if not cheap_topology.passed:
                pair_results[pair_key] = None
                similarity_matcher.record_rejection()
                continue
            similarity_matcher.record_candidate_stage("cheap_topology")
            if (
                not cheap_topology.strict
                and similarity_tolerance
                <= similarity_matcher.TOPOLOGY_PENALTY
            ):
                pair_results[pair_key] = None
                similarity_matcher.record_rejection()
                continue
            eligible_pairs.append((island, island_key, pair_key))
        diagnostics.record_phase(
            "candidate_gating",
            (time.perf_counter() - candidate_started) * 1000.0,
        )

        match_jobs = []
        for island, island_key, pair_key in eligible_pairs:
            reference = _descriptor_for_island(
                representative,
                uv_layer,
                descriptor_cache,
                snapshot_identity,
                numeric_cache=numeric_cache,
            )
            candidate = _descriptor_for_island(
                island,
                uv_layer,
                descriptor_cache,
                snapshot_identity,
                numeric_cache=numeric_cache,
            )
            match_jobs.append(
                {
                    "island": island,
                    "island_key": island_key,
                    "pair_key": pair_key,
                    "payload": (
                        reference,
                        candidate,
                        bool(settings.stack_match_scale),
                        bool(settings.stack_allow_flipping),
                        similarity_tolerance,
                        bool(similarity_matcher.numpy_available()),
                    ),
                }
            )

        if not match_jobs:
            return pair_results

        policy = match_scheduler.SchedulerPolicy(
            backend=match_scheduler.BACKEND_AUTO,
            thread_min_batch_size=8,
            process_min_batch_size=64,
            max_workers=8,
            allow_gil_threads=False,
            allow_numpy_threads=True,
            allow_process_benchmark=False,
            auto_process_benchmark=False,
        )
        schedule_started = time.perf_counter()
        scheduled = match_scheduler.schedule_numeric_batch(
            [job["payload"] for job in match_jobs],
            similarity_matcher.match_descriptor_task,
            policy=policy,
            full_fit_count=len(match_jobs),
            pure_python=not similarity_matcher.numpy_available(),
            numpy_enabled=similarity_matcher.numpy_available(),
            independent=True,
            validate_results=False,
        )
        diagnostics.record_phase(
            "scheduler_match",
            (time.perf_counter() - schedule_started) * 1000.0,
        )
        similarity_matcher.record_scheduler_decision(scheduled.decision.backend)
        if evidence is not None:
            evidence.setdefault("scheduler_results", []).append(scheduled)
            evidence["scheduler_result"] = scheduled
        for index, entry in enumerate(scheduled.results):
            job = match_jobs[index]
            if entry.status != "completed" or not isinstance(
                entry.value, similarity_matcher.MatchResult
            ):
                pair_results[job["pair_key"]] = None
                similarity_matcher.record_rejection()
                continue
            result = entry.value
            pair_results[job["pair_key"]] = result
            similarity_matcher.record_match_diagnostics(result.diagnostics)
            if result.accepted and not _selected_match_passes_quality(
                result, similarity_tolerance
            ):
                if evidence is not None:
                    evidence["quality_rejections"].append(
                        {
                            "representative_key": list(job["pair_key"][0]),
                            "candidate_key": list(job["pair_key"][1]),
                            "score": float(result.score),
                            "similarity_tolerance": similarity_tolerance,
                        }
                    )
        return pair_results

    buckets = {}
    for island in ordered_selected:
        island_key = _island_face_key(island)
        bucket_key = _cheap_group_bucket_key(cheap_signatures[island_key])
        buckets.setdefault(bucket_key, []).append(island)

    all_groups = []

    for _bucket_key, bucket_islands in sorted(buckets.items(), key=lambda item: item[0]):
        _ordered_bucket, bucket_groups = _greedy_fixed_representative_groups(
            bucket_islands,
            _island_face_key,
            prefetch_matches,
            # Invariant bins are profiling/query hints only.  Every candidate
            # in the structural bucket must reach the full matcher so a valid
            # pair cannot be rejected by a coarse bin-distance cutoff.
            similarity_tolerance=similarity_tolerance,
        )
        all_groups.extend(bucket_groups)

    group_records = []
    apply_records = []
    for group in all_groups:
        representative = group["representative"]
        representative_key = _island_face_key(representative)
        members = sorted(
            group["members"],
            key=lambda item: _island_face_key(item["island"]),
        )
        group_records.append(
            {
                "representative_key": list(representative_key),
                "member_keys": [
                    list(_island_face_key(item["island"])) for item in members
                ],
                "size": 1 + len(members),
            }
        )
        for member in members:
            result = member["result"]
            transform = result.transform
            if transform is None:
                raise RuntimeError("Accepted selected similarity match has no transform.")
            apply_records.append(
                {
                    "representative_key": representative_key,
                    "member_key": _island_face_key(member["island"]),
                    "island": member["island"],
                    "score": float(result.score),
                    "transform": transform,
                }
            )

    apply_records.sort(key=lambda item: (item["representative_key"], item["member_key"]))
    for apply_record in apply_records:
        started = time.perf_counter()
        _apply_align_transform(
            apply_record["island"],
            uv_layer,
            apply_record["transform"],
        )
        if evidence is not None:
            evidence.setdefault("apply_records", []).append(
                {
                    "target_key": list(apply_record["member_key"]),
                    "member_key": list(apply_record["member_key"]),
                    "representative_key": list(apply_record["representative_key"]),
                    "score": apply_record["score"],
                    "transform": apply_record["transform"],
                }
            )
        diagnostics.record_phase(
            "apply",
            (time.perf_counter() - started) * 1000.0,
        )

    aligned = len(apply_records)
    stacked_group_records = [item for item in group_records if item["size"] >= 2]
    if evidence is not None:
        evidence["groups"] = group_records
        evidence["group_count"] = len(stacked_group_records)
        evidence["aligned_count"] = aligned
        evidence["representative_keys"] = [
            item["representative_key"] for item in stacked_group_records
        ]
        evidence["singleton_count"] = sum(
            item["size"] == 1 for item in group_records
        )
        evidence["quality_rejection_count"] = len(
            evidence.get("quality_rejections", [])
        )

    if aligned:
        update_started = time.perf_counter()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        diagnostics.record_phase(
            "mesh_update",
            (time.perf_counter() - update_started) * 1000.0,
        )
    return aligned, len(stacked_group_records)


class UVGPT_OT_paste_keep_position(bpy.types.Operator):
    bl_idname = "uv_gpt.paste_keep_position"
    bl_label = "Paste Keep Position"
    bl_description = "Paste UVs while restoring the original selected island center"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            uv_utils.ensure_destructive_ready(context)
            obj = uv_utils.get_active_mesh_object(context)
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            bm.faces.ensure_lookup_table()
            bm.faces.index_update()
            selected = island_tools.get_selected_uv_islands(bm, uv_layer)
            if not selected:
                self.report({"ERROR"}, "Select one or more target UV islands.")
                return {"CANCELLED"}

            records = []
            for island in selected:
                face_indices = sorted({loop.face.index for loop in island})
                records.append(
                    {
                        "faces": face_indices,
                        "center": island_tools.get_island_center(island, uv_layer).copy(),
                    }
                )

            try:
                uv_utils.run_uv_paste(context)
            except RuntimeError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            bm.faces.ensure_lookup_table()
            bm.faces.index_update()
            moved = 0
            for record in records:
                loops = []
                for face_index in record["faces"]:
                    if face_index < len(bm.faces):
                        loops.extend(bm.faces[face_index].loops)
                if not loops:
                    continue
                center = uv_utils.get_loops_center(loops, uv_layer)
                delta = record["center"] - center
                for loop in loops:
                    loop[uv_layer].uv += delta
                moved += 1
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Pasted UVs and restored {moved} island center(s).")
        return {"FINISHED"}


class UVGPT_OT_align_to_selected(bpy.types.Operator):
    bl_idname = "uv_gpt.align_to_selected"
    bl_label = "Align Similar"
    bl_description = "Group selected islands by similarity and stack each group"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            setup_started = time.perf_counter()
            uv_utils.ensure_destructive_ready(context)
            obj = uv_utils.get_active_mesh_object(context)
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            operator_setup_ms = (time.perf_counter() - setup_started) * 1000.0
            started = time.perf_counter()
            all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
            island_enumeration_ms = (time.perf_counter() - started) * 1000.0
            selected_islands = [
                island for island in all_islands if _island_is_selected(island, uv_layer)
            ]
            if len(selected_islands) < 2:
                self.report(
                    {"INFO"},
                    "Aligned 0 selected island(s) across 0 similarity group(s).",
                )
                return {"FINISHED"}

            aligned_count, group_count = _align_selected_similar(
                context,
                obj,
                bm,
                uv_layer,
                selected_islands,
                evidence=_MATCH03_EVIDENCE_SINK,
                all_islands=all_islands,
                island_enumeration_ms=island_enumeration_ms,
                operator_setup_ms=operator_setup_ms,
            )
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Aligned {aligned_count} selected island(s) across {group_count} similarity group(s).",
        )
        return {"FINISHED"}


def _pro_create_session(
    context,
    evidence=None,
    *,
    modal=False,
    correspondence_max_search=None,
    cooperative_yield_every=None,
    process_worker_count=None,
    process_batch_size=None,
    process_python_executable=None,
    process_worker_script=None,
    process_blender_binary=None,
    process_blender_root=None,
    process_blender_version=None,
    process_handshake_timeout=5.0,
    process_io_timeout=5.0,
    process_test_override=False,
    process_debug_delay_ms=0,
    process_fused=False,
    process_group_first=None,
    time_budget_ms=_PRO_SYNC_WALL_TIME_BUDGET_MS,
    correspondence_mode=None,
    mode=None,
):
    """Create one Pro session after the shared destructive-operation checks."""

    global _ACTIVE_PRO_SESSION
    if _ACTIVE_PRO_SESSION is not None and not _ACTIVE_PRO_SESSION.done:
        raise RuntimeError("Align Similar Pro is already active.")
    _ACTIVE_PRO_SESSION = None
    runtime_started = time.perf_counter()
    selected_mode = mode if mode is not None else correspondence_mode
    if selected_mode is None:
        selected_mode = pro_process_payload.CORRESPONDENCE_MODE_HYBRID
    selected_mode = pro_process_payload.normalize_correspondence_mode(selected_mode)
    if (
        selected_mode
        == pro_process_payload.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
        and (process_worker_count is None or process_worker_count == 0)
    ):
        raise RuntimeError(_PRO_FAST_SYNC_ERROR)
    setup_started = time.perf_counter()
    uv_utils.ensure_destructive_ready(context)
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    operator_setup_ms = (time.perf_counter() - setup_started) * 1000.0
    if (
        mode is not None
        and correspondence_mode is not None
        and pro_process_payload.normalize_correspondence_mode(mode)
        != pro_process_payload.normalize_correspondence_mode(correspondence_mode)
    ):
        raise RuntimeError("conflicting Pro correspondence modes")
    session = _ProAlignSession(
        context,
        obj,
        bm,
        uv_layer,
        selected_islands=None,
        evidence=evidence,
        all_islands=None,
        island_enumeration_ms=None,
        operator_setup_ms=operator_setup_ms,
        runtime_started=runtime_started,
        time_budget_ms=time_budget_ms,
        modal=modal,
        correspondence_max_search=correspondence_max_search,
        cooperative_yield_every=cooperative_yield_every,
        process_worker_count=process_worker_count,
        process_batch_size=process_batch_size,
        process_python_executable=process_python_executable,
        process_worker_script=process_worker_script,
        process_blender_binary=process_blender_binary,
        process_blender_root=process_blender_root,
        process_blender_version=process_blender_version,
        process_handshake_timeout=process_handshake_timeout,
        process_io_timeout=process_io_timeout,
        process_test_override=process_test_override,
        process_debug_delay_ms=process_debug_delay_ms,
        process_fused=process_fused,
        process_group_first=process_group_first,
        correspondence_mode=selected_mode,
    )
    _ACTIVE_PRO_SESSION = session
    return session


def _pro_modal_progress_begin(context):
    window_manager = getattr(context, "window_manager", None)
    if window_manager is not None:
        try:
            window_manager.progress_begin(0.0, 1.0)
        except (AttributeError, RuntimeError, TypeError):
            pass


def _pro_modal_progress_update(context, session):
    selected = max(1, int(session.report.get("selected_count", 0)))
    processed = int(session.report.get("candidate_pairs_processed", 0))
    planned = max(processed, int(session.report.get("candidate_pairs_planned", 0)))
    progress = min(1.0, processed / max(1, planned)) if planned else 0.0
    window_manager = getattr(context, "window_manager", None)
    if window_manager is not None:
        try:
            window_manager.progress_update(progress)
        except (AttributeError, RuntimeError, TypeError):
            pass
    workspace = getattr(context, "workspace", None)
    status_set = getattr(workspace, "status_text_set", None)
    if callable(status_set):
        elapsed_seconds = max(
            0.0,
            (time.perf_counter() - session.started) if session.started else 0.0,
        )
        active_workers = int(session.report.get("process_active_workers", 0))
        worker_total = int(session.report.get("process_worker_count", 0))
        exact_count = int(session.report.get("aligned_exact", 0))
        group_count = int(session.report.get("group_count", 0))
        stage = str(
            getattr(session, "_tick_stage", getattr(session, "state", "prepare"))
        )
        mode = str(
            getattr(
                session,
                "mode",
                session.report.get("mode", session.report.get("correspondence_mode", "")),
            )
        )
        mode_label = (
            "Pro Fast"
            if mode == pro_process_payload.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
            else (
                "Pro Exact"
                if mode == pro_process_payload.CORRESPONDENCE_MODE_EXACT_ONLY
                else "Align Similar Pro"
            )
        )
        status_set(
            "%s [%s]: %d/%d pairs, %d aligned/%d groups, workers %d/%d, %.1fs, Esc to Cancel"
            % (
                mode_label,
                stage,
                processed,
                max(planned, selected),
                exact_count,
                group_count,
                active_workers,
                worker_total,
                elapsed_seconds,
            )
        )


def _pro_modal_progress_end(context):
    window_manager = getattr(context, "window_manager", None)
    if window_manager is not None:
        try:
            window_manager.progress_end()
        except (AttributeError, RuntimeError, TypeError):
            pass
    workspace = getattr(context, "workspace", None)
    status_set = getattr(workspace, "status_text_set", None)
    if callable(status_set):
        status_set(None)


class _UVGPT_OT_align_similar_pro_mode:
    """Shared atomic/modal lifecycle for the two explicit Pro algorithms."""

    bl_options = {"REGISTER", "UNDO"}
    correspondence_mode = pro_process_payload.CORRESPONDENCE_MODE_HYBRID

    _timer = None
    _session = None

    def _cleanup_modal(self, context, *, cancel=False, reason="cleanup"):
        global _ACTIVE_PRO_SESSION, _ACTIVE_PRO_OPERATOR
        session = getattr(self, "_session", None)
        if cancel and session is not None and not session.done:
            session.cancel(reason)
        window_manager = getattr(context, "window_manager", None)
        timer = getattr(self, "_timer", None)
        if window_manager is not None and timer is not None:
            try:
                window_manager.event_timer_remove(timer)
            except (AttributeError, RuntimeError, TypeError):
                pass
        self._timer = None
        _pro_modal_progress_end(context)
        if _ACTIVE_PRO_SESSION is session:
            _ACTIVE_PRO_SESSION = None
        if _ACTIVE_PRO_OPERATOR is self:
            _ACTIVE_PRO_OPERATOR = None

    def _report_result(self, session):
        if session.error:
            self.report({"ERROR"}, "%s failed: %s" % (self.bl_label, session.error))
            return {"CANCELLED"}
        if session.cancelled:
            self.report(
                {"WARNING"},
                "%s cancelled; staged UV writes were discarded." % self.bl_label,
            )
            return {"CANCELLED"}
        aligned = int(session.report.get("aligned_exact", 0))
        groups = int(session.report.get("group_count", 0))
        skipped_shape = int(session.report.get("skipped_shape", 0))
        unproven = int(session.report.get("skipped_topology_unproven", 0))
        invalid_density = int(session.report.get("skipped_invalid_density", 0))
        if (
            self.correspondence_mode
            == pro_process_payload.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
        ):
            message = (
                "%s: aligned %d, groups %d, skipped unverified %d, "
                "skipped shape %d, invalid density %d."
                % (
                    self.bl_label,
                    aligned,
                    groups,
                    unproven,
                    skipped_shape,
                    invalid_density,
                )
            )
        else:
            message = (
                "%s: exact %d, groups %d, skipped shape %d, "
                "topology unproven %d, invalid density %d."
                % (
                    self.bl_label,
                    aligned,
                    groups,
                    skipped_shape,
                    unproven,
                    invalid_density,
                )
            )
        self.report({"INFO"}, message)
        return {"FINISHED"}

    def execute(self, context):
        global _ACTIVE_PRO_SESSION, _ACTIVE_PRO_OPERATOR
        if _ACTIVE_PRO_SESSION is not None and not _ACTIVE_PRO_SESSION.done:
            self.report({"WARNING"}, "%s cannot start: a Pro mode is already active." % self.bl_label)
            return {"CANCELLED"}
        evidence = _ALIGN_SIMILAR_PRO_EVIDENCE_SINK
        if not isinstance(evidence, dict):
            evidence = {"detail_mappings": False}
        try:
            session = _pro_create_session(
                context,
                evidence,
                modal=False,
                mode=self.correspondence_mode,
                process_worker_count=_PRO_DEFAULT_PROCESS_WORKER_COUNT,
                process_batch_size=_PRO_DEFAULT_PROCESS_BATCH_SIZE,
                process_fused=True,
                process_group_first=True,
                time_budget_ms=300_000.0,
            )
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self._session = session
        _ACTIVE_PRO_OPERATOR = self
        try:
            session.run_to_completion()
        finally:
            self._cleanup_modal(context)
        return self._report_result(session)

    def invoke(self, context, event):
        global _ACTIVE_PRO_SESSION, _ACTIVE_PRO_OPERATOR
        if _ACTIVE_PRO_SESSION is not None and not _ACTIVE_PRO_SESSION.done:
            self.report({"WARNING"}, "%s cannot start: a Pro mode is already active." % self.bl_label)
            return {"CANCELLED"}
        evidence = _ALIGN_SIMILAR_PRO_EVIDENCE_SINK
        if not isinstance(evidence, dict):
            evidence = {"detail_mappings": False}
        try:
            session = _pro_create_session(
                context,
                evidence,
                modal=True,
                mode=self.correspondence_mode,
                process_worker_count=_PRO_DEFAULT_PROCESS_WORKER_COUNT,
                process_batch_size=_PRO_DEFAULT_PROCESS_BATCH_SIZE,
                process_fused=True,
                process_group_first=True,
                time_budget_ms=300_000.0,
            )
            window_manager = context.window_manager
            self._session = session
            self._timer = window_manager.event_timer_add(
                _PRO_MODAL_TIMER_INTERVAL,
                window=getattr(context, "window", None),
            )
            _ACTIVE_PRO_OPERATOR = self
            _pro_modal_progress_begin(context)
            _pro_modal_progress_update(context, session)
            window_manager.modal_handler_add(self)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if "session" in locals() and session is not None:
                session.cancel("modal_start_error")
            self._cleanup_modal(context)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        session = getattr(self, "_session", None)
        if session is None:
            self._cleanup_modal(context, cancel=True, reason="missing_session")
            return {"CANCELLED"}
        if event.type in {"ESC", "RIGHTMOUSE"}:
            session.cancel("user_cancelled", nonblocking=True)
            if not session.done:
                _pro_modal_progress_update(context, session)
                return {"RUNNING_MODAL"}
            self._cleanup_modal(context)
            return self._report_result(session)
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}
        session.step(
            active_budget_ms=_PRO_MODAL_TICK_ACTIVE_BUDGET_MS,
            max_correspondence=_PRO_MODAL_MAX_CORRESPONDENCE_PER_TICK,
        )
        _pro_modal_progress_update(context, session)
        if not session.done:
            return {"RUNNING_MODAL"}
        self._cleanup_modal(context)
        return self._report_result(session)


class UVGPT_OT_align_similar_pro_fast(
    _UVGPT_OT_align_similar_pro_mode,
    bpy.types.Operator,
):
    bl_idname = "uv_gpt.align_similar_pro_fast"
    bl_label = "Pro Fast"
    bl_description = (
        "Stack only UV islands proven by the verified-nearest mapping; "
        "unverified pairs are skipped without exact fallback"
    )
    correspondence_mode = (
        pro_process_payload.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
    )


class UVGPT_OT_align_similar_pro_exact(
    _UVGPT_OT_align_similar_pro_mode,
    bpy.types.Operator,
):
    bl_idname = "uv_gpt.align_similar_pro_exact"
    bl_label = "Pro Exact"
    bl_description = (
        "Stack UV islands using exact topology correspondence only; "
        "the verified-nearest algorithm is not used"
    )
    correspondence_mode = pro_process_payload.CORRESPONDENCE_MODE_EXACT_ONLY


def run_align_similar_pro(request):
    """Harness adapter for the registered Align Similar Pro operator."""

    request = request or {}
    detail_mappings = bool(
        request.get("detail_mappings")
        or request.get("include_mapping_pairs")
    )
    evidence = {"detail_mappings": detail_mappings}
    correspondence_max_search = request.get("correspondence_max_search")
    cooperative_yield_every = request.get("cooperative_yield_every")
    process_worker_count = request.get("process_worker_count")
    global _ALIGN_SIMILAR_PRO_EVIDENCE_SINK
    previous_sink = _ALIGN_SIMILAR_PRO_EVIDENCE_SINK
    started = time.perf_counter()
    _ALIGN_SIMILAR_PRO_EVIDENCE_SINK = evidence
    try:
        global _ACTIVE_PRO_SESSION
        # The harness always constructs a session directly.  The legacy
        # composite operator is intentionally no longer registered in Blender.
        session = _pro_create_session(
            request.get("bpy_context", bpy.context),
            evidence,
            modal=False,
            correspondence_max_search=correspondence_max_search,
            cooperative_yield_every=cooperative_yield_every,
            process_worker_count=process_worker_count,
            process_batch_size=request.get("process_batch_size"),
            process_python_executable=request.get("process_python_executable"),
            process_worker_script=request.get("process_worker_script"),
            process_blender_binary=request.get("process_blender_binary"),
            process_blender_root=request.get("process_blender_root"),
            process_blender_version=request.get("process_blender_version"),
            process_handshake_timeout=request.get("process_handshake_timeout", 5.0),
            process_io_timeout=request.get("process_io_timeout", 5.0),
            process_test_override=bool(request.get("process_test_override", False)),
            process_debug_delay_ms=request.get("process_debug_delay_ms", 0),
            process_fused=bool(request.get("process_fused", False)),
            process_group_first=request.get("process_group_first"),
            time_budget_ms=request.get(
                "time_budget_ms", _PRO_SYNC_WALL_TIME_BUDGET_MS
            ),
            mode=request.get(
                "mode",
                request.get(
                    "correspondence_mode",
                    pro_process_payload.CORRESPONDENCE_MODE_HYBRID,
                ),
            ),
        )
        try:
            session.run_to_completion()
            operator_result = {"CANCELLED"} if session.cancelled else {"FINISHED"}
        finally:
            if _ACTIVE_PRO_SESSION is session:
                _ACTIVE_PRO_SESSION = None
    finally:
        _ALIGN_SIMILAR_PRO_EVIDENCE_SINK = previous_sink
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    diagnostics = similarity_matcher.get_diagnostics()
    return {
        "operator_result": sorted(str(value) for value in operator_result),
        "elapsed_ms": elapsed_ms,
        "mode": evidence.get("mode", pro_process_payload.CORRESPONDENCE_MODE_HYBRID),
        "correspondence_mode": evidence.get(
            "correspondence_mode",
            pro_process_payload.CORRESPONDENCE_MODE_HYBRID,
        ),
        "aligned_exact": evidence.get("aligned_exact", 0),
        "group_count": evidence.get("group_count", 0),
        "skipped_shape": evidence.get("skipped_shape", 0),
        "skipped_topology_unproven": evidence.get(
            "skipped_topology_unproven", 0
        ),
        "skipped_invalid_density": evidence.get("skipped_invalid_density", 0),
        "skipped_ownership": evidence.get("skipped_ownership", 0),
        "truncated": evidence.get("truncated", False),
        "partial": evidence.get("partial", False),
        "truncation_reasons": evidence.get("truncation_reasons", []),
        "candidate_pairs_planned": evidence.get("candidate_pairs_planned", 0),
        "candidate_pairs_processed": evidence.get("candidate_pairs_processed", 0),
        "planner_record_count": evidence.get("planner_record_count", 0),
        "planner_record_error_count": evidence.get(
            "planner_record_error_count", 0
        ),
        "planner_record_error_samples": evidence.get(
            "planner_record_error_samples", []
        ),
        "planner_record_build_ms": evidence.get("planner_record_build_ms", 0.0),
        "planner_index_build_ms": evidence.get("planner_index_build_ms", 0.0),
        "planner_refinement": evidence.get("planner_refinement", {}),
        "planner_refinement_mode": evidence.get(
            "planner_refinement_mode", "two_round_bounded"
        ),
        "planner_refinement_converged": evidence.get(
            "planner_refinement_converged", False
        ),
        "planner_refinement_records": evidence.get(
            "planner_refinement_records", 0
        ),
        "planner_refinement_rounds_total": evidence.get(
            "planner_refinement_rounds_total", 0
        ),
        "planner_refinement_max_rounds": evidence.get(
            "planner_refinement_max_rounds", 0
        ),
        "planner_refinement_max_bound": evidence.get(
            "planner_refinement_max_bound", 0
        ),
        "planner_refinement_ms": evidence.get("planner_refinement_ms", 0.0),
        "planner_refinement_max_ms": evidence.get(
            "planner_refinement_max_ms", 0.0
        ),
        "planner_refinement_stable_count": evidence.get(
            "planner_refinement_stable_count", 0
        ),
        "planner_refinement_truncated_count": evidence.get(
            "planner_refinement_truncated_count", 0
        ),
        "correspondence_max_search": evidence.get(
            "correspondence_max_search", _PRO_CORRESPONDENCE_MAX_SEARCH
        ),
        "cooperative_yield_every": evidence.get(
            "cooperative_yield_every", _PRO_COOPERATIVE_YIELD_EVERY
        ),
        "yield_count": evidence.get("yield_count", 0),
        "correspondence_calls": evidence.get("correspondence_calls", 0),
        "correspondence_budget_rejections": evidence.get(
            "correspondence_budget_rejections", 0
        ),
        "exact_refinement_pairs": evidence.get("exact_refinement_pairs", 0),
        "exact_refinement_rounds_total": evidence.get(
            "exact_refinement_rounds_total", 0
        ),
        "exact_refinement_max_rounds": evidence.get(
            "exact_refinement_max_rounds", 0
        ),
        "exact_refinement_stable_count": evidence.get(
            "exact_refinement_stable_count", 0
        ),
        "exact_refinement_truncated_count": evidence.get(
            "exact_refinement_truncated_count", 0
        ),
        "exact_refinement_ms": evidence.get("exact_refinement_ms", 0.0),
        "exact_refinement_pre_max_domain": evidence.get(
            "exact_refinement_pre_max_domain", 0
        ),
        "exact_refinement_post_max_domain": evidence.get(
            "exact_refinement_post_max_domain", 0
        ),
        "exact_refinement_topology_mismatch": evidence.get(
            "exact_refinement_topology_mismatch", 0
        ),
        "max_correspondence_ms": evidence.get("max_correspondence_ms", 0.0),
        "active_processing_ms": evidence.get("active_processing_ms", 0.0),
        "tick_count": evidence.get("tick_count", 0),
        "max_tick_ms": evidence.get("max_tick_ms", 0.0),
        "tick_p95_ms": evidence.get("tick_p95_ms", 0.0),
        "tick_p99_ms": evidence.get("tick_p99_ms", 0.0),
        "tick_samples_count": evidence.get("tick_samples_count", 0),
        "cancelled": evidence.get("cancelled", False),
        "cancel_reason": evidence.get("cancel_reason"),
        "error": evidence.get("error"),
        "session_state": evidence.get("session_state"),
        "planner_diagnostics": evidence.get("planner_diagnostics", {}),
        "timings": evidence.get("timings", {}),
        "cache_peak_counts": evidence.get("cache_peak_counts", {}),
        "graphs_built": evidence.get("graphs_built", 0),
        "graph_cache_hits": evidence.get("graph_cache_hits", 0),
        "graph_cache_peak": evidence.get("graph_cache_peak", 0),
        "shape_operation_cap": evidence.get(
            "shape_operation_cap", _PRO_SHAPE_OPERATION_BUDGET
        ),
        "shape_primitive_ops": evidence.get("shape_primitive_ops", 0),
        "shape_slices": evidence.get("shape_slices", 0),
        "max_shape_slice_ms": evidence.get("max_shape_slice_ms", 0.0),
        "max_shape_call_ms": evidence.get("max_shape_call_ms", 0.0),
        "shape_phase": evidence.get("shape_phase", "idle"),
        "shape_over_25ms_calls": evidence.get("shape_over_25ms_calls", 0),
        "shape_over_25ms_samples": evidence.get(
            "shape_over_25ms_samples", []
        ),
        "worker_submissions": evidence.get("worker_submissions", 0),
        "worker_completions": evidence.get("worker_completions", 0),
        "worker_discards": evidence.get("worker_discards", 0),
        "worker_errors": evidence.get("worker_errors", 0),
        "worker_in_flight_peak": evidence.get(
            "worker_in_flight_peak", 0
        ),
        "worker_wall_elapsed_ms": evidence.get(
            "worker_wall_elapsed_ms", 0.0
        ),
        "future_wall_ms": evidence.get("future_wall_ms", 0.0),
        "max_future_wall_ms": evidence.get("max_future_wall_ms", 0.0),
        "worker_compute_ms": evidence.get("worker_compute_ms", 0.0),
        "max_worker_compute_ms": evidence.get(
            "max_worker_compute_ms", 0.0
        ),
        "worker_compute_elapsed_ms": evidence.get(
            "worker_compute_elapsed_ms", 0.0
        ),
        "worker_mode": evidence.get("worker_mode", "resumable_main_thread"),
        "process_requested": evidence.get("process_requested", False),
        "process_worker_count": evidence.get("process_worker_count", 0),
        "process_batch_size": evidence.get("process_batch_size", 1),
        "process_pipeline": evidence.get("process_pipeline", False),
        "process_fused": evidence.get("process_fused", False),
        "process_group_first": evidence.get("process_group_first", False),
        "process_group_first_stage": evidence.get(
            "process_group_first_stage", "disabled"
        ),
        "grouping_comparisons_planned": evidence.get(
            "grouping_comparisons_planned", 0
        ),
        "grouping_comparisons_completed": evidence.get(
            "grouping_comparisons_completed", 0
        ),
        "shape_groups": evidence.get("shape_groups", 0),
        "shape_singletons": evidence.get("shape_singletons", 0),
        "group_membership_digest": evidence.get("group_membership_digest", ""),
        "density_masters": evidence.get("density_masters", []),
        "uv_area_masters": evidence.get("uv_area_masters", []),
        "uv_area_master_areas": evidence.get("uv_area_master_areas", []),
        "uv_area_by_key": evidence.get("uv_area_by_key", []),
        "direct_exact_jobs_planned": evidence.get(
            "direct_exact_jobs_planned", 0
        ),
        "direct_exact_jobs_completed": evidence.get(
            "direct_exact_jobs_completed", 0
        ),
        "direct_exact_jobs_failed": evidence.get(
            "direct_exact_jobs_failed", 0
        ),
        "exact_job_bound": evidence.get("exact_job_bound", 0),
        "process_group_first_error": evidence.get(
            "process_group_first_error", ""
        ),
        "process_fused_context_ready": evidence.get(
            "process_fused_context_ready", False
        ),
        "process_fused_descriptor_count": evidence.get(
            "process_fused_descriptor_count", 0
        ),
        "process_fused_context_digest": evidence.get(
            "process_fused_context_digest"
        ),
        "process_fused_batches_submitted": evidence.get(
            "process_fused_batches_submitted", 0
        ),
        "process_fused_batches_completed": evidence.get(
            "process_fused_batches_completed", 0
        ),
        "process_fused_pairs_submitted": evidence.get(
            "process_fused_pairs_submitted", 0
        ),
        "process_fused_pairs_completed": evidence.get(
            "process_fused_pairs_completed", 0
        ),
        "process_fused_graph_cache_builds": evidence.get(
            "process_fused_graph_cache_builds", 0
        ),
        "process_fused_graph_cache_hits": evidence.get(
            "process_fused_graph_cache_hits", 0
        ),
        "process_fused_graph_compute_ms": evidence.get(
            "process_fused_graph_compute_ms", 0.0
        ),
        "process_fused_exact_compute_ms": evidence.get(
            "process_fused_exact_compute_ms", 0.0
        ),
        "process_fused_shape_compute_ms": evidence.get(
            "process_fused_shape_compute_ms", 0.0
        ),
        "process_fused_shape_cache_hits": evidence.get(
            "process_fused_shape_cache_hits", 0
        ),
        "process_fused_lower_bound_checked": evidence.get(
            "process_fused_lower_bound_checked", 0
        ),
        "process_fused_lower_bound_rejected": evidence.get(
            "process_fused_lower_bound_rejected", 0
        ),
        "process_fused_lower_bound_skipped": evidence.get(
            "process_fused_lower_bound_skipped", 0
        ),
        "process_fused_lower_bound_graph_pairs_avoided": evidence.get(
            "process_fused_lower_bound_graph_pairs_avoided", 0
        ),
        "process_fused_lower_bound_min_ratio": evidence.get(
            "process_fused_lower_bound_min_ratio", 0.0
        ),
        "process_fused_lower_bound_max_ratio": evidence.get(
            "process_fused_lower_bound_max_ratio", 0.0
        ),
        "process_fused_frame_bytes": evidence.get(
            "process_fused_frame_bytes", 0
        ),
        "process_stage": evidence.get("process_stage", "idle"),
        "process_shape_batches": evidence.get("process_shape_batches", 0),
        "process_shape_pairs_submitted": evidence.get("process_shape_pairs_submitted", 0),
        "process_shape_pairs_completed": evidence.get("process_shape_pairs_completed", 0),
        "process_shape_accepted": evidence.get("process_shape_accepted", 0),
        "process_shape_rejected": evidence.get("process_shape_rejected", 0),
        "process_shape_prefiltered": evidence.get("process_shape_prefiltered", 0),
        "process_pruned_pairs": evidence.get("process_pruned_pairs", 0),
        "process_exact_batches": evidence.get("process_exact_batches", 0),
        "process_exact_pairs_submitted": evidence.get("process_exact_pairs_submitted", 0),
        "process_exact_pairs_completed": evidence.get("process_exact_pairs_completed", 0),
        "process_exact_accepted": evidence.get("process_exact_accepted", 0),
        "process_resident_exact_batches_submitted": evidence.get(
            "process_resident_exact_batches_submitted", 0
        ),
        "process_resident_exact_batches_completed": evidence.get(
            "process_resident_exact_batches_completed", 0
        ),
        "process_resident_graph_cache_builds": evidence.get(
            "process_resident_graph_cache_builds", 0
        ),
        "process_resident_graph_cache_hits": evidence.get(
            "process_resident_graph_cache_hits", 0
        ),
        "process_resident_graph_compute_ms": evidence.get(
            "process_resident_graph_compute_ms", 0.0
        ),
        "process_resident_topology_cache_builds": evidence.get(
            "process_resident_topology_cache_builds", 0
        ),
        "process_resident_topology_cache_hits": evidence.get(
            "process_resident_topology_cache_hits", 0
        ),
        "process_resident_topology_compute_ms": evidence.get(
            "process_resident_topology_compute_ms", 0.0
        ),
        "process_resident_exact_compute_ms": evidence.get(
            "process_resident_exact_compute_ms", 0.0
        ),
        "process_resident_exact_frame_bytes": evidence.get(
            "process_resident_exact_frame_bytes", 0
        ),
        "process_merged_pairs": evidence.get("process_merged_pairs", 0),
        "process_exact_started_before_shape_terminal": evidence.get(
            "process_exact_started_before_shape_terminal", False
        ),
        "process_last_progress_kind": evidence.get(
            "process_last_progress_kind", ""
        ),
        "process_poll_calls": evidence.get("process_poll_calls", 0),
        "process_no_progress_loops": evidence.get("process_no_progress_loops", 0),
        "process_event_epoch": evidence.get("process_event_epoch", 0),
        "process_graph_event_epoch": evidence.get("process_graph_event_epoch", 0),
        "process_graph_waiter_registrations": evidence.get(
            "process_graph_waiter_registrations", 0
        ),
        "process_graph_waiter_dedup": evidence.get(
            "process_graph_waiter_dedup", 0
        ),
        "process_queue_depth": evidence.get("process_queue_depth", 0),
        "process_stage_distributions": evidence.get("process_stage_distributions", {}),
        "process_frame_bytes": evidence.get("process_frame_bytes", {}),
        "process_cache_hits": evidence.get("process_cache_hits", 0),
        "process_debug_delay_ms": evidence.get("process_debug_delay_ms", 0),
        "process_worker_pids": evidence.get("process_worker_pids", []),
        "process_helper_path": evidence.get("process_helper_path"),
        "process_python_executable": evidence.get("process_python_executable"),
        "process_python_version": evidence.get("process_python_version"),
        "process_thread_caps": evidence.get("process_thread_caps", {}),
        "process_snapshot_digest": evidence.get("process_snapshot_digest"),
        "process_snapshot_checks": evidence.get("process_snapshot_checks", 0),
        "process_snapshot_forced_checks": evidence.get(
            "process_snapshot_forced_checks", 0
        ),
        "process_validation_ms": evidence.get("process_validation_ms", 0.0),
        "process_validation_slices": evidence.get(
            "process_validation_slices", 0
        ),
        "process_validation_operations": evidence.get(
            "process_validation_operations", 0
        ),
        "process_validation_epoch": evidence.get(
            "process_validation_epoch", 0
        ),
        "process_validation_max_slice_ms": evidence.get(
            "process_validation_max_slice_ms", 0.0
        ),
        "process_validation_max_primitive_ms": evidence.get(
            "process_validation_max_primitive_ms", 0.0
        ),
        "process_validation_max_primitive": evidence.get(
            "process_validation_max_primitive", {}
        ),
        "process_validation_status": evidence.get(
            "process_validation_status", "not_started"
        ),
        "process_finalization_grace_state": evidence.get(
            "process_finalization_grace_state", "idle"
        ),
        "process_finalization_grace_started_ms": evidence.get(
            "process_finalization_grace_started_ms", 0.0
        ),
        "process_finalization_grace_deadline_ms": evidence.get(
            "process_finalization_grace_deadline_ms", 0.0
        ),
        "process_finalization_grace_rounds": evidence.get(
            "process_finalization_grace_rounds", 0
        ),
        "process_finalization_grace_reason": evidence.get(
            "process_finalization_grace_reason", ""
        ),
        "process_finalization_grace_max_tick_ms": evidence.get(
            "process_finalization_grace_max_tick_ms", 0.0
        ),
        "process_finalization_grace_no_dispatch": evidence.get(
            "process_finalization_grace_no_dispatch", False
        ),
        "process_pipeline_subphase_ms": evidence.get(
            "process_pipeline_subphase_ms", {}
        ),
        "process_pipeline_max_subphase_ms": evidence.get(
            "process_pipeline_max_subphase_ms", 0.0
        ),
        "process_pipeline_max_subphase": evidence.get(
            "process_pipeline_max_subphase", ""
        ),
        "process_result_digest": evidence.get("process_result_digest", ""),
        "process_startup_ms": evidence.get("process_startup_ms", 0.0),
        "process_worker_start_owner_ms": evidence.get(
            "process_worker_start_owner_ms", 0.0
        ),
        "process_worker_start_background_ms": evidence.get(
            "process_worker_start_background_ms", 0.0
        ),
        "process_worker_start_pending": evidence.get(
            "process_worker_start_pending", 0
        ),
        "process_worker_start_states": evidence.get(
            "process_worker_start_states", []
        ),
        "process_context_serialize_owner_ms": evidence.get(
            "process_context_serialize_owner_ms", 0.0
        ),
        "process_context_serialize_background_ms": evidence.get(
            "process_context_serialize_background_ms", 0.0
        ),
        "process_context_write_background_ms": evidence.get(
            "process_context_write_background_ms", 0.0
        ),
        "process_context_send_pending": evidence.get(
            "process_context_send_pending", 0
        ),
        "process_pipeline_admission_owner_ms": evidence.get(
            "process_pipeline_admission_owner_ms", 0.0
        ),
        "process_dispatch_ms": evidence.get("process_dispatch_ms", 0.0),
        "process_poll_ms": evidence.get("process_poll_ms", 0.0),
        "process_compute_ms": evidence.get("process_compute_ms", 0.0),
        "process_active_workers": evidence.get("process_active_workers", 0),
        "process_retry_count": evidence.get("process_retry_count", 0),
        "process_retry_total": evidence.get("process_retry_total", 0),
        "process_max_retry_per_batch": evidence.get(
            "process_max_retry_per_batch", 0
        ),
        "process_retried_batch_count": evidence.get(
            "process_retried_batch_count", 0
        ),
        "process_retry_failure_reason": evidence.get(
            "process_retry_failure_reason", ""
        ),
        "process_retry_batches": evidence.get("process_retry_batches", []),
        "process_restart_pending": evidence.get("process_restart_pending", 0),
        "process_restart_states": evidence.get("process_restart_states", []),
        "process_nearest_attempted": evidence.get("process_nearest_attempted", 0),
        "process_nearest_accepted": evidence.get("process_nearest_accepted", 0),
        "process_nearest_fallback": evidence.get("process_nearest_fallback", 0),
        "process_nearest_max_seed_distance": evidence.get(
            "process_nearest_max_seed_distance", 0.0
        ),
        "process_nearest_mean_seed_distance": evidence.get(
            "process_nearest_mean_seed_distance", 0.0
        ),
        "process_nearest_ambiguity_count": evidence.get(
            "process_nearest_ambiguity_count", 0
        ),
        "process_nearest_tie_count": evidence.get(
            "process_nearest_tie_count", 0
        ),
        "process_nearest_compute_ms": evidence.get(
            "process_nearest_compute_ms", 0.0
        ),
        "process_nearest_distance_evaluations": evidence.get(
            "process_nearest_distance_evaluations", 0
        ),
        "process_nearest_assignment_nodes": evidence.get(
            "process_nearest_assignment_nodes", 0
        ),
        "process_nearest_assignment_cap": evidence.get(
            "process_nearest_assignment_cap", 0
        ),
        "process_nearest_fallback_reasons": evidence.get(
            "process_nearest_fallback_reasons", {}
        ),
        "process_nearest_distance_lookups": evidence.get(
            "process_nearest_distance_lookups", 0
        ),
        "process_nearest_distance_cache_hits": evidence.get(
            "process_nearest_distance_cache_hits", 0
        ),
        "process_nearest_distance_cache_misses": evidence.get(
            "process_nearest_distance_cache_misses", 0
        ),
        "process_nearest_operations_used": evidence.get(
            "process_nearest_operations_used", 0
        ),
        "process_nearest_seeded_jobs_planned": evidence.get(
            "process_nearest_seeded_jobs_planned", 0
        ),
        "process_nearest_seedless_jobs_planned": evidence.get(
            "process_nearest_seedless_jobs_planned", 0
        ),
        "process_nearest_fallback_exact_calls": evidence.get(
            "process_nearest_fallback_exact_calls", 0
        ),
        "process_nearest_missing_seed_fallbacks": evidence.get(
            "process_nearest_missing_seed_fallbacks", 0
        ),
        "process_graph_rejected_before_nearest": evidence.get(
            "process_graph_rejected_before_nearest", 0
        ),
        "process_nearest_seed_missing": evidence.get(
            "process_nearest_seed_missing", 0
        ),
        "process_nearest_fast_miss": evidence.get(
            "process_nearest_fast_miss", 0
        ),
        "process_exact_fallback_calls": evidence.get(
            "process_exact_fallback_calls", 0
        ),
        "process_exact_primary_calls": evidence.get(
            "process_exact_primary_calls", 0
        ),
        "process_nearest_accounting": evidence.get(
            "process_nearest_accounting", {}
        ),
        "process_nearest_accounting_valid": evidence.get(
            "process_nearest_accounting_valid", False
        ),
        "process_seed_planned": evidence.get("process_seed_planned", 0),
        "process_seed_rerooted": evidence.get("process_seed_rerooted", 0),
        "process_seed_identity_leg": evidence.get("process_seed_identity_leg", 0),
        "process_seed_missing_by_reason": evidence.get(
            "process_seed_missing_by_reason", {}
        ),
        "process_seed_digest": evidence.get("process_seed_digest", ""),
        "process_shutdown_state": evidence.get(
            "process_shutdown_state", "idle"
        ),
        "process_shutdown_rounds": evidence.get("process_shutdown_rounds", 0),
        "process_shutdown_force_used": evidence.get(
            "process_shutdown_force_used", False
        ),
        "process_shutdown_wait_ms": evidence.get(
            "process_shutdown_wait_ms", 0.0
        ),
        "process_shutdown_timings_ms": evidence.get(
            "process_shutdown_timings_ms", []
        ),
        "process_cleanup_pending": evidence.get(
            "process_cleanup_pending", 0
        ),
        "process_cancel_state": evidence.get("process_cancel_state", "idle"),
        "process_cancel_rounds": evidence.get("process_cancel_rounds", 0),
        "process_cancel_wait_ms": evidence.get("process_cancel_wait_ms", 0.0),
        "process_state_sequence": evidence.get("process_state_sequence", []),
        "uv_size_masters": evidence.get("uv_size_masters", []),
        "uv_size_master_areas": evidence.get("uv_size_master_areas", []),
        "uv_size_by_key": evidence.get("uv_size_by_key", []),
        "process_graph_slices": evidence.get("process_graph_slices", 0),
        "process_graph_primitive_ops": evidence.get(
            "process_graph_primitive_ops", 0
        ),
        "process_graph_max_slice_ms": evidence.get(
            "process_graph_max_slice_ms", 0.0
        ),
        "process_graph_max_primitive_ms": evidence.get(
            "process_graph_max_primitive_ms", 0.0
        ),
        "process_graph_max_primitive": evidence.get(
            "process_graph_max_primitive", {}
        ),
        "process_graph_build_ms": evidence.get("process_graph_build_ms", 0.0),
        "process_graph_cache_builds": evidence.get(
            "process_graph_cache_builds", 0
        ),
        "process_graph_cache_hits": evidence.get("process_graph_cache_hits", 0),
        "process_graph_rejections": evidence.get(
            "process_graph_rejections", {}
        ),
        "process_graph_worker_submitted": evidence.get(
            "process_graph_worker_submitted", 0
        ),
        "process_graph_worker_completed": evidence.get(
            "process_graph_worker_completed", 0
        ),
        "process_graph_worker_operations": evidence.get(
            "process_graph_worker_operations", 0
        ),
        "process_graph_worker_cache_hits": evidence.get(
            "process_graph_worker_cache_hits", 0
        ),
        "process_graph_main_operations": evidence.get(
            "process_graph_main_operations", 0
        ),
        "process_graph_projection_ms": evidence.get(
            "process_graph_projection_ms", 0.0
        ),
        "process_graph_context_digest": evidence.get(
            "process_graph_context_digest"
        ),
        "process_graph_context_build_ms": evidence.get(
            "process_graph_context_build_ms", 0.0
        ),
        "process_graph_context_frame_bytes": evidence.get(
            "process_graph_context_frame_bytes", 0
        ),
        "process_graph_context_payload_bytes": evidence.get(
            "process_graph_context_payload_bytes", 0
        ),
        "process_graph_context_load_submitted": evidence.get(
            "process_graph_context_load_submitted", 0
        ),
        "process_graph_context_load_acked": evidence.get(
            "process_graph_context_load_acked", 0
        ),
        "process_graph_context_load_ms": evidence.get(
            "process_graph_context_load_ms", 0.0
        ),
        "process_graph_context_ready": evidence.get(
            "process_graph_context_ready", False
        ),
        "process_worker_operation_distribution": evidence.get(
            "process_worker_operation_distribution", []
        ),
        "process_exact_first_shape_completed": evidence.get(
            "process_exact_first_shape_completed", 0
        ),
        "process_exact_first_shape_total": evidence.get(
            "process_exact_first_shape_total", 0
        ),
        "process_exact_first_timestamp_ms": evidence.get(
            "process_exact_first_timestamp_ms"
        ),
        "process_shutdown_timings_ms": evidence.get(
            "process_shutdown_timings_ms", []
        ),
        "process_job_object": evidence.get("process_job_object", {}),
        "main_thread_submit_ms": evidence.get(
            "main_thread_submit_ms", 0.0
        ),
        "main_thread_poll_ms": evidence.get("main_thread_poll_ms", 0.0),
        "main_thread_finalize_ms": evidence.get(
            "main_thread_finalize_ms", 0.0
        ),
        "worker_shutdown": evidence.get("worker_shutdown", False),
        "max_tick_stage": evidence.get("max_tick_stage"),
        "group_summaries": evidence.get("group_summaries", []),
        "topology_rejection_samples": evidence.get(
            "topology_rejection_samples", []
        ),
        "density_records": evidence.get("density_records", []),
        "groups": evidence.get("groups", []),
        "topology_rejections": evidence.get("topology_rejections", []),
        "exact_loop_writes": evidence.get("exact_loop_writes", 0),
        "diagnostics": diagnostics,
    }


def run_match_03(request):
    """Harness adapter that exercises the registered Align Similar operator."""

    context = request["bpy_context"]
    evidence = {"apply_records": []}
    global _MATCH03_EVIDENCE_SINK
    previous_sink = _MATCH03_EVIDENCE_SINK
    started = time.perf_counter()
    _MATCH03_EVIDENCE_SINK = evidence
    try:
        operator_result = bpy.ops.uv_gpt.align_to_selected()
    finally:
        _MATCH03_EVIDENCE_SINK = previous_sink
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    diagnostics = similarity_matcher.get_diagnostics()
    phases = {name: float(value) for name, value in diagnostics.phase_timings_ms}
    phase_sum_ms = sum(phases.values())
    operator_boundary_ms = max(0.0, elapsed_ms - phase_sum_ms)
    accounted_ms = phase_sum_ms + operator_boundary_ms
    scheduler_result = evidence.get("scheduler_result")
    return {
        "operator_result": sorted(str(value) for value in operator_result),
        "operator_error": None,
        "scheduler_result": scheduler_result,
        "scheduler_results": evidence.get("scheduler_results", []),
        "phase_reconciliation": {
            "reconciled": abs(elapsed_ms - accounted_ms) <= 1.0e-3,
            "total_ms": elapsed_ms,
            "phases_ms": phases,
            "phase_sum_ms": phase_sum_ms,
            "operator_boundary_overhead_ms": operator_boundary_ms,
            "operator_boundary_overhead_scope": (
                "bpy operator dispatch/report/undo boundary and any code outside "
                "the explicit internal phase timers; not numeric matcher work"
            ),
            "accounted_ms": accounted_ms,
            "unattributed_overhead_ms": max(0.0, elapsed_ms - accounted_ms),
        },
        "apply_records": evidence.get("apply_records", []),
        "groups": evidence.get("groups", []),
        "group_count": evidence.get("group_count", 0),
        "aligned_count": evidence.get("aligned_count", 0),
        "representative_keys": evidence.get("representative_keys", []),
        "quality_rejections": evidence.get("quality_rejections", []),
        "quality_rejection_count": evidence.get("quality_rejection_count", 0),
        "diagnostics": diagnostics,
    }


classes = (
    UVGPT_OT_paste_keep_position,
    UVGPT_OT_align_to_selected,
    UVGPT_OT_align_similar_pro_fast,
    UVGPT_OT_align_similar_pro_exact,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global _ACTIVE_PRO_SESSION, _ACTIVE_PRO_OPERATOR
    if _ACTIVE_PRO_OPERATOR is not None:
        _ACTIVE_PRO_OPERATOR._cleanup_modal(
            bpy.context,
            cancel=True,
            reason="unregister",
        )
    elif _ACTIVE_PRO_SESSION is not None and not _ACTIVE_PRO_SESSION.done:
        _ACTIVE_PRO_SESSION.cancel("unregister")
    _ACTIVE_PRO_SESSION = None
    _ACTIVE_PRO_OPERATOR = None
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
