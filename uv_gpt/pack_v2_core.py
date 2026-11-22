"""Pure external-process Pack V2 solver.

The Blender owner thread only captures primitive mesh/UV records.  This module
reconstructs UV islands, keeps stacked followers exact by loop correspondence,
and packs irregular concave silhouettes with a coarse candidate grid plus an
exact boundary collision refinement.  No Blender imports are allowed here.
"""
from __future__ import annotations

from collections import defaultdict
import ctypes
from dataclasses import dataclass, field
import gc
import math
from pathlib import Path
import random
import sys
import time
from typing import Any, Callable, Iterable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import fast_v2_core  # noqa: E402
import pack_geometry  # noqa: E402
import pro_exact_v2_core  # noqa: E402
import topology_correspondence as tc  # noqa: E402

SCHEMA = "pack-v2-snapshot-v1"
RESULT_SCHEMA = "pack-v2-result-v1"
_EPS = 1.0e-9
# Keep a small extra reservation in the coarse rectangle phase.  Blender can
# round a packed coordinate to float32 before the exact oracle sees it; the
# extra clearance prevents that round-trip from reducing a configured margin
# by a few ulps.  Exact collision checks continue to use the requested margin.
_MARGIN_RESERVE_CLEARANCE = 1.0e-7
_SOURCE_LAYOUT_TOLERANCE = 1.0e-7
_STACK_DIRECT_TOLERANCE = 2.0e-5
_STACK_FALLBACK_MAX_SEARCH = 4096
_GRID_SAMPLES = 7
_SCALE_STEPS = 4
_SYM_AXIS_SAMPLES = 18
_SYM_SPREAD_SAMPLES = 10
_SYM_MAX_CANDIDATES_PER_UNIT = 2048
_SYM_CLEARANCE = 1.0e-7
_MAX_BOUNDARY_POINTS = 96

Point = tuple[float, float]
Polygon = tuple[Point, ...]
ShapeLoops = tuple[Polygon, ...]


@dataclass
class IslandState:
    face_key: tuple[int, ...]
    selected: bool
    uv_area: float
    world_area: float
    loop_uvs: dict[tuple[int, int], Point]
    boundary_loops: ShapeLoops
    face_sizes: tuple[int, ...]
    graph: Any = None


@dataclass
class _OverlapMember:
    island: IslandState
    follower_mappings: tuple[tuple[IslandState, tuple[tuple[tuple[int, int], tuple[int, int]], ...]], ...] = ()


@dataclass
class _UnitVariant:
    angle: float
    width: float
    height: float
    local_loop_uvs: dict[tuple[int, int], Point]
    member_shapes: tuple[ShapeLoops, ...]
    shape_bounds_list: tuple[tuple[float, float, float, float], ...]
    shape_edges_list: tuple[Any, ...]
    overlap_local_uvs: tuple[dict[tuple[int, int], Point], ...] = ()
    inner_holes: tuple[tuple[float, float, float, float], ...] = ()
    origin_shift: Point = (0.0, 0.0)


@dataclass
class PackUnit:
    key: tuple[int, ...]
    representative: IslandState
    follower_mappings: list[tuple[IslandState, tuple[tuple[tuple[int, int], tuple[int, int]], ...]]] = field(default_factory=list)
    overlap_members: list[_OverlapMember] = field(default_factory=list)
    local_loop_uvs: dict[tuple[int, int], Point] = field(default_factory=dict)
    local_shape: ShapeLoops = ()
    member_shapes: tuple[ShapeLoops, ...] = ()
    width: float = 0.0
    height: float = 0.0
    area: float = 0.0
    rotation: int = 0
    variants: tuple[_UnitVariant, ...] = ()
    hull_area: float = 0.0
    outer_area: float = 0.0
    has_hole: bool = False
    solidity: float = 1.0


def _shape_edge_data(shape: ShapeLoops):
    polys = []
    for poly in shape:
        n = len(poly)
        edges = []
        for i in range(n):
            a = poly[i]
            b = poly[(i + 1) % n]
            ax, ay = a
            bx, by = b
            edges.append((
                ax,
                ay,
                bx,
                by,
                ax if ax < bx else bx,
                bx if ax < bx else ax,
                ay if ay < by else by,
                by if ay < by else ay,
            ))
        polys.append(tuple(edges))
    return tuple(polys)


class SpatialIndex:
    def __init__(self, resolution=40):
        self.resolution = max(8, int(resolution))
        self.shapes: list[ShapeLoops] = []
        self.bounds: list[tuple[float, float, float, float]] = []
        self.edges: list[Any] = []
        self.tags: list[int] = []
        self.buckets: dict[tuple[int, int], set[int]] = {}

    def _cells(self, bounds, margin=0.0):
        minx, maxx, miny, maxy = bounds
        minx -= margin
        maxx += margin
        miny -= margin
        maxy += margin
        r = self.resolution
        x0 = max(0, min(r - 1, int(math.floor(minx * r))))
        x1 = max(0, min(r - 1, int(math.floor(maxx * r))))
        y0 = max(0, min(r - 1, int(math.floor(miny * r))))
        y1 = max(0, min(r - 1, int(math.floor(maxy * r))))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                yield gx, gy

    def insert(self, shape: ShapeLoops, tag: int = -1, bounds=None, edges=None):
        idx = len(self.shapes)
        b = shape_bounds(shape) if bounds is None else bounds
        e = _shape_edge_data(shape) if edges is None else edges
        self.shapes.append(shape)
        self.bounds.append(b)
        self.edges.append(e)
        self.tags.append(int(tag))
        for cell in self._cells(b):
            self.buckets.setdefault(cell, set()).add(idx)

    def query_entries(self, bounds, margin=0.0):
        ids = set()
        for cell in self._cells(bounds, margin=margin):
            ids.update(self.buckets.get(cell, ()))
        return tuple((self.shapes[idx], self.bounds[idx], self.edges[idx], self.tags[idx]) for idx in ids)

    def query_records(self, shape: ShapeLoops, margin=0.0):
        ids = set()
        bounds = shape_bounds(shape)
        for cell in self._cells(bounds, margin=margin):
            ids.update(self.buckets.get(cell, ()))
        return tuple((self.shapes[idx], self.bounds[idx]) for idx in ids)

    def query(self, shape: ShapeLoops, margin=0.0):
        return tuple(item[0] for item in self.query_records(shape, margin=margin))


def _progress(cb: Callable[[dict[str, Any]], None] | None, **values: Any) -> None:
    if cb is not None:
        cb(values)


def _fast_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        raise ValueError("invalid Pack V2 snapshot schema")
    return {"schema": fast_v2_core.SCHEMA, "faces": snapshot.get("faces", ()), "options": {}}


def _point_key(point: Point):
    return (round(float(point[0]), 9), round(float(point[1]), 9))


def _signed_area(poly: Polygon):
    total = 0.0
    for index, point in enumerate(poly):
        nxt = poly[(index + 1) % len(poly)]
        total += point[0] * nxt[1] - nxt[0] * point[1]
    return total * 0.5


def _downsample(poly: Polygon, limit=_MAX_BOUNDARY_POINTS):
    if len(poly) <= limit:
        return poly
    result = []
    for index in range(limit):
        source = int(round(index * len(poly) / limit)) % len(poly)
        point = poly[source]
        if not result or point != result[-1]:
            result.append(point)
    return tuple(result)


def boundary_loops_from_segments(segments) -> ShapeLoops:
    """Trace unordered boundary segments into closed loops."""
    values = {}
    edges = set()
    adjacency = defaultdict(list)
    for a, b in segments:
        ka = _point_key(a)
        kb = _point_key(b)
        if ka == kb:
            continue
        values.setdefault(ka, (float(a[0]), float(a[1])))
        values.setdefault(kb, (float(b[0]), float(b[1])))
        edge = (ka, kb) if ka < kb else (kb, ka)
        if edge in edges:
            continue
        edges.add(edge)
        adjacency[ka].append(kb)
        adjacency[kb].append(ka)
    unused = set(edges)
    loops = []
    while unused:
        first = min(unused)
        start, current = first
        previous = start
        unused.remove(first)
        keys = [start, current]
        guard = 0
        while current != start and guard <= len(edges) + 2:
            guard += 1
            choices = []
            for neighbor in adjacency.get(current, ()):
                edge = (current, neighbor) if current < neighbor else (neighbor, current)
                if edge in unused:
                    choices.append((neighbor == previous, neighbor, edge))
            if not choices:
                break
            choices.sort()
            _, neighbor, edge = choices[0]
            unused.remove(edge)
            previous, current = current, neighbor
            if current != start:
                keys.append(current)
        if current == start and len(keys) >= 3:
            # Every boundary vertex participates in collision and margin
            # checks. Sampling can erase an extremum and change with the UV
            # loop's start point after rotation, producing an invalid layout.
            poly = tuple(values[key] for key in keys)
            if len(poly) >= 3 and abs(_signed_area(poly)) > 1.0e-12:
                loops.append(poly)
    loops.sort(key=lambda poly: (-abs(_signed_area(poly)), poly))
    return tuple(loops)


def shape_bounds(shape: ShapeLoops):
    points = [point for poly in shape for point in poly]
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), max(xs), min(ys), max(ys)


def point_in_shape(point: Point, shape: ShapeLoops):
    # Odd-even over all closed loops naturally supports holes and disjoint outers.
    inside = False
    for poly in shape:
        if pack_geometry.point_in_polygon(point, poly):
            inside = not inside
    return inside


def _edges(poly: Polygon):
    return tuple((poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def _shape_sets_conflict_fast(left: ShapeLoops, lb, le_polys, right: ShapeLoops, rb, re_polys, margin=0.0):
    """Exact polygon boundary collision using precomputed shape bounds and edge AABBs."""
    if not left or not right:
        return False
    lm = max(0.0, float(margin))
    if (
        lb[1] + lm < rb[0]
        or rb[1] + lm < lb[0]
        or lb[3] + lm < rb[2]
        or rb[3] + lm < lb[2]
    ):
        return False
    for le in le_polys:
        for re in re_polys:
            for ax, ay, bx, by, lminx, lmaxx, lminy, lmaxy in le:
                for cx, cy, dx, dy, rminx, rmaxx, rminy, rmaxy in re:
                    if (
                        lmaxx + lm < rminx
                        or rmaxx + lm < lminx
                        or lmaxy + lm < rminy
                        or rmaxy + lm < lminy
                    ):
                        continue
                    a = (ax, ay)
                    b = (bx, by)
                    c = (cx, cy)
                    d = (dx, dy)
                    if pack_geometry.segments_intersect(a, b, c, d):
                        return True
                    if lm > 0.0 and pack_geometry._segment_distance(a, b, c, d) < lm - _EPS:
                        return True
    for poly in left:
        for point in poly:
            if point_in_shape(point, right):
                return True
    for poly in right:
        for point in poly:
            if point_in_shape(point, left):
                return True
    return False


def shape_sets_conflict(left: ShapeLoops, right: ShapeLoops, margin=0.0):
    """Exact boundary collision for polygon sets with holes via odd-even fill."""
    if not left or not right:
        return False
    return _shape_sets_conflict_fast(
        left,
        shape_bounds(left),
        _shape_edge_data(left),
        right,
        shape_bounds(right),
        _shape_edge_data(right),
        margin=margin,
    )


def transform_shape(shape: ShapeLoops, scale=1.0, tx=0.0, ty=0.0):
    return tuple(
        tuple((p[0] * scale + tx, p[1] * scale + ty) for p in poly)
        for poly in shape
    )


def _rotate(point: Point, angle):
    if abs(angle) <= 1.0e-15:
        return point
    c = math.cos(angle)
    s = math.sin(angle)
    return (c * point[0] - s * point[1], s * point[0] + c * point[1])


def _principal_axis(points):
    if not points:
        return (1.0, 0.0)
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    xx = yy = xy = 0.0
    for x, y in points:
        dx = x - cx
        dy = y - cy
        xx += dx * dx
        yy += dy * dy
        xy += dx * dy
    if abs(xy) <= 1.0e-16 and abs(xx - yy) <= 1.0e-16:
        return (1.0, 0.0)
    angle = 0.5 * math.atan2(2.0 * xy, xx - yy)
    return (math.cos(angle), math.sin(angle))


def _apply_similarity(points, angle=0.0, scale=1.0, center=None):
    if not points:
        return {}
    if center is None:
        center = (
            sum(p[0] for p in points.values()) / len(points),
            sum(p[1] for p in points.values()) / len(points),
        )
    c = math.cos(angle)
    s = math.sin(angle)
    result = {}
    for key, (x, y) in points.items():
        dx = x - center[0]
        dy = y - center[1]
        result[key] = (
            center[0] + scale * (c * dx - s * dy),
            center[1] + scale * (s * dx + c * dy),
        )
    return result


def _shape_from_loop_uvs(island: IslandState, loop_uvs):
    original_to_new = {key: loop_uvs[key] for key in loop_uvs}
    # Boundary vertices are UV coordinates, so map by original coordinate token.
    uv_map = defaultdict(list)
    for key, original in island.loop_uvs.items():
        uv_map[_point_key(original)].append(original_to_new[key])
    result = []
    for poly in island.boundary_loops:
        new_poly = []
        for point in poly:
            candidates = uv_map.get(_point_key(point))
            new_poly.append(candidates[0] if candidates else point)
        result.append(tuple(new_poly))
    return tuple(result)


def _uv_coordinate_bounds(loop_uvs, shape):
    """Include every written corner in the placement envelope."""
    points = tuple(loop_uvs.values()) + tuple(point for poly in shape for point in poly)
    if not points:
        return shape_bounds(shape)
    return (min(p[0] for p in points), max(p[0] for p in points),
            min(p[1] for p in points), max(p[1] for p in points))


def _normalize_local(loop_uvs, shape):
    bounds = _uv_coordinate_bounds(loop_uvs, shape)
    minx, maxx, miny, maxy = bounds
    local_uvs = {key: (uv[0] - minx, uv[1] - miny) for key, uv in loop_uvs.items()}
    local_shape = tuple(tuple((p[0] - minx, p[1] - miny) for p in poly) for poly in shape)
    return local_uvs, local_shape, max(maxx - minx, 1e-10), max(maxy - miny, 1e-10)


def _parse_islands(snapshot) -> tuple[IslandState, ...]:
    fast_snapshot = _fast_snapshot(snapshot)
    raw_faces = {face.index: face for face in fast_v2_core._parse_faces(fast_snapshot)}
    world_area_by_face = {int(k): float(v) for k, v in dict(snapshot.get("world_area_by_face") or {}).items()}
    islands = fast_v2_core.build_uv_islands(fast_snapshot)
    result = []
    for island in islands:
        loop_uvs = dict(island.loop_uvs)
        segments = island.segments
        loops = boundary_loops_from_segments(segments)
        if not loops:
            bounds = (
                min(p[0] for p in loop_uvs.values()),
                max(p[0] for p in loop_uvs.values()),
                min(p[1] for p in loop_uvs.values()),
                max(p[1] for p in loop_uvs.values()),
            )
            loops = (((bounds[0], bounds[2]), (bounds[1], bounds[2]), (bounds[1], bounds[3]), (bounds[0], bounds[3])),)
        face_sizes = tuple(sorted(len(raw_faces[key].loops) for key in island.face_key))
        world_area = sum(world_area_by_face.get(key, 0.0) for key in island.face_key)
        result.append(
            IslandState(
                face_key=island.face_key,
                selected=island.selected,
                uv_area=float(island.uv_area),
                world_area=float(world_area),
                loop_uvs=loop_uvs,
                boundary_loops=loops,
                face_sizes=face_sizes,
            )
        )
    return tuple(result)


def _topology_bucket(graph):
    return (
        len(graph.faces), len(graph.edges), len(graph.vertices), len(graph.loops),
        len(graph.boundaries),
        tuple(sorted(len(face.loop_keys) for face in graph.faces)),
        tuple(sorted((len(edge.loop_keys), len(edge.face_keys)) for edge in graph.edges)),
        tuple(sorted(len(vertex.loop_keys) for vertex in graph.vertices)),
        tuple(sorted((boundary.role, len(boundary.loop_keys)) for boundary in graph.boundaries)),
    )


def _stack_signature(island: IslandState):
    points = sorted({_point_key(uv) for uv in island.loop_uvs.values()})
    bounds = shape_bounds(island.boundary_loops)
    return (
        len(island.face_key),
        island.face_sizes,
        tuple(round(v, 5) for v in bounds),
        round(island.uv_area, 5),
        tuple((round(p[0], 5), round(p[1], 5)) for p in points),
    )


def exact_follower_writes(master_uvs, mapping):
    """Return exact candidate UV copies from a (candidate, master) mapping."""
    result = []
    for candidate_key, master_key in mapping:
        uv = master_uvs[master_key]
        result.append((candidate_key, (float(uv[0]), float(uv[1]))))
    return tuple(result)


def _validated_stack_mapping(mapping, representative, follower):
    """Return a complete direct-UV stack mapping, or ``None``.

    Graph isomorphism alone is insufficient for stacked UVs with symmetric
    topology: an automorphism can preserve every edge/face relation while
    pairing different coincident labels.  Keep the direct coordinate check at
    this boundary so every caller accepts only the correspondence that can
    actually restore the follower's UVs.
    """
    if mapping is None:
        return None
    mapping = tuple(mapping)
    representative_keys = set(representative.loop_uvs)
    follower_keys = set(follower.loop_uvs)
    if len(mapping) != len(follower_keys) or len(mapping) != len(representative_keys):
        return None
    seen_candidate = set()
    seen_master = set()
    max_direct = 0.0
    for candidate_key, master_key in mapping:
        if candidate_key in seen_candidate or master_key in seen_master:
            return None
        if candidate_key not in follower_keys or master_key not in representative_keys:
            return None
        seen_candidate.add(candidate_key)
        seen_master.add(master_key)
        candidate_uv = follower.loop_uvs[candidate_key]
        master_uv = representative.loop_uvs[master_key]
        max_direct = max(
            max_direct,
            math.hypot(candidate_uv[0] - master_uv[0], candidate_uv[1] - master_uv[1]),
        )
    if seen_candidate != follower_keys or seen_master != representative_keys:
        return None
    if max_direct > _STACK_DIRECT_TOLERANCE:
        return None
    return mapping


def _coincident_uv_labelled_mapping(representative, follower):
    """Find a topology-proven mapping constrained by coincident UV labels.

    ``find_deterministic_exact_mapping`` intentionally chooses one stable
    anchor.  Symmetric topology can make that anchor an incorrect
    automorphism for stacked UVs.  Enumerating only anchors with the same
    direct UV label keeps the search small, and each candidate is still
    propagated and checked by the exact topology verifier.
    """
    try:
        master = tc._validate_graph(representative.graph)
        candidate_graphs = (
            tc._validate_graph(follower.graph),
            tc._validate_graph(pro_exact_v2_core._reverse_graph_winding(follower.graph)),
        )
    except Exception:
        return None
    if (
        len(master.loops) != len(candidate_graphs[0].loops)
        or len(master.faces) != len(candidate_graphs[0].faces)
        or len(master.edges) != len(candidate_graphs[0].edges)
        or len(master.vertices) != len(candidate_graphs[0].vertices)
        or len(master.boundaries) != len(candidate_graphs[0].boundaries)
    ):
        return None

    tolerance = _STACK_DIRECT_TOLERANCE
    def uv_bin(uv):
        return (
            int(math.floor(float(uv[0]) / tolerance)),
            int(math.floor(float(uv[1]) / tolerance)),
        )

    master_by_label = defaultdict(list)
    for master_key in master.loops:
        uv = representative.loop_uvs.get(master_key)
        if uv is not None:
            master_by_label[uv_bin(uv)].append(master_key)
    master_signatures = {
        key: pro_exact_v2_core._loop_structural_signature(master, key)
        for key in master.loops
    }

    for candidate in candidate_graphs:
        candidate_signatures = {
            key: pro_exact_v2_core._loop_structural_signature(candidate, key)
            for key in candidate.loops
        }
        anchor_choices = []
        for candidate_key in candidate.loops:
            candidate_uv = follower.loop_uvs.get(candidate_key)
            if candidate_uv is None:
                continue
            base_x, base_y = uv_bin(candidate_uv)
            matches = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for master_key in master_by_label.get((base_x + dx, base_y + dy), ()):
                        master_uv = representative.loop_uvs[master_key]
                        if (
                            math.hypot(
                                candidate_uv[0] - master_uv[0],
                                candidate_uv[1] - master_uv[1],
                            )
                            <= tolerance
                            and candidate_signatures[candidate_key]
                            == master_signatures[master_key]
                        ):
                            matches.append(master_key)
            if matches:
                anchor_choices.append((len(matches), candidate_key, tuple(sorted(set(matches)))))
        if len(anchor_choices) < len(candidate.loops):
            continue
        anchor_choices.sort(key=lambda item: (item[0], item[1]))
        for _match_count, candidate_anchor, master_anchors in anchor_choices[:2]:
            for master_anchor in master_anchors:
                for direction in (1, -1):
                    mapping = pro_exact_v2_core._propagate_exact_mapping(
                        candidate,
                        master,
                        candidate_anchor,
                        master_anchor,
                        direction,
                    )
                    mapping = _validated_stack_mapping(mapping, representative, follower)
                    if mapping is not None:
                        return mapping
    return None


def _stack_mapping(representative, follower):
    """Resolve one exact stack without accepting topological automorphisms."""
    mapping = pro_exact_v2_core.find_deterministic_exact_mapping(
        representative.graph,
        follower.graph,
    )
    mapping = _validated_stack_mapping(mapping, representative, follower)
    if mapping is not None:
        return mapping

    mapping = _coincident_uv_labelled_mapping(representative, follower)
    if mapping is not None:
        return mapping

    # Keep the legacy search as a bounded last resort for uncommon graphs.
    # Its result still has to pass the same direct-label and bijection checks.
    try:
        result = tc.find_correspondence(
            representative.graph,
            follower.graph,
            allow_flipping=True,
            match_scale=True,
            tolerance=1.0e300,
            max_search=_STACK_FALLBACK_MAX_SEARCH,
        )
    except Exception:
        return None
    if not result.accepted:
        return None
    return _validated_stack_mapping(result.loop_mapping, representative, follower)


def _group_overlapping_units(units):
    """Merge any units whose 2D UV shapes strictly overlap into rigid PackUnits."""
    if len(units) <= 1:
        for unit in units:
            unit.member_shapes = (unit.representative.boundary_loops,)
        return units

    index = SpatialIndex(44)
    bounds_list = []
    for idx, unit in enumerate(units):
        sh = unit.representative.boundary_loops
        b = shape_bounds(sh)
        bounds_list.append(b)
        index.insert(sh, tag=idx, bounds=b)

    parent = list(range(len(units)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for idx, unit in enumerate(units):
        sh = unit.representative.boundary_loops
        b = bounds_list[idx]
        for other_sh, other_b, _other_edges, other_idx in index.query_entries(b, margin=0.0):
            if other_idx <= idx or find(idx) == find(other_idx):
                continue
            if (
                min(b[1], other_b[1]) <= max(b[0], other_b[0]) + 1.0e-8
                or min(b[3], other_b[3]) <= max(b[2], other_b[2]) + 1.0e-8
            ):
                continue
            if pack_geometry.shapes_strictly_overlap(sh, other_sh):
                union(idx, other_idx)

    groups = defaultdict(list)
    for idx in range(len(units)):
        groups[find(idx)].append(idx)

    merged = []
    for comp in sorted(groups.values(), key=lambda g: units[g[0]].key):
        comp.sort(key=lambda idx: units[idx].key)
        anchor = units[comp[0]]
        if len(comp) == 1:
            anchor.member_shapes = (anchor.representative.boundary_loops,)
            merged.append(anchor)
            continue
        overlap_members = list(anchor.overlap_members)
        shapes = [anchor.representative.boundary_loops]
        areas = [anchor.representative.uv_area]
        for other_idx in comp[1:]:
            other = units[other_idx]
            overlap_members.append(
                _OverlapMember(
                    island=other.representative,
                    follower_mappings=tuple(other.follower_mappings),
                )
            )
            shapes.append(other.representative.boundary_loops)
            areas.append(other.representative.uv_area)
            for sub in other.overlap_members:
                overlap_members.append(sub)
                shapes.append(sub.island.boundary_loops)
                areas.append(sub.island.uv_area)
        anchor.overlap_members = overlap_members
        anchor.member_shapes = tuple(shapes)
        anchor.area = max(areas)
        merged.append(anchor)
    return merged


def _build_pack_units(
    snapshot,
    islands,
    movable,
    preserve_stacks,
    progress_cb=None,
    lock_overlapping=None,
):
    if lock_overlapping is None:
        lock_mode = "EXACT" if preserve_stacks else "OFF"
    else:
        lock_mode = str(lock_overlapping).upper()
        if lock_mode not in {"ANY", "EXACT", "OFF"}:
            lock_mode = "EXACT" if preserve_stacks else "OFF"
    if lock_mode == "OFF":
        return [
            PackUnit(
                key=i.face_key,
                representative=i,
                member_shapes=(i.boundary_loops,),
            )
            for i in movable
        ]

    buckets = defaultdict(list)
    for island in movable:
        buckets[_stack_signature(island)].append(island)

    # Exact graphs are only needed for likely coincident stack buckets (size >= 2).
    multi_keys = {i.face_key for members in buckets.values() if len(members) > 1 for i in members}
    if multi_keys:
        exact_snapshot = dict(snapshot)
        exact_snapshot["schema"] = pro_exact_v2_core.SCHEMA
        _, faces_by_index = pro_exact_v2_core._raw_face_map(exact_snapshot)
        fast_islands = fast_v2_core.build_uv_islands(_fast_snapshot(snapshot))
        movable_by_key = {isl.face_key: isl for isl in movable}
        for fast_isl in fast_islands:
            if fast_isl.face_key in multi_keys:
                target_isl = movable_by_key.get(fast_isl.face_key)
                if target_isl is not None:
                    try:
                        target_isl.graph = pro_exact_v2_core._graph_for_island(fast_isl, faces_by_index, {})
                    except ValueError:
                        target_isl.graph = None
    units = []
    done = 0
    for members in buckets.values():
        members = sorted(members, key=lambda i: i.face_key)
        if len(members) == 1 or members[0].graph is None:
            units.extend(
                PackUnit(
                    key=i.face_key,
                    representative=i,
                    member_shapes=(i.boundary_loops,),
                )
                for i in members
            )
            continue
        representative = members[0]
        unit = PackUnit(
            key=representative.face_key,
            representative=representative,
            member_shapes=(representative.boundary_loops,),
        )
        leftovers = []
        for follower in members[1:]:
            if follower.graph is None or _topology_bucket(follower.graph) != _topology_bucket(representative.graph):
                leftovers.append(follower)
                continue
            mapping = _stack_mapping(representative, follower)
            if mapping is None:
                leftovers.append(follower)
                continue
            unit.follower_mappings.append((follower, mapping))
        units.append(unit)
        units.extend(
            PackUnit(
                key=i.face_key,
                representative=i,
                member_shapes=(i.boundary_loops,),
            )
            for i in leftovers
        )
        done += len(members)
        _progress(progress_cb, stage="stacks", percent=8.0, done=done, total=max(1, len(movable)))

    if lock_mode == "ANY":
        units = _group_overlapping_units(units)
    return units


def _extract_shape_holes(shapes: Iterable[ShapeLoops]) -> tuple[tuple[float, float, float, float], ...]:
    """Return unscaled interior hole AABBs (minx, maxx, miny, maxy) sorted largest-first."""
    holes = []
    for sh in shapes:
        if len(sh) <= 1 or len(sh[0]) < 3:
            continue
        outer = sh[0]
        for poly in sh[1:]:
            if len(poly) < 3:
                continue
            area = abs(_signed_area(poly))
            if area > 1.0e-4 and pack_geometry.point_in_polygon(poly[0], outer):
                b = pack_geometry.polygon_bounds(poly)
                holes.append((area, b[0], b[1], b[2], b[3]))
    holes.sort(key=lambda item: -item[0])
    return tuple((h[1], h[2], h[3], h[4]) for h in holes)


def _build_unit_variant(unit: PackUnit, angle: float, density_scale: float, center: Point) -> _UnitVariant:
    rep_uvs = dict(unit.representative.loop_uvs)
    if density_scale != 1.0 or abs(angle) > 1.0e-12:
        rep_uvs = _apply_similarity(rep_uvs, angle=angle, scale=density_scale, center=center)
    rep_shape = _shape_from_loop_uvs(unit.representative, rep_uvs)

    all_points = list(rep_uvs.values()) + [pt for poly in rep_shape for pt in poly]
    overlap_uv_dicts = []
    overlap_shapes = []
    for member in unit.overlap_members:
        m_uvs = dict(member.island.loop_uvs)
        if density_scale != 1.0 or abs(angle) > 1.0e-12:
            m_uvs = _apply_similarity(m_uvs, angle=angle, scale=density_scale, center=center)
        m_shape = _shape_from_loop_uvs(member.island, m_uvs)
        overlap_uv_dicts.append(m_uvs)
        overlap_shapes.append(m_shape)
        all_points.extend(m_uvs.values())
        for poly in m_shape:
            all_points.extend(poly)

    if all_points:
        minx = min(p[0] for p in all_points)
        maxx = max(p[0] for p in all_points)
        miny = min(p[1] for p in all_points)
        maxy = max(p[1] for p in all_points)
    else:
        minx = maxx = miny = maxy = 0.0

    width = max(maxx - minx, 1.0e-10)
    height = max(maxy - miny, 1.0e-10)
    local_rep_uvs = {k: (uv[0] - minx, uv[1] - miny) for k, uv in rep_uvs.items()}
    local_rep_shape = tuple(tuple((p[0] - minx, p[1] - miny) for p in poly) for poly in rep_shape)
    local_member_shapes = [local_rep_shape]
    local_overlap_uvs = []
    for m_uvs, m_shape in zip(overlap_uv_dicts, overlap_shapes):
        local_overlap_uvs.append({k: (uv[0] - minx, uv[1] - miny) for k, uv in m_uvs.items()})
        local_member_shapes.append(
            tuple(tuple((p[0] - minx, p[1] - miny) for p in poly) for poly in m_shape)
        )

    member_shapes_tuple = tuple(local_member_shapes)
    shape_bounds_list = tuple(shape_bounds(sh) for sh in member_shapes_tuple)
    shape_edges_list = tuple(_shape_edge_data(sh) for sh in member_shapes_tuple)
    inner_holes = _extract_shape_holes(member_shapes_tuple)
    return _UnitVariant(
        angle=angle,
        width=width,
        height=height,
        local_loop_uvs=local_rep_uvs,
        member_shapes=member_shapes_tuple,
        shape_bounds_list=shape_bounds_list,
        shape_edges_list=shape_edges_list,
        overlap_local_uvs=tuple(local_overlap_uvs),
        inner_holes=inner_holes,
        origin_shift=(minx - center[0], miny - center[1]),
    )


def _variant_signature(variant: _UnitVariant):
    pts = []
    for sh in variant.member_shapes:
        for poly in sh:
            for x, y in poly:
                pts.append((round(x, 3), round(y, 3)))
    pts.sort()
    return (round(variant.width, 3), round(variant.height, 3), tuple(pts[:20]))


def _min_square_span_candidates(pts_for_box, max_dim0):
    """Return 4 cardinal rotations of min-square-span angle and whether it beats axis max_dim."""
    if not pts_for_box:
        return (), False
    sq_ang = pack_geometry.min_square_span_angle(pts_for_box) % (2.0 * math.pi)
    cs, sn = math.cos(sq_ang), math.sin(sq_ang)
    rxs = [cs * p[0] - sn * p[1] for p in pts_for_box]
    rys = [sn * p[0] + cs * p[1] for p in pts_for_box]
    sq_span = max(max(rxs) - min(rxs), max(rys) - min(rys))
    sq_cands = (
        sq_ang,
        (sq_ang + math.pi * 0.5) % (2.0 * math.pi),
        (sq_ang + math.pi) % (2.0 * math.pi),
        (sq_ang + math.pi * 1.5) % (2.0 * math.pi),
    )
    sq_improves = max_dim0 > 0.0 and sq_span < 0.985 * max_dim0
    return sq_cands, sq_improves


def _append_unique_angles(raw_angles, candidates, tol, cap=None):
    for cand in candidates:
        if all(min(abs(cand - a), 2.0 * math.pi - abs(cand - a)) > tol for a in raw_angles):
            raw_angles.append(cand)
            if cap is not None and len(raw_angles) >= cap:
                break


def _prepare_unit(unit: PackUnit, rotation_mode="NONE", density_scale=1.0):
    all_uv_pts = list(unit.representative.loop_uvs.values())
    all_hull_pts = [p for poly in unit.representative.boundary_loops for p in poly]
    for member in unit.overlap_members:
        all_uv_pts.extend(member.island.loop_uvs.values())
        for poly in member.island.boundary_loops:
            all_hull_pts.extend(poly)
    if all_uv_pts:
        center = (
            sum(p[0] for p in all_uv_pts) / len(all_uv_pts),
            sum(p[1] for p in all_uv_pts) / len(all_uv_pts),
        )
    else:
        center = (0.0, 0.0)

    pts_for_box = all_hull_pts or all_uv_pts
    hull = pack_geometry.convex_hull(pts_for_box) if pts_for_box else ()
    raw_hull_area = abs(_signed_area(hull)) if len(hull) >= 3 else 0.0
    all_shapes = (unit.representative.boundary_loops,) + tuple(
        m.island.boundary_loops for m in unit.overlap_members
    )
    raw_outer_area = max(
        (abs(_signed_area(poly)) for sh in all_shapes for poly in sh),
        default=unit.representative.uv_area,
    )
    has_hole = any(
        len(sh) > 1
        and len(sh[0]) >= 3
        and any(
            len(p) >= 3 and abs(_signed_area(p)) > 1.0e-4 and pack_geometry.point_in_polygon(p[0], sh[0])
            for p in sh[1:]
        )
        for sh in all_shapes
    )
    area_base = max(
        [unit.representative.uv_area] + [m.island.uv_area for m in unit.overlap_members]
    )
    unit.area = max(area_base * density_scale * density_scale, 1.0e-12)
    unit.hull_area = max(raw_hull_area * density_scale * density_scale, unit.area)
    unit.outer_area = max(raw_outer_area * density_scale * density_scale, unit.area)
    unit.has_hole = has_hole
    unit.solidity = min(1.0, unit.area / max(unit.hull_area, 1.0e-12))

    mode = str(rotation_mode or "NONE").upper()
    max_variants_cap = 10
    if mode == "ROT_90":
        angles = [math.pi * 0.5]
    elif mode == "ROT_180":
        angles = [math.pi]
    elif mode == "CARDINAL":
        # Try 0/90/180/270 during placement (0 deg first to allow source-layout reuse).
        angles = [0.0, math.pi * 0.5, math.pi, math.pi * 1.5]
    elif mode == "FREE":
        min_ang = pack_geometry.min_area_rect_angle(pts_for_box) % (2.0 * math.pi)
        axis_box_area = 0.0
        min_box_area = 0.0
        w0 = h0 = 0.0
        if pts_for_box:
            xs0 = [p[0] for p in pts_for_box]
            ys0 = [p[1] for p in pts_for_box]
            w0 = max(max(xs0) - min(xs0), 1.0e-12)
            h0 = max(max(ys0) - min(ys0), 1.0e-12)
            axis_box_area = w0 * h0
            cm, sm = math.cos(min_ang), math.sin(min_ang)
            rxs = [cm * p[0] - sm * p[1] for p in pts_for_box]
            rys = [sm * p[0] + cm * p[1] for p in pts_for_box]
            min_box_area = max(max(rxs) - min(rxs), 1.0e-12) * max(max(rys) - min(rys), 1.0e-12)
        min_first = (min_box_area < 0.88 * axis_box_area) if axis_box_area > 0.0 else False
        max_dim0 = max(w0, h0) * density_scale
        min_dim0 = max(min(w0, h0) * density_scale, 1.0e-9)
        aspect = max_dim0 / min_dim0
        min_cands = (
            min_ang,
            (min_ang + math.pi * 0.5) % (2.0 * math.pi),
            (min_ang + math.pi) % (2.0 * math.pi),
            (min_ang + math.pi * 1.5) % (2.0 * math.pi),
        )
        sq_cands, sq_improves = _min_square_span_candidates(pts_for_box, max_dim0 / max(density_scale, 1.0e-12))
        card_cands = (math.pi * 0.5, math.pi, math.pi * 1.5)
        if unit.solidity > 0.90 and aspect < 1.12 and not has_hole and not min_first and not sq_improves:
            order_pool = (math.pi * 0.5,)
            max_variants_cap = 2
        elif max_dim0 < 0.10 and not has_hole:
            order_pool = (min_cands + card_cands) if min_first else (card_cands + min_cands[:2])
            max_variants_cap = 10
        else:
            extra_angles = tuple(
                math.radians(deg) for deg in (45.0, 135.0, 225.0, 315.0, 22.5, 67.5, 112.5, 157.5)
            )
            order_pool = (
                (min_cands + card_cands) if min_first else (card_cands + min_cands)
            ) + extra_angles
            max_variants_cap = 18
        raw_angles = [0.0]
        if sq_cands:
            _append_unique_angles(raw_angles, sq_cands, 0.025)
        _append_unique_angles(raw_angles, order_pool, 0.08)
        angles = raw_angles
    else:
        angles = [0.0]

    variants = []
    seen_sigs = set()
    for ang in angles:
        var = _build_unit_variant(unit, ang, density_scale, center)
        sig = _variant_signature(var)
        if variants and sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        variants.append(var)
        if len(variants) >= max_variants_cap:
            break

    base = variants[0]
    unit.variants = tuple(variants)
    unit.local_loop_uvs = base.local_loop_uvs
    unit.local_shape = base.member_shapes[0] if base.member_shapes else ()
    unit.member_shapes = base.member_shapes
    unit.width = base.width
    unit.height = base.height


def _shape_fit_tile(shape, margin):
    low = float(margin) - 1e-9
    high = 1.0 - float(margin) + 1e-9
    for poly in shape:
        for x, y in poly:
            if x < low or x > high or y < low or y > high:
                return False
    return True


def _shape_coordinates_close(left, right, tolerance=_SOURCE_LAYOUT_TOLERANCE):
    """Return whether two equally ordered shape loops have the same points."""
    if len(left) != len(right):
        return False
    for left_poly, right_poly in zip(left, right):
        if len(left_poly) != len(right_poly):
            return False
        for (left_x, left_y), (right_x, right_y) in zip(left_poly, right_poly):
            if abs(left_x - right_x) > tolerance or abs(left_y - right_y) > tolerance:
                return False
    return True


def _reuse_source_layout(units, static_shapes, margin):
    """Recover a valid existing layout when preparation preserved its geometry.

    Keep Current Scale is commonly run immediately after another pack.  A
    MaxRects search can reject that already valid layout because its coarse
    AABB reservation is intentionally conservative.  This path only reuses a
    source placement after checking every representative loop, every locked
    overlap/follower member, tile bounds, and the exact configured-margin
    collision oracle.  A source with an unrelated overlap, changed orientation,
    or changed scale returns ``None`` and follows the normal solver path.
    """
    index = SpatialIndex(44)
    for shape in static_shapes:
        index.insert(shape)

    placements = {}
    for unit in units:
        representative = unit.representative
        if representative is None or not unit.local_shape or not unit.local_loop_uvs:
            return None

        variant = unit.variants[0] if unit.variants else None
        all_bounds_pts = [pt for poly in representative.boundary_loops for pt in poly]
        all_bounds_pts.extend(representative.loop_uvs.values())
        for member in unit.overlap_members:
            for poly in member.island.boundary_loops:
                all_bounds_pts.extend(poly)
            all_bounds_pts.extend(member.island.loop_uvs.values())
        min_x = min(p[0] for p in all_bounds_pts)
        min_y = min(p[1] for p in all_bounds_pts)
        placement = (min_x, min_y)

        source_shapes = []
        source_shape = transform_shape(
            unit.local_shape,
            scale=1.0,
            tx=placement[0],
            ty=placement[1],
        )

        # Direct loop labels are the orientation/scale proof.
        for key, local_uv in unit.local_loop_uvs.items():
            source_uv = representative.loop_uvs.get(key)
            if source_uv is None:
                return None
            if (
                abs(local_uv[0] + placement[0] - source_uv[0]) > _SOURCE_LAYOUT_TOLERANCE
                or abs(local_uv[1] + placement[1] - source_uv[1]) > _SOURCE_LAYOUT_TOLERANCE
            ):
                return None
        if not _shape_coordinates_close(source_shape, representative.boundary_loops):
            return None
        if not _shape_fit_tile(source_shape, margin):
            return None
        source_shapes.append(source_shape)

        # Verify rigid overlap members share the exact source transform.
        if unit.overlap_members:
            if variant is None or len(variant.overlap_local_uvs) != len(unit.overlap_members):
                return None
            for idx_m, member in enumerate(unit.overlap_members):
                m_local_uvs = variant.overlap_local_uvs[idx_m]
                m_local_shape = variant.member_shapes[idx_m + 1]
                for key, local_uv in m_local_uvs.items():
                    source_uv = member.island.loop_uvs.get(key)
                    if source_uv is None:
                        return None
                    if (
                        abs(local_uv[0] + placement[0] - source_uv[0]) > _SOURCE_LAYOUT_TOLERANCE
                        or abs(local_uv[1] + placement[1] - source_uv[1]) > _SOURCE_LAYOUT_TOLERANCE
                    ):
                        return None
                m_source_shape = transform_shape(m_local_shape, scale=1.0, tx=placement[0], ty=placement[1])
                if not _shape_coordinates_close(m_source_shape, member.island.boundary_loops):
                    return None
                if not _shape_fit_tile(m_source_shape, margin):
                    return None
                source_shapes.append(m_source_shape)

        for sh in source_shapes:
            sb = shape_bounds(sh)
            se = _shape_edge_data(sh)
            for other, other_b, other_e, _ in index.query_entries(sb, margin=margin):
                if _shape_sets_conflict_fast(sh, sb, se, other, other_b, other_e, margin=margin):
                    return None
        for sh in source_shapes:
            index.insert(sh)
        placements[unit.key] = placement

        def _verify_followers(master_uvs, follower_mappings):
            for follower, mapping in follower_mappings:
                follower_uvs = dict(exact_follower_writes(master_uvs, mapping))
                if set(follower_uvs) != set(follower.loop_uvs):
                    return False
                for key, source_uv in follower.loop_uvs.items():
                    candidate_uv = follower_uvs[key]
                    if (
                        abs(candidate_uv[0] - source_uv[0]) > _SOURCE_LAYOUT_TOLERANCE
                        or abs(candidate_uv[1] - source_uv[1]) > _SOURCE_LAYOUT_TOLERANCE
                    ):
                        return False
                follower_shape = _shape_from_loop_uvs(follower, follower_uvs)
                if not _shape_fit_tile(follower_shape, margin):
                    return False
                if not _shape_coordinates_close(follower_shape, follower.boundary_loops):
                    return False
            return True

        master_uvs = {
            key: (local_uv[0] + placement[0], local_uv[1] + placement[1])
            for key, local_uv in unit.local_loop_uvs.items()
        }
        if not _verify_followers(master_uvs, unit.follower_mappings):
            return None
        for idx_m, member in enumerate(unit.overlap_members):
            if member.follower_mappings:
                m_master_uvs = {
                    key: (local_uv[0] + placement[0], local_uv[1] + placement[1])
                    for key, local_uv in variant.overlap_local_uvs[idx_m].items()
                }
                if not _verify_followers(m_master_uvs, member.follower_mappings):
                    return None

    return placements


def _sample_shape_vertices(shape: ShapeLoops, max_pts=10):
    pts = [p for poly in shape for p in poly]
    if len(pts) <= max_pts:
        return pts
    step = len(pts) / max_pts
    return [pts[int(i * step)] for i in range(max_pts)]


def _candidate_positions(
    width,
    height,
    obstacle_bounds,
    margin,
    seed=None,
    limit=None,
    obstacle_vertices=(),
    env_w=0.0,
    env_h=0.0,
):
    lowx = margin
    lowy = margin
    highx = 1.0 - margin - width
    highy = 1.0 - margin - height
    if highx < lowx - _EPS or highy < lowy - _EPS:
        return ()

    # Coarse tile-wide scan grid: always retained so congested bottom-left
    # obstacle tangents can never crowd out empty upper/right regions of the tile.
    grid_cands = {(lowx, lowy), (highx, lowy), (lowx, highy), (highx, highy)}
    grid_cap = 9 if (limit is None or int(limit) >= 140) else 7
    sx = min(grid_cap, max(2, int((highx - lowx) * 20) + 2))
    sy = min(grid_cap, max(2, int((highy - lowy) * 20) + 2))
    for ix in range(sx + 1):
        x = lowx if sx == 0 else lowx + (highx - lowx) * ix / sx
        for iy in range(sy + 1):
            y = lowy if sy == 0 else lowy + (highy - lowy) * iy / sy
            grid_cands.add((x, y))

    seed_value = None
    if seed is not None:
        sx0 = max(lowx, min(highx, float(seed[0])))
        sy0 = max(lowy, min(highy, float(seed[1])))
        seed_value = (sx0, sy0)

    tangent_cands = set()
    for b in obstacle_bounds:
        ominx, omaxx, ominy, omaxy = b
        xs = (omaxx + margin, ominx - width - margin, ominx, omaxx - width)
        ys = (omaxy + margin, ominy - height - margin, ominy, omaxy - height)
        for x in xs:
            if lowx - _EPS <= x <= highx + _EPS:
                cx = max(lowx, min(highx, x))
                tangent_cands.add((cx, lowy))
                for y in ys:
                    if lowy - _EPS <= y <= highy + _EPS:
                        tangent_cands.add((cx, max(lowy, min(highy, y))))
        for y in ys:
            if lowy - _EPS <= y <= highy + _EPS:
                tangent_cands.add((lowx, max(lowy, min(highy, y))))
    # Sample concave/polygon vertex tangents across all obstacles so early large
    # islands' cavities remain reachable even when many small islands follow.
    if len(obstacle_vertices) > 360:
        vstep = len(obstacle_vertices) / 360
        sampled_obs_verts = [obstacle_vertices[int(i * vstep)] for i in range(360)]
    else:
        sampled_obs_verts = obstacle_vertices
    for vx, vy in sampled_obs_verts:
        for px, py in (
            (vx + margin, vy + margin),
            (vx + margin, vy),
            (vx, vy + margin),
            (vx - width - margin, vy),
            (vx, vy - height - margin),
            (vx - width - margin, vy + margin),
            (vx + margin, vy - height - margin),
            (vx - width - margin, vy - height - margin),
        ):
            if lowx - _EPS <= px <= highx + _EPS and lowy - _EPS <= py <= highy + _EPS:
                tangent_cands.add((max(lowx, min(highx, px)), max(lowy, min(highy, py))))

    def _cand_key(p):
        ew = max(env_w, p[0] + width)
        eh = max(env_h, p[1] + height)
        return (ew * eh, max(ew, eh), p[1] + height, p[0] + width, p[1], p[0])

    # Deduplicate on a fine 0.002 UV grid so sub-0.01 strips never lose valid narrow gaps.
    seen_cells = set()
    filtered_tangents = []
    for p in sorted(tangent_cands - grid_cands, key=_cand_key):
        cell = (int(p[0] * 500.0), int(p[1] * 500.0))
        if cell in seen_cells:
            continue
        seen_cells.add(cell)
        filtered_tangents.append(p)

    sorted_grid = sorted(grid_cands, key=_cand_key)
    if limit is not None:
        cap = max(1, int(limit))
        grid_budget = min(len(sorted_grid), max(16, cap // 3))
        tangent_budget = max(0, cap - grid_budget)
        if len(sorted_grid) > grid_budget:
            step = len(sorted_grid) / grid_budget
            sampled_grid = [sorted_grid[int(i * step)] for i in range(grid_budget)]
        else:
            sampled_grid = sorted_grid
        # Keep the best bottom-left tangents PLUS strided tangents across higher Y bands
        # so upper empty pockets are still reachable when the bottom region is full.
        if len(filtered_tangents) > tangent_budget and tangent_budget > 8:
            head_cnt = tangent_budget // 2
            tail_cnt = tangent_budget - head_cnt
            rest = filtered_tangents[head_cnt:]
            t_step = len(rest) / tail_cnt
            sampled_tangents = filtered_tangents[:head_cnt] + [
                rest[int(i * t_step)] for i in range(tail_cnt)
            ]
        else:
            sampled_tangents = filtered_tangents[:tangent_budget]
        combined = sampled_tangents + sampled_grid
        if seed_value is not None:
            combined.append(seed_value)
        ordered = sorted(set(combined), key=_cand_key)
    else:
        pool = set(filtered_tangents) | grid_cands
        if seed_value is not None:
            pool.add(seed_value)
        ordered = sorted(pool, key=_cand_key)

    return tuple(ordered)


def _variant_conflicts(variant: _UnitVariant, scale: float, tx: float, ty: float, index: SpatialIndex, margin: float) -> bool:
    low = float(margin) - 1.0e-9
    high = 1.0 - float(margin) + 1.0e-9
    for b in variant.shape_bounds_list:
        if (
            b[0] * scale + tx < low
            or b[1] * scale + tx > high
            or b[2] * scale + ty < low
            or b[3] * scale + ty > high
        ):
            return True

    lm = max(0.0, float(margin))
    lm_tol = lm - _EPS
    seg_int = pack_geometry.segments_intersect
    pt_seg = pack_geometry._point_segment_distance
    for sh_local, b_local, edges_local in zip(
        variant.member_shapes,
        variant.shape_bounds_list,
        variant.shape_edges_list,
    ):
        tb0 = b_local[0] * scale + tx
        tb1 = b_local[1] * scale + tx
        tb2 = b_local[2] * scale + ty
        tb3 = b_local[3] * scale + ty
        nearby = index.query_entries((tb0, tb1, tb2, tb3), margin=lm)
        if not nearby:
            continue
        tshape = None
        for other_sh, ob, other_e, _ in nearby:
            if (
                tb1 + lm < ob[0]
                or ob[1] + lm < tb0
                or tb3 + lm < ob[2]
                or ob[3] + lm < tb2
            ):
                continue
            for poly_edges in edges_local:
                for ax, ay, bx, by, lminx, lmaxx, lminy, lmaxy in poly_edges:
                    lax0 = lminx * scale + tx
                    lax1 = lmaxx * scale + tx
                    lay0 = lminy * scale + ty
                    lay1 = lmaxy * scale + ty
                    a = (ax * scale + tx, ay * scale + ty)
                    b = (bx * scale + tx, by * scale + ty)
                    for re in other_e:
                        for cx, cy, dx, dy, rminx, rmaxx, rminy, rmaxy in re:
                            if (
                                lax1 + lm < rminx
                                or rmaxx + lm < lax0
                                or lay1 + lm < rminy
                                or rmaxy + lm < lay0
                            ):
                                continue
                            c = (cx, cy)
                            d = (dx, dy)
                            if seg_int(a, b, c, d):
                                return True
                            if lm > 0.0 and (
                                pt_seg(a, c, d) < lm_tol
                                or pt_seg(b, c, d) < lm_tol
                                or pt_seg(c, a, b) < lm_tol
                                or pt_seg(d, a, b) < lm_tol
                            ):
                                return True
            if tshape is None:
                tshape = transform_shape(sh_local, scale=scale, tx=tx, ty=ty)
            for poly in tshape:
                if poly:
                    if point_in_shape(poly[0], other_sh):
                        return True
                    if len(poly) > 2 and point_in_shape(poly[len(poly) // 2], other_sh):
                        return True
            for poly in other_sh:
                if poly:
                    if point_in_shape(poly[0], tshape):
                        return True
                    if len(poly) > 2 and point_in_shape(poly[len(poly) // 2], tshape):
                        return True
    return False


def _compact_placement(variant: _UnitVariant, scale: float, x: float, y: float, index: SpatialIndex, margin: float) -> tuple[float, float]:
    low = float(margin)
    if x > low + 1.0e-5:
        probe_x = max(low, x - 0.008)
        if _variant_conflicts(variant, scale, probe_x, y, index, margin):
            lo_x, hi_x = probe_x, x
            for _ in range(3):
                mid_x = (lo_x + hi_x) * 0.5
                if _variant_conflicts(variant, scale, mid_x, y, index, margin):
                    lo_x = mid_x
                else:
                    hi_x = mid_x
            x = hi_x
        elif probe_x <= low + 1.0e-6 or not _variant_conflicts(variant, scale, low, y, index, margin):
            x = low
        else:
            lo_x, hi_x = low, probe_x
            for _ in range(5):
                mid_x = (lo_x + hi_x) * 0.5
                if _variant_conflicts(variant, scale, mid_x, y, index, margin):
                    lo_x = mid_x
                else:
                    hi_x = mid_x
            x = hi_x
    if y > low + 1.0e-5:
        probe_y = max(low, y - 0.008)
        if _variant_conflicts(variant, scale, x, probe_y, index, margin):
            lo_y, hi_y = probe_y, y
            for _ in range(3):
                mid_y = (lo_y + hi_y) * 0.5
                if _variant_conflicts(variant, scale, x, mid_y, index, margin):
                    lo_y = mid_y
                else:
                    hi_y = mid_y
            y = hi_y
        elif probe_y <= low + 1.0e-6 or not _variant_conflicts(variant, scale, x, low, index, margin):
            y = low
        else:
            lo_y, hi_y = low, probe_y
            for _ in range(5):
                mid_y = (lo_y + hi_y) * 0.5
                if _variant_conflicts(variant, scale, x, mid_y, index, margin):
                    lo_y = mid_y
                else:
                    hi_y = mid_y
            y = hi_y
    # Second slide-left pass after settling vertically into a cavity.
    if x > low + 1.0e-4:
        probe_x = max(low, x - 0.008)
        if not _variant_conflicts(variant, scale, probe_x, y, index, margin):
            if probe_x <= low + 1.0e-6 or not _variant_conflicts(variant, scale, low, y, index, margin):
                x = low
            else:
                lo_x, hi_x = low, probe_x
                for _ in range(5):
                    mid_x = (lo_x + hi_x) * 0.5
                    if _variant_conflicts(variant, scale, mid_x, y, index, margin):
                        lo_x = mid_x
                    else:
                        hi_x = mid_x
                x = hi_x
    return x, y


def _compact_placement_dir(
    variant: _UnitVariant,
    scale: float,
    x: float,
    y: float,
    index: SpatialIndex,
    margin: float,
    mode: str = "bl_bot",
) -> tuple[float, float]:
    """Slide a valid placement flush along its sweep corner directions."""
    low = float(margin)
    hx = max(low, 1.0 - low - variant.width * scale)
    hy = max(low, 1.0 - low - variant.height * scale)
    tx_goal = hx if mode in ("br_bot", "tr_top") else low
    ty_goal = hy if mode in ("tl_top", "tr_top") else low

    def _slide_axis(cx: float, cy: float, horizontal: bool) -> float:
        cur = cx if horizontal else cy
        goal = tx_goal if horizontal else ty_goal
        hi_lim = hx if horizontal else hy
        if abs(cur - goal) <= 1.0e-4:
            return cur
        move_low = goal <= low + 1.0e-9
        step = -0.008 if move_low else 0.008
        probe = max(low, min(hi_lim, cur + step))
        qx, qy = (probe, cy) if horizontal else (cx, probe)
        if _variant_conflicts(variant, scale, qx, qy, index, margin):
            lo, hi = (probe, cur) if move_low else (cur, probe)
            for _ in range(3):
                mid = (lo + hi) * 0.5
                mx, my = (mid, cy) if horizontal else (cx, mid)
                if _variant_conflicts(variant, scale, mx, my, index, margin):
                    if move_low:
                        lo = mid
                    else:
                        hi = mid
                else:
                    if move_low:
                        hi = mid
                    else:
                        lo = mid
            return hi if move_low else lo
        gx, gy = (goal, cy) if horizontal else (cx, goal)
        if not _variant_conflicts(variant, scale, gx, gy, index, margin):
            return goal
        lo, hi = (goal, probe) if move_low else (probe, goal)
        for _ in range(4):
            mid = (lo + hi) * 0.5
            mx, my = (mid, cy) if horizontal else (cx, mid)
            if _variant_conflicts(variant, scale, mx, my, index, margin):
                if move_low:
                    lo = mid
                else:
                    hi = mid
            else:
                if move_low:
                    hi = mid
                else:
                    lo = mid
        return hi if move_low else lo

    if mode in ("lb_left", "lb_right"):
        x = _slide_axis(x, y, True)
        y = _slide_axis(x, y, False)
        x = _slide_axis(x, y, True)
    else:
        y = _slide_axis(x, y, False)
        x = _slide_axis(x, y, True)
        y = _slide_axis(x, y, False)
    return x, y


def _order_units(units, order_mode="area"):
    if order_mode == "hull":
        return sorted(
            units,
            key=lambda u: (-max(u.hull_area, u.area), -max(u.width, u.height), u.key),
        )
    if order_mode == "outer_hole":
        return sorted(
            units,
            key=lambda u: (
                -max(u.outer_area, u.area) * (1.6 if u.has_hole else 1.0),
                -max(u.width, u.height),
                u.key,
            ),
        )
    if order_mode == "strips_hull":
        def _strip_key(u):
            maxd = max(u.width, u.height)
            mind = max(min(u.width, u.height), 1.0e-9)
            is_strip = 0 if (maxd > 0.30 and (maxd / mind) > 3.5) else 1
            return (is_strip, -max(u.hull_area, u.area), -maxd, u.key)
        return sorted(units, key=_strip_key)
    if order_mode == "max_dim":
        return sorted(units, key=lambda u: (-max(u.width, u.height), -u.area, u.key))
    if order_mode == "height":
        return sorted(units, key=lambda u: (-u.height, -u.width, -u.area, u.key))
    if order_mode == "bbox_area":
        return sorted(units, key=lambda u: (-u.width * u.height, -u.area, u.key))
    if order_mode == "perim":
        return sorted(units, key=lambda u: (-(u.width + u.height), -u.area, u.key))
    return sorted(units, key=lambda u: (-u.area, -max(u.width, u.height), u.key))


class _DualBitmaskTile:
    """High-resolution dual bitmask occupancy grid for fast hole/pocket discovery."""

    __slots__ = ("size", "margin", "rows_raw", "rows_pad", "pad_cells")

    def __init__(self, size: int = 288, margin: float = 0.0):
        self.size = max(64, int(size))
        self.margin = max(0.0, float(margin))
        self.rows_raw = [0] * self.size
        self.pad_cells = min(4, max(0, int(math.floor(self.margin * self.size + 0.25))))
        self.rows_pad = [0] * self.size if self.pad_cells > 0 else self.rows_raw

    def _rasterize_shape(self, shape: ShapeLoops, scale: float, ox: float, oy: float, num_rows: int, num_cols: int):
        rows = [0] * num_rows
        n = self.size
        inv_n = 1.0 / n
        max_x_tile = num_cols * inv_n
        max_y_tile = num_rows * inv_n
        for poly in shape:
            if len(poly) < 3:
                continue
            pts = [(ox + p[0] * scale, oy + p[1] * scale) for p in poly]
            ymin = min(p[1] for p in pts)
            ymax = max(p[1] for p in pts)
            r0 = max(0, int(math.floor(ymin * n)))
            r1 = min(num_rows - 1, int(math.floor(ymax * n)))
            edges = list(zip(pts, pts[1:] + pts[:1]))
            for r in range(r0, r1 + 1):
                y = (r + 0.5) * inv_n
                xs = []
                for (ax, ay), (bx, by) in edges:
                    if (ay <= y < by) or (by <= y < ay):
                        xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
                if len(xs) >= 2:
                    xs.sort()
                    for i in range(0, len(xs) - 1, 2):
                        c0 = max(0, int(math.floor(xs[i] * n)))
                        c1 = min(num_cols - 1, int(math.floor(xs[i + 1] * n)))
                        if c1 >= c0:
                            rows[r] ^= ((1 << (c1 - c0 + 1)) - 1) << c0
            for (ax, ay), (bx, by) in edges:
                if max(ax, bx) < 0.0 or min(ax, bx) > max_x_tile or max(ay, by) < 0.0 or min(ay, by) > max_y_tile:
                    continue
                steps = max(1, min(n * 2, int(max(abs(bx - ax), abs(by - ay)) * n) + 1))
                for k in range(steps + 1):
                    t = k / steps
                    cr = int((ay + (by - ay) * t) * n)
                    cc = int((ax + (bx - ax) * t) * n)
                    if 0 <= cr < num_rows and 0 <= cc < num_cols:
                        rows[cr] |= 1 << cc
        return rows

    def _rasterize_shape_center(self, shape: ShapeLoops, scale: float, ox: float, oy: float, num_rows: int, num_cols: int):
        rows = [0] * num_rows
        n = self.size
        inv_n = 1.0 / n
        max_x_tile = num_cols * inv_n
        max_y_tile = num_rows * inv_n
        for poly in shape:
            if len(poly) < 3:
                continue
            pts = [(ox + p[0] * scale, oy + p[1] * scale) for p in poly]
            xmin = min(p[0] for p in pts)
            xmax = max(p[0] for p in pts)
            ymin = min(p[1] for p in pts)
            ymax = max(p[1] for p in pts)
            r0 = max(0, int(math.floor(ymin * n)))
            r1 = min(num_rows - 1, int(math.floor(ymax * n)))
            edges = list(zip(pts, pts[1:] + pts[:1]))
            for r in range(r0, r1 + 1):
                y = (r + 0.5) * inv_n
                xs = []
                for (ax, ay), (bx, by) in edges:
                    if (ay <= y < by) or (by <= y < ay):
                        xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
                if len(xs) >= 2:
                    xs.sort()
                    for i in range(0, len(xs) - 1, 2):
                        c0 = max(0, int(math.ceil(xs[i] * n - 0.5)))
                        c1 = min(num_cols - 1, int(math.floor(xs[i + 1] * n - 0.5)))
                        if c1 >= c0:
                            rows[r] ^= ((1 << (c1 - c0 + 1)) - 1) << c0
            # Safeguard ultra-thin needles/ribbons narrower than 1.5 grid cells so
            # scanline center parity never drops thin prongs or slivers.
            if min(xmax - xmin, ymax - ymin) * n < 1.5 or not any(rows):
                for (ax, ay), (bx, by) in edges:
                    if max(ax, bx) < 0.0 or min(ax, bx) > max_x_tile or max(ay, by) < 0.0 or min(ay, by) > max_y_tile:
                        continue
                    steps = max(1, min(n * 2, int(max(abs(bx - ax), abs(by - ay)) * n * 1.5) + 1))
                    for k in range(steps + 1):
                        t = k / steps
                        cr = int((ay + (by - ay) * t) * n)
                        cc = int((ax + (bx - ax) * t) * n)
                        if 0 <= cr < num_rows and 0 <= cc < num_cols:
                            rows[cr] |= 1 << cc
        return rows

    def _rasterize_union_center(self, shapes: Iterable[ShapeLoops], scale: float, ox: float, oy: float, num_rows: int, num_cols: int):
        rows = [0] * num_rows
        for sh in shapes:
            sr = self._rasterize_shape_center(sh, scale, ox, oy, num_rows, num_cols)
            for r, m in enumerate(sr):
                if m:
                    rows[r] |= m
        if not any(rows) and num_rows > 0 and num_cols > 0:
            rows[0] = 1
        return rows

    def _rasterize_union(self, shapes: Iterable[ShapeLoops], scale: float, ox: float, oy: float, num_rows: int, num_cols: int):
        rows = [0] * num_rows
        for sh in shapes:
            sr = self._rasterize_shape(sh, scale, ox, oy, num_rows, num_cols)
            for r, m in enumerate(sr):
                if m:
                    rows[r] |= m
        return rows

    def insert_shape(self, shape: ShapeLoops):
        mr = self._rasterize_shape_center(shape, 1.0, 0.0, 0.0, self.size, self.size)
        for r, m in enumerate(mr):
            if m:
                self.rows_raw[r] |= m
        if self.pad_cells > 0:
            mask_col = (1 << self.size) - 1
            pr = list(mr)
            for r in range(self.size):
                v = pr[r]
                if v:
                    for _ in range(self.pad_cells):
                        v |= ((v << 1) & mask_col) | (v >> 1)
                    pr[r] = v
            rows_pad = self.rows_pad
            for dr in range(-self.pad_cells, self.pad_cells + 1):
                for r in range(self.size):
                    sr = r + dr
                    if 0 <= sr < self.size and pr[sr]:
                        rows_pad[r] |= pr[sr]

    @staticmethod
    def _smear_left(mask: int, width: int) -> int:
        if width <= 1 or mask == 0:
            return mask
        rem = width - 1
        shift = 1
        m = mask
        while rem:
            step = shift if shift < rem else rem
            m |= m >> step
            rem -= step
            shift <<= 1
        return m

    @staticmethod
    def _extract_runs(row_mask: int):
        runs = []
        m = row_mask
        while m:
            lsb = m & -m
            start = lsb.bit_length() - 1
            shifted = m >> start
            inv = ~shifted
            zero_lsb = inv & -inv
            run_len = zero_lsb.bit_length() - 1
            runs.append((start, run_len))
            m &= ~(((1 << run_len) - 1) << start)
        return runs

    def prepare_piece_full(self, shapes: Iterable[ShapeLoops], width: float, height: float, scale: float):
        n = self.size
        pw = min(n, max(1, int(math.ceil(width * scale * n - 0.05))))
        ph = min(n, max(1, int(math.ceil(height * scale * n - 0.05))))
        prows = self._rasterize_union_center(shapes, scale, 0.0, 0.0, ph, pw)
        non_empty = [(dr, tuple(self._extract_runs(m))) for dr, m in enumerate(prows) if m]
        non_empty.sort(key=lambda item: -max(rl for _, rl in item[1]))
        return pw, ph, tuple(non_empty), tuple(prows)

    def prepare_piece(self, shapes: Iterable[ShapeLoops], width: float, height: float, scale: float):
        pw, ph, non_empty, _prows = self.prepare_piece_full(shapes, width, height, scale)
        return pw, ph, non_empty

    def find_candidates(self, piece_info, width: float, height: float, scale: float, use_pad: bool = True):
        pw, ph, row_runs = piece_info
        if not row_runs:
            return ()
        n = self.size
        m_cells = max(0, int(math.floor(self.margin * n)))
        r_lo = m_cells
        r_hi = n - m_cells - ph
        c_lo = m_cells
        c_hi = n - m_cells - pw
        if r_hi < r_lo or c_hi < c_lo:
            return ()
        valid_col_mask = ((1 << (c_hi - c_lo + 1)) - 1) << c_lo
        rows = self.rows_pad if use_pad else self.rows_raw
        smear = self._smear_left
        smear_cache = {}
        inv_n = 1.0 / n
        low_x = self.margin
        low_y = self.margin
        high_x = max(low_x, 1.0 - self.margin - width * scale)
        high_y = max(low_y, 1.0 - self.margin - height * scale)

        free_by_row = []
        for r0 in range(r_lo, r_hi + 1):
            free_cols = valid_col_mask
            for dr, runs in row_runs:
                brow = rows[r0 + dr]
                if not brow:
                    continue
                for dx, rlen in runs:
                    key = (r0 + dr, rlen)
                    sm = smear_cache.get(key)
                    if sm is None:
                        sm = smear(brow, rlen)
                        smear_cache[key] = sm
                    free_cols &= ~(sm >> dx)
                    if not free_cols:
                        break
                if not free_cols:
                    break
            free_by_row.append(free_cols)

        cands = []
        seen_cell = set()
        prev_free = 0
        n_rows = len(free_by_row)
        for idx, fc in enumerate(free_by_row):
            if not fc:
                prev_free = 0
                continue
            next_free = free_by_row[idx + 1] if idx + 1 < n_rows else 0
            r0 = r_lo + idx
            lr_edges = (fc & ~(fc << 1)) | (fc & ~(fc >> 1))
            tb_edges = (fc & ~prev_free) | (fc & ~next_free)
            corners = lr_edges | tb_edges
            true_corners = lr_edges & tb_edges
            m = corners
            while m:
                lsb = m & -m
                c0 = lsb.bit_length() - 1
                m ^= lsb
                bucket = (c0, r0) if (true_corners & lsb) else (c0 // 2, r0 // 2)
                if bucket in seen_cell:
                    continue
                seen_cell.add(bucket)
                tx = max(low_x, min(high_x, c0 * inv_n))
                ty = max(low_y, min(high_y, r0 * inv_n))
                cands.append((tx, ty))
            prev_free = fc
        return cands


_RasterTile = _DualBitmaskTile


def _unit_variants_for_layout(unit: PackUnit) -> tuple[_UnitVariant, ...]:
    if unit.variants:
        return unit.variants
    shapes = unit.member_shapes or ((unit.local_shape,) if unit.local_shape else ())
    return (
        _UnitVariant(
            angle=0.0,
            width=unit.width,
            height=unit.height,
            local_loop_uvs=unit.local_loop_uvs,
            member_shapes=shapes,
            shape_bounds_list=tuple(shape_bounds(s) for s in shapes),
            shape_edges_list=tuple(_shape_edge_data(s) for s in shapes),
        ),
    )


def _try_general_layout_once(
    ordered,
    static_shapes,
    scale,
    margin,
    progress_cb=None,
    score_kind="env",
    grid_size=288,
    deadline=None,
    allow_tangent_fallback=True,
):
    index = SpatialIndex(44)
    tile = _DualBitmaskTile(size=grid_size, margin=margin)
    obstacle_bounds = []
    obstacle_vertices = []
    for shape in static_shapes:
        index.insert(shape)
        tile.insert_shape(shape)
        if allow_tangent_fallback:
            obstacle_bounds.append(shape_bounds(shape))
            obstacle_vertices.extend(_sample_shape_vertices(shape, max_pts=8))
    placements = {}
    total = len(ordered)
    env_w = margin
    env_h = margin
    high_limit_margin = 1.0 - 2.0 * margin + _EPS
    nudge_step = max(margin + 2.0e-4, 0.25 / grid_size)
    fwd_nudges = (
        (0.0, 0.0),
        (nudge_step, 0.0),
        (0.0, nudge_step),
        (nudge_step, nudge_step),
    )
    all_nudges = fwd_nudges + (
        (-nudge_step, 0.0),
        (0.0, -nudge_step),
        (nudge_step, -nudge_step),
        (-nudge_step, nudge_step),
    )

    for pos, unit in enumerate(ordered):
        if deadline is not None and time.perf_counter() > deadline:
            return None, pos, unit
        variants = _unit_variants_for_layout(unit)
        if len(variants) > 1:
            min_box = min(v.width * v.height for v in variants)
            primary_variants = [v for v in variants if v.width * v.height <= min_box * 1.18]
            fallback_variants = [v for v in variants if v.width * v.height > min_box * 1.18]
            if not primary_variants:
                primary_variants = list(variants)
                fallback_variants = []
        else:
            primary_variants = list(variants)
            fallback_variants = []
        max_primary = 4 if (len(primary_variants) > 4 and pos >= 24 and unit.area < 0.008) else 6
        eval_variants = primary_variants[:max_primary]
        extra_variants = fallback_variants[:2]
        use_box_tiebreak = len(variants) > 4

        best_choice = None
        for pass_variants in (eval_variants, extra_variants):
            if not pass_variants or best_choice is not None:
                continue
            for var in pass_variants:
                vw = var.width * scale
                vh = var.height * scale
                if vw > high_limit_margin or vh > high_limit_margin:
                    continue
                pinfo = tile.prepare_piece(var.member_shapes, var.width, var.height, scale)
                use_modes = (True, False) if tile.pad_cells > 0 else (False,)
                high_x = max(margin, 1.0 - margin - vw)
                high_y = max(margin, 1.0 - margin - vh)
                var_box_pen = 0.08 * (vw * vh) if use_box_tiebreak else 0.0
                var_placed = False

                for use_pad in use_modes:
                    cands = tile.find_candidates(pinfo, var.width, var.height, scale, use_pad=use_pad)
                    if not cands:
                        continue
                    scored = []
                    for tx, ty in cands:
                        nw = env_w if tx + vw <= env_w else tx + vw
                        nh = env_h if ty + vh <= env_h else ty + vh
                        base_area = nw * nh + var_box_pen
                        if score_kind == "bl_x":
                            skey = (base_area, max(nw, nh), tx + vw, ty + vh)
                        else:
                            skey = (base_area, max(nw, nh), ty + vh, tx + vw)
                        scored.append((skey, tx, ty))
                    scored.sort()
                    for cand_idx, (_skey, tx, ty) in enumerate(scored[:110]):
                        if deadline is not None and time.perf_counter() > deadline:
                            return None, pos, unit
                        nudges = ((0.0, 0.0),) if use_pad else (all_nudges if cand_idx < 18 else fwd_nudges)
                        ok_pos = None
                        tested_pts = set()
                        for dx, dy in nudges:
                            qx = min(max(margin, tx + dx), high_x)
                            qy = min(max(margin, ty + dy), high_y)
                            qkey = (round(qx, 7), round(qy, 7))
                            if qkey in tested_pts:
                                continue
                            tested_pts.add(qkey)
                            if not _variant_conflicts(var, scale, qx, qy, index, margin):
                                ok_pos = (qx, qy)
                                break
                        if ok_pos is None:
                            continue
                        cx, cy = _compact_placement(var, scale, ok_pos[0], ok_pos[1], index, margin)
                        nw = max(env_w, cx + vw)
                        nh = max(env_h, cy + vh)
                        base_area = nw * nh + var_box_pen
                        if score_kind == "bl_x":
                            sc = (base_area, max(nw, nh), cx + vw, cy + vh)
                        else:
                            sc = (base_area, max(nw, nh), cy + vh, cx + vw)
                        if best_choice is None or sc < best_choice[0]:
                            best_choice = (sc, cx, cy, var)
                        var_placed = True
                        break
                    if var_placed:
                        break

                if not var_placed and allow_tangent_fallback:
                    geom_cands = _candidate_positions(
                        vw,
                        vh,
                        obstacle_bounds,
                        margin,
                        limit=96,
                        obstacle_vertices=obstacle_vertices,
                        env_w=env_w,
                        env_h=env_h,
                    )
                    for x, y in geom_cands:
                        if deadline is not None and time.perf_counter() > deadline:
                            return None, pos, unit
                        if _variant_conflicts(var, scale, x, y, index, margin):
                            continue
                        cx, cy = _compact_placement(var, scale, x, y, index, margin)
                        nw = max(env_w, cx + vw)
                        nh = max(env_h, cy + vh)
                        base_area = nw * nh + var_box_pen
                        sc = (base_area, max(nw, nh), cx + vw, cy + vh) if score_kind == "bl_x" else (base_area, max(nw, nh), cy + vh, cx + vw)
                        if best_choice is None or sc < best_choice[0]:
                            best_choice = (sc, cx, cy, var)
                        break

        if best_choice is None:
            return None, pos, unit

        _, px, py, chosen_var = best_choice
        for sh_local in chosen_var.member_shapes:
            placed_sh = transform_shape(sh_local, scale=scale, tx=px, ty=py)
            index.insert(placed_sh)
            tile.insert_shape(placed_sh)
            if allow_tangent_fallback:
                obstacle_bounds.append(shape_bounds(placed_sh))
                obstacle_vertices.extend(_sample_shape_vertices(placed_sh, max_pts=4))
        env_w = max(env_w, px + chosen_var.width * scale)
        env_h = max(env_h, py + chosen_var.height * scale)
        placements[unit.key] = (px, py, chosen_var)
        if progress_cb is not None and (pos % 12 == 0 or pos + 1 == total):
            _progress(progress_cb, stage="placing", percent=20.0 + 55.0 * (pos + 1) / max(1, total), done=pos + 1, total=max(1, total))
    return placements, total, None


def _insert_board_rows(
    prows: Iterable[int],
    c0: int,
    r0: int,
    n: int,
    pad_cells: int,
    mask_full: int,
    rows_raw: list[int],
    rows_pad: list[int],
    row_ver_raw: list[int],
    row_ver_pad: list[int],
) -> None:
    """Bit-shift rasterized piece rows into raw and cross-dilated board bitmasks."""
    for dr, m in enumerate(prows):
        if not m:
            continue
        rr = r0 + dr
        if rr < 0 or rr >= n:
            continue
        s = (m << c0) & mask_full if c0 >= 0 else ((m >> (-c0)) & mask_full)
        if not s:
            continue
        old = rows_raw[rr]
        new = old | s
        if new != old:
            rows_raw[rr] = new
            row_ver_raw[rr] += 1
        if pad_cells > 0:
            h = s | ((s << 1) & mask_full) | (s >> 1)
            for _ in range(pad_cells - 1):
                h |= ((h << 1) & mask_full) | (h >> 1)
            for off in range(-pad_cells, pad_cells + 1):
                tr = rr + off
                if 0 <= tr < n:
                    # Exact 4-neighbor cross when pad_cells == 1 avoids diagonal over-bloat.
                    add = s if (pad_cells == 1 and abs(off) == 1) else h
                    op = rows_pad[tr]
                    np_val = op | add
                    if np_val != op:
                        rows_pad[tr] = np_val
                        row_ver_pad[tr] += 1


def _extract_bay_holes(prows: tuple[int, ...], pw: int, ph: int, n: int) -> tuple[tuple[float, float, float, float], ...]:
    """Detect up to 2 large enclosed/U-pocket bays directly from rasterized piece bits."""
    if pw < 18 or ph < 18:
        return ()
    total_bits = sum(m.bit_count() for m in prows)
    if total_bits >= 0.74 * pw * ph:
        return ()
    best_spans = []
    cur = None
    min_span = max(6, pw // 5)
    for r in range(1, ph - 1):
        m = prows[r]
        if not m:
            if cur is not None:
                best_spans.append(cur)
                cur = None
            continue
        c_l = (m & -m).bit_length() - 1
        c_r = m.bit_length() - 1
        if c_r - c_l + 1 < min_span:
            if cur is not None:
                best_spans.append(cur)
                cur = None
            continue
        inner_zeros = ((((1 << (c_r - c_l + 1)) - 1) << c_l) & ~m)
        span = None
        zm = inner_zeros
        while zm:
            lsb = zm & -zm
            s0 = lsb.bit_length() - 1
            shifted = zm >> s0
            inv = ~shifted
            run_len = (inv & -inv).bit_length() - 1
            zm &= ~(((1 << run_len) - 1) << s0)
            if run_len >= min_span and (span is None or run_len > span[2]):
                span = (s0, s0 + run_len, run_len)
        if span is not None:
            if cur is not None and span[0] <= cur[1] and span[1] >= cur[0]:
                cur = (min(cur[0], span[0]), max(cur[1], span[1]), cur[2], r + 1)
            else:
                if cur is not None:
                    best_spans.append(cur)
                cur = (span[0], span[1], r, r + 1)
        elif cur is not None:
            best_spans.append(cur)
            cur = None
    if cur is not None:
        best_spans.append(cur)

    inv_n = 1.0 / n
    bays = []
    for c0, c1, r0, r1 in best_spans:
        area = ((c1 - c0) * inv_n) * ((r1 - r0) * inv_n)
        if area >= 0.0012 and (c1 - c0) >= min_span and (r1 - r0) >= 6:
            bays.append((area, c0 * inv_n, c1 * inv_n, r0 * inv_n, r1 * inv_n))
    bays.sort(key=lambda item: -item[0])
    return tuple((b[1], b[2], b[3], b[4]) for b in bays[:2])


def _build_scale_piece_cache(units: Iterable[PackUnit], scale: float, margin: float, grid_size: int):
    """Precompute center-rasterized bitmasks, run signatures, and hole rects once per scale."""
    tile = _DualBitmaskTile(size=grid_size, margin=margin)
    limit = 1.0 - 2.0 * margin + 1.0e-9
    cache = {}
    for unit in units:
        entries = []
        seen_bm = set()
        for var in _unit_variants_for_layout(unit):
            vw = var.width * scale
            vh = var.height * scale
            if vw > limit or vh > limit:
                continue
            pw, ph, rruns, prows = tile.prepare_piece_full(var.member_shapes, var.width, var.height, scale)
            stride = max(1, ph // 8)
            bm_key = (pw, ph, prows[::stride])
            if bm_key in seen_bm:
                continue
            seen_bm.add(bm_key)
            scaled_holes = [
                (hx0 * scale, hx1 * scale, hy0 * scale, hy1 * scale)
                for hx0, hx1, hy0, hy1 in var.inner_holes
            ]
            bay_holes = _extract_bay_holes(prows, pw, ph, grid_size)
            for bx0, bx1, by0, by1 in bay_holes:
                if not any(
                    abs(bx0 - hx0) < 0.015 and abs(by0 - hy0) < 0.015
                    and abs(bx1 - hx1) < 0.015 and abs(by1 - hy1) < 0.015
                    for hx0, hx1, hy0, hy1 in scaled_holes
                ):
                    scaled_holes.append((bx0, bx1, by0, by1))
            entries.append((var, (pw, ph, rruns, prows, tuple(scaled_holes))))
        cache[unit.key] = tuple(entries)
    return cache


def _try_general_layout_v3_once(
    ordered,
    static_shapes,
    pcache,
    scale: float,
    margin: float,
    grid_size: int = 256,
    mode: str = "bl_bot",
    max_vars: int = 14,
    deadline: float | None = None,
    allow_tangent_fallback: bool = False,
):
    """Winner-only-compact multi-row bitmask sweep with O(0.1ms) interior hole nesting."""
    n = max(64, int(grid_size))
    tile = _DualBitmaskTile(size=n, margin=margin)
    index = SpatialIndex(44)
    rows_raw = [0] * n
    rows_pad = [0] * n
    row_ver_raw = [0] * n
    row_ver_pad = [0] * n
    scache_raw = {}
    scache_pad = {}
    mf = (1 << n) - 1
    smear = _DualBitmaskTile._smear_left
    mc = max(0, int(math.floor(margin * n)))
    inv_n = 1.0 / n
    pad_cells = min(4, max(1, int(math.ceil(margin * n - 0.1)))) if margin > 1.0e-6 else 0
    nd = max(margin + 2.0e-4, 0.55 * inv_n)
    nudges_pad = ((0.0, 0.0), (nd * 0.45, 0.0), (0.0, nd * 0.45), (-nd * 0.45, 0.0), (0.0, -nd * 0.45))
    nudges_raw = ((0.0, 0.0), (nd, 0.0), (0.0, nd), (-nd, 0.0), (0.0, -nd), (nd, nd), (nd, -nd), (-nd, nd))

    obstacle_bounds = []
    obstacle_vertices = []
    active_holes = []
    for shape in static_shapes:
        index.insert(shape)
        sb = shape_bounds(shape)
        if allow_tangent_fallback:
            obstacle_bounds.append(sb)
            obstacle_vertices.extend(_sample_shape_vertices(shape, max_pts=8))
        if (
            sb[1] > -margin - 0.01
            and sb[0] < 1.0 + margin + 0.01
            and sb[3] > -margin - 0.01
            and sb[2] < 1.0 + margin + 0.01
        ):
            sr = tile._rasterize_shape_center(shape, 1.0, 0.0, 0.0, n, n)
            _insert_board_rows(sr, 0, 0, n, pad_cells, mf, rows_raw, rows_pad, row_ver_raw, row_ver_pad)
            for hx0, hx1, hy0, hy1 in _extract_shape_holes((shape,)):
                cx0 = max(margin, hx0)
                cx1 = min(1.0 - margin, hx1)
                cy0 = max(margin, hy0)
                cy1 = min(1.0 - margin, hy1)
                if cx1 - cx0 > 2.0 * margin + 1.0e-3 and cy1 - cy0 > 2.0 * margin + 1.0e-3:
                    active_holes.append((cx0, cx1, cy0, cy1))

    placements = {}
    total = len(ordered)
    high_lim = 1.0 - 2.0 * margin + _EPS
    env_w = margin
    env_h = margin
    env_c = mc
    env_r = mc

    def _diverse_hits(vhits, cap):
        if cap <= 0:
            return []
        vhits.sort(key=lambda x: x[0])
        if len(vhits) <= cap:
            return vhits
        head_cnt = max(1, min(cap, max(4, cap // 2)))
        head = vhits[:head_cnt]
        tail_cnt = cap - head_cnt
        if tail_cnt <= 0:
            return head
        rest = vhits[head_cnt:]
        step = len(rest) / tail_cnt
        return head + [rest[int(i * step)] for i in range(tail_cnt)]

    for pos, unit in enumerate(ordered):
        if deadline is not None and time.perf_counter() > deadline:
            return None, pos, unit
        entries = pcache.get(unit.key)
        if not entries:
            return None, pos, unit
        var_cap = min(max_vars, 5 if (max_vars > 5 and pos >= 20) else max_vars)
        eval_entries = entries[:var_cap]
        chosen = None

        # 1) Explicit interior hole / bay nesting check for small/medium islands.
        if active_holes and unit.area < 0.025:
            for hx0, hx1, hy0, hy1 in active_holes[:16]:
                hw = hx1 - hx0
                hh = hy1 - hy0
                for var, (pw, ph, rruns, prows, vholes) in eval_entries[:4]:
                    vw = var.width * scale
                    vh = var.height * scale
                    if vw + 2.0 * margin > hw or vh + 2.0 * margin > hh:
                        continue
                    r_lo = max(mc, int(math.floor((hy0 + margin) * n)))
                    r_hi = min(n - mc - ph, int(math.ceil((hy1 - margin - vh) * n)))
                    c_lo = max(mc, int(math.floor((hx0 + margin) * n)))
                    c_hi = min(n - mc - pw, int(math.ceil((hx1 - margin - vw) * n)))
                    if r_hi < r_lo or c_hi < c_lo:
                        continue
                    vcm = ((1 << (c_hi - c_lo + 1)) - 1) << c_lo
                    hx_tile = max(margin, 1.0 - margin - vw)
                    hy_tile = max(margin, 1.0 - margin - vh)
                    for r0 in range(r_lo, r_hi + 1):
                        fc = vcm
                        for dr, runs in rruns:
                            rr = r0 + dr
                            brow = rows_raw[rr]
                            if not brow:
                                continue
                            rv = row_ver_raw[rr]
                            for dx, rlen in runs:
                                k = (rr, rv, rlen)
                                sm = scache_raw.get(k)
                                if sm is None:
                                    sm = smear(brow, rlen)
                                    scache_raw[k] = sm
                                fc &= ~(sm >> dx)
                                if not fc:
                                    break
                            if not fc:
                                break
                        if not fc:
                            continue
                        c0 = (fc & -fc).bit_length() - 1
                        tx = min(max(margin, c0 * inv_n), hx_tile)
                        ty = min(max(margin, r0 * inv_n), hy_tile)
                        for dx, dy in nudges_raw:
                            qx = min(max(margin, tx + dx), hx_tile)
                            qy = min(max(margin, ty + dy), hy_tile)
                            if not _variant_conflicts(var, scale, qx, qy, index, margin):
                                cx, cy = _compact_placement_dir(var, scale, qx, qy, index, margin, "bl_bot")
                                chosen = (cx, cy, var, prows, vholes)
                                break
                        if chosen is not None:
                            break
                    if chosen is not None:
                        break
                if chosen is not None:
                    break

        # 2) Multi-row bitmask sweep ranked across variants, verifying only exact-bound hits.
        #    Note: never tighten a sweep bound from unverified bitmask hits across variants.
        if chosen is None:
            for use_pad in ((True, False) if pad_cells > 0 else (False,)):
                rows = rows_pad if use_pad else rows_raw
                rver = row_ver_pad if use_pad else row_ver_raw
                scache = scache_pad if use_pad else scache_raw
                nudges = nudges_pad if use_pad else nudges_raw
                bm_hits = []
                rows_per_var = 6 if use_pad else 14
                for var, (pw, ph, rruns, prows, vholes) in eval_entries:
                    if deadline is not None and time.perf_counter() > deadline:
                        return None, pos, unit
                    vw = var.width * scale
                    vh = var.height * scale
                    if vw > high_lim or vh > high_lim:
                        continue
                    hx = max(margin, 1.0 - margin - vw)
                    hy = max(margin, 1.0 - margin - vh)
                    r_lo = mc
                    r_hi = n - mc - ph
                    c_lo = mc
                    c_hi = n - mc - pw
                    if r_hi < r_lo or c_hi < c_lo:
                        continue
                    vcm = ((1 << (c_hi - c_lo + 1)) - 1) << c_lo
                    r_iter = (
                        range(r_hi, r_lo - 1, -1)
                        if mode in ("tl_top", "tr_top")
                        else range(r_lo, r_hi + 1)
                    )
                    vhits = []
                    for r0 in r_iter:
                        if use_pad and len(vhits) >= 6:
                            if mode in ("bl_bot", "br_bot", "tl_top", "tr_top"):
                                break
                            if mode == "bl_top" and (r0 + ph) > vhits[0][0][0] + 4:
                                break
                        fc = vcm
                        for dr, runs in rruns:
                            rr = r0 + dr
                            brow = rows[rr]
                            if not brow:
                                continue
                            rv = rver[rr]
                            for dx, rlen in runs:
                                k = (rr, rv, rlen)
                                sm = scache.get(k)
                                if sm is None:
                                    sm = smear(brow, rlen)
                                    scache[k] = sm
                                fc &= ~(sm >> dx)
                                if not fc:
                                    break
                            if not fc:
                                break
                        if not fc:
                            continue
                        c_l = (fc & -fc).bit_length() - 1
                        c_r = fc.bit_length() - 1
                        cols_to_add = (c_r,) if mode in ("br_bot", "tr_top") else (c_l,)
                        if not use_pad and c_l != c_r:
                            cols_to_add = (c_l, c_r)
                        for c0 in cols_to_add:
                            if mode == "bl_bot":
                                skey = (r0, c0)
                            elif mode == "br_bot":
                                skey = (r0, n - c0 - pw)
                            elif mode == "tl_top":
                                skey = (-r0 - ph, c0)
                            elif mode == "tr_top":
                                skey = (-r0 - ph, n - c0 - pw)
                            elif mode == "bl_top":
                                skey = (r0 + ph, c0 + pw, r0, c0)
                            elif mode == "env":
                                nw = env_c if c0 + pw <= env_c else c0 + pw
                                nh = env_r if r0 + ph <= env_r else r0 + ph
                                skey = (nw * nh, max(nw, nh), r0 + ph, c0 + pw)
                            elif mode == "minmax":
                                nw = env_c if c0 + pw <= env_c else c0 + pw
                                nh = env_r if r0 + ph <= env_r else r0 + ph
                                skey = (max(nw, nh), nw + nh, r0 + ph, c0 + pw)
                            elif mode == "lb_left":
                                skey = (c0, r0)
                            else:
                                skey = (c0 + pw, r0 + ph, c0, r0)
                            vhits.append((skey, min(max(margin, c0 * inv_n), hx), min(max(margin, r0 * inv_n), hy), r0))
                    if vhits:
                        for h_item in _diverse_hits(vhits, rows_per_var):
                            bm_hits.append((h_item[0], h_item[1], h_item[2], var, prows, vholes))
                if not bm_hits:
                    continue
                hit_cap = 10 if use_pad else 36
                for _skey, tx, ty, var, prows, vholes in _diverse_hits(bm_hits, hit_cap):
                    if deadline is not None and time.perf_counter() > deadline:
                        return None, pos, unit
                    vw = var.width * scale
                    vh = var.height * scale
                    hx = max(margin, 1.0 - margin - vw)
                    hy = max(margin, 1.0 - margin - vh)
                    ok_pt = None
                    for dx, dy in nudges:
                        qx = min(max(margin, tx + dx), hx)
                        qy = min(max(margin, ty + dy), hy)
                        if not _variant_conflicts(var, scale, qx, qy, index, margin):
                            ok_pt = (qx, qy)
                            break
                    if ok_pt is not None:
                        cx, cy = _compact_placement_dir(var, scale, ok_pt[0], ok_pt[1], index, margin, mode)
                        chosen = (cx, cy, var, prows, vholes)
                        break
                if chosen is not None:
                    break

        # 3) Exact tangent fallback when static cavities or tiny scenes need sub-grid precision.
        if chosen is None and allow_tangent_fallback:
            for var, (_pw, _ph, _rruns, prows, vholes) in eval_entries[:4]:
                vw = var.width * scale
                vh = var.height * scale
                if vw > high_lim or vh > high_lim:
                    continue
                geom_cands = _candidate_positions(
                    vw,
                    vh,
                    obstacle_bounds,
                    margin,
                    limit=96,
                    obstacle_vertices=obstacle_vertices,
                    env_w=env_w,
                    env_h=env_h,
                )
                for x, y in geom_cands:
                    if deadline is not None and time.perf_counter() > deadline:
                        return None, pos, unit
                    if _variant_conflicts(var, scale, x, y, index, margin):
                        continue
                    cx, cy = _compact_placement_dir(var, scale, x, y, index, margin, mode)
                    chosen = (cx, cy, var, prows, vholes)
                    break
                if chosen is not None:
                    break

        if chosen is None:
            return None, pos, unit

        px, py, chosen_var, prows, vholes = chosen
        for sh_local in chosen_var.member_shapes:
            placed_sh = transform_shape(sh_local, scale=scale, tx=px, ty=py)
            index.insert(placed_sh)
            if allow_tangent_fallback:
                obstacle_bounds.append(shape_bounds(placed_sh))
                obstacle_vertices.extend(_sample_shape_vertices(placed_sh, max_pts=4))
        c0 = max(0, min(n - 1, int(round(px * n))))
        r0 = max(0, min(n - 1, int(round(py * n))))
        _insert_board_rows(prows, c0, r0, n, pad_cells, mf, rows_raw, rows_pad, row_ver_raw, row_ver_pad)
        for hx0, hx1, hy0, hy1 in vholes:
            active_holes.append((px + hx0, px + hx1, py + hy0, py + hy1))
        if len(active_holes) > 16:
            active_holes.sort(key=lambda h: -(h[1] - h[0]) * (h[3] - h[2]))
            del active_holes[16:]
        env_w = max(env_w, px + chosen_var.width * scale)
        env_h = max(env_h, py + chosen_var.height * scale)
        env_c = max(env_c, int(math.ceil(env_w * n)))
        env_r = max(env_r, int(math.ceil(env_h * n)))
        placements[unit.key] = (px, py, chosen_var)

    return placements, total, None


def _try_general_layout_v3(
    units,
    static_shapes,
    scale: float,
    margin: float,
    pcache=None,
    deadline: float | None = None,
    max_iters: int = 18,
    allow_free_rich: bool = True,
):
    """Run multi-order, multi-direction, 2x2-corner env/bl_x, and SWO sweeps at one scale."""
    total = len(units)
    grid_size = 256 if total <= 96 else 240 if total <= 220 else 192
    if pcache is None:
        pcache = _build_scale_piece_cache(units, scale, margin, grid_size)

    order_names = ("hull", "outer_hole", "strips_hull", "bbox_area", "area", "max_dim")
    pre_ordered = [(name, _order_units(units, order_mode=name)) for name in order_names]
    modes = ("bl_bot", "bl_top", "lb_left", "br_bot", "tl_top", "lb_right")
    rng = random.Random(42)
    weights = {u.key: 0.0 for u in units}
    base_rank = {u.key: i for i, u in enumerate(pre_ordered[0][1])}
    tangent_ok = total <= 64 or bool(static_shapes)
    has_rich = any(len(pcache.get(u.key, ())) > 6 for u in units)

    def _iter_mvars(it_idx: int) -> int:
        if allow_free_rich and has_rich:
            rem = it_idx % 3
            return 4 if rem == 0 else (6 if rem == 1 else 14)
        return 14 if allow_free_rich else 6

    # Phase A: ultra-fast hole-magnet + zero-bloat directional sweeps (excels on concave/hollow sheets).
    fast_iters = 4 if total > 96 else min(max_iters, len(pre_ordered) * 2)
    for it in range(fast_iters):
        if deadline is not None and time.perf_counter() > deadline:
            return None
        ordered = pre_ordered[it % len(pre_ordered)][1]
        mode = modes[it % len(modes)]
        mvars = _iter_mvars(it)
        res, fail_pos, fail_unit = _try_general_layout_v3_once(
            ordered,
            static_shapes,
            pcache,
            scale,
            margin,
            grid_size=grid_size,
            mode=mode,
            max_vars=mvars,
            deadline=deadline,
            allow_tangent_fallback=tangent_ok or (it >= 2 and total <= 128),
        )
        if res is not None:
            return res
        if fail_unit is not None:
            weights[fail_unit.key] += 5.0 + (total - fail_pos) * 0.25

    # Phase B: 2x2-corner-deduped global envelope sweeps (excels on 100-200 compact island sheets).
    once_grid = 288 if total <= 180 else 256 if total <= 360 else 224
    phase_b_strats = (
        (("area", "bl_x"), ("bbox_area", "env"))
        if total <= 96
        else (("area", "bl_x"), ("bbox_area", "env"), ("area", "env"))
    )
    for once_order, once_score in phase_b_strats:
        if deadline is not None and time.perf_counter() > deadline:
            return None
        ordered = _order_units(units, order_mode=once_order)
        res, fail_pos, fail_unit = _try_general_layout_once(
            ordered,
            static_shapes,
            scale,
            margin,
            score_kind=once_score,
            grid_size=once_grid,
            deadline=deadline,
            allow_tangent_fallback=(total <= 64 or bool(static_shapes)),
        )
        if res is not None:
            return res
        if fail_unit is not None:
            weights[fail_unit.key] += 4.0 + (total - fail_pos) * 0.20

    # Phase C: Accumulated Squeaky-Wheel sweeps across all directions for the remaining budget.
    for it in range(fast_iters, max_iters):
        if deadline is not None and time.perf_counter() > deadline:
            break
        mode = modes[it % len(modes)]
        jitter = 0.12 if (it % 2 == 0) else 0.30
        ordered = sorted(
            units,
            key=lambda u: -weights[u.key] + base_rank[u.key] * (1.0 + rng.uniform(-jitter, jitter)),
        )
        mvars = _iter_mvars(it)
        res, fail_pos, fail_unit = _try_general_layout_v3_once(
            ordered,
            static_shapes,
            pcache,
            scale,
            margin,
            grid_size=grid_size,
            mode=mode,
            max_vars=mvars,
            deadline=deadline,
            allow_tangent_fallback=tangent_ok or total <= 128,
        )
        if res is not None:
            return res
        if fail_unit is not None:
            weights[fail_unit.key] += 5.0 + (total - fail_pos) * 0.25
    return None


def _try_uniform_scale_up(units, placements, scale: float, margin: float):
    """Expand an existing placement uniformly to the 0-1 tile border when collision-free."""
    if not units or not placements:
        return scale, placements
    resolved = {}
    max_u = margin
    max_v = margin
    for unit in units:
        pl = placements.get(unit.key)
        if pl is None:
            return scale, placements
        px, py = float(pl[0]), float(pl[1])
        var = pl[2] if len(pl) >= 3 else (_unit_variants_for_layout(unit)[0])
        resolved[unit.key] = (px, py, var)
        max_u = max(max_u, px + var.width * scale)
        max_v = max(max_v, py + var.height * scale)

    span = max(max_u - margin, max_v - margin)
    usable = 1.0 - 2.0 * margin - 1.0e-5
    if span <= 1.0e-6 or span >= usable - 1.0e-4:
        return scale, placements

    base_ratio = usable / span
    for factor in (1.0, 0.998):
        ratio = base_ratio * factor
        if ratio <= 1.0 + 1.0e-4:
            continue
        cand_scale = scale * ratio
        cand_pl = {}
        idx = SpatialIndex(44)
        valid = True
        for unit in units:
            px, py, var = resolved[unit.key]
            nx = margin + (px - margin) * ratio
            ny = margin + (py - margin) * ratio
            if _variant_conflicts(var, cand_scale, nx, ny, idx, margin):
                valid = False
                break
            placed_ok = True
            for sh_local in var.member_shapes:
                placed_sh = transform_shape(sh_local, scale=cand_scale, tx=nx, ty=ny)
                if not _shape_fit_tile(placed_sh, margin):
                    placed_ok = False
                    break
                idx.insert(placed_sh)
            if not placed_ok:
                valid = False
                break
            cand_pl[unit.key] = (nx, ny, var)
        if valid:
            return cand_scale, cand_pl
    return scale, placements


def _try_general_layout(
    units,
    static_shapes,
    scale,
    margin,
    progress_cb=None,
    seed_placements=None,
    seed_scale=None,
    candidate_limit=None,
    work_budget=None,
    order_mode="area",
    deadline=None,
    score_kind="env",
    squeaky_retries=0,
):
    del progress_cb, seed_placements, seed_scale, candidate_limit, work_budget, order_mode, score_kind
    return _try_general_layout_v3(
        units,
        static_shapes,
        scale,
        margin,
        pcache=None,
        deadline=deadline,
        max_iters=max(6, 4 + int(squeaky_retries) * 4),
    )



def _rect_intersection(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    x0=max(ax,bx); y0=max(ay,by); x1=min(ax+aw,bx+bw); y1=min(ay+ah,by+bh)
    if x1 <= x0 + _EPS or y1 <= y0 + _EPS: return None
    return (x0,y0,x1-x0,y1-y0)


def _prune_free_rects(rects):
    clean=[]
    for i,a in enumerate(rects):
        if a[2] <= _EPS or a[3] <= _EPS: continue
        contained=False
        for j,b in enumerate(rects):
            if i==j: continue
            if (a[0] >= b[0]-_EPS and a[1] >= b[1]-_EPS and
                a[0]+a[2] <= b[0]+b[2]+_EPS and a[1]+a[3] <= b[1]+b[3]+_EPS):
                if (b[2]*b[3] > a[2]*a[3] + _EPS) or j < i:
                    contained=True; break
        if not contained: clean.append(a)
    return clean


def _split_free_rects(free_rects, used):
    out=[]
    ux,uy,uw,uh=used; ur=ux+uw; ut=uy+uh
    for rect in free_rects:
        inter=_rect_intersection(rect,used)
        if inter is None:
            out.append(rect); continue
        x,y,w,h=rect; r=x+w; t=y+h
        if ux > x + _EPS: out.append((x,y,ux-x,h))
        if ur < r - _EPS: out.append((ur,y,r-ur,h))
        if uy > y + _EPS: out.append((x,y,w,uy-y))
        if ut < t - _EPS: out.append((x,ut,w,t-ut))
    return _prune_free_rects(out)


def _maxrect_layout(units, static_shapes, scale, margin):
    tile=max(0.0,1.0-2.0*margin)
    free=[(margin,margin,tile,tile)]
    for shape in static_shapes:
        minx,maxx,miny,maxy=shape_bounds(shape)
        used=(minx-margin,miny-margin,(maxx-minx)+2*margin,(maxy-miny)+2*margin)
        free=_split_free_rects(free,used)
        if not free: break
    placements={}
    ordered=sorted(units,key=lambda u:(-u.area,-max(u.width,u.height),u.key))
    reserve=max(0.0,float(margin)) + _MARGIN_RESERVE_CLEARANCE
    for unit in ordered:
        w=unit.width*scale; h=unit.height*scale
        best=None
        for idx,rect in enumerate(free):
            x,y,rw,rh=rect
            if w > rw + _EPS or h > rh + _EPS: continue
            short=min(rw-w,rh-h); area=rw*rh-w*h
            score=(area,short,y,x)
            if best is None or score < best[0]: best=(score,idx,x,y)
        if best is None: return None
        _,_,x,y=best
        placements[unit.key]=(x,y)
        used=(x-reserve,y-reserve,w+2.0*reserve,h+2.0*reserve)
        free=_split_free_rects(free,used)
    return placements


def _maxrect_variant_layout(units, static_shapes, scale, margin, order_mode="area"):
    tile = max(0.0, 1.0 - 2.0 * margin)
    free = [(margin, margin, tile, tile)]
    for shape in static_shapes:
        minx, maxx, miny, maxy = shape_bounds(shape)
        used = (minx - margin, miny - margin, (maxx - minx) + 2 * margin, (maxy - miny) + 2 * margin)
        free = _split_free_rects(free, used)
        if not free:
            break
    placements = {}
    ordered = _order_units(units, order_mode=order_mode)
    reserve = max(0.0, float(margin)) + _MARGIN_RESERVE_CLEARANCE
    env_w = margin
    env_h = margin
    for unit in ordered:
        variants = unit.variants or ()
        if not variants:
            w = unit.width * scale
            h = unit.height * scale
            best = None
            for rect in free:
                x, y, rw, rh = rect
                if w > rw + _EPS or h > rh + _EPS:
                    continue
                short = min(rw - w, rh - h)
                area = rw * rh - w * h
                score = (area, short, y, x)
                if best is None or score < best[0]:
                    best = (score, x, y, w, h, None)
        else:
            best = None
            # Only test distinct width/height pairs inside MaxRects to keep it instant.
            seen_dims = set()
            for var in variants:
                dim_key = (round(var.width, 5), round(var.height, 5))
                if dim_key in seen_dims:
                    continue
                seen_dims.add(dim_key)
                w = var.width * scale
                h = var.height * scale
                for rect in free:
                    x, y, rw, rh = rect
                    if w > rw + _EPS or h > rh + _EPS:
                        continue
                    new_w = max(env_w, x + w)
                    new_h = max(env_h, y + h)
                    short = min(rw - w, rh - h)
                    area = rw * rh - w * h
                    score = (new_w * new_h, area, short, y, x)
                    if best is None or score < best[0]:
                        best = (score, x, y, w, h, var)
        if best is None:
            return None
        _, x, y, w, h, chosen_var = best
        placements[unit.key] = (x, y) if chosen_var is None else (x, y, chosen_var)
        env_w = max(env_w, x + w)
        env_h = max(env_h, y + h)
        used = (x - reserve, y - reserve, w + 2.0 * reserve, h + 2.0 * reserve)
        free = _split_free_rects(free, used)
    return placements


def _solve_rect_baseline(units, static_shapes, margin, scale_to_fit):
    if not units:
        return 1.0, {}
    if not scale_to_fit:
        for order_mode in ("area", "max_dim", "height"):
            p = _maxrect_variant_layout(units, static_shapes, 1.0, margin, order_mode=order_mode)
            if p is not None:
                return 1.0, p
        return 1.0, None
    high = _general_scale_upper(units, margin)
    best_scale = 0.0
    best_placements = None
    for order_mode in ("area", "max_dim", "height"):
        low = 0.0
        hi = high
        local_best = None
        for _ in range(13):
            mid = (low + hi) * 0.5
            p = _maxrect_variant_layout(units, static_shapes, mid, margin, order_mode=order_mode)
            if p is None:
                hi = mid
            else:
                low = mid
                local_best = p
        if local_best is not None and low > best_scale:
            best_scale = low
            best_placements = local_best
    return best_scale, best_placements


def _shape_is_axis_aligned_rect(shape):
    if len(shape)!=1 or len(shape[0])!=4: return False
    poly=shape[0]; b=shape_bounds(shape)
    corners={(round(b[0],9),round(b[2],9)),(round(b[1],9),round(b[2],9)),(round(b[1],9),round(b[3],9)),(round(b[0],9),round(b[3],9))}
    return {(round(x,9),round(y,9)) for x,y in poly} == corners


def _general_scale_upper(units, margin):
    tile = max(1e-9, 1.0 - 2.0 * margin)
    upper = float("inf")
    total_area = 0.0
    for unit in units:
        if unit.variants:
            unit_bound = max(
                min(tile / max(v.width, 1e-10), tile / max(v.height, 1e-10))
                for v in unit.variants
            )
            upper = min(upper, unit_bound)
        else:
            upper = min(upper, tile / max(unit.width, 1e-10), tile / max(unit.height, 1e-10))
        total_area += max(0.0, float(unit.area))
    if total_area > 1.0e-16:
        area_upper = math.sqrt((tile * tile) / total_area) * 1.015
        upper = min(upper, area_upper)
    return max(0.0, upper if math.isfinite(upper) else 1.0)


def _solve_general(units, static_shapes, margin, scale_to_fit, progress_cb=None):
    if not units:
        return 1.0, {}
    if not scale_to_fit:
        source_layout = _reuse_source_layout(units, static_shapes, margin)
        if source_layout is not None:
            _progress(
                progress_cb,
                stage="source_layout",
                percent=93.0,
                done=len(units),
                total=max(1, len(units)),
            )
            return 1.0, source_layout
    baseline_scale, baseline = _solve_rect_baseline(units, static_shapes, margin, scale_to_fit)
    if not scale_to_fit:
        if baseline is not None:
            return 1.0, baseline
        deadline = time.perf_counter() + 16.0
        res = _try_general_layout_v3(
            units,
            static_shapes,
            1.0,
            margin,
            deadline=deadline,
            max_iters=28,
        )
        if res is not None:
            return 1.0, res
        return 1.0, None
    if baseline is None:
        baseline_scale, baseline = 0.0, {}

    if baseline and all(
        not unit.overlap_members and _shape_is_axis_aligned_rect(unit.local_shape) and len(unit.variants) <= 2
        for unit in units
    ) and all(_shape_is_axis_aligned_rect(shape) for shape in static_shapes):
        _progress(progress_cb, stage="optimizing", percent=93.0, done=1, total=1)
        return baseline_scale, baseline

    upper = _general_scale_upper(units, margin)
    low = baseline_scale
    best = baseline
    if low < 1.0 - 1.0e-6:
        source_layout = _reuse_source_layout(units, static_shapes, margin)
        if source_layout is not None:
            low = 1.0
            best = source_layout
    high = max(low, upper)
    count = len(units)
    deadline = time.perf_counter() + (14.0 if count <= 96 else 22.0)
    steps = 8 if count <= 96 else 6
    grid_size = 256 if count <= 96 else 240 if count <= 220 else 192

    for step in range(steps):
        now = time.perf_counter()
        if now > deadline or high - low <= 1.5e-3 * max(1.0, low):
            break
        rem_steps = max(1, steps - step)
        step_budget = min(deadline - now, max(3.6 if count > 96 else 2.2, (deadline - now) / rem_steps * 1.40))
        step_deadline = min(deadline, now + step_budget)
        mid = (low + high) * 0.5
        pcache = _build_scale_piece_cache(units, mid, margin, grid_size)
        placements = _try_general_layout_v3(
            units,
            static_shapes,
            mid,
            margin,
            pcache=pcache,
            deadline=step_deadline,
            max_iters=24,
        )
        if placements is None:
            high = mid
        else:
            eff_scale = mid
            if not static_shapes:
                eff_scale, placements = _try_uniform_scale_up(units, placements, mid, margin)
            low = max(mid, eff_scale)
            best = placements
        _progress(
            progress_cb,
            stage="optimizing",
            percent=75.0 + 18.0 * (step + 1) / steps,
            done=step + 1,
            total=steps,
        )

    # Safeguard: ensure scale_to_fit reaches >= 1.0 whenever feasible within upper.
    if best and low < 1.0 - 1.0e-4 and upper >= 1.0 - 1.0e-6:
        p1_deadline = time.perf_counter() + (5.5 if count > 96 else 4.0)
        pcache_1 = _build_scale_piece_cache(units, 1.0, margin, grid_size)
        p1 = _try_general_layout_v3(
            units,
            static_shapes,
            1.0,
            margin,
            pcache=pcache_1,
            deadline=p1_deadline,
            max_iters=24,
        )
        if p1 is not None:
            eff_scale = 1.0
            if not static_shapes:
                eff_scale, p1 = _try_uniform_scale_up(units, p1, 1.0, margin)
            low = eff_scale
            best = p1

    # Final polish probe cleanly inside remaining gap when time budget remains.
    if best and time.perf_counter() < deadline - 0.8 and high - low > 1.5e-3 * max(1.0, low):
        polish_scale = low + (high - low) * 0.40
        if polish_scale > low + 1.0e-4:
            pcache_p = _build_scale_piece_cache(units, polish_scale, margin, grid_size)
            placements = _try_general_layout_v3(
                units,
                static_shapes,
                polish_scale,
                margin,
                pcache=pcache_p,
                deadline=deadline,
                max_iters=18,
            )
            if placements is not None:
                eff_scale = polish_scale
                if not static_shapes:
                    eff_scale, placements = _try_uniform_scale_up(units, placements, polish_scale, margin)
                if eff_scale > low:
                    low = eff_scale
                    best = placements

    if best and not static_shapes:
        low, best = _try_uniform_scale_up(units, best, low, margin)

    # Downward geometric probe when static blockers narrow the feasible scale band.
    if not best and upper > _EPS:
        probe_deadline = time.perf_counter() + 4.5
        for factor in (0.25, 0.125, 0.05, 0.02):
            if time.perf_counter() > probe_deadline:
                break
            probe_scale = upper * factor
            pcache_d = _build_scale_piece_cache(units, probe_scale, margin, grid_size)
            placements = _try_general_layout_v3(
                units,
                static_shapes,
                probe_scale,
                margin,
                pcache=pcache_d,
                deadline=probe_deadline,
                max_iters=14,
            )
            if placements is not None:
                return probe_scale, placements
        return 0.0, None
    return (low, best) if best else (0.0, None)


def _shape_feature(island: IslandState):
    points = list(island.loop_uvs.values())
    bounds = shape_bounds(island.boundary_loops)
    width = max(bounds[1] - bounds[0], 1e-9)
    height = max(bounds[3] - bounds[2], 1e-9)
    aspect = max(width, height) / max(min(width, height), 1e-9)
    cx = sum(p[0] for p in points) / max(1, len(points))
    cy = sum(p[1] for p in points) / max(1, len(points))
    radii = sorted(math.hypot(p[0] - cx, p[1] - cy) for p in points)
    rms = math.sqrt(sum(r * r for r in radii) / max(1, len(radii))) or 1.0
    normalized = [r / rms for r in radii]
    count = min(12, len(normalized))
    radial = tuple(normalized[round(i * (len(normalized) - 1) / max(1, count - 1))] for i in range(count)) if normalized else ()
    topology = (len(island.face_key), island.face_sizes, len({_point_key(p) for p in points}))
    return {"island": island, "topology": topology, "feature": (aspect, radial), "area": island.uv_area}


def _aligned_pair_uvs(left: IslandState, right: IslandState, axis_kind):
    left_uv = dict(left.loop_uvs)
    right_uv = dict(right.loop_uvs)
    lp = list(left_uv.values())
    rp = list(right_uv.values())
    lc = (sum(p[0] for p in lp) / len(lp), sum(p[1] for p in lp) / len(lp))
    rc = (sum(p[0] for p in rp) / len(rp), sum(p[1] for p in rp) / len(rp))
    la = _principal_axis(lp)
    ra = _principal_axis(rp)
    desired = (-la[0], la[1]) if axis_kind == "U" else (la[0], -la[1])
    angle = math.atan2(desired[1], desired[0]) - math.atan2(ra[1], ra[0])
    lr = math.sqrt(sum((p[0]-lc[0])**2 + (p[1]-lc[1])**2 for p in lp) / len(lp))
    rr = math.sqrt(sum((p[0]-rc[0])**2 + (p[1]-rc[1])**2 for p in rp) / len(rp))
    scale = lr / rr if rr > 1e-12 else 1.0
    return left_uv, _apply_similarity(right_uv, angle=angle, scale=scale, center=rc)


def _sym_rotation_angle(island, rotation_mode, loop_uvs, points=None):
    mode = str(rotation_mode or "NONE").upper()
    if mode == "ROT_90":
        return math.pi * 0.5
    if mode == "ROT_180":
        return math.pi
    if mode == "CARDINAL":
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        else:
            shape = _shape_from_loop_uvs(island, loop_uvs)
            minx, maxx, miny, maxy = _uv_coordinate_bounds(loop_uvs, shape)
        if maxy - miny > maxx - minx:
            return math.pi * 0.5
    elif mode == "FREE":
        if points is None:
            shape = _shape_from_loop_uvs(island, loop_uvs)
            points = [pt for poly in shape for pt in poly] or list(loop_uvs.values())
        return pack_geometry.min_area_rect_angle(points)
    return 0.0


def _sym_units(
    movable,
    axis_kind,
    density_scales,
    exact_by_key=None,
    rotation_mode="NONE",
    followers_by_key=None,
    overlap_members_by_key=None,
):
    """Build symmetry units with a proven loop correspondence for every pair.

    Shape descriptors are only a cheap candidate generator.  A pair is
    admitted when the exact topology mapper returns a complete bijection for
    both islands.  The master coordinates are then copied through that
    correspondence and reflected in the requested local axis, so the final
    placement has a loop-level mirror invariant instead of a PCA-only guess.
    Islands without a proven mapping (or rigid locked-overlap groups) remain
    rigid similarity-transformed singles; their UV shape is never fabricated
    from another island.
    """
    overlap_map = overlap_members_by_key or {}
    pair_candidates = []
    locked_singles = []
    for island in movable:
        rec = _shape_feature(island)
        if overlap_map.get(island.face_key):
            locked_singles.append(rec)
        else:
            pair_candidates.append(rec)
    candidate_pairs, singles = pack_geometry.pair_similar_records(pair_candidates, max_distance=0.20)
    singles = list(singles) + locked_singles
    units = []
    accepted_pairs = 0

    def centered(loop_uvs, shape):
        minx, maxx, miny, maxy = _uv_coordinate_bounds(loop_uvs, shape)
        cx = (minx + maxx) * 0.5
        cy = (miny + maxy) * 0.5
        local_uv = {key: (point[0] - cx, point[1] - cy) for key, point in loop_uvs.items()}
        local_shape = tuple(
            tuple((point[0] - cx, point[1] - cy) for point in polygon)
            for polygon in shape
        )
        return local_uv, local_shape, maxx - minx, maxy - miny

    for left_record, right_record in candidate_pairs:
        left = left_record["island"]
        right = right_record["island"]
        mapping = None
        if exact_by_key:
            left_exact = exact_by_key.get(left.face_key)
            right_exact = exact_by_key.get(right.face_key)
            if left_exact is not None and right_exact is not None:
                mapping = pro_exact_v2_core.find_deterministic_exact_mapping(
                    left_exact.graph,
                    right_exact.graph,
                )
                if mapping is not None:
                    left_keys = set(left.loop_uvs)
                    right_keys = set(right.loop_uvs)
                    mapping = tuple(mapping)
                    if (
                        len(mapping) != len(left_keys)
                        or {pair[0] for pair in mapping} != right_keys
                        or {pair[1] for pair in mapping} != left_keys
                    ):
                        mapping = None
        if mapping is None:
            # Keep both islands as independent rigid singles.  In particular,
            # do not force a guessed correspondence merely because topology
            # descriptors happened to land in the same cheap bucket.
            singles.extend((left_record, right_record))
            continue

        # Use one common density scale for the pair.  A geometric mean keeps
        # the pair's relative target density balanced while preserving exact
        # mirror dimensions; applying independent scales would break the
        # loop-coordinate reflection contract.
        ls = max(0.0, float(density_scales.get(left.face_key, 1.0)))
        rs = max(0.0, float(density_scales.get(right.face_key, 1.0)))
        pair_scale = math.sqrt(ls * rs) if ls > 0.0 and rs > 0.0 else max(ls, rs, 1.0)
        left_uv = dict(left.loop_uvs)
        if pair_scale != 1.0:
            left_uv = _apply_similarity(left_uv, scale=pair_scale)
        angle = _sym_rotation_angle(left, rotation_mode, left_uv)
        if angle:
            left_uv = _apply_similarity(left_uv, angle=angle)
        left_shape = _shape_from_loop_uvs(left, left_uv)
        left_local, left_local_shape, left_width, left_height = centered(left_uv, left_shape)

        # ``mapping`` is candidate(right) -> master(left).  Copy the master's
        # local coordinates through that bijection and reflect one coordinate
        # exactly.  The two centers are later placed as a U_HALF or V_HALF
        # pair, making the corresponding final loop coordinates sum to one.
        right_local = {}
        for right_key, left_key in mapping:
            point = left_local[left_key]
            if axis_kind == "U":
                right_local[right_key] = (-point[0], point[1])
            else:
                right_local[right_key] = (point[0], -point[1])
        if set(right_local) != set(right.loop_uvs):
            singles.extend((left_record, right_record))
            continue
        right_shape = _shape_from_loop_uvs(right, right_local)
        right_local, right_local_shape, right_width, right_height = centered(
            right_local,
            right_shape,
        )
        # Centering the reflected copy should be a no-op up to round-off.  If
        # a degenerate/ambiguous boundary violates that invariant, keep both
        # islands rigid singles instead of silently introducing a skew.
        if axis_kind == "U":
            residual = max(
                math.hypot(
                    right_local[key][0] + left_local[left_key][0],
                    right_local[key][1] - left_local[left_key][1],
                )
                for key, left_key in mapping
            )
        else:
            residual = max(
                math.hypot(
                    right_local[key][0] - left_local[left_key][0],
                    right_local[key][1] + left_local[left_key][1],
                )
                for key, left_key in mapping
            )
        if residual > 1.0e-7:
            singles.extend((left_record, right_record))
            continue
        units.append(
            {
                "kind": "pair",
                "members": (
                    (
                        left,
                        left_local,
                        left_local_shape,
                        left_width,
                        left_height,
                        tuple((followers_by_key or {}).get(left.face_key, ())),
                    ),
                    (
                        right,
                        right_local,
                        right_local_shape,
                        right_width,
                        right_height,
                        tuple((followers_by_key or {}).get(right.face_key, ())),
                    ),
                ),
                "area": left.uv_area + right.uv_area,
            }
        )
        accepted_pairs += 1

    for rec in singles:
        island = rec["island"]
        ds = density_scales.get(island.face_key, 1.0)
        group_overlaps = tuple(overlap_map.get(island.face_key, ()))
        if not group_overlaps:
            uv = dict(island.loop_uvs)
            if ds != 1.0:
                uv = _apply_similarity(uv, scale=ds)
            angle = _sym_rotation_angle(island, rotation_mode, uv)
            if angle:
                uv = _apply_similarity(uv, angle=angle)
            shape = _shape_from_loop_uvs(island, uv)
            local_uv, local_shape, width, height = centered(uv, shape)
            overlap_entries = ()
            area = island.uv_area
        else:
            raw_pts = list(island.loop_uvs.values())
            raw_hull = [p for poly in island.boundary_loops for p in poly]
            for ov in group_overlaps:
                raw_pts.extend(ov.island.loop_uvs.values())
                for poly in ov.island.boundary_loops:
                    raw_hull.extend(poly)
            pivot = (
                sum(p[0] for p in raw_pts) / len(raw_pts),
                sum(p[1] for p in raw_pts) / len(raw_pts),
            ) if raw_pts else (0.0, 0.0)
            uv = dict(island.loop_uvs)
            if ds != 1.0:
                uv = _apply_similarity(uv, scale=ds, center=pivot)
            angle = _sym_rotation_angle(island, rotation_mode, uv, points=raw_hull or raw_pts)
            if angle or ds != 1.0:
                uv = _apply_similarity(island.loop_uvs, angle=angle, scale=ds, center=pivot)
            shape = _shape_from_loop_uvs(island, uv)

            ov_data = []
            all_points = list(uv.values()) + [pt for poly in shape for pt in poly]
            area = island.uv_area
            for ov in group_overlaps:
                area = max(area, ov.island.uv_area)
                ov_uv = _apply_similarity(ov.island.loop_uvs, angle=angle, scale=ds, center=pivot)
                ov_shape = _shape_from_loop_uvs(ov.island, ov_uv)
                ov_data.append((ov_uv, ov_shape, ov.follower_mappings))
                all_points.extend(ov_uv.values())
                for poly in ov_shape:
                    all_points.extend(poly)

            minx = min(p[0] for p in all_points)
            maxx = max(p[0] for p in all_points)
            miny = min(p[1] for p in all_points)
            maxy = max(p[1] for p in all_points)
            cx = (minx + maxx) * 0.5
            cy = (miny + maxy) * 0.5
            width = max(maxx - minx, 1.0e-10)
            height = max(maxy - miny, 1.0e-10)
            local_uv = {k: (pt[0] - cx, pt[1] - cy) for k, pt in uv.items()}
            local_shape = tuple(tuple((pt[0] - cx, pt[1] - cy) for pt in poly) for poly in shape)
            overlap_entries = tuple(
                (
                    {k: (pt[0] - cx, pt[1] - cy) for k, pt in ov_uv.items()},
                    tuple(tuple((pt[0] - cx, pt[1] - cy) for pt in poly) for poly in ov_shape),
                    ov_followers,
                )
                for ov_uv, ov_shape, ov_followers in ov_data
            )

        units.append(
            {
                "kind": "single",
                "members": (
                    (
                        island,
                        local_uv,
                        local_shape,
                        width,
                        height,
                        tuple((followers_by_key or {}).get(island.face_key, ())),
                        overlap_entries,
                    ),
                ),
                "area": area,
            }
        )
    units.sort(key=lambda unit: (0 if unit["kind"] == "pair" else 1, -unit["area"]))
    return units, accepted_pairs, len(singles)


def _sym_member_shape(member, scale, center):
    return transform_shape(member[2], scale=scale, tx=center[0], ty=center[1])


def _sym_member_shapes(member, scale, center):
    shapes = [transform_shape(member[2], scale=scale, tx=center[0], ty=center[1])]
    if len(member) > 6 and member[6]:
        for _ov_uv, ov_shape, _ov_followers in member[6]:
            shapes.append(transform_shape(ov_shape, scale=scale, tx=center[0], ty=center[1]))
    return tuple(shapes)


def _sym_member_bounds(member, scale, center):
    b_list = [shape_bounds(member[2])]
    if len(member) > 6 and member[6]:
        for _ov_uv, ov_shape, _ov_followers in member[6]:
            b_list.append(shape_bounds(ov_shape))
    min_x = min(b[0] for b in b_list)
    max_x = max(b[1] for b in b_list)
    min_y = min(b[2] for b in b_list)
    max_y = max(b[3] for b in b_list)
    return (
        min_x * scale + center[0],
        max_x * scale + center[0],
        min_y * scale + center[1],
        max_y * scale + center[1],
    )


def _bounds_conflict(left, right, margin):
    return not (
        left[1] + margin < right[0]
        or right[1] + margin < left[0]
        or left[3] + margin < right[2]
        or right[3] + margin < left[2]
    )


def _sym_member_writes(member, scale, center):
    island, local_uv, _shape, _width, _height = member[:5]
    master_uvs = {
        key: (point[0] * scale + center[0], point[1] * scale + center[1])
        for key, point in local_uv.items()
    }
    writes = [(key[0], key[1], uv[0], uv[1]) for key, uv in master_uvs.items()]
    for follower, mapping in member[5] if len(member) > 5 else ():
        writes.extend(
            (key[0], key[1], uv[0], uv[1])
            for key, uv in exact_follower_writes(master_uvs, mapping)
        )
    if len(member) > 6 and member[6]:
        for ov_local_uv, _ov_shape, ov_followers in member[6]:
            ov_uvs = {
                key: (point[0] * scale + center[0], point[1] * scale + center[1])
                for key, point in ov_local_uv.items()
            }
            writes.extend((key[0], key[1], uv[0], uv[1]) for key, uv in ov_uvs.items())
            for follower, mapping in ov_followers:
                writes.extend(
                    (key[0], key[1], uv[0], uv[1])
                    for key, uv in exact_follower_writes(ov_uvs, mapping)
                )
    return writes


def _sym_centers(unit, axis_kind, axis_pos, spread):
    if unit["kind"]=="single":
        return ((0.5,axis_pos),) if axis_kind=="U" else ((axis_pos,0.5),)
    if axis_kind=="U":
        return ((0.5-spread,axis_pos),(0.5+spread,axis_pos))
    return ((axis_pos,0.5+spread),(axis_pos,0.5-spread))


def _try_sym_layout(units, static_shapes, scale, axis_kind, margin, exact=True):
    index=SpatialIndex(44); bounds=[]
    for shape in static_shapes: index.insert(shape); bounds.append(shape_bounds(shape))
    placements={}
    for idx,unit in enumerate(units):
        members=unit["members"]
        if axis_kind=="U":
            axis_half=max(m[4] for m in members)*scale*0.5
        else:
            axis_half=max(m[3] for m in members)*scale*0.5
        low=margin+axis_half; high=1.0-margin-axis_half
        if low>high: return None
        axis_values={0.5,low,high}
        for i in range(_SYM_AXIS_SAMPLES+1): axis_values.add(low+(high-low)*i/_SYM_AXIS_SAMPLES)
        for b in bounds[-96:]:
            omin=b[2] if axis_kind=="U" else b[0]; omax=b[3] if axis_kind=="U" else b[1]
            axis_values.add(max(low,min(high,omax+margin+axis_half+_SYM_CLEARANCE)))
            axis_values.add(max(low,min(high,omin-margin-axis_half-_SYM_CLEARANCE)))
        # Blocker-heavy layouts commonly have their only legal corridor at a
        # tile edge.  Probe the outer edge first; with no blockers the center
        # biased order keeps compact pairs near the axis and avoids needless
        # spread.  Both orders remain exact-oracle checked below.
        if static_shapes:
            axis_values=sorted(axis_values,key=lambda v:(-v,abs(v-0.5)))
        else:
            axis_values=sorted(axis_values,key=lambda v:(abs(v-0.5),v))
        if unit["kind"]=="single": spreads=(0.0,)
        else:
            if axis_kind=="U": cross=[m[3]*scale for m in members]
            else: cross=[m[4]*scale for m in members]
            min_spread=(cross[0]+cross[1])*0.25+margin*0.5
            max_spread=min(0.5-margin-cross[0]*0.5,0.5-margin-cross[1]*0.5)
            if min_spread>max_spread: return None
            samples=tuple(min_spread+(max_spread-min_spread)*i/_SYM_SPREAD_SAMPLES for i in range(_SYM_SPREAD_SAMPLES+1))
            spreads=tuple(sorted(samples,reverse=bool(static_shapes)))
        found=None
        attempts=0
        for axis_pos in axis_values:
            for spread in spreads:
                attempts += 1
                if attempts > _SYM_MAX_CANDIDATES_PER_UNIT:
                    break
                centers=_sym_centers(unit,axis_kind,axis_pos,spread)
                if exact:
                    shape_groups=tuple(
                        _sym_member_shapes(member,scale,centers[i])
                        for i,member in enumerate(members)
                    )
                    shapes=tuple(sh for group in shape_groups for sh in group)
                    if not all(_shape_fit_tile(shape,margin) for shape in shapes):
                        continue
                    own_conflict=False
                    if len(shape_groups)>1:
                        for left_sh in shape_groups[0]:
                            left_bounds=shape_bounds(left_sh)
                            for right_sh in shape_groups[1]:
                                right_bounds=shape_bounds(right_sh)
                                if _bounds_conflict(left_bounds,right_bounds,margin):
                                    if shape_sets_conflict(left_sh,right_sh,margin=margin):
                                        own_conflict=True
                                        break
                            if own_conflict:
                                break
                    if own_conflict: continue
                    conflict=False
                    for shape in shapes:
                        shape_bounds_value=shape_bounds(shape)
                        nearby=index.query_records(shape,margin=margin)
                        for other,other_bounds in nearby:
                            if not _bounds_conflict(shape_bounds_value,other_bounds,margin):
                                continue
                            if shape_sets_conflict(shape,other,margin=margin):
                                conflict=True
                                break
                        if conflict:
                            break
                    if conflict: continue
                    found=(axis_pos,spread,shapes)
                else:
                    coarse_bounds=tuple(
                        _sym_member_bounds(member,scale,centers[i])
                        for i,member in enumerate(members)
                    )
                    low_bound=margin - 1e-9
                    high_bound=1.0 - margin + 1e-9
                    if any(
                        bound[0] < low_bound
                        or bound[1] > high_bound
                        or bound[2] < low_bound
                        or bound[3] > high_bound
                        for bound in coarse_bounds
                    ):
                        continue
                    if len(coarse_bounds)>1 and _bounds_conflict(
                        coarse_bounds[0],coarse_bounds[1],margin
                    ):
                        continue
                    if any(
                        _bounds_conflict(candidate_bound,other_bound,margin)
                        for candidate_bound in coarse_bounds
                        for other_bound in bounds
                    ):
                        continue
                    found=(axis_pos,spread,coarse_bounds)
                break
            if attempts > _SYM_MAX_CANDIDATES_PER_UNIT:
                break
            if found: break
        if not found: return None
        axis_pos,spread,placed_shapes=found
        placements[idx]=(axis_pos,spread)
        if exact:
            for shape in placed_shapes:
                index.insert(shape)
                bounds.append(shape_bounds(shape))
        else:
            bounds.extend(placed_shapes)
    return placements


def _solve_symmetry(units, static_shapes, axis_kind, margin, progress_cb=None):
    tile=max(1e-9,1-2*margin); high=float("inf")
    for unit in units:
        if unit["kind"]=="single":
            m=unit["members"][0]; high=min(high,tile/max(m[3],1e-10),tile/max(m[4],1e-10))
        else:
            a,b=unit["members"]
            if axis_kind=="U": w=a[3]+b[3]+margin; h=max(a[4],b[4])
            else: w=max(a[3],b[3]); h=a[4]+b[4]+margin
            high=min(high,tile/max(w,1e-10),tile/max(h,1e-10))
    if not math.isfinite(high): high=1.0
    # AABB separation is a conservative, exact-safe placement oracle for the
    # first pass.  It avoids spending seconds on polygon edge tests for every
    # failed high-scale candidate, which was the source of the Pack Symmetry
    # stall on the 149-island fixture.  The accepted layout is still verified
    # with the exact polygon oracle before returning.
    low=0.0; best=None
    for step in range(8):
        mid=(low+high)*0.5
        candidate=_try_sym_layout(
            units,
            static_shapes,
            mid,
            axis_kind,
            margin,
            exact=False,
        )
        if candidate is None:
            high=mid
        else:
            low=mid; best=candidate
        # The symmetry probe materializes many short-lived polygon tuples.
        # Collect between probes so the bounded worker does not retain a large
        # stale candidate graph across the next exact-sized attempt.
        gc.collect()

    _progress(progress_cb,stage="optimizing",percent=78.0,done=1,total=2)
    if best is not None:
        index=SpatialIndex(44)
        for shape in static_shapes:
            index.insert(shape)
        placed=[]
        valid=True
        for idx,unit in enumerate(units):
            axis_pos,spread=best[idx]
            centers=_sym_centers(unit,axis_kind,axis_pos,spread)
            shape_groups=tuple(
                _sym_member_shapes(member,low,centers[member_index])
                for member_index,member in enumerate(unit["members"])
            )
            shapes=tuple(sh for group in shape_groups for sh in group)
            if not all(_shape_fit_tile(shape,margin) for shape in shapes):
                valid=False
                break
            for shape in shapes:
                shape_bounds_value=shape_bounds(shape)
                for other,other_bounds in index.query_records(shape,margin=margin):
                    if (
                        shape_bounds_value[1]+margin < other_bounds[0]
                        or other_bounds[1]+margin < shape_bounds_value[0]
                        or shape_bounds_value[3]+margin < other_bounds[2]
                        or other_bounds[3]+margin < shape_bounds_value[2]
                    ):
                        continue
                    if shape_sets_conflict(shape,other,margin=margin):
                        valid=False
                        break
                if not valid:
                    break
            if not valid:
                break
            if len(shape_groups)>1:
                for left_sh in shape_groups[0]:
                    left_bounds=shape_bounds(left_sh)
                    for right_sh in shape_groups[1]:
                        right_bounds=shape_bounds(right_sh)
                        if not (
                            left_bounds[1]+margin < right_bounds[0]
                            or right_bounds[1]+margin < left_bounds[0]
                            or left_bounds[3]+margin < right_bounds[2]
                            or right_bounds[3]+margin < left_bounds[2]
                        ) and shape_sets_conflict(left_sh,right_sh,margin=margin):
                            valid=False
                            break
                    if not valid:
                        break
                if not valid:
                    break
            for shape in shapes:
                index.insert(shape)
                placed.append(shape)
        if valid:
            _progress(progress_cb,stage="optimizing",percent=96.0,done=2,total=2)
            return low,best

    # This path is only reachable when the conservative coarse verifier could
    # not produce a layout.  Keep a bounded exact fallback for concave blocker
    # cavities; it is tried at a low scale and retains the same final oracle.
    fallback_scale=max(0.0, min(high, low if low > 0.0 else high * 0.125))
    if fallback_scale > 0.0:
        candidate=_try_sym_layout(
            units,
            static_shapes,
            fallback_scale,
            axis_kind,
            margin,
            exact=True,
        )
        if candidate is not None:
            _progress(progress_cb,stage="optimizing",percent=96.0,done=2,total=2)
            return fallback_scale,candidate
    _progress(progress_cb,stage="optimizing",percent=96.0,done=2,total=2)
    return 0.0,None


def _density_scales_for_symmetry(movable):
    valid=[]
    for island in movable:
        if island.uv_area>0 and island.world_area>0:
            valid.append(math.sqrt(island.uv_area/island.world_area))
    if not valid: return {}
    target=sum(valid)/len(valid)
    result={}
    for island in movable:
        if island.uv_area>0 and island.world_area>0:
            current=math.sqrt(island.uv_area/island.world_area)
            result[island.face_key]=target/current if current>0 else 1.0
    return result


def _emit_unit_writes(unit: PackUnit, scale, placement):
    if len(placement) == 3:
        x, y, var = placement
        rep_local = var.local_loop_uvs
        overlap_locals = var.overlap_local_uvs
    else:
        x, y = placement
        var = unit.variants[0] if unit.variants else None
        rep_local = var.local_loop_uvs if var is not None else unit.local_loop_uvs
        overlap_locals = var.overlap_local_uvs if var is not None else ()
    master_uvs={key:(uv[0]*scale+x,uv[1]*scale+y) for key,uv in rep_local.items()}
    writes=[(key[0],key[1],uv[0],uv[1]) for key,uv in master_uvs.items()]
    for follower,mapping in unit.follower_mappings:
        for key,uv in exact_follower_writes(master_uvs,mapping):
            writes.append((key[0],key[1],uv[0],uv[1]))
    for member, member_local in zip(unit.overlap_members, overlap_locals):
        member_uvs = {key: (uv[0] * scale + x, uv[1] * scale + y) for key, uv in member_local.items()}
        writes.extend((key[0], key[1], uv[0], uv[1]) for key, uv in member_uvs.items())
        for follower, mapping in member.follower_mappings:
            for key, uv in exact_follower_writes(member_uvs, mapping):
                writes.append((key[0], key[1], uv[0], uv[1]))
    return writes


def _estimate_tile_coverage_pct(shapes: Iterable[ShapeLoops], grid: int = 160) -> float:
    """Measure non-overlapping union UV coverage % across the 0-1 tile."""
    tile = _DualBitmaskTile(size=grid, margin=0.0)
    rows = [0] * grid
    for sh in shapes:
        sr = tile._rasterize_shape_center(sh, 1.0, 0.0, 0.0, grid, grid)
        for r, m in enumerate(sr):
            if m:
                rows[r] |= m
    return round(100.0 * sum(m.bit_count() for m in rows) / (grid * grid), 2)


def solve_pack(snapshot: dict[str, Any], progress_cb=None) -> dict[str, Any]:
    started=time.perf_counter()
    options=dict(snapshot.get("options") or {})
    mode=str(options.get("mode","selected"))
    if mode not in {"selected","whole","symmetry"}: raise ValueError("invalid Pack V2 mode")
    margin=max(0.0,float(options.get("margin",0.001)))
    preserve=bool(options.get("preserve_stacks",False))
    lock_overlapping=options.get("lock_overlapping",None)
    scale_to_fit=bool(options.get("scale_to_fit",True))
    rotation_mode=str(options.get("rotation_mode","NONE"))
    axis_kind=str(options.get("axis_kind","U"))
    _progress(progress_cb,stage="islands",percent=3.0,done=0,total=1)
    islands=list(_parse_islands(snapshot))
    if mode=="whole": movable=list(islands); static=[]
    else:
        movable=[i for i in islands if i.selected]
        static=[i for i in islands if not i.selected]
    if not movable: raise ValueError("no UV islands selected for Pack V2")
    static_shapes=[i.boundary_loops for i in static]

    writes=[]; pair_count=single_count=0; stack_followers=0; overlap_followers=0
    cover_shapes = [
        sh for sh in static_shapes
        if (b := shape_bounds(sh)) and b[1] > 0.0 and b[0] < 1.0 and b[3] > 0.0 and b[2] < 1.0
    ]
    if mode=="symmetry":
        symmetry_pack_units = _build_pack_units(
            snapshot,
            islands,
            movable,
            preserve,
            progress_cb,
            lock_overlapping=lock_overlapping,
        )
        symmetry_representatives = [unit.representative for unit in symmetry_pack_units]
        followers_by_key = {
            unit.representative.face_key: tuple(unit.follower_mappings)
            for unit in symmetry_pack_units
        }
        overlap_members_by_key = {
            unit.representative.face_key: tuple(unit.overlap_members)
            for unit in symmetry_pack_units
            if unit.overlap_members
        }
        stack_followers = sum(
            len(unit.follower_mappings)
            + sum(len(member.follower_mappings) for member in unit.overlap_members)
            for unit in symmetry_pack_units
        )
        overlap_followers = sum(len(unit.overlap_members) for unit in symmetry_pack_units)
        density_scales=_density_scales_for_symmetry(symmetry_representatives)
        # Symmetry pairing has a stricter contract than cheap shape matching:
        # only a complete exact topology mapping may authorize a pair.  The
        # graph build is pure and bounded; failures leave all candidates as
        # rigid singles rather than fabricating a correspondence.
        exact_by_key = {}
        try:
            exact_snapshot = dict(snapshot)
            exact_snapshot["schema"] = pro_exact_v2_core.SCHEMA
            exact_islands = pro_exact_v2_core.build_exact_islands(
                exact_snapshot,
                selected_only=False,
            )
            exact_by_key = {item.face_key: item for item in exact_islands}
        except (TypeError, ValueError, KeyError):
            exact_by_key = {}
        units,pair_count,single_count=_sym_units(
            symmetry_representatives,
            axis_kind,
            density_scales,
            exact_by_key=exact_by_key,
            rotation_mode=rotation_mode,
            followers_by_key=followers_by_key,
            overlap_members_by_key=overlap_members_by_key,
        )
        _progress(progress_cb,stage="pairing",percent=20.0,done=pair_count*2+single_count,total=len(movable))
        scale,placements=_solve_symmetry(units,static_shapes,axis_kind,margin,progress_cb)
        if placements is None: raise RuntimeError("Pack Symmetry V2 could not find a collision-free layout")
        for idx,unit in enumerate(units):
            axis_pos,spread=placements[idx]
            centers=_sym_centers(unit,axis_kind,axis_pos,spread)
            for mi,member in enumerate(unit["members"]):
                center=centers[mi]
                writes.extend(_sym_member_writes(member, scale, center))
                cover_shapes.extend(_sym_member_shapes(member, scale, center))
    else:
        units=_build_pack_units(
            snapshot,
            islands,
            movable,
            preserve,
            progress_cb,
            lock_overlapping=lock_overlapping,
        )
        stack_followers=sum(
            len(unit.follower_mappings)
            + sum(len(member.follower_mappings) for member in unit.overlap_members)
            for unit in units
        )
        overlap_followers=sum(len(unit.overlap_members) for unit in units)
        has_native = _load_native_pack_lib() is not None
        prep_rot = (
            "NONE"
            if (has_native and str(rotation_mode or "NONE").upper() in {"NONE", "CARDINAL", "FREE"})
            else rotation_mode
        )
        for unit in units: _prepare_unit(unit,rotation_mode=prep_rot,density_scale=1.0)
        _progress(progress_cb,stage="shapes",percent=15.0,done=len(units),total=max(1,len(units)))
        native_res = _solve_general_native(units, static_shapes, margin, scale_to_fit, rotation_mode, progress_cb)
        if native_res is not None:
            scale, placements = native_res
        else:
            if prep_rot != rotation_mode:
                for unit in units: _prepare_unit(unit,rotation_mode=rotation_mode,density_scale=1.0)
            scale,placements=_solve_general(units,static_shapes,margin,scale_to_fit,progress_cb)
        if placements is None: raise RuntimeError("Pack V2 could not find a collision-free 0-1 layout")
        for unit in units:
            pl = placements[unit.key]
            writes.extend(_emit_unit_writes(unit, scale, pl))
            px, py = float(pl[0]), float(pl[1])
            var = pl[2] if len(pl) >= 3 else (unit.variants[0] if unit.variants else None)
            if var is not None:
                for sh_local in var.member_shapes:
                    cover_shapes.append(transform_shape(sh_local, scale=scale, tx=px, ty=py))

    coverage_pct = _estimate_tile_coverage_pct(cover_shapes, grid=160)
    _progress(progress_cb,stage="done",percent=100.0,done=len(movable),total=len(movable))
    return {
        "schema":RESULT_SCHEMA,
        "mode":mode,
        "writes":tuple(writes),
        "movable_count":len(movable),
        "static_count":len(static),
        "scale":float(scale),
        "coverage_pct":float(coverage_pct),
        "stack_followers":stack_followers,
        "overlap_followers":overlap_followers,
        "pair_count":pair_count,
        "single_count":single_count,
        "elapsed_ms":(time.perf_counter()-started)*1000.0,
    }


_NATIVE_LIB = None
_NATIVE_LIB_CHECKED = False


def _load_native_pack_lib():
    """Load optional native C++20 multi-threaded pack DLL via ctypes, or return None."""
    global _NATIVE_LIB, _NATIVE_LIB_CHECKED
    if _NATIVE_LIB_CHECKED:
        return _NATIVE_LIB
    _NATIVE_LIB_CHECKED = True
    dll_path = Path(__file__).with_name("uv_gpt_pack_native.dll")
    if not dll_path.is_file():
        return None
    try:
        lib = ctypes.CDLL(str(dll_path))
        fn = lib.uvgpt_pack_solve
        fn.restype = ctypes.c_int
        fn.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_double,
            ctypes.c_int,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
        ]
        _NATIVE_LIB = lib
    except (OSError, AttributeError):
        _NATIVE_LIB = None
    return _NATIVE_LIB


def _unit_center_and_angles(unit: PackUnit, rotation_mode: str) -> tuple[Point, tuple[float, ...]]:
    all_uv_pts = list(unit.representative.loop_uvs.values())
    all_hull_pts = [p for poly in unit.representative.boundary_loops for p in poly]
    for member in unit.overlap_members:
        all_uv_pts.extend(member.island.loop_uvs.values())
        for poly in member.island.boundary_loops:
            all_hull_pts.extend(poly)
    if all_uv_pts:
        center = (
            sum(p[0] for p in all_uv_pts) / len(all_uv_pts),
            sum(p[1] for p in all_uv_pts) / len(all_uv_pts),
        )
    else:
        center = (0.0, 0.0)

    mode = str(rotation_mode or "NONE").upper()
    if mode == "ROT_90":
        return center, (math.pi * 0.5,)
    if mode == "ROT_180":
        return center, (math.pi,)
    if mode == "CARDINAL":
        return center, (0.0, math.pi * 0.5, math.pi, math.pi * 1.5)
    if mode != "FREE":
        return center, (0.0,)

    pts_for_box = all_hull_pts or all_uv_pts
    min_ang = pack_geometry.min_area_rect_angle(pts_for_box) % (2.0 * math.pi)
    axis_box_area = min_box_area = w0 = h0 = 0.0
    if pts_for_box:
        xs0 = [p[0] for p in pts_for_box]
        ys0 = [p[1] for p in pts_for_box]
        w0 = max(max(xs0) - min(xs0), 1.0e-12)
        h0 = max(max(ys0) - min(ys0), 1.0e-12)
        axis_box_area = w0 * h0
        cm, sm = math.cos(min_ang), math.sin(min_ang)
        rxs = [cm * p[0] - sm * p[1] for p in pts_for_box]
        rys = [sm * p[0] + cm * p[1] for p in pts_for_box]
        min_box_area = max(max(rxs) - min(rxs), 1.0e-12) * max(max(rys) - min(rys), 1.0e-12)
    min_first = (min_box_area < 0.88 * axis_box_area) if axis_box_area > 0.0 else False
    max_dim0 = max(w0, h0)
    aspect = max_dim0 / max(min(w0, h0), 1.0e-9)
    min_cands = (
        min_ang,
        (min_ang + math.pi * 0.5) % (2.0 * math.pi),
        (min_ang + math.pi) % (2.0 * math.pi),
        (min_ang + math.pi * 1.5) % (2.0 * math.pi),
    )
    sq_cands, sq_improves = _min_square_span_candidates(pts_for_box, max_dim0)
    card_cands = (math.pi * 0.5, math.pi, math.pi * 1.5)
    if unit.solidity > 0.90 and aspect < 1.12 and not unit.has_hole and not min_first and not sq_improves:
        pool = (math.pi * 0.5,)
        cap = 2
    elif max_dim0 < 0.08 and not unit.has_hole:
        pool = (min_cands + card_cands) if min_first else (card_cands + min_cands[:2] + (math.pi * 0.25,))
        cap = 10
    else:
        grid_24 = tuple(k * math.pi / 12.0 for k in range(24))
        pool = ((min_cands + card_cands) if min_first else (card_cands + min_cands)) + grid_24
        cap = 28
    raw_angles = [0.0]
    if sq_cands and cap > 2:
        _append_unique_angles(raw_angles, sq_cands, 0.025, cap=cap)
    _append_unique_angles(raw_angles, pool, 0.06, cap=cap)
    return center, tuple(raw_angles)


def _solve_general_native(units, static_shapes, margin: float, scale_to_fit: bool, rotation_mode: str, progress_cb=None):
    """Run C++20 32-thread native pack engine and verify 100% against Python exact oracle."""
    lib = _load_native_pack_lib()
    if lib is None or not units:
        return None
    if not scale_to_fit:
        source_layout = _reuse_source_layout(units, static_shapes, margin)
        if source_layout is not None:
            return 1.0, source_layout

    source_layout = None
    min_scale_hint = 0.0
    if scale_to_fit and len(units) <= 220:
        source_layout = _reuse_source_layout(units, static_shapes, margin)
        if source_layout is not None:
            min_scale_hint = 1.0

    unit_shape_offsets = [0]
    shape_loop_offsets = [0]
    loop_vert_offsets = [0]
    verts_x = []
    verts_y = []
    unit_areas = []
    unit_angle_offsets = [0]
    unit_angles_flat = []
    unit_meta_py = []

    for unit in units:
        center, angles = _unit_center_and_angles(unit, rotation_mode)
        cx, cy = center
        unit_meta_py.append((unit, center, angles))
        raw_shapes = (unit.representative.boundary_loops,) + tuple(
            m.island.boundary_loops for m in unit.overlap_members
        )
        for sh in raw_shapes:
            for poly in sh:
                if len(poly) < 3:
                    continue
                for px, py in poly:
                    verts_x.append(float(px) - cx)
                    verts_y.append(float(py) - cy)
                loop_vert_offsets.append(len(verts_x))
            shape_loop_offsets.append(len(loop_vert_offsets) - 1)
        unit_shape_offsets.append(len(shape_loop_offsets) - 1)
        unit_areas.append(float(unit.area))
        unit_angles_flat.extend(angles)
        unit_angle_offsets.append(len(unit_angles_flat))

    static_loop_offsets = [0]
    static_vert_offsets = [0]
    static_x = []
    static_y = []
    for sh in static_shapes:
        for poly in sh:
            if len(poly) < 3:
                continue
            for px, py in poly:
                static_x.append(float(px))
                static_y.append(float(py))
            static_vert_offsets.append(len(static_x))
        static_loop_offsets.append(len(static_vert_offsets) - 1)

    def _to_c_int(seq):
        return (ctypes.c_int * max(1, len(seq)))(*(seq if seq else (0,)))

    def _to_c_dbl(seq):
        return (ctypes.c_double * max(1, len(seq)))(*(seq if seq else (0.0,)))

    nu = len(units)
    c_out_scale = ctypes.c_double(0.0)
    c_out_angle_idx = (ctypes.c_int * nu)()
    c_out_tx = (ctypes.c_double * nu)()
    c_out_ty = (ctypes.c_double * nu)()
    c_out_cov = ctypes.c_double(0.0)

    budget = 2.0 if not scale_to_fit else (0.7 if nu <= 8 else (2.85 if nu <= 96 else 3.4))
    _progress(progress_cb, stage="optimizing", percent=45.0, done=0, total=1)
    ok = lib.uvgpt_pack_solve(
        nu,
        _to_c_int(unit_shape_offsets),
        _to_c_int(shape_loop_offsets),
        _to_c_int(loop_vert_offsets),
        _to_c_dbl(verts_x),
        _to_c_dbl(verts_y),
        _to_c_dbl(unit_areas),
        len(static_shapes),
        _to_c_int(static_loop_offsets),
        _to_c_int(static_vert_offsets),
        _to_c_dbl(static_x),
        _to_c_dbl(static_y),
        _to_c_int(unit_angle_offsets),
        _to_c_dbl(unit_angles_flat),
        float(margin),
        1 if scale_to_fit else 0,
        float(min_scale_hint),
        float(budget),
        0,
        ctypes.byref(c_out_scale),
        c_out_angle_idx,
        c_out_tx,
        c_out_ty,
        ctypes.byref(c_out_cov),
    )
    def _fallback_source_or_none():
        if source_layout is not None:
            _progress(progress_cb, stage="optimizing", percent=93.0, done=1, total=1)
            return 1.0, source_layout
        return None

    if not ok:
        return _fallback_source_or_none()

    scale = float(c_out_scale.value)
    if not math.isfinite(scale) or scale <= 0.0 or (source_layout is not None and scale < 1.0 - 1.0e-6):
        return _fallback_source_or_none()

    # Exact verification against Python geometry oracle
    idx = SpatialIndex(44)
    for sh in static_shapes:
        idx.insert(sh)

    placements = {}
    for i, (unit, center, angles) in enumerate(unit_meta_py):
        ai = int(c_out_angle_idx[i])
        if ai < 0 or ai >= len(angles):
            return _fallback_source_or_none()
        win_angle = angles[ai]
        var = _build_unit_variant(unit, win_angle, 1.0, center)
        px = float(c_out_tx[i]) + scale * var.origin_shift[0]
        py = float(c_out_ty[i]) + scale * var.origin_shift[1]
        if _variant_conflicts(var, scale, px, py, idx, margin):
            return _fallback_source_or_none()
        for sh_local in var.member_shapes:
            placed_sh = transform_shape(sh_local, scale=scale, tx=px, ty=py)
            if not _shape_fit_tile(placed_sh, margin):
                return _fallback_source_or_none()
            idx.insert(placed_sh)
        placements[unit.key] = (px, py, var)

    _progress(progress_cb, stage="optimizing", percent=93.0, done=1, total=1)
    return scale, placements


__all__=["SCHEMA","RESULT_SCHEMA","solve_pack","shape_sets_conflict","exact_follower_writes","boundary_loops_from_segments"]
