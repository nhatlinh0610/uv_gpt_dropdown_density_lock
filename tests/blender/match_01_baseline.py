"""MATCH-01 read-only Blender baseline diagnosis.

This script is intentionally fixture-preserving: it opens the exact fixture in
the Blender process, may enter Edit Mode and mutate BMesh/selection state in
memory, but never saves a .blend file.  It writes only the JSON result under
the project-local ``benchmarks`` directory.

The companion runner invokes this file as:

    blender.exe --background C:\\Users\\linhp\\Downloads\\cc.blend \\
        --python tests/blender/match_01_baseline.py -- \\
        --project-root <project-root> --fixture C:\\Users\\linhp\\Downloads\\cc.blend
"""

import hashlib
import json
import math
import os
import platform
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path


# The add-on is imported from the project source.  Do not let Blender's Python
# create .pyc files below uv_gpt/, which is reference-only for this packet.
sys.dont_write_bytecode = True

import bmesh
import bpy
from mathutils import Vector


PACKET_ID = "MATCH-01"
DEFAULT_FIXTURE = Path(r"C:\Users\linhp\Downloads\cc.blend")
SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_PROJECT_ROOT = SCRIPT_PATH.parents[2]
RESULT_NAME = "match_01_baseline.json"
EPSILON = 1.0e-8


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


PROJECT_ROOT = Path(_arg_value("--project-root", str(DEFAULT_PROJECT_ROOT))).resolve()
FIXTURE_PATH = Path(_arg_value("--fixture", str(DEFAULT_FIXTURE))).resolve()
BENCHMARK_ROOT = PROJECT_ROOT / "benchmarks"
RESULT_PATH = BENCHMARK_ROOT / RESULT_NAME


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_and_artifact_hashes():
    hashes = {}
    for path in sorted((PROJECT_ROOT / "uv_gpt").glob("*.py")):
        hashes[path.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(path)
    artifact = PROJECT_ROOT / "uv_gpt_v1.2.5.zip"
    if artifact.is_file():
        hashes[artifact.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(artifact)
    return hashes


def harness_hashes():
    hashes = {}
    for path in (
        PROJECT_ROOT / "tests" / "blender" / "match_01_baseline.py",
        PROJECT_ROOT / "tests" / "blender" / "run_match_01.ps1",
    ):
        if path.is_file():
            hashes[path.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(path)
    return hashes


def clean_json(value):
    """Convert Blender/math values and non-finite floats to JSON-safe values."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Vector):
        return [clean_json(component) for component in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def histogram(values):
    counts = Counter(values)
    return {str(key): counts[key] for key in sorted(counts, key=lambda item: (str(type(item)), str(item)))}


def point_key(point):
    return (round(float(point.x), 7), round(float(point.y), 7))


def face_key(island):
    return tuple(sorted({int(loop.face.index) for loop in island}))


def loop_coord_key(face, local_index):
    return (int(face.index), int(local_index))


def setup_bmesh(bm):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()


def uv_layer_names(obj):
    return [layer.name for layer in obj.data.uv_layers]


def active_uv_name(obj):
    active = getattr(obj.data.uv_layers, "active", None)
    return active.name if active is not None else None


def new_mesh_bmesh(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    setup_bmesh(bm)
    return bm


def selected_loop(loop, uv_layer):
    luv = loop[uv_layer]
    return bool(
        getattr(luv, "select", False)
        or getattr(luv, "select_edge", False)
        or loop.face.select
        or loop.vert.select
        or loop.edge.select
    )


def boundary_graph_info(segments):
    adjacency = defaultdict(set)
    for start, end in segments:
        start_key = point_key(start)
        end_key = point_key(end)
        if start_key == end_key:
            continue
        adjacency[start_key].add(end_key)
        adjacency[end_key].add(start_key)

    visited = set()
    components = 0
    for start in adjacency:
        if start in visited:
            continue
        components += 1
        stack = [start]
        visited.add(start)
        while stack:
            current = stack.pop()
            for linked in adjacency[current]:
                if linked not in visited:
                    visited.add(linked)
                    stack.append(linked)

    open_nodes = sum(1 for neighbours in adjacency.values() if len(neighbours) != 2)
    closed = bool(adjacency) and open_nodes == 0
    return {
        "point_count": len(adjacency),
        "component_count": components,
        "open_node_count": open_nodes,
        "closed": closed,
        # For a connected orientable UV boundary graph, one component is the
        # outside boundary and additional components are hole boundaries.
        "hole_count_estimate": max(components - 1, 0) if closed else None,
    }


def polygon_area(points):
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, point in enumerate(points):
        other = points[(index + 1) % len(points)]
        total += point.x * other.y - other.x * point.y
    return abs(total) * 0.5


def island_descriptor(island, uv_layer, stack_tools, include_faces=True):
    faces = sorted({loop.face for loop in island}, key=lambda item: item.index)
    vertices = {loop.vert for loop in island}
    edges = {loop.edge for loop in island}
    points = {point_key(loop[uv_layer].uv) for loop in island}
    segments = stack_tools._island_boundary_segments(island, uv_layer)
    boundary = boundary_graph_info(segments)
    area = sum(polygon_area([loop[uv_layer].uv for loop in face.loops]) for face in faces)
    bounds = (
        min((loop[uv_layer].uv.x for loop in island), default=0.0),
        max((loop[uv_layer].uv.x for loop in island), default=0.0),
        min((loop[uv_layer].uv.y for loop in island), default=0.0),
        max((loop[uv_layer].uv.y for loop in island), default=0.0),
    )
    descriptor = {
        "face_key": list(face_key(island)),
        "face_count": len(faces),
        "loop_count": len(island),
        "vertex_count": len(vertices),
        "edge_count": len(edges),
        "unique_uv_point_count": len(points),
        "face_degree_histogram": histogram(len(face.loops) for face in faces),
        "boundary_segment_count": len(segments),
        "boundary": boundary,
        "area": area,
        "bounds": bounds,
    }
    if include_faces:
        descriptor["faces"] = [int(face.index) for face in faces]
    return descriptor


def topology_summary(bm):
    face_degrees = [len(face.loops) for face in bm.faces if not face.hide]
    edge_incidence = [len([face for face in edge.link_faces if not face.hide]) for edge in bm.edges]
    vertex_valence = [len(vert.link_edges) for vert in bm.verts if not vert.hide]
    return {
        "face_degree_histogram": histogram(face_degrees),
        "edge_face_incidence_histogram": histogram(edge_incidence),
        "vertex_valence_histogram": histogram(vertex_valence),
        "boundary_edge_count": sum(1 for value in edge_incidence if value == 1),
        "manifold_edge_count": sum(1 for value in edge_incidence if value == 2),
        "non_manifold_edge_count": sum(1 for value in edge_incidence if value > 2),
        "hidden_face_count": sum(1 for face in bm.faces if face.hide),
    }


def selection_summary(islands, uv_layer):
    selected_loops = [loop for island in islands for loop in island if selected_loop(loop, uv_layer)]
    selected_faces = {loop.face for loop in selected_loops}
    selected_islands = [island for island in islands if any(selected_loop(loop, uv_layer) for loop in island)]
    return {
        "selected_loop_count": len(selected_loops),
        "selected_face_count": len(selected_faces),
        "selected_island_count": len(selected_islands),
        "selected_island_keys": [list(face_key(island)) for island in selected_islands],
        "selected_island_face_count_histogram": histogram(len(face_key(island)) for island in selected_islands),
    }


def uv_map_inventory(obj, uv_name, island_tools, stack_tools, include_samples=True):
    bm = new_mesh_bmesh(obj)
    try:
        uv_layer = bm.loops.layers.uv.get(uv_name)
        if uv_layer is None:
            return {"name": uv_name, "error": "UV layer missing from BMesh"}
        islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        selected = selection_summary(islands, uv_layer)
        descriptors = [island_descriptor(island, uv_layer, stack_tools) for island in islands]
        boundary_values = [item["boundary_segment_count"] for item in descriptors]
        point_values = [item["boundary"]["point_count"] for item in descriptors]
        component_values = [item["boundary"]["component_count"] for item in descriptors]
        hole_values = [
            item["boundary"]["hole_count_estimate"]
            for item in descriptors
            if item["boundary"]["hole_count_estimate"] is not None
        ]
        result = {
            "name": uv_name,
            "island_count": len(islands),
            "island_face_count_histogram": histogram(item["face_count"] for item in descriptors),
            "island_loop_count_histogram": histogram(item["loop_count"] for item in descriptors),
            "island_boundary_segment_histogram": histogram(boundary_values),
            "island_boundary_point_histogram": histogram(point_values),
            "island_boundary_component_histogram": histogram(component_values),
            "island_hole_count_estimate_histogram": histogram(hole_values),
            "islands_with_open_boundary_graph": sum(
                not item["boundary"]["closed"] for item in descriptors
            ),
            "selection": selected,
        }
        if include_samples:
            ordered = sorted(descriptors, key=lambda item: tuple(item["face_key"]))
            result["island_samples"] = ordered[:20]
        return result
    finally:
        bm.free()


def mesh_inventory(obj, island_tools, stack_tools):
    result = {
        "name": obj.name,
        "data_name": obj.data.name,
        "mode_at_inventory": obj.mode,
        "visible": not bool(getattr(obj, "hide_viewport", False)),
        "vertex_count": len(obj.data.vertices),
        "edge_count": len(obj.data.edges),
        "polygon_count": len(obj.data.polygons),
        "loop_count": sum(len(polygon.loop_indices) for polygon in obj.data.polygons),
        "uv_maps": uv_layer_names(obj),
        "active_uv_map": active_uv_name(obj),
    }
    bm = new_mesh_bmesh(obj)
    try:
        result["topology"] = topology_summary(bm)
    finally:
        bm.free()
    result["uv_map_details"] = [
        uv_map_inventory(obj, uv_name, island_tools, stack_tools)
        for uv_name in uv_layer_names(obj)
    ]
    return result


def object_is_eligible(obj):
    return (
        obj.type == "MESH"
        and bool(obj.data.uv_layers)
        and not bool(getattr(obj, "hide_viewport", False))
        and not bool(getattr(obj, "hide_render", False))
    )


def ordered_mesh_objects():
    return sorted(
        [obj for obj in bpy.data.objects if object_is_eligible(obj)],
        key=lambda obj: obj.name,
    )


def activate_object_in_object_mode(obj):
    current = bpy.context.object
    if current is not None and current.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for candidate in bpy.context.view_layer.objects:
        candidate.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def set_active_uv_map_in_memory(obj, uv_name):
    for index, layer in enumerate(obj.data.uv_layers):
        if layer.name == uv_name:
            obj.data.uv_layers.active_index = index
            return True
    return False


def case_from_object(obj, uv_name, island_tools):
    bm = new_mesh_bmesh(obj)
    uv_layer = bm.loops.layers.uv.get(uv_name)
    if uv_layer is None:
        bm.free()
        return None
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected = island_tools.get_selected_uv_islands(bm, uv_layer)
    return bm, uv_layer, islands, selected


def score_pair(ref_island, target_island, uv_layer, stack_tools, match_scale, allow_flipping):
    transform = stack_tools._best_align_transform(
        ref_island,
        target_island,
        uv_layer,
        match_scale,
        allow_flipping,
    )
    if transform is None:
        return None
    return {
        "ref_key": list(face_key(ref_island)),
        "target_key": list(face_key(target_island)),
        "score": float(transform["score"]),
        "boundary_segment_count": len(
            stack_tools._island_boundary_segments(ref_island, uv_layer)
        ),
        "target_boundary_segment_count": len(
            stack_tools._island_boundary_segments(target_island, uv_layer)
        ),
    }


def boundary_signature(island, uv_layer, stack_tools):
    segments = stack_tools._island_boundary_segments(island, uv_layer)
    points = stack_tools._unique_segment_points(segments)
    return len(segments), len(points)


def pair_candidates(selected_islands, all_islands, uv_layer, stack_tools, match_scale, allow_flipping):
    selected_keys = {face_key(island) for island in selected_islands}
    pairs = []
    for ref_island in sorted(selected_islands, key=face_key):
        for target_island in sorted(all_islands, key=face_key):
            if face_key(target_island) in selected_keys:
                continue
            pair = score_pair(
                ref_island,
                target_island,
                uv_layer,
                stack_tools,
                match_scale,
                allow_flipping,
            )
            if pair is not None:
                pairs.append(pair)
    return sorted(pairs, key=lambda item: (item["score"], item["ref_key"], item["target_key"]))


def all_pair_candidates(all_islands, uv_layer, stack_tools, match_scale, allow_flipping):
    # Deterministic scenario selection must not turn diagnosis into an
    # unbounded O(islands^2) scan on a production fixture.  The current matcher
    # itself is benchmarked later with one selected reference.  Here use the
    # same cheap boundary-count/point-count gate and score a bounded, sorted
    # sample; the cap is recorded in the result's method description.
    pairs = []
    ordered = sorted(all_islands, key=face_key)
    signature_groups = defaultdict(list)
    for island in ordered:
        signature_groups[boundary_signature(island, uv_layer, stack_tools)].append(island)

    evaluated = 0
    max_evaluated = 256
    for ref_index, ref_island in enumerate(ordered):
        signature = boundary_signature(ref_island, uv_layer, stack_tools)
        for target_island in signature_groups[signature]:
            if target_island is ref_island:
                continue
            if evaluated >= max_evaluated:
                return sorted(
                    pairs,
                    key=lambda item: (item["score"], item["ref_key"], item["target_key"]),
                )
            evaluated += 1
            pair = score_pair(
                ref_island,
                target_island,
                uv_layer,
                stack_tools,
                match_scale,
                allow_flipping,
            )
            if pair is not None:
                pairs.append(pair)
        if pairs and any(item["score"] <= 0.01 for item in pairs):
            break
    return sorted(pairs, key=lambda item: (item["score"], item["ref_key"], item["target_key"]))


def saved_selection_analysis(objects, island_tools, stack_tools, settings):
    analyses = []
    for obj in objects:
        for uv_name in sorted(uv_layer_names(obj)):
            case = case_from_object(obj, uv_name, island_tools)
            if case is None:
                continue
            bm, uv_layer, islands, selected = case
            try:
                pairs = pair_candidates(
                    selected,
                    islands,
                    uv_layer,
                    stack_tools,
                    settings["stack_match_scale"],
                    settings["stack_allow_flipping"],
                )
                analyses.append(
                    {
                        "object": obj.name,
                        "uv_map": uv_name,
                        "island_count": len(islands),
                        "selection": selection_summary(islands, uv_layer),
                        "pair_candidates": pairs[:20],
                        "suitable": bool(
                            selected
                            and any(
                                pair["score"] <= settings["stack_similarity_tolerance"]
                                for pair in pairs
                            )
                        ),
                    }
                )
            finally:
                bm.free()
    suitable = [item for item in analyses if item["suitable"]]
    suitable.sort(key=lambda item: (item["object"], item["uv_map"]))
    return {
        "all_objects": analyses,
        "suitable_count": len(suitable),
        "selected_suitable": suitable[0] if suitable else None,
    }


def deterministic_pair_analysis(objects, island_tools, stack_tools, settings):
    analyses = []
    for obj in objects:
        for uv_name in sorted(uv_layer_names(obj)):
            case = case_from_object(obj, uv_name, island_tools)
            if case is None:
                continue
            bm, uv_layer, islands, _selected = case
            try:
                pairs = all_pair_candidates(
                    islands,
                    uv_layer,
                    stack_tools,
                    settings["stack_match_scale"],
                    settings["stack_allow_flipping"],
                )
                analyses.append(
                    {
                        "object": obj.name,
                        "uv_map": uv_name,
                        "island_count": len(islands),
                        "pair_candidates": pairs[:20],
                        "first_island_key": list(face_key(sorted(islands, key=face_key)[0]))
                        if islands
                        else None,
                    }
                )
            finally:
                bm.free()
    analyses.sort(key=lambda item: (item["object"], item["uv_map"]))
    suitable = [
        item
        for item in analyses
        if any(
            pair["score"] <= settings["stack_similarity_tolerance"]
            for pair in item["pair_candidates"]
        )
    ]
    chosen = suitable[0] if suitable else None
    if chosen is None:
        with_two_or_more = [item for item in analyses if item["island_count"] >= 2]
        chosen = with_two_or_more[0] if with_two_or_more else (analyses[0] if analyses else None)
    return {"all_objects": analyses, "chosen": chosen}


def find_island_by_key(islands, key):
    wanted = tuple(key)
    return next((island for island in islands if face_key(island) == wanted), None)


def configure_selection_scenario(obj, uv_name, selection_source, selected_keys, uv_utils, island_tools):
    activate_object_in_object_mode(obj)
    if not set_active_uv_map_in_memory(obj, uv_name):
        raise RuntimeError(f"UV map not found: {obj.name}/{uv_name}")
    bpy.ops.object.mode_set(mode="EDIT")
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected_islands = [find_island_by_key(islands, key) for key in selected_keys]
    selected_islands = [island for island in selected_islands if island is not None]
    if not selected_islands:
        raise RuntimeError("Selection scenario resolved to no islands")
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(bm, uv_layer, selected_islands)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected_islands = island_tools.get_selected_uv_islands(bm, uv_layer)
    return {
        "object": obj,
        "uv_name": uv_name,
        "bm": bm,
        "uv_layer": uv_layer,
        "islands": islands,
        "selected_islands": selected_islands,
        "selection_source": selection_source,
    }


def snapshot_uv_coordinates(bm, uv_layer):
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            uv = loop[uv_layer].uv
            result[loop_coord_key(face, local_index)] = (float(uv.x), float(uv.y))
    return result


def restore_uv_coordinates(bm, uv_layer, snapshot):
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            value = snapshot.get(loop_coord_key(face, local_index))
            if value is not None:
                loop[uv_layer].uv = Vector(value)


def coordinate_delta(snapshot_before, snapshot_after, keys=None):
    selected_keys = set(keys) if keys is not None else None
    deltas = []
    for key, before in snapshot_before.items():
        if selected_keys is not None and key not in selected_keys:
            continue
        after = snapshot_after.get(key, before)
        deltas.append(max(abs(before[0] - after[0]), abs(before[1] - after[1])))
    return max(deltas, default=0.0)


def coordinate_keys_for_faces(bm, face_indices):
    wanted = set(face_indices)
    return [
        loop_coord_key(face, local_index)
        for face in bm.faces
        if face.index in wanted
        for local_index, _loop in enumerate(face.loops)
    ]


def restore_baseline(case, uv_utils, coordinate_snapshot, selection_snapshot):
    bm = bmesh.from_edit_mesh(case["object"].data)
    setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(case["uv_name"])
    if uv_layer is None:
        raise RuntimeError(f"UV layer missing during restore: {case['uv_name']}")
    restore_uv_coordinates(bm, uv_layer, coordinate_snapshot)
    uv_utils.restore_uv_selection(bm, uv_layer, selection_snapshot)
    bmesh.update_edit_mesh(case["object"].data, loop_triangles=False, destructive=False)
    return bm, uv_layer


def case_size(bm, uv_layer, island_tools, stack_tools):
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected = island_tools.get_selected_uv_islands(bm, uv_layer)
    descriptors = [island_descriptor(island, uv_layer, stack_tools, include_faces=False) for island in islands]
    selected_keys = {face_key(island) for island in selected}
    return {
        "mesh_faces": len(bm.faces),
        "mesh_edges": len(bm.edges),
        "mesh_verts": len(bm.verts),
        "mesh_loops": sum(len(face.loops) for face in bm.faces),
        "uv_islands": len(islands),
        "selected_islands": len(selected),
        "unselected_candidates": sum(1 for island in islands if face_key(island) not in selected_keys),
        "island_face_count_histogram": histogram(item["face_count"] for item in descriptors),
        "island_boundary_segment_histogram": histogram(
            item["boundary_segment_count"] for item in descriptors
        ),
        "island_hole_count_estimate_histogram": histogram(
            item["boundary"]["hole_count_estimate"]
            for item in descriptors
            if item["boundary"]["hole_count_estimate"] is not None
        ),
    }


def empty_instrumentation():
    return {
        "extraction_seconds": 0.0,
        "match_seconds": 0.0,
        "apply_seconds": 0.0,
        "extraction_calls": 0,
        "match_calls": 0,
        "apply_calls": 0,
        "match_scores_before_apply": [],
        "apply_records": [],
    }


def instrument_stack_calls(island_tools, stack_tools, metrics):
    original_get_islands = island_tools.get_uv_islands
    original_best = stack_tools._best_align_transform
    original_apply = stack_tools._apply_align_transform

    def timed_get_islands(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_get_islands(*args, **kwargs)
        finally:
            metrics["extraction_seconds"] += time.perf_counter() - started
            metrics["extraction_calls"] += 1

    def timed_best(ref_island, target_island, uv_layer, match_scale, allow_flipping):
        started = time.perf_counter()
        result = original_best(ref_island, target_island, uv_layer, match_scale, allow_flipping)
        metrics["match_seconds"] += time.perf_counter() - started
        metrics["match_calls"] += 1
        if result is not None:
            metrics["match_scores_before_apply"].append(
                {
                    "ref_key": list(face_key(ref_island)),
                    "target_key": list(face_key(target_island)),
                    "score": float(result["score"]),
                }
            )
        return result

    def timed_apply(target_island, uv_layer, transform):
        started = time.perf_counter()
        try:
            return original_apply(target_island, uv_layer, transform)
        finally:
            metrics["apply_seconds"] += time.perf_counter() - started
            metrics["apply_calls"] += 1
            metrics["apply_records"].append(
                {
                    "target_key": list(face_key(target_island)),
                    "score": float(transform["score"]),
                }
            )

    island_tools.get_uv_islands = timed_get_islands
    stack_tools._best_align_transform = timed_best
    stack_tools._apply_align_transform = timed_apply

    def restore():
        island_tools.get_uv_islands = original_get_islands
        stack_tools._best_align_transform = original_best
        stack_tools._apply_align_transform = original_apply

    return restore


def post_alignment_quality(case, island_tools, stack_tools, tolerance, applied_keys):
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, case["object"])
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected = island_tools.get_selected_uv_islands(bm, uv_layer)
    selected_keys = {face_key(island) for island in selected}
    candidate_scores = []
    for island in islands:
        key = face_key(island)
        if key in selected_keys:
            continue
        scores = []
        for reference in selected:
            transform = stack_tools._best_align_transform(
                reference,
                island,
                uv_layer,
                True,
                False,
            )
            if transform is not None:
                scores.append(float(transform["score"]))
        if scores:
            candidate_scores.append(
                {
                    "target_key": list(key),
                    "best_post_score": min(scores),
                    "within_tolerance": min(scores) <= tolerance,
                    "was_applied": key in applied_keys,
                }
            )
    applied_scores = [
        item["best_post_score"] for item in candidate_scores if item["was_applied"]
    ]
    return {
        "candidate_scores": candidate_scores,
        "finite_candidate_score_count": len(candidate_scores),
        "applied_candidate_scores": [
            item for item in candidate_scores if item["was_applied"]
        ],
        "aligned_applied_candidates_with_post_score": sum(
            item["within_tolerance"]
            for item in candidate_scores
            if item["was_applied"]
        ),
        "max_post_score_all_finite_candidates": max(
            (item["best_post_score"] for item in candidate_scores),
            default=None,
        ),
        "max_applied_post_score": max(applied_scores, default=None),
    }


def run_operator_once(case, uv_utils, island_tools, stack_tools, coordinate_snapshot, selection_snapshot, baseline_islands, measured, tolerance):
    bm, uv_layer = restore_baseline(case, uv_utils, coordinate_snapshot, selection_snapshot)
    metrics = empty_instrumentation()
    before_case = case_size(bm, uv_layer, island_tools, stack_tools)
    restore_instrumentation = instrument_stack_calls(island_tools, stack_tools, metrics) if measured else None
    started = time.perf_counter()
    operator_result = None
    operator_error = None
    try:
        operator_result = bpy.ops.uv_gpt.align_to_selected()
    except Exception:
        operator_error = traceback.format_exc()
    elapsed = time.perf_counter() - started
    if restore_instrumentation is not None:
        restore_instrumentation()

    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, case["object"])
    after_coords = snapshot_uv_coordinates(bm, uv_layer)
    after_selection = uv_utils.store_uv_selection(bm, uv_layer)
    target_face_indices = sorted({index for island in case["selected_islands"] for index in face_key(island)})
    target_coord_keys = coordinate_keys_for_faces(bm, target_face_indices)
    selected_uv_max_delta = coordinate_delta(coordinate_snapshot, after_coords, target_coord_keys)
    selection_unchanged = after_selection == selection_snapshot

    changed_candidates = []
    for island in baseline_islands:
        key = face_key(island)
        if key in {face_key(item) for item in case["selected_islands"]}:
            continue
        candidate_keys = coordinate_keys_for_faces(bm, key)
        delta = coordinate_delta(coordinate_snapshot, after_coords, candidate_keys)
        if delta > EPSILON:
            changed_candidates.append({"target_key": list(key), "max_uv_delta": delta})

    applied_keys = {
        tuple(record["target_key"])
        for record in metrics["apply_records"]
    }
    post_quality = post_alignment_quality(
        case,
        island_tools,
        stack_tools,
        tolerance,
        applied_keys,
    )
    result = {
        "measured": measured,
        "elapsed_seconds": elapsed,
        "elapsed_ms": elapsed * 1000.0,
        "operator_result": sorted(operator_result) if operator_result is not None else None,
        "operator_error": operator_error,
        "case_size": before_case,
        "instrumentation": {
            "extraction_ms": metrics["extraction_seconds"] * 1000.0,
            "match_ms": metrics["match_seconds"] * 1000.0,
            "apply_ms": metrics["apply_seconds"] * 1000.0,
            "extraction_calls": metrics["extraction_calls"],
            "match_calls": metrics["match_calls"],
            "apply_calls": metrics["apply_calls"],
            "match_scores_before_apply": metrics["match_scores_before_apply"][:100],
            "apply_records": metrics["apply_records"][:100],
        },
        "correctness": {
            "selected_uv_unchanged": selected_uv_max_delta <= EPSILON,
            "selected_uv_max_delta": selected_uv_max_delta,
            "selection_snapshot_unchanged": selection_unchanged,
            "candidate_changed_count": len(changed_candidates),
            "changed_candidates": changed_candidates[:100],
            "post_alignment": post_quality,
        },
    }
    restore_baseline(case, uv_utils, coordinate_snapshot, selection_snapshot)
    return result


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def benchmark_summary(runs):
    measured = [run for run in runs if run.get("measured")]
    elapsed = [run["elapsed_ms"] for run in measured]
    extraction = [run["instrumentation"]["extraction_ms"] for run in measured]
    matching = [run["instrumentation"]["match_ms"] for run in measured]
    applying = [run["instrumentation"]["apply_ms"] for run in measured]

    def stats(values):
        return {
            "count": len(values),
            "min_ms": min(values) if values else None,
            "median_ms": sorted(values)[len(values) // 2] if values else None,
            "p95_ms": percentile(values, 95.0),
        }

    return {
        "measured_run_count": len(measured),
        "total": stats(elapsed),
        "extraction": stats(extraction),
        "match": stats(matching),
        "apply": stats(applying),
        "case_sizes": [run.get("case_size") for run in measured],
    }


def runtime_inventory():
    numpy_info = {"available": False, "version": None, "error": None}
    try:
        import numpy

        numpy_info.update({"available": True, "version": numpy.__version__})
    except Exception as exc:
        numpy_info["error"] = repr(exc)
    return {
        "blender_app_version": list(bpy.app.version),
        "blender_app_version_string": bpy.app.version_string,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy": numpy_info,
        "logical_cpu_count": os.cpu_count(),
        "parallelism_note": (
            "The current add-on path is pure Python and synchronous. Python-level threads "
            "remain GIL-bound for this CPU work; process scheduling or a future NumPy path "
            "would need explicit Blender data isolation and main-thread apply/undo boundaries. "
            "No threading or multiprocessing was introduced or measured in MATCH-01."
        ),
    }


def addon_settings(scene):
    settings = scene.uv_gpt_settings
    settings.duplicate_before_operations = False
    try:
        settings.active_uv_map = "NONE"
    except Exception:
        pass
    settings.stack_match_scale = True
    settings.stack_allow_flipping = False
    settings.stack_similarity_tolerance = 0.01
    return {
        "duplicate_before_operations": bool(settings.duplicate_before_operations),
        "active_uv_map": str(settings.active_uv_map),
        "stack_match_scale": bool(settings.stack_match_scale),
        "stack_allow_flipping": bool(settings.stack_allow_flipping),
        "stack_similarity_tolerance": float(settings.stack_similarity_tolerance),
    }


def choose_selection_plan(objects, saved_analysis, deterministic_analysis, settings):
    selected = saved_analysis.get("selected_suitable")
    if selected is not None:
        return {
            "source": "saved_selection",
            "object": selected["object"],
            "uv_map": selected["uv_map"],
            "selected_keys": selected["selection"]["selected_island_keys"],
            "criteria": "Use saved selected islands when at least one unselected island has a finite border-shape score within the configured tolerance.",
        }

    chosen = deterministic_analysis.get("chosen")
    if chosen is None:
        return {
            "source": "none",
            "object": None,
            "uv_map": None,
            "selected_keys": [],
            "criteria": "No eligible mesh object with a UV map was available.",
        }

    pair = next(
        (
            item
            for item in chosen["pair_candidates"]
            if item["score"] <= settings["stack_similarity_tolerance"]
        ),
        None,
    )
    if pair is None and chosen["pair_candidates"]:
        pair = chosen["pair_candidates"][0]
    if pair is not None:
        selected_keys = [pair["ref_key"]]
        criteria = (
            "Clear saved selection and select exactly the deterministically chosen reference: "
            "sort eligible object/UV map names and face-index keys, gate by boundary "
            "segment/point signature, then choose the lowest score among the bounded "
            "reproducible candidate sample; leave its paired island unselected."
        )
    else:
        ordered = sorted(
            item["first_island_key"]
            for item in [chosen]
            if item.get("first_island_key") is not None
        )
        selected_keys = [ordered[0]] if ordered else []
        criteria = (
            "No finite border-shape pair exists; select the first face-index island in the "
            "deterministically chosen eligible object and document the zero-match limitation."
        )
    return {
        "source": "deterministic_in_memory",
        "object": chosen["object"],
        "uv_map": chosen["uv_map"],
        "selected_keys": selected_keys,
        "criteria": criteria,
        "chosen_pair": pair,
    }


def match_02_design():
    return {
        "status": "proposed_only_not_implemented",
        "pure_python_fallback": [
            "Keep extraction and application on Blender's main thread; represent each island by canonical boundary loops, normalized perimeter samples, area, bounds, face count, and UV-point count.",
            "Replace the current unordered nearest-point score with cyclic boundary-loop resampling, orientation/reversal handling, and a bounded Procrustes transform; reject degenerate/open graphs explicitly.",
            "Use deterministic candidate ordering and a coarse signature gate before expensive scoring; preserve selected-target immutability and per-candidate single apply.",
        ],
        "numpy_path": [
            "Optionally batch resampled boundary arrays and candidate transforms in NumPy after extraction; keep a dependency-free pure-Python path for Blender builds without bundled NumPy.",
            "Measure conversion and array allocation separately; only use NumPy when the case-size crossover beats Python, and convert results back to scalar transforms before BMesh apply.",
        ],
        "topology_and_hole_matching": [
            "Build ordered boundary loops from the UV boundary graph, record loop count/hole count, perimeter, signed area, and open/non-manifold status.",
            "Gate matches on outer/hole loop counts, face/edge/vertex incidence signatures, and optional hole-aware correspondence before geometric score.",
        ],
        "cache": [
            "Cache immutable extraction descriptors by mesh data identity/version, active UV layer, and a UV-coordinate/selection-independent revision token; invalidate after BMesh writes, undo, UV-map changes, or topology edits.",
            "Do not cache mutable BMLoop objects or transforms across edits; cache serializable descriptors and keep cache project-local/in-memory unless a later packet explicitly defines persistence.",
        ],
        "cpu_scheduling": [
            "Use coarse candidate batches only after proving a measurable CPU bottleneck; a thread pool will not speed Python loops under the GIL.",
            "If process workers are justified, send compact numeric descriptors rather than Blender data, bound worker count to logical CPUs, cancel stale generations, and marshal only final transforms back to the Blender main thread for one undo-safe apply.",
        ],
    }


def write_result(result):
    BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
    with RESULT_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(clean_json(result), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def main():
    result = {
        "packet": PACKET_ID,
        "status": "started",
        "script": str(SCRIPT_PATH),
        "project_root": str(PROJECT_ROOT),
        "fixture": str(FIXTURE_PATH),
        "commands": {
            "version": "<project>/.test_runtime/blender-5.0.0/blender-5.0.0-windows-x64/blender.exe --version",
            "background": "blender.exe --factory-startup --disable-autoexec --background C:\\Users\\linhp\\Downloads\\cc.blend --python tests/blender/match_01_baseline.py",
        },
        "source_artifact_hashes_before": {},
        "source_artifact_hashes_after": {},
        "harness_hashes_before": {},
        "harness_hashes_after": {},
        "diagnosis": [],
        "match_02_design": match_02_design(),
    }
    addon = None
    registered = False
    fixture_before = None
    try:
        fixture_before = sha256_file(FIXTURE_PATH)
        result["fixture_sha256_before"] = fixture_before
        result["source_artifact_hashes_before"] = source_and_artifact_hashes()
        result["harness_hashes_before"] = harness_hashes()
        result["runtime"] = runtime_inventory()
        result["fixture_opened_path"] = str(Path(bpy.data.filepath).resolve())
        result["fixture_opened_path_exact"] = str(Path(bpy.data.filepath).resolve()) == str(FIXTURE_PATH)
        result["load_context"] = {
            "context_mode": bpy.context.mode,
            "active_object": (
                bpy.context.view_layer.objects.active.name
                if bpy.context.view_layer.objects.active is not None
                else None
            ),
            "object_modes": {obj.name: obj.mode for obj in bpy.data.objects if obj.type == "MESH"},
        }

        addon_parent = str(PROJECT_ROOT)
        if addon_parent not in sys.path:
            sys.path.insert(0, addon_parent)
        import uv_gpt

        addon = uv_gpt
        addon.register()
        registered = True
        result["addon"] = {
            "package": "uv_gpt",
            "registered": True,
            "operator_idname": "uv_gpt.align_to_selected",
            "operator_registered": hasattr(bpy.ops.uv_gpt, "align_to_selected"),
        }

        settings = addon_settings(bpy.context.scene)
        result["settings"] = settings

        # Inventory is read from object-mode BMeshes so it covers all mesh
        # objects and all UV maps before the controlled test selection.
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        mesh_objects = sorted(
            [obj for obj in bpy.data.objects if obj.type == "MESH"],
            key=lambda obj: obj.name,
        )
        result["fixture_inventory"] = {
            "mesh_object_count": len(mesh_objects),
            "mesh_objects": [mesh_inventory(obj, uv_gpt.island_tools, uv_gpt.stack_tools) for obj in mesh_objects],
        }

        eligible_objects = ordered_mesh_objects()
        saved_analysis = saved_selection_analysis(
            eligible_objects,
            uv_gpt.island_tools,
            uv_gpt.stack_tools,
            settings,
        )
        deterministic_analysis = deterministic_pair_analysis(
            eligible_objects,
            uv_gpt.island_tools,
            uv_gpt.stack_tools,
            settings,
        )
        plan = choose_selection_plan(
            eligible_objects,
            saved_analysis,
            deterministic_analysis,
            settings,
        )
        result["selection_analysis"] = {
            "saved_selection": saved_analysis,
            "deterministic_options": deterministic_analysis,
            "chosen_plan": plan,
        }
        if plan["source"] == "none":
            raise RuntimeError("No eligible mesh object with UV data for operator context")

        chosen_object = bpy.data.objects.get(plan["object"])
        if chosen_object is None:
            raise RuntimeError(f"Chosen object disappeared: {plan['object']}")
        case = configure_selection_scenario(
            chosen_object,
            plan["uv_map"],
            plan["source"],
            plan["selected_keys"],
            uv_gpt.uv_utils,
            uv_gpt.island_tools,
        )
        bm = case["bm"]
        uv_layer = case["uv_layer"]
        baseline_islands = list(case["islands"])
        baseline_selected = list(case["selected_islands"])
        coordinate_snapshot = snapshot_uv_coordinates(bm, uv_layer)
        selection_snapshot = uv_gpt.uv_utils.store_uv_selection(bm, uv_layer)
        result["execution_case"] = {
            "object": chosen_object.name,
            "uv_map": plan["uv_map"],
            "selection_source": plan["source"],
            "selection_criteria": plan["criteria"],
            "selected_target_keys": [list(face_key(island)) for island in baseline_selected],
            "all_island_count": len(baseline_islands),
            "case_size": case_size(bm, uv_layer, uv_gpt.island_tools, uv_gpt.stack_tools),
            "selected_target_descriptors": [
                island_descriptor(island, uv_layer, uv_gpt.stack_tools) for island in baseline_selected
            ],
            "unselected_candidate_descriptors": [
                island_descriptor(island, uv_layer, uv_gpt.stack_tools)
                for island in baseline_islands
                if face_key(island) not in {face_key(item) for item in baseline_selected}
            ][:100],
        }

        # One warmup is separate from the five measured runs.  Every run is
        # restored to the same UV/selection snapshot before and after execution.
        warmup = run_operator_once(
            case,
            uv_gpt.uv_utils,
            uv_gpt.island_tools,
            uv_gpt.stack_tools,
            coordinate_snapshot,
            selection_snapshot,
            baseline_islands,
            measured=False,
            tolerance=settings["stack_similarity_tolerance"],
        )
        measured_runs = [
            run_operator_once(
                case,
                uv_gpt.uv_utils,
                uv_gpt.island_tools,
                uv_gpt.stack_tools,
                coordinate_snapshot,
                selection_snapshot,
                baseline_islands,
                measured=True,
                tolerance=settings["stack_similarity_tolerance"],
            )
            for _index in range(5)
        ]
        result["benchmark"] = {
            "warmup": warmup,
            "measured_runs": measured_runs,
            "summary": benchmark_summary(measured_runs),
            "timing_method": "time.perf_counter; extraction wraps island_tools.get_uv_islands, match wraps stack_tools._best_align_transform, apply wraps stack_tools._apply_align_transform, total wraps bpy.ops.uv_gpt.align_to_selected.",
        }
        result["correctness_summary"] = {
            "all_measured_runs_selected_uv_unchanged": all(
                run["correctness"]["selected_uv_unchanged"] for run in measured_runs
            ),
            "all_measured_runs_selection_unchanged": all(
                run["correctness"]["selection_snapshot_unchanged"] for run in measured_runs
            ),
            "candidate_transformed_counts": [
                run["instrumentation"]["apply_calls"] for run in measured_runs
            ],
            "candidate_changed_counts": [
                run["correctness"]["candidate_changed_count"] for run in measured_runs
            ],
            "post_alignment_max_scores": [
                run["correctness"]["post_alignment"]["max_applied_post_score"]
                for run in measured_runs
            ],
            "post_alignment_applied_within_tolerance": [
                run["correctness"]["post_alignment"][
                    "aligned_applied_candidates_with_post_score"
                ]
                for run in measured_runs
            ],
        }
        result["diagnosis"].extend(
            [
                "Operator executed through bpy.ops.uv_gpt.align_to_selected in Blender background context.",
                "Selected-target UV coordinates and selection flags were snapshotted and restored around each run.",
            ]
        )
        if plan["source"] == "deterministic_in_memory":
            result["diagnosis"].append(
                "Saved fixture selection was not suitable within tolerance; the measured scenario was created deterministically in memory and was not saved."
            )
        if not any(run["instrumentation"]["apply_calls"] for run in measured_runs):
            result["diagnosis"].append(
                "No candidate was applied in the measured case; interpret timing as a valid zero-match baseline, not as alignment quality evidence."
            )
        if any(
            not run["correctness"]["selected_uv_unchanged"]
            or not run["correctness"]["selection_snapshot_unchanged"]
            for run in measured_runs
        ):
            result["diagnosis"].append(
                "FAILURE: a measured run changed selected-target UVs or selection state."
            )
        if any(run["operator_error"] for run in measured_runs):
            result["diagnosis"].append("FAILURE: at least one measured operator call raised an exception.")
        if any(
            run["correctness"]["post_alignment"]["max_applied_post_score"] is not None
            and run["correctness"]["post_alignment"]["max_applied_post_score"] > settings["stack_similarity_tolerance"]
            for run in measured_runs
        ):
            result["diagnosis"].append(
                "FAILURE: at least one applied candidate exceeded the configured post-alignment score tolerance."
            )
        result["status"] = "completed"
    except Exception:
        result["status"] = "failed"
        result["fatal_error"] = traceback.format_exc()
    finally:
        if registered and addon is not None:
            try:
                addon.unregister()
            except Exception:
                result["unregister_error"] = traceback.format_exc()
        try:
            result["fixture_sha256_after"] = sha256_file(FIXTURE_PATH)
            result["fixture_sha256_unchanged"] = (
                fixture_before is not None and result["fixture_sha256_after"] == fixture_before
            )
        except Exception:
            result["fixture_sha256_after_error"] = traceback.format_exc()
        try:
            result["source_artifact_hashes_after"] = source_and_artifact_hashes()
            result["source_artifact_hashes_unchanged"] = (
                result["source_artifact_hashes_before"] == result["source_artifact_hashes_after"]
            )
        except Exception:
            result["source_artifact_hashes_after_error"] = traceback.format_exc()
        try:
            result["harness_hashes_after"] = harness_hashes()
            result["harness_hashes_unchanged"] = (
                result["harness_hashes_before"] == result["harness_hashes_after"]
            )
        except Exception:
            result["harness_hashes_after_error"] = traceback.format_exc()
        result["result_path"] = str(RESULT_PATH)
        try:
            write_result(result)
        except Exception:
            print(traceback.format_exc())
            raise
        print(
            "MATCH-01 status={} fixture_sha256_unchanged={} source_artifact_hashes_unchanged={} result={}".format(
                result.get("status"),
                result.get("fixture_sha256_unchanged"),
                result.get("source_artifact_hashes_unchanged"),
                RESULT_PATH,
            )
        )


main()
