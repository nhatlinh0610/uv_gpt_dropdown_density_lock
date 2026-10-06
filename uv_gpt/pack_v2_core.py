"""Pure external-process Pack V2 solver.

The Blender owner thread only captures primitive mesh/UV records.  This module
reconstructs UV islands, keeps stacked followers exact by loop correspondence,
and packs irregular concave silhouettes with a coarse candidate grid plus an
exact boundary collision refinement.  No Blender imports are allowed here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import gc
import math
from pathlib import Path
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
class PackUnit:
    key: tuple[int, ...]
    representative: IslandState
    follower_mappings: list[tuple[IslandState, tuple[tuple[tuple[int, int], tuple[int, int]], ...]]] = field(default_factory=list)
    local_loop_uvs: dict[tuple[int, int], Point] = field(default_factory=dict)
    local_shape: ShapeLoops = ()
    width: float = 0.0
    height: float = 0.0
    area: float = 0.0
    rotation: int = 0


class SpatialIndex:
    def __init__(self, resolution=40):
        self.resolution = max(8, int(resolution))
        self.shapes: list[ShapeLoops] = []
        self.bounds: list[tuple[float, float, float, float]] = []
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

    def insert(self, shape: ShapeLoops):
        idx = len(self.shapes)
        bounds = shape_bounds(shape)
        self.shapes.append(shape)
        self.bounds.append(bounds)
        for cell in self._cells(bounds):
            self.buckets.setdefault(cell, set()).add(idx)

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


def shape_sets_conflict(left: ShapeLoops, right: ShapeLoops, margin=0.0):
    """Exact boundary collision for polygon sets with holes via odd-even fill."""
    if not left or not right:
        return False
    lm = max(0.0, float(margin))
    lb = shape_bounds(left)
    rb = shape_bounds(right)
    if (
        lb[1] + lm < rb[0]
        or rb[1] + lm < lb[0]
        or lb[3] + lm < rb[2]
        or rb[3] + lm < lb[2]
    ):
        return False
    for lp in left:
        for rp in right:
            le = _edges(lp)
            re = _edges(rp)
            for a, b in le:
                for c, d in re:
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
        anchor_choices.sort(key=lambda item: (item[0], item[1]))
        for _match_count, candidate_anchor, master_anchors in anchor_choices:
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


def _build_pack_units(snapshot, islands, movable, preserve_stacks, progress_cb=None):
    if not preserve_stacks:
        return [PackUnit(key=i.face_key, representative=i) for i in movable]

    # Exact graphs are only needed for likely coincident stack buckets.
    exact_snapshot = dict(snapshot)
    exact_snapshot["schema"] = pro_exact_v2_core.SCHEMA
    exact_all = pro_exact_v2_core.build_exact_islands(exact_snapshot, selected_only=False)
    exact_by_key = {item.face_key: item for item in exact_all}
    for island in islands:
        exact = exact_by_key.get(island.face_key)
        if exact is not None:
            island.graph = exact.graph

    buckets = defaultdict(list)
    for island in movable:
        buckets[_stack_signature(island)].append(island)
    units = []
    done = 0
    for members in buckets.values():
        members = sorted(members, key=lambda i: i.face_key)
        if len(members) == 1 or members[0].graph is None:
            units.extend(PackUnit(key=i.face_key, representative=i) for i in members)
            continue
        representative = members[0]
        unit = PackUnit(key=representative.face_key, representative=representative)
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
        units.extend(PackUnit(key=i.face_key, representative=i) for i in leftovers)
        done += len(members)
        _progress(progress_cb, stage="stacks", percent=8.0, done=done, total=max(1, len(movable)))
    return units


def _prepare_unit(unit: PackUnit, rotation_mode="NONE", density_scale=1.0):
    loop_uvs = dict(unit.representative.loop_uvs)
    if density_scale != 1.0:
        loop_uvs = _apply_similarity(loop_uvs, scale=density_scale)
    angle = 0.0
    if rotation_mode == "ROT_90":
        angle = math.pi * 0.5
    elif rotation_mode == "ROT_180":
        angle = math.pi
    elif rotation_mode == "CARDINAL":
        shape = _shape_from_loop_uvs(unit.representative, loop_uvs)
        minx, maxx, miny, maxy = _uv_coordinate_bounds(loop_uvs, shape)
        if maxy - miny > maxx - minx:
            angle = math.pi * 0.5
    if angle:
        loop_uvs = _apply_similarity(loop_uvs, angle=angle)
    shape = _shape_from_loop_uvs(unit.representative, loop_uvs)
    local_uvs, local_shape, width, height = _normalize_local(loop_uvs, shape)
    unit.local_loop_uvs = local_uvs
    unit.local_shape = local_shape
    unit.width = width
    unit.height = height
    unit.area = max(unit.representative.uv_area * density_scale * density_scale, 1.0e-12)


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
    source placement after checking every representative loop, every stacked
    follower, tile bounds, and the exact configured-margin collision oracle.
    A source with a real overlap, changed orientation, or changed scale returns
    ``None`` and therefore follows the normal solver path.
    """
    index = SpatialIndex(44)
    for shape in static_shapes:
        # Existing static islands are intentionally preserved even when they
        # lie outside the movable tile envelope.  They still act as exact
        # collision blockers for the source-layout verification below.
        index.insert(shape)

    placements = {}
    for unit in units:
        representative = unit.representative
        if representative is None or not unit.local_shape or not unit.local_loop_uvs:
            return None

        min_x, _max_x, min_y, _max_y = shape_bounds(representative.boundary_loops)
        placement = (min_x, min_y)
        source_shape = transform_shape(
            unit.local_shape,
            scale=1.0,
            tx=placement[0],
            ty=placement[1],
        )

        # Direct loop labels are the orientation/scale proof.  A rotated or
        # density-scaled preparation cannot pass this check accidentally via
        # a symmetric shape whose unordered point set happens to match.
        for key, local_uv in unit.local_loop_uvs.items():
            source_uv = representative.loop_uvs.get(key)
            if source_uv is None:
                return None
            if (
                abs(local_uv[0] + placement[0] - source_uv[0])
                > _SOURCE_LAYOUT_TOLERANCE
                or abs(local_uv[1] + placement[1] - source_uv[1])
                > _SOURCE_LAYOUT_TOLERANCE
            ):
                return None
        if not _shape_coordinates_close(source_shape, representative.boundary_loops):
            return None
        if not _shape_fit_tile(source_shape, margin):
            return None

        nearby = index.query(source_shape, margin=margin)
        if any(shape_sets_conflict(source_shape, other, margin=margin) for other in nearby):
            return None
        index.insert(source_shape)
        placements[unit.key] = placement

        # A stacked unit is represented by one collision shape.  Its follower
        # still has to prove that the existing source is the exact stack that
        # the mapping will restore; otherwise normal solving must repack it.
        master_uvs = {
            key: (local_uv[0] + placement[0], local_uv[1] + placement[1])
            for key, local_uv in unit.local_loop_uvs.items()
        }
        for follower, mapping in unit.follower_mappings:
            follower_uvs = dict(exact_follower_writes(master_uvs, mapping))
            if set(follower_uvs) != set(follower.loop_uvs):
                return None
            for key, source_uv in follower.loop_uvs.items():
                candidate_uv = follower_uvs[key]
                if (
                    abs(candidate_uv[0] - source_uv[0]) > _SOURCE_LAYOUT_TOLERANCE
                    or abs(candidate_uv[1] - source_uv[1]) > _SOURCE_LAYOUT_TOLERANCE
                ):
                    return None
            follower_shape = _shape_from_loop_uvs(follower, follower_uvs)
            if not _shape_fit_tile(follower_shape, margin):
                return None
            if not _shape_coordinates_close(follower_shape, follower.boundary_loops):
                return None

    return placements


def _candidate_positions(width, height, obstacle_bounds, margin, seed=None, limit=None):
    lowx = margin
    lowy = margin
    highx = 1.0 - margin - width
    highy = 1.0 - margin - height
    if highx < lowx - _EPS or highy < lowy - _EPS:
        return ()
    candidates = {(lowx, lowy), (highx, lowy), (lowx, highy), (highx, highy)}
    seed_value = None
    if seed is not None:
        sx0 = max(lowx, min(highx, float(seed[0])))
        sy0 = max(lowy, min(highy, float(seed[1])))
        seed_value = (sx0, sy0)
        candidates.add(seed_value)
    # Coarse search samples the whole tile, allowing a concave shape to nest
    # inside another shape's AABB rather than only trying rectangle corners.
    sx = min(_GRID_SAMPLES, max(2, int((highx - lowx) * 24) + 2))
    sy = min(_GRID_SAMPLES, max(2, int((highy - lowy) * 24) + 2))
    for ix in range(sx + 1):
        x = lowx if sx == 0 else lowx + (highx - lowx) * ix / sx
        for iy in range(sy + 1):
            y = lowy if sy == 0 else lowy + (highy - lowy) * iy / sy
            candidates.add((x, y))
    for b in obstacle_bounds[-96:]:
        ominx, omaxx, ominy, omaxy = b
        xs = (omaxx + margin, ominx - width - margin, ominx, omaxx - width)
        ys = (omaxy + margin, ominy - height - margin, ominy, omaxy - height)
        for x in xs:
            if lowx - _EPS <= x <= highx + _EPS:
                candidates.add((max(lowx, min(highx, x)), lowy))
                for y in (ominy, omaxy - height, omaxy + margin, ominy - height - margin):
                    if lowy - _EPS <= y <= highy + _EPS:
                        candidates.add((max(lowx, min(highx, x)), max(lowy, min(highy, y))))
        for y in ys:
            if lowy - _EPS <= y <= highy + _EPS:
                candidates.add((lowx, max(lowy, min(highy, y))))
    # Compact bottom-left order, then envelope area as tie-break.
    ordered = sorted(candidates, key=lambda p: (p[1] + height, p[0] + width, p[1], p[0]))
    if seed_value is not None:
        ordered = [seed_value] + [p for p in ordered if p != seed_value]
    if limit is not None and len(ordered) > int(limit):
        ordered = ordered[: max(1, int(limit))]
    return tuple(ordered)


def _try_general_layout(units, static_shapes, scale, margin, progress_cb=None, seed_placements=None, seed_scale=None, candidate_limit=None, work_budget=None):
    index = SpatialIndex(44)
    obstacle_bounds = []
    for shape in static_shapes:
        index.insert(shape)
        obstacle_bounds.append(shape_bounds(shape))
    placements = {}
    ordered = sorted(units, key=lambda u: (-u.area, -max(u.width, u.height), u.key))
    total = len(ordered)
    checks = 0
    if candidate_limit is None:
        candidate_limit = 192 if total <= 32 else 96 if total <= 64 else 56 if total <= 128 else 28
    if work_budget is None:
        work_budget = max(2048, total * candidate_limit)
    for pos, unit in enumerate(ordered):
        width = unit.width * scale
        height = unit.height * scale
        placed = None
        seed = None
        if seed_placements and unit.key in seed_placements:
            bx, by = seed_placements[unit.key]
            if seed_scale and seed_scale > 1.0e-12:
                old_w = unit.width * seed_scale
                old_h = unit.height * seed_scale
                center_x = bx + old_w * 0.5
                center_y = by + old_h * 0.5
                seed = (center_x - width * 0.5, center_y - height * 0.5)
            else:
                seed = (bx, by)
        for x, y in _candidate_positions(width, height, obstacle_bounds, margin, seed=seed, limit=candidate_limit):
            checks += 1
            if checks > work_budget:
                return None
            shape = transform_shape(unit.local_shape, scale=scale, tx=x, ty=y)
            if not _shape_fit_tile(shape, margin):
                continue
            nearby = index.query(shape, margin=margin)
            if any(shape_sets_conflict(shape, other, margin=margin) for other in nearby):
                continue
            placed = (x, y)
            index.insert(shape)
            obstacle_bounds.append(shape_bounds(shape))
            placements[unit.key] = placed
            break
        if placed is None:
            return None
        if pos % 8 == 0 or pos + 1 == total:
            _progress(progress_cb, stage="placing", percent=20.0 + 55.0 * (pos + 1) / max(1, total), done=pos + 1, total=max(1, total))
    return placements



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
    # Static blockers reserve their AABB only in the coarse phase.  Exact
    # refinement later can recover concave cavities that this intentionally
    # conservative pass ignores.
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
        # Reserve the configured clearance on both sides of the rectangle.
        # The previous half-margin envelope left adjacent movable islands only
        # ``margin / 2`` apart, which the exact margin oracle correctly rejects.
        used=(x-reserve,y-reserve,w+2.0*reserve,h+2.0*reserve)
        free=_split_free_rects(free,used)
    return placements


def _solve_rect_baseline(units, static_shapes, margin, scale_to_fit):
    if not units: return 1.0,{}
    if not scale_to_fit:
        return 1.0,_maxrect_layout(units,static_shapes,1.0,margin)
    high=_general_scale_upper(units,margin); low=0.0; best=None
    for _ in range(12):
        mid=(low+high)*0.5
        p=_maxrect_layout(units,static_shapes,mid,margin)
        if p is None: high=mid
        else: low=mid; best=p
    return low,best


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
        upper = min(upper, tile / max(unit.width, 1e-10), tile / max(unit.height, 1e-10))
        total_area += max(0.0, float(unit.area))
    # Area is a strict upper bound for non-overlapping filled UV shapes and is
    # dramatically tighter than the single-island dimension bound for large
    # selections.  A small numerical cushion keeps binary search from clipping
    # the true optimum because UV area estimates can differ from filled shapes.
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
            # Rectangle-safe layout is already exact-safe.
            return 1.0, baseline
        return 1.0, _try_general_layout(units, static_shapes, 1.0, margin, progress_cb)
    if baseline is None:
        baseline_scale, baseline = 0.0, {}

    # When every movable and blocker is an axis-aligned rectangle, exact
    # silhouette refinement cannot create legal AABB overlap; MaxRects is the
    # correct fast terminal path for large regular selections.
    if all(_shape_is_axis_aligned_rect(unit.local_shape) for unit in units) and all(
        _shape_is_axis_aligned_rect(shape) for shape in static_shapes
    ):
        _progress(progress_cb, stage="optimizing", percent=93.0, done=1, total=1)
        return baseline_scale, baseline

    upper = _general_scale_upper(units, margin)
    low = baseline_scale
    best = baseline
    high = max(low, upper)
    count = len(units)
    # Large selections get a deliberately bounded concave probe just above the
    # fast MaxRects baseline.  If the probe cannot prove a better exact layout
    # within its work budget we keep the already-valid baseline immediately,
    # preventing the exponential-feeling stalls common to irregular packing.
    if count > 64:
        growth = 1.06 if count <= 128 else 1.03
        probe = min(high, baseline_scale * growth)
        if probe > baseline_scale + 1.0e-9:
            placements = _try_general_layout(
                units,
                static_shapes,
                probe,
                margin,
                None,
                seed_placements=baseline,
                seed_scale=max(baseline_scale, 1.0e-12),
                candidate_limit=48 if count <= 128 else 24,
                work_budget=max(2500, count * (28 if count <= 128 else 14)),
            )
            if placements is not None:
                low = probe
                best = placements
        _progress(progress_cb, stage="optimizing", percent=93.0, done=1, total=1)
        return low, best

    steps = 4 if count <= 32 else 2
    for step in range(steps):
        mid = (low + high) * 0.5
        placements = _try_general_layout(
            units,
            static_shapes,
            mid,
            margin,
            None,
            seed_placements=baseline,
            seed_scale=max(baseline_scale, 1.0e-12),
            candidate_limit=192 if count <= 32 else 96,
            work_budget=max(4096, count * (192 if count <= 32 else 96)),
        )
        if placements is None:
            high = mid
        else:
            low = mid
            best = placements
        _progress(progress_cb, stage="optimizing", percent=75.0 + 18.0 * (step + 1) / steps, done=step + 1, total=steps)
    return low, best


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


def _sym_rotation_angle(island, rotation_mode, loop_uvs):
    if rotation_mode == "ROT_90":
        return math.pi * 0.5
    if rotation_mode == "ROT_180":
        return math.pi
    if rotation_mode == "CARDINAL":
        shape = _shape_from_loop_uvs(island, loop_uvs)
        minx, maxx, miny, maxy = _uv_coordinate_bounds(loop_uvs, shape)
        if maxy - miny > maxx - minx:
            return math.pi * 0.5
    return 0.0


def _sym_units(
    movable,
    axis_kind,
    density_scales,
    exact_by_key=None,
    rotation_mode="NONE",
    followers_by_key=None,
):
    """Build symmetry units with a proven loop correspondence for every pair.

    Shape descriptors are only a cheap candidate generator.  A pair is
    admitted when the exact topology mapper returns a complete bijection for
    both islands.  The master coordinates are then copied through that
    correspondence and reflected in the requested local axis, so the final
    placement has a loop-level mirror invariant instead of a PCA-only guess.
    Islands without a proven mapping remain rigid similarity-transformed
    singles; their UV shape is never fabricated from another island.
    """
    records = [_shape_feature(i) for i in movable]
    candidate_pairs, singles = pack_geometry.pair_similar_records(records, max_distance=0.20)
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
        uv = dict(island.loop_uvs)
        ds = density_scales.get(island.face_key, 1.0)
        if ds != 1.0:
            uv = _apply_similarity(uv, scale=ds)
        angle = _sym_rotation_angle(island, rotation_mode, uv)
        if angle:
            uv = _apply_similarity(uv, angle=angle)
        shape = _shape_from_loop_uvs(island, uv)
        local_uv, local_shape, width, height = centered(uv, shape)
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
                    ),
                ),
                "area": island.uv_area,
            }
        )
    units.sort(key=lambda unit: (0 if unit["kind"] == "pair" else 1, -unit["area"]))
    return units, accepted_pairs, len(singles)


def _sym_member_shape(member, scale, center):
    return transform_shape(member[2], scale=scale, tx=center[0], ty=center[1])


def _sym_member_bounds(member, scale, center):
    min_x, max_x, min_y, max_y = shape_bounds(member[2])
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
                    shapes=tuple(
                        _sym_member_shape(member,scale,centers[i])
                        for i,member in enumerate(members)
                    )
                    if not all(_shape_fit_tile(shape,margin) for shape in shapes):
                        continue
                    own_conflict=False
                    if len(shapes)>1:
                        left_bounds=shape_bounds(shapes[0]); right_bounds=shape_bounds(shapes[1])
                        if _bounds_conflict(left_bounds,right_bounds,margin):
                            own_conflict=shape_sets_conflict(
                                shapes[0],shapes[1],margin=margin
                            )
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
            shapes=tuple(
                _sym_member_shape(member,low,centers[member_index])
                for member_index,member in enumerate(unit["members"])
            )
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
            if len(shapes)>1:
                left_bounds=shape_bounds(shapes[0]); right_bounds=shape_bounds(shapes[1])
                if not (
                    left_bounds[1]+margin < right_bounds[0]
                    or right_bounds[1]+margin < left_bounds[0]
                    or left_bounds[3]+margin < right_bounds[2]
                    or right_bounds[3]+margin < left_bounds[2]
                ) and shape_sets_conflict(shapes[0],shapes[1],margin=margin):
                    valid=False
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
    x,y=placement
    master_uvs={key:(uv[0]*scale+x,uv[1]*scale+y) for key,uv in unit.local_loop_uvs.items()}
    writes=[(key[0],key[1],uv[0],uv[1]) for key,uv in master_uvs.items()]
    for follower,mapping in unit.follower_mappings:
        for key,uv in exact_follower_writes(master_uvs,mapping):
            writes.append((key[0],key[1],uv[0],uv[1]))
    return writes


def solve_pack(snapshot: dict[str, Any], progress_cb=None) -> dict[str, Any]:
    started=time.perf_counter()
    options=dict(snapshot.get("options") or {})
    mode=str(options.get("mode","selected"))
    if mode not in {"selected","whole","symmetry"}: raise ValueError("invalid Pack V2 mode")
    margin=max(0.0,float(options.get("margin",0.001)))
    preserve=bool(options.get("preserve_stacks",False))
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

    writes=[]; pair_count=single_count=0; stack_followers=0
    if mode=="symmetry":
        symmetry_pack_units = _build_pack_units(
            snapshot,
            islands,
            movable,
            preserve,
            progress_cb,
        )
        symmetry_representatives = [unit.representative for unit in symmetry_pack_units]
        followers_by_key = {
            unit.representative.face_key: tuple(unit.follower_mappings)
            for unit in symmetry_pack_units
        }
        stack_followers = sum(len(unit.follower_mappings) for unit in symmetry_pack_units)
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
    else:
        units=_build_pack_units(snapshot,islands,movable,preserve,progress_cb)
        stack_followers=sum(len(unit.follower_mappings) for unit in units)
        for unit in units: _prepare_unit(unit,rotation_mode=rotation_mode,density_scale=1.0)
        _progress(progress_cb,stage="shapes",percent=15.0,done=len(units),total=max(1,len(units)))
        scale,placements=_solve_general(units,static_shapes,margin,scale_to_fit,progress_cb)
        if placements is None: raise RuntimeError("Pack V2 could not find a collision-free 0-1 layout")
        for unit in units: writes.extend(_emit_unit_writes(unit,scale,placements[unit.key]))

    _progress(progress_cb,stage="done",percent=100.0,done=len(movable),total=len(movable))
    return {
        "schema":RESULT_SCHEMA,
        "mode":mode,
        "writes":tuple(writes),
        "movable_count":len(movable),
        "static_count":len(static),
        "scale":float(scale),
        "stack_followers":stack_followers,
        "pair_count":pair_count,
        "single_count":single_count,
        "elapsed_ms":(time.perf_counter()-started)*1000.0,
    }


__all__=["SCHEMA","RESULT_SCHEMA","solve_pack","shape_sets_conflict","exact_follower_writes","boundary_loops_from_segments"]
