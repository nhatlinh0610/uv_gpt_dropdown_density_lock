"""Pure external-process exact Pro stack core.

No Blender imports.  The owner thread captures primitive face/loop records once;
this module reconstructs UV islands and exact topology graphs, chooses the
largest UV-area master in each compatible topology bucket, proves a complete
loop-to-loop correspondence and returns master UV copies for every target.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import fast_v2_core  # noqa: E402
import topology_correspondence as tc  # noqa: E402

SCHEMA = "pro-exact-v2-snapshot-v1"
RESULT_SCHEMA = "pro-exact-v2-result-v1"
_FORCE_TOLERANCE = 1.0e300


@dataclass(frozen=True)
class ExactIsland:
    face_key: tuple[int, ...]
    loop_uvs: tuple[tuple[tuple[int, int], tuple[float, float]], ...]
    uv_area: float
    graph: tc.IslandGraph


def _progress(cb: Callable[[dict[str, Any]], None] | None, **values: Any) -> None:
    if cb is not None:
        cb(values)


def _fast_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        raise ValueError("invalid Pro Exact V2 snapshot schema")
    return {
        "schema": fast_v2_core.SCHEMA,
        "faces": snapshot.get("faces", ()),
        "options": {},
    }


def _raw_face_map(snapshot: dict[str, Any]):
    faces = fast_v2_core._parse_faces(_fast_snapshot(snapshot))
    return faces, {face.index: face for face in faces}


def _uv_token(uv):
    return (round(float(uv[0]), 10), round(float(uv[1]), 10))


def _boundary_components(loop_data, boundary_keys):
    if not boundary_keys:
        return ()
    endpoints = {}
    endpoint_to_loops = {}
    for key in boundary_keys:
        face_index, local_index = key
        face = loop_data["faces"][face_index]
        loop = face.loops[local_index]
        nxt = face.loops[(local_index + 1) % len(face.loops)]
        # Vertices already represent UV-connected corner fans. Tiny coordinate
        # differences accepted by island discovery must not open a closed ring.
        start = int(loop[0])
        end = int(nxt[0])
        endpoints[key] = (start, end)
        endpoint_to_loops.setdefault(start, []).append(key)
        endpoint_to_loops.setdefault(end, []).append(key)

    adjacency = {key: set() for key in boundary_keys}
    for keys in endpoint_to_loops.values():
        if len(keys) != 2:
            raise ValueError("boundary_component_branch_or_open")
        left, right = keys
        if left == right:
            raise ValueError("boundary_component_degenerate_segment")
        adjacency[left].add(right)
        adjacency[right].add(left)
    if any(len(values) != 2 for values in adjacency.values()):
        raise ValueError("boundary_component_not_closed")

    unseen = set(boundary_keys)
    components = []
    while unseen:
        start = min(unseen)
        stack = [start]
        unseen.remove(start)
        component = []
        while stack:
            key = stack.pop()
            component.append(key)
            for neighbor in sorted(adjacency[key]):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        components.append(tuple(sorted(component)))

    ordered_components = []
    for component in components:
        start = min(component)
        ordered = [start]
        previous = None
        current = start
        while len(ordered) < len(component):
            candidates = sorted(
                n for n in adjacency[current] if n != previous and n not in ordered
            )
            if not candidates:
                raise ValueError("boundary_component_trace_failed")
            previous, current = current, candidates[0]
            ordered.append(current)
        if start not in adjacency[current]:
            raise ValueError("boundary_component_trace_not_closed")
        area_twice = 0.0
        for key in component:
            face_index, local_index = key
            face = loop_data["faces"][face_index]
            loop = face.loops[local_index]
            nxt = face.loops[(local_index + 1) % len(face.loops)]
            area_twice += float(loop[2]) * float(nxt[3]) - float(nxt[2]) * float(loop[3])
        area = abs(area_twice) * 0.5
        if not math.isfinite(area) or area <= 1.0e-14:
            raise ValueError("boundary_component_degenerate_area")
        ordered_components.append((area, tuple(ordered)))

    ordered_components.sort(key=lambda item: (-item[0], item[1]))
    outer_key = ("boundary", 0)
    return tuple(
        tc.BoundaryComponentRecord(
            key=("boundary", index),
            loop_keys=loop_keys,
            role="outer" if index == 0 else "hole",
            parent_key=None if index == 0 else outer_key,
            signature=("closed", len(loop_keys)),
        )
        for index, (_area, loop_keys) in enumerate(ordered_components)
    )


def _uv_topology_faces(island, faces_by_index):
    """Split mesh edges/vertices at UV cuts without changing source loop keys.

    A slit can have both incident faces inside the same connected island.
    Mesh incidence alone incorrectly closes that slit and leaves open boundary
    chains. Join corner fans only across UV-continuous mesh edges instead.
    """
    faces = {key: faces_by_index[key] for key in island.face_key}
    corners = {(key, j): loop for key, face in faces.items()
               for j, loop in enumerate(face.loops)}
    parent = {key: key for key in corners}
    edge_parent = dict(parent)

    def find(table, key):
        while table[key] != key:
            table[key] = table[table[key]]
            key = table[key]
        return key

    def join(table, left, right):
        left, right = find(table, left), find(table, right)
        if left != right:
            table[max(left, right)] = min(left, right)

    def next_key(key):
        return (key[0], (key[1] + 1) % len(faces[key[0]].loops))

    occurrences = defaultdict(list)
    for key, loop in corners.items():
        occurrences[loop[1]].append(key)
    for keys in occurrences.values():
        for index, left in enumerate(keys):
            for right in keys[index + 1:]:
                if left[0] == right[0]:
                    continue
                left_ends = (left, next_key(left))
                right_ends = (right, next_key(right))
                pairs = [(a, b) for a in left_ends for b in right_ends
                         if corners[a][0] == corners[b][0]]
                if len(pairs) != 2 or not all(
                    fast_v2_core._uv_close(corners[a][2:], corners[b][2:])
                    for a, b in pairs
                ):
                    continue
                join(edge_parent, left, right)
                for a, b in pairs:
                    join(parent, a, b)
    vertex_ids = {key: index for index, key in enumerate(sorted(
        {find(parent, key) for key in corners}))}
    edge_ids = {key: index for index, key in enumerate(sorted(
        {find(edge_parent, key) for key in corners}))}
    result = {}
    for key, face in faces.items():
        loops = tuple((vertex_ids[find(parent, (key, j))],
                       edge_ids[find(edge_parent, (key, j))], loop[2], loop[3])
                      for j, loop in enumerate(face.loops))
        result[key] = fast_v2_core.RawFace(key, face.selected, face.hidden, loops)
    return result


def _graph_for_island(island, faces_by_index, global_edge_faces):
    face_keys = tuple(island.face_key)
    faces = _uv_topology_faces(island, faces_by_index)
    face_loop_keys = {}
    edge_loops = defaultdict(list)
    vertex_loops = defaultdict(list)
    loop_uv = {}
    loop_vertex = {}
    loop_edge = {}

    for face_key in face_keys:
        face = faces[face_key]
        keys = []
        for local_index, loop in enumerate(face.loops):
            key = (face_key, local_index)
            keys.append(key)
            edge_loops[int(loop[1])].append(key)
            vertex_loops[int(loop[0])].append(key)
            loop_uv[key] = (float(loop[2]), float(loop[3]))
            loop_vertex[key] = int(loop[0])
            loop_edge[key] = int(loop[1])
        face_loop_keys[face_key] = tuple(keys)

    vertex_uvs = defaultdict(set)
    for key, vertex_key in loop_vertex.items():
        vertex_uvs[vertex_key].add(_uv_token(loop_uv[key]))
    uv_split = {key: False for key in vertex_uvs}

    edge_face_keys = {}
    edge_boundary = {}
    edge_non_manifold = {}
    for edge_key, keys in edge_loops.items():
        local_faces = tuple(sorted({key[0] for key in keys}))
        edge_face_keys[edge_key] = local_faces
        edge_boundary[edge_key] = len(local_faces) == 1
        edge_non_manifold[edge_key] = len(local_faces) > 2

    loop_records = []
    for key in sorted(loop_uv):
        face_key, local_index = key
        cycle = face_loop_keys[face_key]
        edge_key = loop_edge[key]
        vertex_key = loop_vertex[key]
        loop_records.append(
            tc.LoopRecord(
                key=key,
                face_key=face_key,
                edge_key=edge_key,
                vertex_key=vertex_key,
                next_key=cycle[(local_index + 1) % len(cycle)],
                prev_key=cycle[(local_index - 1) % len(cycle)],
                uv=loop_uv[key],
                boundary=edge_boundary[edge_key],
                seam=uv_split[vertex_key],
                signature=("uv_split", uv_split[vertex_key]),
            )
        )

    face_records = tuple(
        tc.FaceRecord(key=key, loop_keys=face_loop_keys[key]) for key in sorted(face_loop_keys)
    )
    edge_records = tuple(
        tc.EdgeRecord(
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
        tc.VertexRecord(
            key=vertex_key,
            loop_keys=tuple(sorted(keys)),
            boundary=any(edge_boundary[loop_edge[key]] for key in keys),
            signature=("uv_split", uv_split[vertex_key]),
        )
        for vertex_key, keys in sorted(vertex_loops.items())
    )
    boundary_keys = tuple(key for key in sorted(loop_uv) if edge_boundary[loop_edge[key]])
    boundaries = _boundary_components({"faces": faces}, boundary_keys)
    return tc.make_graph(
        faces=face_records,
        edges=edge_records,
        vertices=vertex_records,
        loops=tuple(loop_records),
        boundaries=boundaries,
    )


def _bucket_key(graph):
    return (
        len(graph.faces),
        len(graph.edges),
        len(graph.vertices),
        len(graph.loops),
        len(graph.boundaries),
        tuple(sorted(len(face.loop_keys) for face in graph.faces)),
        tuple(sorted((len(edge.loop_keys), len(edge.face_keys)) for edge in graph.edges)),
        tuple(sorted(len(vertex.loop_keys) for vertex in graph.vertices)),
        tuple(sorted((boundary.role, len(boundary.loop_keys)) for boundary in graph.boundaries)),
    )


def build_exact_islands(snapshot: dict[str, Any], selected_only: bool = True) -> tuple[ExactIsland, ...]:
    fast_snapshot = _fast_snapshot(snapshot)
    raw_faces, faces_by_index = _raw_face_map(snapshot)
    global_edge_faces = defaultdict(set)
    for face in raw_faces:
        for loop in face.loops:
            global_edge_faces[int(loop[1])].add(face.index)
    islands = fast_v2_core.build_uv_islands(fast_snapshot)
    selected = [island for island in islands if island.selected] if selected_only else list(islands)
    result = []
    for island in selected:
        graph = _graph_for_island(island, faces_by_index, global_edge_faces)
        result.append(
            ExactIsland(
                face_key=island.face_key,
                loop_uvs=island.loop_uvs,
                uv_area=float(island.uv_area),
                graph=graph,
            )
        )
    return tuple(result)



def _loop_structural_signature(validated, key):
    loop = validated.loops[key]
    face = validated.faces[loop.face_key]
    edge = validated.edges[loop.edge_key]
    vertex = validated.vertices[loop.vertex_key]
    prev_loop = validated.loops[loop.prev_key]
    next_loop = validated.loops[loop.next_key]
    prev_edge = validated.edges[prev_loop.edge_key]
    next_edge = validated.edges[next_loop.edge_key]
    return (
        len(face.loop_keys),
        face.signature,
        len(edge.loop_keys),
        len(edge.face_keys),
        bool(edge.boundary),
        bool(edge.non_manifold),
        edge.signature,
        len(vertex.loop_keys),
        bool(vertex.boundary),
        vertex.signature,
        bool(loop.boundary),
        bool(loop.seam),
        loop.signature,
        bool(prev_edge.boundary),
        bool(next_edge.boundary),
    )


def _propagate_exact_mapping(candidate, master, candidate_anchor, master_anchor, direction):
    mapping = {}
    reverse = {}
    queued_faces = []
    queued_face_keys = set()

    def assign(candidate_key, master_key):
        existing = mapping.get(candidate_key)
        if existing is not None:
            return existing == master_key
        existing_candidate = reverse.get(master_key)
        if existing_candidate is not None:
            return existing_candidate == candidate_key
        candidate_loop = candidate.loops[candidate_key]
        master_loop = master.loops[master_key]
        if (
            candidate_loop.boundary != master_loop.boundary
            or candidate_loop.seam != master_loop.seam
            or candidate_loop.signature != master_loop.signature
        ):
            return False
        mapping[candidate_key] = master_key
        reverse[master_key] = candidate_key
        return True

    def queue_face(candidate_loop_key, master_loop_key):
        candidate_face_key = candidate.loops[candidate_loop_key].face_key
        master_face_key = master.loops[master_loop_key].face_key
        token = (candidate_face_key, master_face_key)
        if token not in queued_face_keys:
            queued_face_keys.add(token)
            queued_faces.append((candidate_loop_key, master_loop_key))

    if not assign(candidate_anchor, master_anchor):
        return None
    queue_face(candidate_anchor, master_anchor)
    cursor = 0
    while cursor < len(queued_faces):
        candidate_seed, master_seed = queued_faces[cursor]
        cursor += 1
        candidate_face = candidate.faces[candidate.loops[candidate_seed].face_key]
        master_face = master.faces[master.loops[master_seed].face_key]
        if len(candidate_face.loop_keys) != len(master_face.loop_keys):
            return None
        if candidate_face.signature != master_face.signature:
            return None
        candidate_cycle = candidate_face.loop_keys
        master_cycle = master_face.loop_keys
        try:
            candidate_index = candidate_cycle.index(candidate_seed)
            master_index = master_cycle.index(master_seed)
        except ValueError:
            return None
        size = len(candidate_cycle)
        for offset in range(size):
            candidate_key = candidate_cycle[(candidate_index + offset) % size]
            master_key = master_cycle[(master_index + direction * offset) % size]
            if not assign(candidate_key, master_key):
                return None

        for candidate_key in candidate_cycle:
            master_key = mapping[candidate_key]
            candidate_loop = candidate.loops[candidate_key]
            master_loop = master.loops[master_key]
            candidate_edge = candidate.edges[candidate_loop.edge_key]
            master_edge = master.edges[master_loop.edge_key]
            if (
                len(candidate_edge.loop_keys) != len(master_edge.loop_keys)
                or len(candidate_edge.face_keys) != len(master_edge.face_keys)
                or candidate_edge.boundary != master_edge.boundary
                or candidate_edge.non_manifold != master_edge.non_manifold
                or candidate_edge.signature != master_edge.signature
            ):
                return None
            if len(candidate_edge.loop_keys) == 2:
                candidate_other = next(key for key in candidate_edge.loop_keys if key != candidate_key)
                master_other = next(key for key in master_edge.loop_keys if key != master_key)
                if not assign(candidate_other, master_other):
                    return None
                queue_face(candidate_other, master_other)

    if len(mapping) != len(candidate.loops):
        return None
    verified = tc._verify_full_mapping(candidate, master, mapping)
    if verified is None:
        return None
    return tuple(sorted(mapping.items()))


def _reverse_graph_winding(graph):
    """Reverse face winding while retaining each UV corner's original key.

    A reversed corner uses its previous edge, not its original outgoing edge.
    Merely traversing next/prev backwards does not reverse that incidence.
    """
    loops = {loop.key: loop for loop in graph.loops}
    reversed_loops = tuple(replace(
        loop, edge_key=loops[loop.prev_key].edge_key,
        boundary=loops[loop.prev_key].boundary,
        next_key=loop.prev_key, prev_key=loop.next_key,
    ) for loop in graph.loops)
    by_key = {loop.key: loop for loop in reversed_loops}
    return tc.make_graph(
        faces=tuple(replace(face, loop_keys=tuple(reversed(face.loop_keys)))
                    for face in graph.faces),
        edges=tuple(replace(edge, loop_keys=tuple(sorted(
            loops[key].next_key for key in edge.loop_keys))) for edge in graph.edges),
        vertices=tuple(replace(vertex, boundary=any(
            by_key[key].boundary for key in vertex.loop_keys)) for vertex in graph.vertices),
        loops=reversed_loops,
        boundaries=tuple(replace(boundary, loop_keys=tuple(
            loops[key].next_key for key in reversed(boundary.loop_keys)))
            for boundary in graph.boundaries),
    )


def find_deterministic_exact_mapping(master_graph, candidate_graph):
    for graph in (candidate_graph, _reverse_graph_winding(candidate_graph)):
        mapping = _find_oriented_exact_mapping(master_graph, graph)
        if mapping is not None:
            return mapping
    return None


def _find_oriented_exact_mapping(master_graph, candidate_graph):
    """Return a complete candidate->master loop isomorphism without branch search.

    The mapper anchors one structurally rare loop, propagates the face/edge
    incidence deterministically across the connected UV island and validates
    the complete result with topology_correspondence's exact verifier.  It
    therefore handles highly symmetric grids without consuming an exponential
    search budget while still returning only proven full mappings.
    """
    try:
        master = tc._validate_graph(master_graph)
        candidate = tc._validate_graph(candidate_graph)
    except Exception:
        return None
    if (
        len(master.loops) != len(candidate.loops)
        or len(master.faces) != len(candidate.faces)
        or len(master.edges) != len(candidate.edges)
        or len(master.vertices) != len(candidate.vertices)
        or len(master.boundaries) != len(candidate.boundaries)
    ):
        return None

    master_by_signature = defaultdict(list)
    for key in master.loops:
        master_by_signature[_loop_structural_signature(master, key)].append(key)
    candidate_choices = []
    for key in candidate.loops:
        signature = _loop_structural_signature(candidate, key)
        matches = master_by_signature.get(signature, ())
        if matches:
            candidate_choices.append((len(matches), key, tuple(sorted(matches))))
    if not candidate_choices:
        return None
    _count, candidate_anchor, master_anchors = min(candidate_choices)
    for master_anchor in master_anchors:
        for direction in (1, -1):
            mapping = _propagate_exact_mapping(
                candidate, master, candidate_anchor, master_anchor, direction
            )
            if mapping is not None:
                return mapping
    return None

def solve_exact(snapshot: dict[str, Any], progress_cb=None, match_cb=None) -> dict[str, Any]:
    started = time.perf_counter()
    options = dict(snapshot.get("options") or {})
    max_search = max(1000, int(options.get("max_search", 100000)))
    _progress(progress_cb, stage="islands", percent=3.0, done=0, total=1)
    islands = build_exact_islands(snapshot)
    total = len(islands)
    if total < 2:
        _progress(progress_cb, stage="done", percent=100.0, done=total, total=max(1, total))
        return {
            "schema": RESULT_SCHEMA,
            "writes": (),
            "selected_count": total,
            "aligned_count": 0,
            "group_count": total,
            "skipped_count": total,
            "master_count": 0,
            "unmatched_islands": tuple(
                {"face_key": tuple(island.face_key), "reason": "no_topology_peer"}
                for island in islands
            ),
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }

    buckets = defaultdict(list)
    for island in islands:
        buckets[_bucket_key(island.graph)].append(island)
    uv_by_island = {island.face_key: dict(island.loop_uvs) for island in islands}
    writes = []
    aligned = 0
    groups = 0
    pair_checks = 0
    fallback_calls = 0
    fallback_failures = 0
    initial_bucket_count = len(buckets)
    target_total = sum(max(0, len(members) - 1) for members in buckets.values())
    resolved_targets = 0
    unmatched = [
        {"face_key": tuple(members[0].face_key), "reason": "no_topology_peer"}
        for members in buckets.values() if len(members) == 1
    ]
    skipped = len(unmatched)
    matched_master_keys = set()
    aligned_keys = set()

    for members in buckets.values():
        ordered = sorted(members, key=lambda item: (-item.uv_area, item.face_key))
        masters = []
        for candidate_index, candidate in enumerate(ordered):
            if not masters:
                masters.append(candidate)
                groups += 1
                continue

            accepted_master = None
            accepted_mapping = None
            used_fallback = False
            rejection_reason = "mapping_unproven"
            for master in masters:
                pair_checks += 1
                mapping = find_deterministic_exact_mapping(master.graph, candidate.graph)
                if mapping is None:
                    fallback_calls += 1
                    used_fallback = True
                    _progress(
                        progress_cb,
                        stage="fallback",
                        percent=10.0 + 85.0 * resolved_targets / max(1, target_total),
                        done=resolved_targets,
                        total=max(1, target_total),
                    )
                    result = tc.find_correspondence(
                        master.graph,
                        candidate.graph,
                        allow_flipping=True,
                        match_scale=True,
                        tolerance=_FORCE_TOLERANCE,
                        max_search=max_search,
                    )
                    if result.accepted:
                        mapping = tuple(result.loop_mapping)
                    else:
                        fallback_failures += 1
                        rejection_reason = str(result.reason)
                if mapping is None:
                    continue

                master_uv = uv_by_island[master.face_key]
                candidate_keys = set(dict(candidate.loop_uvs))
                master_keys = set(master_uv)
                if (
                    len(mapping) != len(candidate_keys)
                    or {pair[0] for pair in mapping} != candidate_keys
                    or {pair[1] for pair in mapping} != master_keys
                ):
                    continue
                accepted_master = master
                accepted_mapping = tuple(mapping)
                break

            if accepted_master is None or accepted_mapping is None:
                masters.append(candidate)
                groups += 1
                skipped += 1
                unmatched.append({"face_key": tuple(candidate.face_key), "reason": rejection_reason})
            else:
                master_uv = uv_by_island[accepted_master.face_key]
                target_writes = []
                for candidate_key, master_key in accepted_mapping:
                    uv = master_uv[master_key]
                    write = (candidate_key[0], candidate_key[1], float(uv[0]), float(uv[1]))
                    writes.append(write)
                    target_writes.append(write)
                aligned += 1
                matched_master_keys.add(accepted_master.face_key)
                aligned_keys.add(candidate.face_key)
                if match_cb is not None:
                    match_cb(
                        {
                            "target_face_key": tuple(candidate.face_key),
                            "master_face_key": tuple(accepted_master.face_key),
                            "writes": tuple(target_writes),
                            "used_fallback": bool(used_fallback),
                        }
                    )

            resolved_targets += 1
            _progress(
                progress_cb,
                stage="exact" if accepted_mapping is not None else "skipped",
                percent=10.0 + 85.0 * resolved_targets / max(1, target_total),
                done=resolved_targets,
                total=max(1, target_total),
            )

    # A rejected candidate can later become a valid group's master. Count only
    # islands that finish with neither a target mapping nor matched followers.
    reasons = {item["face_key"]: item["reason"] for item in unmatched}
    unmatched = [
        {"face_key": tuple(island.face_key),
         "reason": reasons.get(island.face_key, "no_compatible_peer")}
        for island in islands
        if island.face_key not in aligned_keys and island.face_key not in matched_master_keys
    ]
    skipped = len(unmatched)
    _progress(progress_cb, stage="done", percent=100.0, done=max(1, target_total), total=max(1, target_total))
    return {
        "schema": RESULT_SCHEMA,
        "writes": tuple(writes),
        "selected_count": total,
        "aligned_count": aligned,
        "group_count": groups,
        "skipped_count": skipped,
        "unmatched_islands": tuple(unmatched),
        "master_count": len(matched_master_keys),
        "pair_checks": pair_checks,
        "fallback_calls": fallback_calls,
        "fallback_failures": fallback_failures,
        "initial_bucket_count": initial_bucket_count,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


__all__ = ["SCHEMA", "RESULT_SCHEMA", "build_exact_islands", "find_deterministic_exact_mapping", "solve_exact"]
