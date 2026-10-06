"""Pure external-process core for UV GPT Fast V2.

The module deliberately has no Blender imports.  It receives one compact
primitive snapshot copied from BMesh by the owner thread, reconstructs UV
islands, groups similar selected islands, computes candidate->master
similarity transforms and returns immutable loop writes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import similarity_matcher  # noqa: E402

SCHEMA = "fast-v2-snapshot-v1"
_EPS = 1.0e-6


@dataclass(frozen=True)
class RawFace:
    index: int
    selected: bool
    hidden: bool
    loops: tuple[tuple[int, int, float, float], ...]


@dataclass(frozen=True)
class UVIsland:
    face_key: tuple[int, ...]
    loop_keys: tuple[tuple[int, int], ...]
    segments: tuple[tuple[tuple[float, float], tuple[float, float]], ...]
    topology: dict[str, Any]
    selected: bool
    uv_area: float
    loop_uvs: tuple[tuple[tuple[int, int], tuple[float, float]], ...]


def _parse_faces(snapshot: dict[str, Any]) -> tuple[RawFace, ...]:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        raise ValueError("invalid Fast V2 snapshot schema")
    raw_faces = snapshot.get("faces", ())
    if not isinstance(raw_faces, (tuple, list)):
        raise ValueError("Fast V2 faces must be a sequence")
    faces = []
    seen = set()
    for item in raw_faces:
        if not isinstance(item, (tuple, list)) or len(item) != 4:
            raise ValueError("invalid Fast V2 face record")
        face_index, selected, hidden, raw_loops = item
        face_index = int(face_index)
        if face_index < 0 or face_index in seen:
            raise ValueError("duplicate/invalid Fast V2 face index")
        seen.add(face_index)
        loops = []
        for loop in raw_loops:
            if not isinstance(loop, (tuple, list)) or len(loop) != 4:
                raise ValueError("invalid Fast V2 loop record")
            vertex, edge, u, v = loop
            u = float(u)
            v = float(v)
            if not math.isfinite(u) or not math.isfinite(v):
                raise ValueError("non-finite Fast V2 UV")
            loops.append((int(vertex), int(edge), u, v))
        if len(loops) < 3:
            raise ValueError("Fast V2 face must contain at least three loops")
        faces.append(RawFace(face_index, bool(selected), bool(hidden), tuple(loops)))
    return tuple(sorted(faces, key=lambda face: face.index))


def _uv_close(a: tuple[float, float], b: tuple[float, float], eps: float = _EPS) -> bool:
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def _edge_match(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
) -> bool:
    return (_uv_close(a0, b1) and _uv_close(a1, b0)) or (
        _uv_close(a0, b0) and _uv_close(a1, b1)
    )


def _polygon_area(points: Iterable[tuple[float, float]]) -> float:
    points = tuple(points)
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        total += point[0] * nxt[1] - nxt[0] * point[1]
    return abs(total) * 0.5


def build_uv_islands(snapshot: dict[str, Any]) -> tuple[UVIsland, ...]:
    """Reconstruct UV islands using mesh edge incidence + UV continuity."""

    faces = tuple(face for face in _parse_faces(snapshot) if not face.hidden)
    face_by_index = {face.index: face for face in faces}
    edge_occurrences: dict[int, list[tuple[int, int, tuple[float, float], tuple[float, float]]]] = defaultdict(list)
    for face in faces:
        for local_index, loop in enumerate(face.loops):
            nxt = face.loops[(local_index + 1) % len(face.loops)]
            start = (loop[2], loop[3])
            end = (nxt[2], nxt[3])
            edge_occurrences[loop[1]].append((face.index, local_index, start, end))

    adjacency: dict[int, set[int]] = {face.index: set() for face in faces}
    for occurrences in edge_occurrences.values():
        for left_index in range(len(occurrences)):
            lf, _lli, l0, l1 = occurrences[left_index]
            for right_index in range(left_index + 1, len(occurrences)):
                rf, _rli, r0, r1 = occurrences[right_index]
                if lf == rf or not _edge_match(l0, l1, r0, r1):
                    continue
                adjacency[lf].add(rf)
                adjacency[rf].add(lf)

    visited = set()
    islands = []
    for start in sorted(face_by_index):
        if start in visited:
            continue
        pending = [start]
        visited.add(start)
        component = []
        while pending:
            current = pending.pop()
            component.append(current)
            for linked in sorted(adjacency[current], reverse=True):
                if linked not in visited:
                    visited.add(linked)
                    pending.append(linked)
        component.sort()
        component_set = set(component)
        loop_keys = []
        loop_uvs = []
        edge_faces: dict[int, set[int]] = defaultdict(set)
        vertices = set()
        segments = []
        uv_area = 0.0
        selected = False

        for face_index in component:
            face = face_by_index[face_index]
            selected = selected or (face.selected and not face.hidden)
            uv_area += _polygon_area((loop[2], loop[3]) for loop in face.loops)
            for local_index, loop in enumerate(face.loops):
                loop_key = (face.index, local_index)
                loop_keys.append(loop_key)
                loop_uvs.append((loop_key, (loop[2], loop[3])))
                vertices.add(loop[0])
                edge_faces[loop[1]].add(face.index)

        for edge_key in edge_faces:
            occurrences = edge_occurrences.get(edge_key, ())
            local_occurrences = [item for item in occurrences if item[0] in component_set]
            if not local_occurrences:
                continue
            for face_index, _local_index, start_uv, end_uv in local_occurrences:
                has_neighbor = False
                for other_face, _oli, other_start, other_end in local_occurrences:
                    if other_face == face_index:
                        continue
                    if _edge_match(start_uv, end_uv, other_start, other_end):
                        has_neighbor = True
                        break
                if not has_neighbor and (
                    abs(start_uv[0] - end_uv[0]) > 1.0e-14
                    or abs(start_uv[1] - end_uv[1]) > 1.0e-14
                ):
                    segments.append((start_uv, end_uv))

        incidence = Counter(len(face_set) for face_set in edge_faces.values())
        topology = {
            "face_count": len(component),
            "edge_count": len(edge_faces),
            "vertex_count": len(vertices),
            "non_manifold_edge_count": sum(
                count for degree, count in incidence.items() if degree > 2
            ),
            "edge_incidence_histogram": dict(incidence),
        }
        islands.append(
            UVIsland(
                face_key=tuple(component),
                loop_keys=tuple(sorted(loop_keys)),
                segments=tuple(segments),
                topology=topology,
                selected=bool(selected),
                uv_area=float(uv_area),
                loop_uvs=tuple(sorted(loop_uvs)),
            )
        )
    return tuple(sorted(islands, key=lambda island: island.face_key))


def _bucket_key(signature: Any) -> tuple[Any, ...]:
    return (
        signature.topology.core_key,
        signature.segment_count,
        signature.point_count,
        signature.component_count,
        signature.closed_component_count,
        signature.open_component_count,
        signature.ambiguous_component_count,
        signature.cycle_count,
    )


def _progress(cb: Callable[[dict[str, Any]], None] | None, **values: Any) -> None:
    if cb is not None:
        cb(values)


def solve_fast(
    snapshot: dict[str, Any],
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Return transform-based stack writes for selected similar UV islands."""

    started = __import__("time").perf_counter()
    options = dict(snapshot.get("options") or {})
    match_scale = bool(options.get("match_scale", True))
    allow_flipping = bool(options.get("allow_flipping", False))
    tolerance = max(0.0, float(options.get("tolerance", 0.01)))

    _progress(progress_cb, stage="islands", percent=3.0, done=0, total=1)
    islands = build_uv_islands(snapshot)
    selected = [island for island in islands if island.selected]
    total_selected = len(selected)
    if total_selected < 2:
        _progress(progress_cb, stage="done", percent=100.0, done=total_selected, total=max(1, total_selected))
        return {
            "schema": "fast-v2-result-v1",
            "writes": (),
            "selected_count": total_selected,
            "aligned_count": 0,
            "group_count": total_selected,
            "elapsed_ms": (__import__("time").perf_counter() - started) * 1000.0,
        }

    cheap = {}
    descriptors = {}
    buckets: dict[tuple[Any, ...], list[UVIsland]] = defaultdict(list)
    for index, island in enumerate(selected):
        signature = similarity_matcher.build_cheap_signature(
            island.segments,
            face_key=island.face_key,
            topology=island.topology,
        )
        cheap[island.face_key] = signature
        buckets[_bucket_key(signature)].append(island)
        if index % 8 == 0 or index + 1 == total_selected:
            _progress(
                progress_cb,
                stage="describe",
                percent=5.0 + 20.0 * (index + 1) / max(1, total_selected),
                done=index + 1,
                total=total_selected,
            )

    def descriptor(island: UVIsland):
        value = descriptors.get(island.face_key)
        if value is None:
            value = similarity_matcher.build_descriptor(
                island.segments,
                face_key=island.face_key,
                topology=island.topology,
            )
            descriptors[island.face_key] = value
        return value

    writes = []
    aligned = 0
    groups = 0
    assigned_done = 0
    # Larger UV-area island is a stable master; ties preserve face-key order.
    for _key, members in sorted(buckets.items(), key=lambda item: repr(item[0])):
        remaining = sorted(members, key=lambda i: (-i.uv_area, i.face_key))
        while remaining:
            master = remaining.pop(0)
            groups += 1
            assigned_done += 1
            master_desc = descriptor(master)
            survivors = []
            for candidate in remaining:
                cheap_gate = similarity_matcher.cheap_boundary_gate(
                    cheap[master.face_key], cheap[candidate.face_key]
                )
                topo_gate = similarity_matcher.cheap_topology_gate(
                    cheap[master.face_key], cheap[candidate.face_key]
                )
                result = None
                if cheap_gate.passed and topo_gate.passed:
                    result = similarity_matcher.match_descriptors(
                        master_desc,
                        descriptor(candidate),
                        match_scale=match_scale,
                        allow_flipping=allow_flipping,
                        tolerance=tolerance,
                        use_numpy=False,
                        diagnostics=similarity_matcher.MatcherDiagnostics(),
                    )
                if result is None or not result.accepted or result.transform is None:
                    survivors.append(candidate)
                    continue
                transform = result.transform
                for (face_key, local_index), uv in candidate.loop_uvs:
                    new_u, new_v = transform.apply(uv)
                    writes.append((int(face_key), int(local_index), float(new_u), float(new_v)))
                aligned += 1
                assigned_done += 1
                if assigned_done % 4 == 0 or assigned_done == total_selected:
                    _progress(
                        progress_cb,
                        stage="match",
                        percent=25.0 + 70.0 * assigned_done / max(1, total_selected),
                        done=assigned_done,
                        total=total_selected,
                    )
            remaining = survivors

    _progress(progress_cb, stage="done", percent=100.0, done=total_selected, total=total_selected)
    return {
        "schema": "fast-v2-result-v1",
        "writes": tuple(sorted(writes)),
        "selected_count": total_selected,
        "aligned_count": int(aligned),
        "group_count": int(groups),
        "elapsed_ms": (__import__("time").perf_counter() - started) * 1000.0,
    }


__all__ = ["SCHEMA", "UVIsland", "build_uv_islands", "solve_fast"]
