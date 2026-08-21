"""MATCH-02 Blender 5.0 fixture harness.

This is the C12 background evidence harness.  It deliberately exercises the
real ``uv_gpt.align_to_selected`` operator and refuses to silently fall back to
the MATCH-01 point-cloud implementation.

The primary integration must expose this small diagnostics adapter from
``uv_gpt.similarity_matcher``::

    reset_diagnostics() -> None
    get_diagnostics() -> Mapping[str, int]

The returned mapping must expose (directly or under a ``counters`` mapping)
the counters ``descriptor_builds``, ``cache_hits``, ``coarse_candidates``,
``topology_candidates`` and ``full_fits``.  The harness also wraps the existing
stack_tools extraction/best-fit/apply seams for timing and applied-key
evidence.  Missing adapter functions are a hard error.

The process may mutate the opened fixture only in memory.  It never saves the
blend.  All files written by this script are under ``benchmarks/match_02_*``.
"""

import copy
import hashlib
import importlib
import json
import math
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path


# Must be set before importing the project package.  The runner also exports
# PYTHONDONTWRITEBYTECODE=1 as a second guard.
sys.dont_write_bytecode = True

import bmesh
import bpy
from mathutils import Vector


PACKET_ID = "MATCH-02"
DEFAULT_FIXTURE = Path(r"C:\Users\linhp\Downloads\cc.blend")
SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_PROJECT_ROOT = SCRIPT_PATH.parents[2]
RESULT_NAME = "match_02_baseline.json"
BEFORE_SVG_NAME = "match_02_uv_before.svg"
AFTER_SVG_NAME = "match_02_uv_after.svg"
EXPECTED_FIXTURE_SHA = (
    "840EA32C822784201EFAB30B9441A98621E6FBD87DC9BDD431B7EB90A2FF93CD"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
TARGET_FACE_KEY = (602, 603, 604, 605)
EXPECTED_CANDIDATE_FACE_KEY = (9448, 9484, 9967, 17967)
QUALITY_TOLERANCE = 1.0e-4
SELECTION_EPSILON = 1.0e-12
BOUNDARY_EPSILON = 1.0e-10


class HarnessError(RuntimeError):
    """A reproducibility or API-contract error in the harness."""


def _arg_value(name, default):
    if "--" not in sys.argv:
        return default
    args = sys.argv[sys.argv.index("--") + 1 :]
    try:
        index = args.index(name)
    except ValueError:
        return default
    if index + 1 >= len(args):
        return default
    return args[index + 1]


PROJECT_ROOT = Path(
    _arg_value("--project-root", str(DEFAULT_PROJECT_ROOT))
).resolve()
FIXTURE_PATH = Path(_arg_value("--fixture", str(DEFAULT_FIXTURE))).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
BENCHMARK_ROOT = PROJECT_ROOT / "benchmarks"
RESULT_PATH = BENCHMARK_ROOT / RESULT_NAME
BEFORE_SVG_PATH = BENCHMARK_ROOT / BEFORE_SVG_NAME
AFTER_SVG_PATH = BENCHMARK_ROOT / AFTER_SVG_NAME


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_and_artifact_hashes():
    result = {}
    for path in sorted((PROJECT_ROOT / "uv_gpt").glob("*.py")):
        result[path.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(path)
    artifact = PROJECT_ROOT / "uv_gpt_v1.2.5.zip"
    if artifact.is_file():
        result[artifact.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(artifact)
    return result


def harness_hashes():
    result = {}
    paths = [
        PROJECT_ROOT / "tests" / "blender" / "match_02_fixture.py",
        PROJECT_ROOT / "tests" / "blender" / "run_match_02.ps1",
    ]
    for path in paths:
        if path.is_file():
            result[path.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(path)
    return result


def clean_json(value):
    """Convert Blender/math values and diagnostic objects to JSON-safe data."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Vector):
        return [clean_json(component) for component in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return clean_json(value.to_dict())
    if hasattr(value, "__dict__"):
        return clean_json(vars(value))
    return str(value)


def face_key(island):
    return tuple(sorted({int(loop.face.index) for loop in island}))


def loop_key(face, local_index):
    return int(face.index), int(local_index)


def setup_bmesh(bm):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()


def activate_object_in_object_mode(obj):
    current = bpy.context.object
    if current is not None and current.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for candidate in bpy.context.view_layer.objects:
        candidate.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def set_active_uv_map(obj, uv_name):
    for index, layer in enumerate(obj.data.uv_layers):
        if layer.name == uv_name:
            obj.data.uv_layers.active_index = index
            return
    raise HarnessError(f"UV map missing: {obj.name}/{uv_name}")


def open_case(obj, uv_name, island_tools, uv_utils):
    activate_object_in_object_mode(obj)
    set_active_uv_map(obj, uv_name)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    target = next((item for item in islands if face_key(item) == TARGET_FACE_KEY), None)
    if target is None:
        raise HarnessError(
            f"Deterministic target island not found: {TARGET_OBJECT_NAME}/{TARGET_UV_NAME}/"
            f"{TARGET_FACE_KEY}"
        )
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(bm, uv_layer, [target])
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    selected = island_tools.get_selected_uv_islands(bm, uv_layer)
    if [face_key(item) for item in selected] != [TARGET_FACE_KEY]:
        raise HarnessError(
            "Selection setup failed; expected exactly the deterministic target "
            f"{TARGET_FACE_KEY}, got {[face_key(item) for item in selected]}"
        )
    return bm, uv_layer


def snapshot_uv(bm, uv_layer):
    setup_bmesh(bm)
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            uv = loop[uv_layer].uv
            result[loop_key(face, local_index)] = (float(uv.x), float(uv.y))
    return result


def snapshot_selection(bm, uv_layer):
    setup_bmesh(bm)
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            result[loop_key(face, local_index)] = (
                bool(getattr(luv, "select", False)),
                bool(getattr(luv, "select_edge", False)),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
            )
    return result


def restore_state(obj, uv_utils, uv_snapshot, selection_snapshot):
    bm = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError(f"UV layer missing while restoring: {TARGET_UV_NAME}")
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            value = uv_snapshot.get(loop_key(face, local_index))
            if value is not None:
                loop[uv_layer].uv = Vector(value)
    uv_utils.restore_uv_selection(bm, uv_layer, selection_snapshot)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return bm, uv_layer


def selected_snapshot_unchanged(before, after):
    return before == after


def max_uv_delta(before, after, keys=None):
    wanted = set(keys) if keys is not None else set(before)
    deltas = []
    for key in wanted:
        lhs = before.get(key)
        rhs = after.get(key, lhs)
        if lhs is None or rhs is None:
            continue
        deltas.append(max(abs(lhs[0] - rhs[0]), abs(lhs[1] - rhs[1])))
    return max(deltas, default=0.0)


def island_loop_keys(island):
    return [loop_key(loop.face, local_index) for loop in island for local_index, candidate in enumerate(loop.face.loops) if candidate is loop]


def island_delta(before, after, island):
    keys = island_loop_keys(island)
    return max_uv_delta(before, after, keys)


def _point_key(point):
    return round(float(point.x), 10), round(float(point.y), 10)


def boundary_segments(island, uv_layer, stack_tools):
    function = getattr(stack_tools, "_island_boundary_segments", None)
    if not callable(function):
        raise HarnessError(
            "MATCH-02 integration mismatch: stack_tools._island_boundary_segments "
            "is required by the visual/metric harness"
        )
    return [(start.copy(), end.copy()) for start, end in function(island, uv_layer)]


def _component_nodes(segments):
    adjacency = defaultdict(set)
    points = {}
    for start, end in segments:
        start_key = _point_key(start)
        end_key = _point_key(end)
        if start_key == end_key:
            continue
        points[start_key] = (float(start.x), float(start.y))
        points[end_key] = (float(end.x), float(end.y))
        adjacency[start_key].add(end_key)
        adjacency[end_key].add(start_key)
    return points, adjacency


def _trace_component(nodes, adjacency):
    if not nodes:
        return [], False
    closed = all(len(adjacency[node]) == 2 for node in nodes)
    start = min(nodes)
    path = [start]
    previous = None
    current = start
    visited_edges = set()
    max_steps = max(len(nodes) * 3, 8)
    for _ in range(max_steps):
        neighbours = sorted(adjacency[current])
        candidates = [
            neighbour
            for neighbour in neighbours
            if (min(current, neighbour), max(current, neighbour)) not in visited_edges
        ]
        if not candidates:
            break
        if previous is not None and len(candidates) > 1:
            non_previous = [item for item in candidates if item != previous]
            next_node = min(non_previous or candidates)
        else:
            next_node = min(candidates)
        visited_edges.add((min(current, next_node), max(current, next_node)))
        previous, current = current, next_node
        if current == start:
            break
        path.append(current)
    return [nodes[node] for node in path], bool(closed and current == start)


def _polygon_signed_area(points):
    if len(points) < 3:
        return 0.0
    return 0.5 * sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )


def _perimeter(points, closed):
    if len(points) < 2:
        return 0.0
    total = sum(
        math.hypot(
            points[index][0] - points[index + 1][0],
            points[index][1] - points[index + 1][1],
        )
        for index in range(len(points) - 1)
    )
    if closed:
        total += math.hypot(
            points[-1][0] - points[0][0], points[-1][1] - points[0][1]
        )
    return total


def ordered_boundary_components(island, uv_layer, stack_tools):
    segments = boundary_segments(island, uv_layer, stack_tools)
    points, adjacency = _component_nodes(segments)
    unseen = set(adjacency)
    components = []
    while unseen:
        seed = min(unseen)
        nodes = set([seed])
        stack = [seed]
        while stack:
            current = stack.pop()
            for linked in adjacency[current]:
                if linked not in nodes:
                    nodes.add(linked)
                    stack.append(linked)
        unseen.difference_update(nodes)
        ordered, closed = _trace_component(
            {node: points[node] for node in nodes}, adjacency
        )
        area = _polygon_signed_area(ordered) if closed else 0.0
        components.append(
            {
                "points": ordered,
                "closed": closed,
                "signed_area": area,
                "area": abs(area),
                "perimeter": _perimeter(ordered, closed),
                "point_count": len(ordered),
                "node_degree_histogram": {
                    str(degree): sum(1 for node in nodes if len(adjacency[node]) == degree)
                    for degree in sorted({len(adjacency[node]) for node in nodes})
                },
            }
        )
    closed_indices = [
        index for index, item in enumerate(components) if item["closed"] and item["point_count"] >= 3
    ]
    # For a single connected UV island this is the standard outer/layer
    # classification: largest closed loop is outer, remaining loops are holes.
    # Disconnected/open cases are kept explicit and are never silently treated
    # as a zero-hole compatible island.
    outer_index = max(
        closed_indices, key=lambda index: (components[index]["area"], -index), default=None
    )
    for index, item in enumerate(components):
        if not item["closed"]:
            item["role"] = "open"
        elif index == outer_index:
            item["role"] = "outer"
        else:
            item["role"] = "hole"
    return components


def boundary_summary(island, uv_layer, stack_tools):
    components = ordered_boundary_components(island, uv_layer, stack_tools)
    return {
        "component_count": len(components),
        "closed_component_count": sum(item["closed"] for item in components),
        "open_component_count": sum(not item["closed"] for item in components),
        "outer_loop_count": sum(item.get("role") == "outer" for item in components),
        "hole_loop_count": sum(item.get("role") == "hole" for item in components),
        "zero_length_or_degenerate": any(
            item["perimeter"] <= BOUNDARY_EPSILON
            or item["point_count"] < 3
            for item in components
        ),
        "classification_method": "largest-closed-loop-outer; remaining closed components holes; open explicit",
        "components": components,
    }


def _resample_closed(points, sample_count=128):
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return None
    lengths = []
    total = 0.0
    for index, point in enumerate(points):
        other = points[(index + 1) % len(points)]
        length = math.hypot(point[0] - other[0], point[1] - other[1])
        lengths.append(length)
        total += length
    if total <= BOUNDARY_EPSILON:
        return None
    samples = []
    for sample_index in range(sample_count):
        distance = total * sample_index / sample_count
        travelled = 0.0
        for index, length in enumerate(lengths):
            if distance <= travelled + length or index == len(lengths) - 1:
                ratio = 0.0 if length <= BOUNDARY_EPSILON else (distance - travelled) / length
                lhs = points[index]
                rhs = points[(index + 1) % len(points)]
                samples.append(
                    (
                        lhs[0] + (rhs[0] - lhs[0]) * ratio,
                        lhs[1] + (rhs[1] - lhs[1]) * ratio,
                    )
                )
                break
            travelled += length
    return samples


def _best_loop_rms(reference, candidate, sample_count=128):
    ref = _resample_closed(reference, sample_count)
    cand = _resample_closed(candidate, sample_count)
    if ref is None or cand is None:
        return None
    best = None
    count = len(ref)
    for reverse in (False, True):
        for shift in range(count):
            squared = 0.0
            max_distance = 0.0
            for index, point in enumerate(ref):
                source_index = (shift + index) % count
                if reverse:
                    source_index = (shift - index) % count
                other = cand[source_index]
                distance = math.hypot(point[0] - other[0], point[1] - other[1])
                squared += distance * distance
                max_distance = max(max_distance, distance)
            rms = math.sqrt(squared / count)
            if best is None or (rms, max_distance, reverse, shift) < best:
                best = (rms, max_distance, reverse, shift)
    return best


def boundary_rms(reference_summary, candidate_summary):
    reference_outer = [
        item for item in reference_summary["components"] if item.get("role") == "outer"
    ]
    candidate_outer = [
        item for item in candidate_summary["components"] if item.get("role") == "outer"
    ]
    reference_holes = sorted(
        [item for item in reference_summary["components"] if item.get("role") == "hole"],
        key=lambda item: (-item["area"], item["point_count"]),
    )
    candidate_holes = sorted(
        [item for item in candidate_summary["components"] if item.get("role") == "hole"],
        key=lambda item: (-item["area"], item["point_count"]),
    )
    if (
        len(reference_outer) != 1
        or len(candidate_outer) != 1
        or len(reference_holes) != len(candidate_holes)
    ):
        return {
            "normalized_rms": None,
            "max_residual": None,
            "within_tolerance": False,
            "reason": "outer/hole count mismatch or unsupported open boundary",
        }
    pairs = [(reference_outer[0], candidate_outer[0])]
    pairs.extend(zip(reference_holes, candidate_holes))
    loop_metrics = []
    weighted_squared = 0.0
    weight_total = 0
    max_residual = 0.0
    for reference, candidate in pairs:
        metric = _best_loop_rms(reference["points"], candidate["points"])
        if metric is None:
            return {
                "normalized_rms": None,
                "max_residual": None,
                "within_tolerance": False,
                "reason": "degenerate loop",
            }
        rms, residual, reverse, shift = metric
        sample_count = 128
        weighted_squared += rms * rms * sample_count
        weight_total += sample_count
        max_residual = max(max_residual, residual)
        loop_metrics.append(
            {
                "rms": rms,
                "max_residual": residual,
                "reverse": reverse,
                "cyclic_shift": shift,
                "reference_perimeter": reference["perimeter"],
                "candidate_perimeter": candidate["perimeter"],
            }
        )
    normalizer = max(reference_outer[0]["perimeter"], BOUNDARY_EPSILON)
    normalized = math.sqrt(weighted_squared / weight_total) / normalizer
    return {
        "normalized_rms": normalized,
        "max_residual": max_residual / normalizer,
        "within_tolerance": normalized <= QUALITY_TOLERANCE,
        "reason": None,
        "loop_metrics": loop_metrics,
        "normalizer": normalizer,
    }


def _diagnostics_mapping(value):
    if value is None:
        raise HarnessError("similarity_matcher.get_diagnostics() returned None")
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    elif hasattr(value, "__dict__") and not isinstance(value, dict):
        value = vars(value)
    if not isinstance(value, dict):
        raise HarnessError(
            "similarity_matcher.get_diagnostics() must return a mapping or an object "
            f"with __dict__, got {type(value).__name__}"
        )
    result = dict(value)
    for nested_key in ("counters", "counts", "metrics"):
        nested = result.get(nested_key)
        if isinstance(nested, dict):
            for key, item in nested.items():
                result.setdefault(key, item)
    return result


DIAGNOSTIC_ALIASES = {
    "descriptor_builds": (
        "descriptor_builds",
        "descriptor_build_count",
        "descriptors_built",
    ),
    "cache_hits": ("cache_hits", "descriptor_cache_hits", "cache_hit_count"),
    "coarse_candidates": (
        "coarse_candidates",
        "coarse_candidate_count",
        "boundary_candidates",
    ),
    "topology_candidates": (
        "topology_candidates",
        "topology_candidate_count",
        "topology_passes",
    ),
    "full_fits": (
        "full_fits",
        "full_fit_count",
        "full_matches",
        "fits",
    ),
}


def resolve_diagnostics_adapter(similarity_matcher):
    reset = getattr(similarity_matcher, "reset_diagnostics", None)
    get = getattr(similarity_matcher, "get_diagnostics", None)
    missing = []
    if not callable(reset):
        missing.append("reset_diagnostics()")
    if not callable(get):
        missing.append("get_diagnostics()")
    if missing:
        raise HarnessError(
            "MATCH-02 API mismatch: uv_gpt.similarity_matcher must expose "
            + ", ".join(missing)
            + "; the harness refuses to reduce coverage"
        )

    def reset_and_read():
        reset()
        return read()

    def read():
        raw = _diagnostics_mapping(get())
        normalized = {}
        lowered = {str(key).lower(): value for key, value in raw.items()}
        for canonical, aliases in DIAGNOSTIC_ALIASES.items():
            found = next((lowered[name.lower()] for name in aliases if name.lower() in lowered), None)
            if found is None:
                raise HarnessError(
                    "MATCH-02 diagnostics mismatch: missing counter "
                    f"{canonical}; returned keys={sorted(map(str, raw))}"
                )
            try:
                normalized[canonical] = int(found)
            except (TypeError, ValueError) as exc:
                raise HarnessError(
                    f"MATCH-02 diagnostics counter {canonical} is not an integer: {found!r}"
                ) from exc
            if normalized[canonical] < 0:
                raise HarnessError(
                    f"MATCH-02 diagnostics counter {canonical} is negative: {found!r}"
                )
        normalized["raw"] = clean_json(raw)
        return normalized

    return reset, read


def _transform_value(transform, name, default=None):
    if isinstance(transform, dict):
        return transform.get(name, default)
    return getattr(transform, name, default)


def instrument_stack(island_tools, stack_tools, metrics):
    original_get = getattr(island_tools, "get_uv_islands", None)
    original_best = getattr(stack_tools, "_best_align_transform", None)
    original_apply = getattr(stack_tools, "_apply_align_transform", None)
    if not callable(original_get) or not callable(original_best) or not callable(original_apply):
        raise HarnessError(
            "MATCH-02 integration mismatch: expected island_tools.get_uv_islands, "
            "stack_tools._best_align_transform and stack_tools._apply_align_transform"
        )

    def timed_get(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_get(*args, **kwargs)
        finally:
            metrics["extraction_seconds"] += time.perf_counter() - started
            metrics["extraction_calls"] += 1

    def timed_best(*args, **kwargs):
        started = time.perf_counter()
        result = original_best(*args, **kwargs)
        metrics["match_seconds"] += time.perf_counter() - started
        metrics["match_calls"] += 1
        if result is not None:
            reference = args[0] if len(args) > 0 else kwargs.get("ref_island")
            candidate = args[1] if len(args) > 1 else kwargs.get("target_island")
            metrics["match_results"].append(
                {
                    "ref_key": list(face_key(reference)) if reference is not None else None,
                    "target_key": list(face_key(candidate)) if candidate is not None else None,
                    "score": clean_json(_transform_value(result, "score")),
                    "transform": clean_json(result),
                }
            )
        return result

    def timed_apply(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_apply(*args, **kwargs)
        finally:
            metrics["apply_seconds"] += time.perf_counter() - started
            metrics["apply_calls"] += 1
            island = args[0] if args else kwargs.get("target_island")
            transform = args[2] if len(args) > 2 else kwargs.get("transform")
            metrics["apply_records"].append(
                {
                    "target_key": list(face_key(island)) if island is not None else None,
                    "score": clean_json(_transform_value(transform, "score")),
                    "transform": clean_json(transform),
                }
            )

    island_tools.get_uv_islands = timed_get
    stack_tools._best_align_transform = timed_best
    stack_tools._apply_align_transform = timed_apply

    def restore():
        island_tools.get_uv_islands = original_get
        stack_tools._best_align_transform = original_best
        stack_tools._apply_align_transform = original_apply

    return restore


def empty_metrics():
    return {
        "extraction_seconds": 0.0,
        "match_seconds": 0.0,
        "apply_seconds": 0.0,
        "extraction_calls": 0,
        "match_calls": 0,
        "apply_calls": 0,
        "match_results": [],
        "apply_records": [],
    }


def _islands_by_key(islands):
    return {face_key(island): island for island in islands}


def _case_size(bm, islands, selected):
    return {
        "mesh_faces": len(bm.faces),
        "mesh_edges": len(bm.edges),
        "mesh_verts": len(bm.verts),
        "mesh_loops": sum(len(face.loops) for face in bm.faces),
        "uv_islands": len(islands),
        "selected_islands": len(selected),
        "unselected_candidates": len(islands) - len(selected),
    }


def _operator_run(
    obj,
    island_tools,
    stack_tools,
    uv_utils,
    diagnostics_reset,
    diagnostics_read,
    baseline_uv,
    baseline_selection,
    measured,
):
    bm, uv_layer = restore_state(obj, uv_utils, baseline_uv, baseline_selection)
    diagnostics_reset()
    islands_before = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected_before = island_tools.get_selected_uv_islands(bm, uv_layer)
    if [face_key(item) for item in selected_before] != [TARGET_FACE_KEY]:
        raise HarnessError("Baseline restore lost deterministic target selection")
    before_islands = _islands_by_key(islands_before)
    before_summaries = {
        key: boundary_summary(island, uv_layer, stack_tools)
        for key, island in before_islands.items()
        if key in (TARGET_FACE_KEY, EXPECTED_CANDIDATE_FACE_KEY)
    }
    before_uv = snapshot_uv(bm, uv_layer)
    before_selection = snapshot_selection(bm, uv_layer)
    diagnostics_read()  # Require a valid adapter before executing the operator.
    metrics = empty_metrics()
    restore_instrumentation = instrument_stack(island_tools, stack_tools, metrics)
    started = time.perf_counter()
    try:
        operator_result = list(bpy.ops.uv_gpt.align_to_selected())
    except Exception as exc:
        operator_result = []
        operator_error = f"{type(exc).__name__}: {exc}"
    else:
        operator_error = None
    elapsed = time.perf_counter() - started
    restore_instrumentation()
    diagnostics = diagnostics_read()

    bm_after = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm_after)
    uv_layer_after = bm_after.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer_after is None:
        raise HarnessError("UV layer disappeared after operator")
    islands_after = island_tools.get_uv_islands(bm_after, uv_layer_after, selected_only=False)
    selected_after = island_tools.get_selected_uv_islands(bm_after, uv_layer_after)
    after_islands = _islands_by_key(islands_after)
    after_uv = snapshot_uv(bm_after, uv_layer_after)
    after_selection = snapshot_selection(bm_after, uv_layer_after)

    selected_delta = max_uv_delta(before_uv, after_uv, [
        key
        for island in selected_before
        for key in island_loop_keys(island)
    ])
    selected_unchanged = selected_delta <= SELECTION_EPSILON
    selection_unchanged = selected_snapshot_unchanged(before_selection, after_selection)
    changed_candidates = []
    applied_keys = {
        tuple(record["target_key"])
        for record in metrics["apply_records"]
        if record.get("target_key") is not None
    }
    for key, island in sorted(after_islands.items()):
        if key == TARGET_FACE_KEY:
            continue
        delta = island_delta(before_uv, after_uv, island)
        if delta > SELECTION_EPSILON:
            changed_candidates.append(
                {
                    "target_key": list(key),
                    "max_uv_delta": delta,
                    "observed_apply_call": key in applied_keys,
                }
            )

    after_summaries = {
        key: boundary_summary(island, uv_layer_after, stack_tools)
        for key, island in after_islands.items()
        if key in (TARGET_FACE_KEY, EXPECTED_CANDIDATE_FACE_KEY)
    }
    quality = {}
    for key in (EXPECTED_CANDIDATE_FACE_KEY,):
        if key in before_summaries and key in after_summaries:
            quality["candidate"] = {
                "target_key": list(key),
                "before": boundary_rms(before_summaries[TARGET_FACE_KEY], before_summaries[key]),
                "after": boundary_rms(after_summaries[TARGET_FACE_KEY], after_summaries[key]),
            }
    for item in changed_candidates:
        key = tuple(item["target_key"])
        if key in before_summaries and key in after_summaries:
            quality.setdefault("changed_candidates", []).append(
                {
                    "target_key": list(key),
                    "after": boundary_rms(
                        after_summaries[TARGET_FACE_KEY], after_summaries[key]
                    ),
                }
            )
    return {
        "measured": measured,
        "elapsed_seconds": elapsed,
        "elapsed_ms": elapsed * 1000.0,
        "operator_result": operator_result,
        "operator_error": operator_error,
        "case_size": _case_size(bm_after, islands_after, selected_after),
        "diagnostics": diagnostics,
        "instrumentation": {
            **metrics,
            "extraction_ms": metrics["extraction_seconds"] * 1000.0,
            "match_ms": metrics["match_seconds"] * 1000.0,
            "apply_ms": metrics["apply_seconds"] * 1000.0,
        },
        "correctness": {
            "selected_uv_unchanged": selected_unchanged,
            "selected_uv_max_delta": selected_delta,
            "selection_snapshot_unchanged": selection_unchanged,
            "changed_candidate_count": len(changed_candidates),
            "changed_candidates": changed_candidates,
            "applied_candidate_keys": [list(key) for key in sorted(applied_keys)],
            "unobserved_changed_candidates": [
                item for item in changed_candidates if not item["observed_apply_call"]
            ],
            "quality": quality,
        },
        "visual_before": {
            "target": copy.deepcopy(before_summaries.get(TARGET_FACE_KEY)),
            "candidate": copy.deepcopy(before_summaries.get(EXPECTED_CANDIDATE_FACE_KEY)),
        },
        "visual_after": {
            "target": copy.deepcopy(after_summaries.get(TARGET_FACE_KEY)),
            "candidate": copy.deepcopy(after_summaries.get(EXPECTED_CANDIDATE_FACE_KEY)),
        },
    }


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    ratio = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * ratio


def timing_summary(runs):
    def summary(values):
        values = [float(item) for item in values]
        return {
            "count": len(values),
            "min_ms": min(values) if values else None,
            "median_ms": sorted(values)[len(values) // 2] if values else None,
            "p95_ms": _percentile(values, 0.95),
        }

    return {
        "measured_run_count": len(runs),
        "total": summary([item["elapsed_ms"] for item in runs]),
        "extraction": summary([item["instrumentation"]["extraction_ms"] for item in runs]),
        "match": summary([item["instrumentation"]["match_ms"] for item in runs]),
        "apply": summary([item["instrumentation"]["apply_ms"] for item in runs]),
        "case_sizes": [item["case_size"] for item in runs],
    }


def _svg_escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _svg_path(points, bounds, panel_x, panel_y, panel_width, panel_height):
    if not points:
        return ""
    min_x, max_x, min_y, max_y = bounds
    span_x = max(max_x - min_x, BOUNDARY_EPSILON)
    span_y = max(max_y - min_y, BOUNDARY_EPSILON)
    pad = 36.0

    def project(point):
        x = panel_x + pad + (point[0] - min_x) / span_x * (panel_width - 2 * pad)
        y = panel_y + panel_height - pad - (point[1] - min_y) / span_y * (panel_height - 2 * pad)
        return x, y

    projected = [project(point) for point in points]
    commands = [f"M {projected[0][0]:.3f},{projected[0][1]:.3f}"]
    commands.extend(f"L {x:.3f},{y:.3f}" for x, y in projected[1:])
    return " ".join(commands)


def write_svg(path, title, before, after=None):
    groups = [("Before operator", before["target"], before["candidate"])]
    if after is not None:
        groups.append(("After operator", after["target"], after["candidate"]))
    all_points = []
    for _label, target, candidate in groups:
        for summary in (target, candidate):
            if summary:
                for component in summary["components"]:
                    all_points.extend(component["points"])
    if not all_points:
        raise HarnessError("Cannot create SVG: no ordered boundary points")
    min_x = min(point[0] for point in all_points)
    max_x = max(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    max_y = max(point[1] for point in all_points)
    bounds = (min_x, max_x, min_y, max_y)
    panel_width, panel_height = 570, 520
    panel_gap = 20
    width = panel_width * len(groups) + panel_gap * (len(groups) + 1)
    height = 620
    fragments = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<title>{_svg_escape(title)}</title>',
        '<rect width="100%" height="100%" fill="#10151c"/>',
        '<style>text{font-family:Segoe UI,Arial,sans-serif}.outer{fill:none;stroke-width:3}.hole{fill:none;stroke-width:2;stroke-dasharray:7 5}.legend{font-size:15px}.label{font-size:18px;font-weight:600}</style>',
    ]
    colors = (("#4db6ff", "target"), ("#ff9f43", "candidate"))
    for panel_index, (label, target, candidate) in enumerate(groups):
        panel_x = panel_gap + panel_index * (panel_width + panel_gap)
        panel_y = 54
        fragments.append(
            f'<rect x="{panel_x}" y="{panel_y}" width="{panel_width}" height="{panel_height}" rx="8" fill="#18222e" stroke="#34495e"/>'
        )
        fragments.append(
            f'<text class="label" x="{panel_x + 18}" y="32" fill="#ecf0f1">{_svg_escape(label)}</text>'
        )
        for summary, (color, name) in zip((target, candidate), colors):
            if not summary:
                continue
            for component in summary["components"]:
                path_data = _svg_path(
                    component["points"], bounds, panel_x, panel_y, panel_width, panel_height
                )
                if not path_data:
                    continue
                role = component.get("role", "open")
                class_name = "hole" if role == "hole" else "outer"
                close = " Z" if component.get("closed") else ""
                fragments.append(
                    f'<path class="{class_name}" d="{path_data}{close}" stroke="{color}"/>'
                )
            fragments.append(
                f'<text class="legend" x="{panel_x + 18}" y="{panel_y + 30 + (0 if name == "target" else 24)}" fill="{color}">{name}: outer solid / holes dashed</text>'
            )
    fragments.append(
        '<text class="legend" x="20" y="602" fill="#bdc3c7">Ordered UV boundary evidence; blue=target, orange=candidate. Geometry only; no aesthetic claim.</text>'
    )
    fragments.append("</svg>")
    path.write_text("\n".join(fragments), encoding="utf-8", newline="\n")


def _baseline_summary():
    path = BENCHMARK_ROOT / "match_01_baseline.json"
    if not path.is_file():
        return {"available": False, "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "path": str(path), "error": str(exc)}
    benchmark = data.get("benchmark", {})
    return {
        "available": True,
        "path": str(path),
        "total": benchmark.get("summary", {}).get("total"),
        "match": benchmark.get("summary", {}).get("match"),
        "extraction": benchmark.get("summary", {}).get("extraction"),
        "candidate_count": data.get("execution_case", {}).get("case_size", {}).get(
            "unselected_candidates"
        ),
        "match_calls": benchmark.get("measured_runs", [{}])[0]
        .get("instrumentation", {})
        .get("match_calls"),
    }


def _cache_scan():
    paths = []
    for suffix in ("*.pyc", "*.pyo"):
        paths.extend(
            path
            for path in (PROJECT_ROOT / "uv_gpt").rglob(suffix)
            if path.is_file()
        )
    return [path.relative_to(PROJECT_ROOT).as_posix() for path in sorted(paths)]


def _validate_completed(runs, svg_paths, source_before, source_after, harness_before, harness_after):
    measured = [item for item in runs if item["measured"]]
    if len(measured) != 5:
        raise HarnessError(f"Expected 5 measured runs, got {len(measured)}")
    for index, item in enumerate(measured, 1):
        if item["operator_result"] != ["FINISHED"] or item["operator_error"]:
            raise HarnessError(f"Measured run {index} did not finish: {item}")
        correctness = item["correctness"]
        if not correctness["selected_uv_unchanged"]:
            raise HarnessError(f"Measured run {index} changed selected target UV")
        if not correctness["selection_snapshot_unchanged"]:
            raise HarnessError(f"Measured run {index} changed selection snapshot")
        if correctness["unobserved_changed_candidates"]:
            raise HarnessError(
                f"Measured run {index} changed candidate without apply evidence: "
                f"{correctness['unobserved_changed_candidates']}"
            )
        after = correctness["quality"].get("candidate", {}).get("after")
        if not after or not after.get("within_tolerance"):
            raise HarnessError(f"Measured run {index} failed normalized boundary RMS gate: {after}")
        full_fits = item["diagnostics"]["full_fits"]
        if full_fits < 1:
            raise HarnessError(f"Measured run {index} reported no full fits")
    if not all(path.is_file() for path in svg_paths):
        raise HarnessError(f"Missing SVG evidence: {svg_paths}")
    if source_before != source_after:
        raise HarnessError("Source/artifact hashes changed during Blender run")
    if harness_before != harness_after:
        raise HarnessError("Harness hashes changed during Blender run")
    cache_files = _cache_scan()
    if cache_files:
        raise HarnessError(f"Project add-on cache/pyc remains: {cache_files}")


def run_harness():
    if not FIXTURE_PATH.is_file():
        raise HarnessError(f"Fixture missing: {FIXTURE_PATH}")
    fixture_sha_in_process_before = sha256_file(FIXTURE_PATH)
    if FIXTURE_SHA_BEFORE_EXTERNAL and FIXTURE_SHA_BEFORE_EXTERNAL != fixture_sha_in_process_before:
        raise HarnessError(
            "Fixture SHA changed between runner preflight and Blender script: "
            f"external={FIXTURE_SHA_BEFORE_EXTERNAL}, in_process={fixture_sha_in_process_before}"
        )
    if fixture_sha_in_process_before != EXPECTED_FIXTURE_SHA:
        raise HarnessError(
            f"Unexpected fixture SHA before run: {fixture_sha_in_process_before}; "
            f"expected {EXPECTED_FIXTURE_SHA}"
        )

    source_before = source_and_artifact_hashes()
    harness_before = harness_hashes()
    if "uv_gpt/stack_tools.py" not in source_before:
        raise HarnessError("Project source package missing uv_gpt/stack_tools.py")

    addon = None
    registered = False
    runs = []
    result = None
    try:
        addon = importlib.import_module("uv_gpt")
        similarity_matcher = importlib.import_module("uv_gpt.similarity_matcher")
        island_tools = importlib.import_module("uv_gpt.island_tools")
        stack_tools = importlib.import_module("uv_gpt.stack_tools")
        uv_utils = importlib.import_module("uv_gpt.uv_utils")
        diagnostics_reset, diagnostics_read = resolve_diagnostics_adapter(similarity_matcher)
        addon.register()
        registered = True
        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH":
            raise HarnessError(f"Target mesh missing: {TARGET_OBJECT_NAME}")
        bm, uv_layer = open_case(obj, TARGET_UV_NAME, island_tools, uv_utils)
        baseline_uv = snapshot_uv(bm, uv_layer)
        baseline_selection = snapshot_selection(bm, uv_layer)
        baseline_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        baseline_selected = island_tools.get_selected_uv_islands(bm, uv_layer)
        baseline_size = _case_size(bm, baseline_islands, baseline_selected)
        if baseline_size["uv_islands"] != 577 or baseline_size["unselected_candidates"] != 576:
            raise HarnessError(f"Unexpected MATCH-01 case size: {baseline_size}")
        warmup = _operator_run(
            obj,
            island_tools,
            stack_tools,
            uv_utils,
            diagnostics_reset,
            diagnostics_read,
            baseline_uv,
            baseline_selection,
            measured=False,
        )
        runs.append(warmup)
        for _index in range(5):
            runs.append(
                _operator_run(
                    obj,
                    island_tools,
                    stack_tools,
                    uv_utils,
                    diagnostics_reset,
                    diagnostics_read,
                    baseline_uv,
                    baseline_selection,
                    measured=True,
                )
            )
        measured = [item for item in runs if item["measured"]]
        visual_before = measured[0]["visual_before"]
        visual_after = measured[0]["visual_after"]
        write_svg(
            BEFORE_SVG_PATH,
            "MATCH-02 ordered UV boundary evidence",
            visual_before,
        )
        write_svg(
            AFTER_SVG_PATH,
            "MATCH-02 ordered UV boundary evidence",
            visual_before,
            visual_after,
        )
        # Always restore the baseline before leaving Edit Mode.  This is not a
        # save operation and prevents the harness from leaving in-memory edits
        # behind if Blender executes cleanup handlers.
        restore_state(obj, uv_utils, baseline_uv, baseline_selection)
        source_after = source_and_artifact_hashes()
        harness_after = harness_hashes()
        fixture_sha_after = sha256_file(FIXTURE_PATH)
        if fixture_sha_after != fixture_sha_in_process_before:
            raise HarnessError(
                f"Fixture SHA changed during run: before={fixture_sha_in_process_before}, after={fixture_sha_after}"
            )
        _validate_completed(
            runs,
            (BEFORE_SVG_PATH, AFTER_SVG_PATH),
            source_before,
            source_after,
            harness_before,
            harness_after,
        )
        result = {
            "packet": PACKET_ID,
            "status": "completed",
            "script": str(SCRIPT_PATH),
            "project_root": str(PROJECT_ROOT),
            "fixture": str(FIXTURE_PATH),
            "commands": {
                "background": (
                    "blender.exe --factory-startup --disable-autoexec --background "
                    f"{FIXTURE_PATH} --python {SCRIPT_PATH} -- --project-root {PROJECT_ROOT} "
                    f"--fixture {FIXTURE_PATH} --fixture-sha-before {fixture_sha_in_process_before}"
                ),
                "fixture_sha_preflight": FIXTURE_SHA_BEFORE_EXTERNAL or None,
            },
            "fixture_sha256_before_external": FIXTURE_SHA_BEFORE_EXTERNAL or None,
            "fixture_sha256_before_in_process": fixture_sha_in_process_before,
            "fixture_sha256_after": fixture_sha_after,
            "fixture_sha256_unchanged": fixture_sha_after == fixture_sha_in_process_before,
            "runtime": {
                "blender_version": bpy.app.version_string,
                "blender_version_tuple": list(bpy.app.version),
                "python_version": sys.version,
                "numpy": _numpy_info(),
                "logical_cpu_count": os.cpu_count(),
            },
            "fixture_opened_path": bpy.data.filepath,
            "fixture_opened_path_exact": os.path.normcase(os.path.abspath(bpy.data.filepath))
            == os.path.normcase(str(FIXTURE_PATH)),
            "load_context": {
                "factory_startup": True,
                "disable_autoexec": True,
                "persistent_addon_install": False,
                "save_called": False,
            },
            "addon": {
                "loaded_from_project_source": True,
                "registered_in_memory": registered,
                "operator": "uv_gpt.align_to_selected",
                "similarity_matcher_diagnostics_adapter": [
                    "reset_diagnostics()",
                    "get_diagnostics()",
                ],
            },
            "execution_case": {
                "object": TARGET_OBJECT_NAME,
                "uv_map": TARGET_UV_NAME,
                "selection_source": "deterministic_in_memory",
                "selected_target_keys": [list(TARGET_FACE_KEY)],
                "expected_compatible_candidate_key": list(EXPECTED_CANDIDATE_FACE_KEY),
                "case_size": baseline_size,
            },
            "benchmark": {
                "warmup": warmup,
                "measured_runs": measured,
                "summary": timing_summary(measured),
                "baseline_match_01": _baseline_summary(),
                "comparison_note": (
                    "Timing is a correctness baseline comparison only; MATCH-03 owns CPU scheduling. "
                    "Diagnostics counters provide candidate-pruning evidence."
                ),
            },
            "correctness_summary": {
                "all_measured_selected_uv_unchanged": all(
                    item["correctness"]["selected_uv_unchanged"] for item in measured
                ),
                "all_measured_selection_unchanged": all(
                    item["correctness"]["selection_snapshot_unchanged"] for item in measured
                ),
                "changed_candidate_counts": [
                    item["correctness"]["changed_candidate_count"] for item in measured
                ],
                "applied_candidate_counts": [
                    len(item["correctness"]["applied_candidate_keys"]) for item in measured
                ],
                "normalized_boundary_rms_threshold": QUALITY_TOLERANCE,
                "max_applied_normalized_boundary_rms": max(
                    item["correctness"]["quality"]["candidate"]["after"]["normalized_rms"]
                    for item in measured
                ),
                "incompatible_candidate_change_count": sum(
                    len(item["correctness"]["unobserved_changed_candidates"])
                    for item in measured
                ),
            },
            "boundary_topology_hole_outcomes": {
                "target_before": visual_before.get("target"),
                "candidate_before": visual_before.get("candidate"),
                "target_after": visual_after.get("target"),
                "candidate_after": visual_after.get("candidate"),
            },
            "visual_evidence": {
                "before_svg": str(BEFORE_SVG_PATH),
                "after_svg": str(AFTER_SVG_PATH),
                "before_semantics": "single pre-operator panel",
                "after_semantics": "before/after comparison panels",
                "legend": "blue target; orange candidate; solid outer; dashed holes",
            },
            "source_artifact_hashes_before": source_before,
            "source_artifact_hashes_after": source_after,
            "source_artifact_hashes_unchanged": source_before == source_after,
            "harness_hashes_before": harness_before,
            "harness_hashes_after": harness_after,
            "harness_hashes_unchanged": harness_before == harness_after,
            "uv_gpt_cache_files": _cache_scan(),
            "result_path": str(RESULT_PATH),
        }
    finally:
        if registered and addon is not None:
            addon.unregister()
            registered = False
    return result


def _numpy_info():
    try:
        numpy = importlib.import_module("numpy")
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "version": getattr(numpy, "__version__", None)}


def main():
    BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        result = run_harness()
    except Exception as exc:
        failure = {
            "packet": PACKET_ID,
            "status": "failed",
            "script": str(SCRIPT_PATH),
            "project_root": str(PROJECT_ROOT),
            "fixture": str(FIXTURE_PATH),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "fixture_sha256_observed": sha256_file(FIXTURE_PATH) if FIXTURE_PATH.is_file() else None,
            "source_artifact_hashes": source_and_artifact_hashes(),
            "harness_hashes": harness_hashes(),
            "uv_gpt_cache_files": _cache_scan(),
        }
        RESULT_PATH.write_text(json.dumps(clean_json(failure), indent=2, sort_keys=True), encoding="utf-8")
        print(f"MATCH-02 status=failed: {failure['error']}")
        return 1
    RESULT_PATH.write_text(json.dumps(clean_json(result), indent=2, sort_keys=True), encoding="utf-8")
    print("MATCH-02 status=completed")
    print(f"MATCH-02 result: {RESULT_PATH}")
    print(f"MATCH-02 before SVG: {BEFORE_SVG_PATH}")
    print(f"MATCH-02 after SVG: {AFTER_SVG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
