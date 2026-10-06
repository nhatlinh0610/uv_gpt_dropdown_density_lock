import math
import json
import os
from pathlib import Path
import pickle
import shutil
import subprocess
import tempfile
import threading
import time

import bpy
import bmesh
from mathutils import Vector

from . import island_tools, overlay, pack_geometry, pro_process_runtime, texel_density, uv_utils
from . import symmetry_pair


STACK_KEY_PRECISION = 5
PACK_EPSILON = 1e-8


def _island_face_key(island):
    return tuple(sorted({loop.face.index for loop in island}))


def _pre_rotate_islands(islands, uv_layer, mode):
    if mode == "NONE":
        return
    for island in islands:
        center = island_tools.get_island_center(island, uv_layer)
        if mode == "ROT_90":
            uv_utils.rotate_island(island, uv_layer, center, math.radians(90.0))
        elif mode == "ROT_180":
            uv_utils.rotate_island(island, uv_layer, center, math.radians(180.0))
        elif mode == "CARDINAL":
            min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
            if (max_v - min_v) > (max_u - min_u):
                uv_utils.rotate_island(island, uv_layer, center, math.radians(90.0))


def _islands_inside_tile(islands, uv_layer):
    epsilon = 1e-6
    for island in islands:
        for loop in island:
            uv = loop[uv_layer].uv
            if uv.x < -epsilon or uv.x > 1.0 + epsilon:
                return False
            if uv.y < -epsilon or uv.y > 1.0 + epsilon:
                return False
    return True


def _island_signature(island, uv_layer):
    bounds = tuple(
        round(value, STACK_KEY_PRECISION)
        for value in island_tools.get_island_bounds(island, uv_layer)
    )
    area = round(island_tools.get_island_area(island, uv_layer), STACK_KEY_PRECISION)
    uv_points = sorted(
        {
            (
                round(loop[uv_layer].uv.x, STACK_KEY_PRECISION),
                round(loop[uv_layer].uv.y, STACK_KEY_PRECISION),
            )
            for loop in island
        }
    )
    return (bounds, area, tuple(uv_points))


def _island_loop_records(island, uv_layer):
    records = []
    for face in island_tools.island_faces(island):
        for local_index, loop in enumerate(face.loops):
            records.append((face.index, local_index))
    return records


def _uv_snapshot(islands, uv_layer):
    snapshot = {}
    for face in {loop.face for island in islands for loop in island}:
        for local_index, loop in enumerate(face.loops):
            snapshot[(face.index, local_index)] = loop[uv_layer].uv.copy()
    return snapshot


def _restore_uv_snapshot(bm, uv_layer, snapshot):
    for (face_index, local_index), uv in snapshot.items():
        loop = _loop_from_record(bm, face_index, local_index)
        if loop is not None:
            loop[uv_layer].uv = uv.copy()


def _uv_snapshot_matches(bm, uv_layer, snapshot):
    for (face_index, local_index), expected in snapshot.items():
        loop = _loop_from_record(bm, face_index, local_index)
        if loop is None:
            return False
        actual = loop[uv_layer].uv
        if actual.x != expected.x or actual.y != expected.y:
            return False
    return True


def _pin_snapshot(islands, uv_layer):
    snapshot = {}
    for face in {loop.face for island in islands for loop in island}:
        for local_index, loop in enumerate(face.loops):
            snapshot[(face.index, local_index)] = bool(getattr(loop[uv_layer], "pin_uv", False))
    return snapshot


def _restore_pin_snapshot(bm, uv_layer, snapshot):
    for (face_index, local_index), value in snapshot.items():
        loop = _loop_from_record(bm, face_index, local_index)
        if loop is None:
            continue
        try:
            loop[uv_layer].pin_uv = bool(value)
        except Exception:
            pass


def _pin_static_islands_for_pack(moving_islands, static_islands, uv_layer):
    for island in moving_islands:
        for loop in island:
            try:
                loop[uv_layer].pin_uv = False
            except Exception:
                pass
    for island in static_islands:
        for loop in island:
            try:
                loop[uv_layer].pin_uv = True
            except Exception:
                pass


def _loop_from_record(bm, face_index, local_index):
    if face_index >= len(bm.faces):
        return None
    face = bm.faces[face_index]
    face_loops = list(face.loops)
    if local_index >= len(face_loops):
        return None
    return face_loops[local_index]


def _loops_from_records(bm, records):
    loops = []
    for face_index, local_index in records:
        loop = _loop_from_record(bm, face_index, local_index)
        if loop is not None:
            loops.append(loop)
    return loops


def _records_bounds(bm, uv_layer, records):
    loops = _loops_from_records(bm, records)
    return island_tools.get_island_bounds(loops, uv_layer)


def _scope_static_islands(all_islands, scope_islands):
    scope_keys = {_island_face_key(island) for island in scope_islands}
    return [
        island
        for island in all_islands
        if _island_face_key(island) not in scope_keys
    ]


def _rect_intersects(a, b):
    return not (
        a["max_u"] <= b["min_u"] + PACK_EPSILON
        or a["min_u"] >= b["max_u"] - PACK_EPSILON
        or a["max_v"] <= b["min_v"] + PACK_EPSILON
        or a["min_v"] >= b["max_v"] - PACK_EPSILON
    )


def _inflate_rect(rect, margin):
    return {
        "min_u": rect["min_u"] - margin,
        "max_u": rect["max_u"] + margin,
        "min_v": rect["min_v"] - margin,
        "max_v": rect["max_v"] + margin,
    }


def _rect_from_bounds(bounds):
    min_u, max_u, min_v, max_v = bounds
    return {
        "min_u": min_u,
        "max_u": max_u,
        "min_v": min_v,
        "max_v": max_v,
    }


def _clip_rect_to_tile(rect):
    clipped = {
        "min_u": max(rect["min_u"], 0.0),
        "max_u": min(rect["max_u"], 1.0),
        "min_v": max(rect["min_v"], 0.0),
        "max_v": min(rect["max_v"], 1.0),
    }
    if clipped["max_u"] <= clipped["min_u"] or clipped["max_v"] <= clipped["min_v"]:
        return None
    return clipped


def _static_blockers(static_islands, uv_layer, margin):
    blockers = []
    for island in static_islands:
        rect = _clip_rect_to_tile(_rect_from_bounds(island_tools.get_island_bounds(island, uv_layer)))
        if rect is not None:
            blockers.append(_inflate_rect(rect, margin))
    return blockers


def _find_free_position(width, height, blockers, margin):
    xs = {margin}
    ys = {margin}
    for blocker in blockers:
        xs.add(max(margin, blocker["max_u"]))
        ys.add(max(margin, blocker["max_v"]))

    max_u = 1.0 - margin
    max_v = 1.0 - margin
    for y in sorted(ys):
        if y + height > max_v + PACK_EPSILON:
            continue
        for x in sorted(xs):
            if x + width > max_u + PACK_EPSILON:
                continue
            rect = {
                "min_u": x,
                "max_u": x + width,
                "min_v": y,
                "max_v": y + height,
            }
            if not any(_rect_intersects(rect, blocker) for blocker in blockers):
                return x, y
    return None


def _layout_with_static_islands(rectangles, static_islands, uv_layer, scale, margin):
    blockers = _static_blockers(static_islands, uv_layer, margin)
    placements = {}
    ordered = sorted(rectangles, key=lambda item: item["height"], reverse=True)
    for item in ordered:
        width = max(item["width"] * scale, 1e-9)
        height = max(item["height"] * scale, 1e-9)
        position = _find_free_position(width, height, blockers, margin)
        if position is None:
            return None
        x, y = position
        placements[item["index"]] = (x, y)
        blockers.append(
            _inflate_rect(
                {
                    "min_u": x,
                    "max_u": x + width,
                    "min_v": y,
                    "max_v": y + height,
                },
                margin,
            )
        )
    return placements


def _solve_rectangle_layout(rectangles, static_islands, uv_layer, margin, scale_to_fit=True):
    if scale_to_fit:
        high = 1.0
        while _layout_with_static_islands(rectangles, static_islands, uv_layer, high, margin) and high < 1024.0:
            high *= 2.0
        low = 0.0
        best_placements = None
        for _ in range(36):
            mid = (low + high) * 0.5
            placements = _layout_with_static_islands(rectangles, static_islands, uv_layer, mid, margin)
            if placements is None:
                high = mid
            else:
                low = mid
                best_placements = placements
        return low, best_placements

    scale = 1.0
    placements = _layout_with_static_islands(rectangles, static_islands, uv_layer, scale, margin)
    return scale, placements


def _layout_symmetry_on_axis(rectangles, static_islands, uv_layer, axis_kind, margin, scale):
    blockers = _static_blockers(static_islands, uv_layer, margin)
    placements = {}

    if axis_kind == 'V':
        candidates = {margin}
        for blocker in blockers:
            candidates.add(max(margin, blocker['max_u']))
        ordered = sorted(rectangles, key=lambda item: item['width'], reverse=True)
        fixed_y_cache = {}
        for item in ordered:
            width = max(item['width'] * scale, 1e-9)
            height = max(item['height'] * scale, 1e-9)
            y = 0.5 - height * 0.5
            if y < margin - PACK_EPSILON or y + height > 1.0 - margin + PACK_EPSILON:
                return None
            placed = False
            for x in sorted(candidates):
                if x + width > 1.0 - margin + PACK_EPSILON:
                    continue
                rect = {
                    'min_u': x,
                    'max_u': x + width,
                    'min_v': y,
                    'max_v': y + height,
                }
                if any(_rect_intersects(rect, blocker) for blocker in blockers):
                    continue
                placements[item['index']] = (x, y)
                blockers.append(_inflate_rect(rect, margin))
                candidates.add(max(margin, rect['max_u']))
                placed = True
                break
            if not placed:
                return None
        return placements

    candidates = {margin}
    for blocker in blockers:
        candidates.add(max(margin, blocker['max_v']))
    ordered = sorted(rectangles, key=lambda item: item['height'], reverse=True)
    for item in ordered:
        width = max(item['width'] * scale, 1e-9)
        height = max(item['height'] * scale, 1e-9)
        x = 0.5 - width * 0.5
        if x < margin - PACK_EPSILON or x + width > 1.0 - margin + PACK_EPSILON:
            return None
        placed = False
        for y in sorted(candidates):
            if y + height > 1.0 - margin + PACK_EPSILON:
                continue
            rect = {
                'min_u': x,
                'max_u': x + width,
                'min_v': y,
                'max_v': y + height,
            }
            if any(_rect_intersects(rect, blocker) for blocker in blockers):
                continue
            placements[item['index']] = (x, y)
            blockers.append(_inflate_rect(rect, margin))
            candidates.add(max(margin, rect['max_v']))
            placed = True
            break
        if not placed:
            return None
    return placements


def _solve_symmetry_axis_layout(rectangles, static_islands, uv_layer, axis_kind, margin):
    high = 1.0
    while _layout_symmetry_on_axis(rectangles, static_islands, uv_layer, axis_kind, margin, high) and high < 1024.0:
        high *= 2.0
    low = 0.0
    best_placements = None
    for _ in range(36):
        mid = (low + high) * 0.5
        placements = _layout_symmetry_on_axis(rectangles, static_islands, uv_layer, axis_kind, margin, mid)
        if placements is None:
            high = mid
        else:
            low = mid
            best_placements = placements
    return low, best_placements


def _pack_islands_around_static(bm, uv_layer, islands, static_islands, margin, scale_to_fit=True):
    rectangles = []
    for index, island in enumerate(islands):
        min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
        rectangles.append(
            {
                "index": index,
                "min_u": min_u,
                "min_v": min_v,
                "width": max(max_u - min_u, 1e-6),
                "height": max(max_v - min_v, 1e-6),
            }
        )

    if scale_to_fit:
        high = 1.0
        while _layout_with_static_islands(rectangles, static_islands, uv_layer, high, margin) and high < 1024.0:
            high *= 2.0
        low = 0.0
        best_placements = None
        for _ in range(36):
            mid = (low + high) * 0.5
            placements = _layout_with_static_islands(rectangles, static_islands, uv_layer, mid, margin)
            if placements is None:
                high = mid
            else:
                low = mid
                best_placements = placements
        scale = low
        placements = best_placements
    else:
        scale = 1.0
        placements = _layout_with_static_islands(rectangles, static_islands, uv_layer, scale, margin)

    if placements is None:
        return False

    rect_by_index = {item["index"]: item for item in rectangles}
    for index, island in enumerate(islands):
        item = rect_by_index[index]
        target_u, target_v = placements[index]
        for loop in island:
            uv = loop[uv_layer].uv
            uv.x = target_u + (uv.x - item["min_u"]) * scale
            uv.y = target_v + (uv.y - item["min_v"]) * scale
    return True


def _stack_preserve_plan(islands, uv_layer):
    buckets = {}
    for island in islands:
        buckets.setdefault(_island_signature(island, uv_layer), []).append(island)

    pack_islands = []
    pack_records = []
    stack_groups = []
    for group in buckets.values():
        representative = group[0]
        representative_records = _island_loop_records(representative, uv_layer)
        pack_islands.append(representative)
        pack_records.append(representative_records)
        if len(group) > 1:
            stack_groups.append(
                {
                    "representative": representative_records,
                    "source_bounds": island_tools.get_island_bounds(representative, uv_layer),
                    "followers": [
                        _island_loop_records(island, uv_layer) for island in group[1:]
                    ],
                }
            )
    return pack_islands, pack_records, stack_groups


def _expand_with_stacked_islands(selected_islands, all_islands, uv_layer):
    selected_signatures = {
        _island_signature(island, uv_layer) for island in selected_islands
    }
    return [
        island
        for island in all_islands
        if _island_signature(island, uv_layer) in selected_signatures
    ]


def _restore_stacked_groups(bm, uv_layer, stack_groups):
    restored = 0
    for group in stack_groups:
        source_min_u, source_max_u, source_min_v, source_max_v = group["source_bounds"]
        target_min_u, target_max_u, target_min_v, target_max_v = _records_bounds(
            bm,
            uv_layer,
            group["representative"],
        )
        source_width = max(source_max_u - source_min_u, 1e-8)
        source_height = max(source_max_v - source_min_v, 1e-8)
        target_width = target_max_u - target_min_u
        target_height = target_max_v - target_min_v
        scale_u = target_width / source_width
        scale_v = target_height / source_height

        for follower_records in group["followers"]:
            moved = False
            for face_index, local_index in follower_records:
                loop = _loop_from_record(bm, face_index, local_index)
                if loop is None:
                    continue
                uv = loop[uv_layer].uv
                uv.x = target_min_u + (uv.x - source_min_u) * scale_u
                uv.y = target_min_v + (uv.y - source_min_v) * scale_v
                moved = True
            if moved:
                restored += 1
    return restored


def _selected_average_px_cm(context, islands, obj, uv_layer):
    values = []
    for island in islands:
        value = texel_density.calculate_island_px_cm(context, obj, island, uv_layer)
        if value > 0.0:
            values.append(value)
    if not values:
        return None
    return sum(values) / len(values)


def _normalize_selected_density(context, islands, obj, uv_layer):
    target = _selected_average_px_cm(context, islands, obj, uv_layer)
    if target is None:
        return 0, None
    changed = 0
    for island in islands:
        current = texel_density.calculate_island_px_cm(context, obj, island, uv_layer)
        if current <= 0.0:
            continue
        scale = target / current
        center = island_tools.get_island_center(island, uv_layer)
        uv_utils.scale_island(island, uv_layer, center, scale)
        changed += 1
    return changed, target


def _island_unique_point_count(island, uv_layer):
    seen = set()
    for loop in island:
        uv = loop[uv_layer].uv
        seen.add((round(float(uv.x), 6), round(float(uv.y), 6)))
    return len(seen)


def _island_pairing_record(island, uv_layer):
    faces = island_tools.island_faces(island)
    face_sizes = tuple(sorted(len(face.loops) for face in faces))
    point_count = _island_unique_point_count(island, uv_layer)
    min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
    width = max(max_u - min_u, 1e-8)
    height = max(max_v - min_v, 1e-8)
    aspect = max(width, height) / max(min(width, height), 1e-8)

    data = symmetry_pair._island_data(island, uv_layer)
    points = symmetry_pair._unique_uv_points(island, uv_layer)
    center = data['centroid']
    radius = max(data['rms_radius'], 1e-8)
    normalized = sorted(((point - center).length / radius) for point in points)
    if normalized:
        count = min(12, len(normalized))
        quantiles = []
        for index in range(count):
            pos = round(index * (len(normalized) - 1) / max(1, count - 1))
            quantiles.append(round(float(normalized[pos]), 4))
        radial = tuple(quantiles)
    else:
        radial = ()

    return {
        'island': island,
        'topology': (len(faces), face_sizes, point_count),
        'feature': (round(aspect, 4), radial),
        'area': island_tools.get_island_area(island, uv_layer),
    }


def _pair_islands_for_symmetry(islands, uv_layer):
    records = [_island_pairing_record(island, uv_layer) for island in islands]
    pairs, singles = pack_geometry.pair_similar_records(records, max_distance=0.20)
    pairs = sorted(
        pairs,
        key=lambda pair: max(pair[0]['area'], pair[1]['area']),
        reverse=True,
    )
    singles = sorted(singles, key=lambda item: item['area'], reverse=True)
    return pairs, singles


def _align_pair_for_symmetry(left_island, right_island, uv_layer, axis_kind):
    left_data = symmetry_pair._island_data(left_island, uv_layer)
    right_data = symmetry_pair._island_data(right_island, uv_layer)

    left_radius = left_data.get('rms_radius') or 0.0
    right_radius = right_data.get('rms_radius') or 0.0
    scale = 1.0
    if left_radius > 1.0e-12 and right_radius > 1.0e-12:
        scale = left_radius / right_radius

    desired_axis = None
    ref_axis = left_data.get('principal_axis')
    if ref_axis is not None:
        desired_axis = symmetry_pair._mirror_direction(ref_axis, axis_kind)

    angle = symmetry_pair._rotation_angle(right_data.get('principal_axis'), desired_axis)
    symmetry_pair._apply_similarity_transform(
        right_island,
        uv_layer,
        right_data['centroid'],
        angle,
        scale,
    )


def _build_symmetry_units(islands, uv_layer, axis_kind, gap):
    pairs, singles = _pair_islands_for_symmetry(islands, uv_layer)
    units = []

    for left_record, right_record in pairs:
        left_island = left_record['island']
        right_island = right_record['island']
        _align_pair_for_symmetry(left_island, right_island, uv_layer, axis_kind)

        left_bounds = island_tools.get_island_bounds(left_island, uv_layer)
        right_bounds = island_tools.get_island_bounds(right_island, uv_layer)
        left_width = max(left_bounds[1] - left_bounds[0], 1e-8)
        left_height = max(left_bounds[3] - left_bounds[2], 1e-8)
        right_width = max(right_bounds[1] - right_bounds[0], 1e-8)
        right_height = max(right_bounds[3] - right_bounds[2], 1e-8)

        if axis_kind == 'V':
            unit_width = max(left_width, right_width)
            unit_height = left_height + right_height + gap
        else:
            unit_width = left_width + right_width + gap
            unit_height = max(left_height, right_height)

        units.append({
            'kind': 'pair',
            'members': [left_island, right_island],
            'width': unit_width,
            'height': unit_height,
            'left_width': left_width,
            'left_height': left_height,
            'right_width': right_width,
            'right_height': right_height,
        })

    for record in singles:
        island = record['island']
        bounds = island_tools.get_island_bounds(island, uv_layer)
        units.append({
            'kind': 'single',
            'members': [island],
            'width': max(bounds[1] - bounds[0], 1e-8),
            'height': max(bounds[3] - bounds[2], 1e-8),
        })

    # Pairs get first claim on the symmetry axis; singles then search the
    # remaining exact free space on that same axis.
    units.sort(
        key=lambda item: (
            0 if item['kind'] == 'pair' else 1,
            -(item['width'] * item['height']),
        )
    )
    return units, len(pairs), len(singles)


SYMMETRY_SCALE_STEPS = 12
SYMMETRY_AXIS_SAMPLES = 12
SYMMETRY_SPREAD_SAMPLES = 6
SYMMETRY_MAX_BOUNDARY_POINTS = 48
SYMMETRY_WORK_BUDGET = 3500


class _SymmetryWorkBudgetExceeded(RuntimeError):
    pass


def _uv_boundary_key(x, y):
    return (round(float(x), 8), round(float(y), 8))


def _downsample_closed_polygon(polygon, max_points=SYMMETRY_MAX_BOUNDARY_POINTS):
    polygon = tuple(polygon)
    count = len(polygon)
    if count <= max_points:
        return polygon
    indices = []
    for index in range(max_points):
        sample = int(round(index * count / max_points)) % count
        if not indices or sample != indices[-1]:
            indices.append(sample)
    return tuple(polygon[index] for index in indices)


def _signed_polygon_area(polygon):
    area = 0.0
    for index, point in enumerate(polygon):
        nxt = polygon[(index + 1) % len(polygon)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    return area * 0.5


def _island_boundary_polygons(island, uv_layer):
    """Trace only UV-island boundary loops instead of every face polygon.

    Pack Symmetry used to exact-check every quad/triangle for every placement
    candidate. Dense character UVs therefore multiplied thousands of faces by
    dozens of candidates and could lock Blender's main thread. Boundary loops
    preserve the concave silhouette while reducing collision work to the
    perimeter of each island.
    """
    segment_counts = {}
    point_values = {}
    for face in island_tools.island_faces(island):
        loops = list(face.loops)
        for index, loop in enumerate(loops):
            next_loop = loops[(index + 1) % len(loops)]
            a_uv = loop[uv_layer].uv
            b_uv = next_loop[uv_layer].uv
            a = _uv_boundary_key(a_uv.x, a_uv.y)
            b = _uv_boundary_key(b_uv.x, b_uv.y)
            if a == b:
                continue
            point_values.setdefault(a, (float(a_uv.x), float(a_uv.y)))
            point_values.setdefault(b, (float(b_uv.x), float(b_uv.y)))
            edge = (a, b) if a < b else (b, a)
            segment_counts[edge] = segment_counts.get(edge, 0) + 1

    boundary_edges = {edge for edge, count in segment_counts.items() if count == 1}
    if not boundary_edges:
        min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
        return (((min_u, min_v), (max_u, min_v), (max_u, max_v), (min_u, max_v)),)

    adjacency = {}
    for a, b in boundary_edges:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    for key in adjacency:
        adjacency[key].sort()

    unused = set(boundary_edges)
    polygons = []
    while unused:
        first_edge = min(unused)
        start, current = first_edge
        previous = start
        unused.remove(first_edge)
        polygon_keys = [start, current]
        guard = 0
        while current != start and guard <= len(boundary_edges) + 2:
            guard += 1
            choices = []
            for neighbor in adjacency.get(current, ()):
                edge = (current, neighbor) if current < neighbor else (neighbor, current)
                if edge in unused:
                    choices.append((neighbor, edge))
            if not choices:
                break
            choices.sort(key=lambda item: (item[0] == previous, item[0]))
            neighbor, edge = choices[0]
            unused.remove(edge)
            previous, current = current, neighbor
            if current != start:
                polygon_keys.append(current)

        if current == start and len(polygon_keys) >= 3:
            polygon = tuple(point_values[key] for key in polygon_keys)
            polygon = _downsample_closed_polygon(polygon)
            if len(polygon) >= 3 and abs(_signed_polygon_area(polygon)) > 1.0e-12:
                polygons.append(polygon)

    if polygons:
        # Largest loop first. Inner holes remain blockers (safe/fail-closed),
        # while the outer concave silhouette is still available for packing.
        polygons.sort(key=lambda poly: abs(_signed_polygon_area(poly)), reverse=True)
        return tuple(polygons)

    min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
    return (((min_u, min_v), (max_u, min_v), (max_u, max_v), (min_u, max_v)),)


def _prepare_unit_local_polygons(unit, uv_layer, axis_kind, gap):
    del axis_kind, gap
    member_polygons = []
    for island in unit['members']:
        center = island_tools.get_island_center(island, uv_layer)
        polygons = []
        for polygon in _island_boundary_polygons(island, uv_layer):
            polygons.append(
                tuple((point[0] - center.x, point[1] - center.y) for point in polygon)
            )
        member_polygons.append(tuple(polygons))
    unit['_local_member_polygons'] = tuple(member_polygons)


def _build_polygon_spatial_index(polygons, resolution=32):
    resolution = max(8, int(resolution))
    buckets = {}
    for index, polygon in enumerate(polygons):
        min_u, max_u, min_v, max_v = pack_geometry.polygon_bounds(polygon)
        x0 = max(0, min(resolution - 1, int(math.floor(min_u * resolution))))
        x1 = max(0, min(resolution - 1, int(math.floor(max_u * resolution))))
        y0 = max(0, min(resolution - 1, int(math.floor(min_v * resolution))))
        y1 = max(0, min(resolution - 1, int(math.floor(max_v * resolution))))
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                buckets.setdefault((x, y), set()).add(index)
    return (tuple(polygons), buckets, resolution)


def _query_polygon_spatial_index(index_data, query_polygons, margin):
    polygons, buckets, resolution = index_data
    found = set()
    for polygon in query_polygons:
        min_u, max_u, min_v, max_v = pack_geometry.polygon_bounds(polygon)
        min_u -= margin
        max_u += margin
        min_v -= margin
        max_v += margin
        x0 = max(0, min(resolution - 1, int(math.floor(min_u * resolution))))
        x1 = max(0, min(resolution - 1, int(math.floor(max_u * resolution))))
        y0 = max(0, min(resolution - 1, int(math.floor(min_v * resolution))))
        y1 = max(0, min(resolution - 1, int(math.floor(max_v * resolution))))
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                found.update(buckets.get((x, y), ()))
    return tuple(polygons[item] for item in found)


def _unit_axis_half_extent(unit, scale, axis_kind):
    if axis_kind == 'U':
        heights = [unit.get('left_height', unit['height']), unit.get('right_height', unit['height'])]
        return 0.5 * max(heights) * scale
    widths = [unit.get('left_width', unit['width']), unit.get('right_width', unit['width'])]
    return 0.5 * max(widths) * scale


def _unit_fixed_half_extent(unit, scale, axis_kind):
    if unit['kind'] == 'pair':
        return None
    if axis_kind == 'U':
        return 0.5 * unit['width'] * scale
    return 0.5 * unit['height'] * scale


def _limited_centered_candidates(values, low, high, limit):
    clean = sorted(
        {max(low, min(high, float(value))) for value in values if low - PACK_EPSILON <= value <= high + PACK_EPSILON},
        key=lambda value: (abs(value - 0.5), value),
    )
    if len(clean) <= limit:
        return tuple(clean)
    # Preserve candidates near the tile center plus both extremes.
    chosen = clean[: max(1, limit - 2)] + [low, high]
    return tuple(dict.fromkeys(chosen))


def _axis_candidate_positions(unit, scale, axis_kind, margin, obstacle_polygons):
    half = _unit_axis_half_extent(unit, scale, axis_kind)
    low = margin + half
    high = 1.0 - margin - half
    if low > high + PACK_EPSILON:
        return ()

    candidates = {0.5, low, high}
    if high > low:
        for index in range(SYMMETRY_AXIS_SAMPLES + 1):
            candidates.add(low + (high - low) * index / SYMMETRY_AXIS_SAMPLES)

    for polygon in obstacle_polygons:
        bounds = pack_geometry.polygon_bounds(polygon)
        obstacle_min = bounds[2] if axis_kind == 'U' else bounds[0]
        obstacle_max = bounds[3] if axis_kind == 'U' else bounds[1]
        candidates.add(obstacle_max + margin + half)
        candidates.add(obstacle_min - margin - half)

    return _limited_centered_candidates(
        candidates,
        low,
        high,
        SYMMETRY_AXIS_SAMPLES + 8,
    )


def _pair_spread_candidates(unit, scale, axis_kind, margin, gap, obstacle_polygons):
    if unit['kind'] != 'pair':
        return (0.0,)

    if axis_kind == 'U':
        left_size = unit['left_width'] * scale
        right_size = unit['right_width'] * scale
        obstacle_min_index, obstacle_max_index = 0, 1
    else:
        left_size = unit['left_height'] * scale
        right_size = unit['right_height'] * scale
        obstacle_min_index, obstacle_max_index = 2, 3

    min_spread = (left_size + right_size) * 0.25 + gap * scale * 0.5
    max_spread = min(
        0.5 - margin - left_size * 0.5,
        0.5 - margin - right_size * 0.5,
    )
    if min_spread > max_spread + PACK_EPSILON:
        return ()

    candidates = {min_spread, max_spread}
    if max_spread > min_spread:
        for index in range(SYMMETRY_SPREAD_SAMPLES + 1):
            candidates.add(
                min_spread + (max_spread - min_spread) * index / SYMMETRY_SPREAD_SAMPLES
            )

    # Important for character UVs: let a mirrored pair widen around a large
    # center blocker (torso/face) instead of forcing both islands next to the
    # symmetry axis and then escaping to the top/bottom of the tile.
    for polygon in obstacle_polygons:
        bounds = pack_geometry.polygon_bounds(polygon)
        obstacle_min = bounds[obstacle_min_index]
        obstacle_max = bounds[obstacle_max_index]
        left_clear = 0.5 + left_size * 0.5 + margin - obstacle_min
        right_clear = obstacle_max + margin - 0.5 + right_size * 0.5
        candidates.add(max(min_spread, left_clear, right_clear))

    clean = sorted(
        {max(min_spread, min(max_spread, float(value))) for value in candidates},
    )
    if len(clean) > SYMMETRY_SPREAD_SAMPLES + 6:
        # Keep full-range coverage; small spread alone is not sufficient when a
        # large unselected island occupies the tile center.
        indices = [
            round(i * (len(clean) - 1) / (SYMMETRY_SPREAD_SAMPLES + 5))
            for i in range(SYMMETRY_SPREAD_SAMPLES + 6)
        ]
        clean = [clean[index] for index in sorted(set(indices))]
    return tuple(clean)


def _unit_target_centers(unit, axis_kind, axis_position, spread):
    if unit['kind'] == 'single':
        return ((0.5, axis_position),) if axis_kind == 'U' else ((axis_position, 0.5),)
    if axis_kind == 'U':
        return ((0.5 - spread, axis_position), (0.5 + spread, axis_position))
    return ((axis_position, 0.5 + spread), (axis_position, 0.5 - spread))


def _transformed_unit_polygons(unit, scale, axis_kind, axis_position, spread):
    polygons = []
    centers = _unit_target_centers(unit, axis_kind, axis_position, spread)
    for member_index, member_polygons in enumerate(unit['_local_member_polygons']):
        tx, ty = centers[member_index]
        polygons.extend(pack_geometry.transform_polygons(member_polygons, scale, tx, ty))
    return tuple(polygons)


def _symmetry_scale_upper_bound(units, axis_kind, margin, gap):
    tile = max(1.0e-8, 1.0 - 2.0 * margin)
    upper = float('inf')
    for unit in units:
        if unit['kind'] == 'single':
            width = max(unit['width'], 1.0e-8)
            height = max(unit['height'], 1.0e-8)
        elif axis_kind == 'U':
            width = max(unit['left_width'] + unit['right_width'] + gap, 1.0e-8)
            height = max(unit['left_height'], unit['right_height'], 1.0e-8)
        else:
            width = max(unit['left_width'], unit['right_width'], 1.0e-8)
            height = max(unit['left_height'] + unit['right_height'] + gap, 1.0e-8)
        upper = min(upper, tile / width, tile / height)
    if not math.isfinite(upper):
        return 1.0
    return max(0.0, upper)


def _try_symmetry_exact_layout(
    units,
    static_index,
    static_polygons,
    scale,
    axis_kind,
    margin,
    gap,
    budget,
):
    placements = {}
    occupied = []
    for unit in units:
        all_obstacles = tuple(static_polygons) + tuple(occupied)
        axis_candidates = _axis_candidate_positions(
            unit, scale, axis_kind, margin, all_obstacles
        )
        if not axis_candidates:
            return None

        spread_candidates = _pair_spread_candidates(
            unit, scale, axis_kind, margin, gap, all_obstacles
        )
        if not spread_candidates:
            return None

        placed = False
        for axis_position in axis_candidates:
            for spread in spread_candidates:
                budget['checks'] += 1
                if budget['checks'] > SYMMETRY_WORK_BUDGET:
                    raise _SymmetryWorkBudgetExceeded(
                        'Pack Symmetry reached its safe work limit; try fewer selected islands or a smaller margin.'
                    )
                transformed = _transformed_unit_polygons(
                    unit, scale, axis_kind, axis_position, spread
                )
                if not pack_geometry.polygons_fit_tile(transformed, margin=margin):
                    continue
                nearby_static = _query_polygon_spatial_index(
                    static_index, transformed, margin
                )
                if pack_geometry.polygon_sets_conflict(
                    transformed, nearby_static, margin=margin
                ):
                    continue
                if pack_geometry.polygon_sets_conflict(
                    transformed, tuple(occupied), margin=margin
                ):
                    continue
                placements[unit['index']] = (axis_position, spread)
                occupied.extend(transformed)
                placed = True
                break
            if placed:
                break
        if not placed:
            return None
    return placements


def _solve_symmetry_exact_layout(units, static_islands, uv_layer, axis_kind, margin, gap):
    for unit in units:
        _prepare_unit_local_polygons(unit, uv_layer, axis_kind, gap)

    static_polygons = tuple(
        polygon
        for island in static_islands
        for polygon in _island_boundary_polygons(island, uv_layer)
    )
    static_index = _build_polygon_spatial_index(static_polygons)
    upper = _symmetry_scale_upper_bound(units, axis_kind, margin, gap)
    if upper <= PACK_EPSILON:
        return 0.0, None

    low = 0.0
    high = upper
    best = None
    budget = {'checks': 0}

    # Bounded binary search: unlike the old face-by-face 28-step solver this
    # has a deterministic main-thread workload and cannot grow with face count.
    for _ in range(SYMMETRY_SCALE_STEPS):
        mid = (low + high) * 0.5
        result = _try_symmetry_exact_layout(
            units,
            static_index,
            static_polygons,
            mid,
            axis_kind,
            margin,
            gap,
            budget,
        )
        if result is None:
            high = mid
        else:
            low = mid
            best = result
    return low, best


def _place_symmetry_units_exact(units, placements, layout_scale, uv_layer, axis_kind, gap):
    del gap
    for unit in units:
        axis_position, spread = placements[unit['index']]
        centers = _unit_target_centers(unit, axis_kind, axis_position, spread)
        for member_index, island in enumerate(unit['members']):
            current_center = island_tools.get_island_center(island, uv_layer)
            uv_utils.scale_island(island, uv_layer, current_center, layout_scale)
            target_center = Vector(centers[member_index])
            scaled_center = island_tools.get_island_center(island, uv_layer)
            uv_utils.translate_island(island, uv_layer, target_center - scaled_center)


def _place_symmetry_units(units, placements, layout_scale, uv_layer, axis_kind, gap):
    for unit in units:
        placement = placements[unit['index']]
        width = unit['width'] * layout_scale
        height = unit['height'] * layout_scale
        unit_center = Vector((placement[0] + width * 0.5, placement[1] + height * 0.5))

        for island in unit['members']:
            center = island_tools.get_island_center(island, uv_layer)
            uv_utils.scale_island(island, uv_layer, center, layout_scale)

        if unit['kind'] == 'single':
            island = unit['members'][0]
            current_center = island_tools.get_island_center(island, uv_layer)
            uv_utils.translate_island(island, uv_layer, unit_center - current_center)
            continue

        left_island, right_island = unit['members']
        scaled_gap = gap * layout_scale
        left_width = unit['left_width'] * layout_scale
        left_height = unit['left_height'] * layout_scale
        right_width = unit['right_width'] * layout_scale
        right_height = unit['right_height'] * layout_scale

        if axis_kind == 'V':
            top_height = left_height
            bottom_height = right_height
            top_center = Vector((unit_center.x, unit_center.y + (scaled_gap * 0.5 + top_height * 0.5)))
            bottom_center = Vector((unit_center.x, unit_center.y - (scaled_gap * 0.5 + bottom_height * 0.5)))
            current_top = island_tools.get_island_center(left_island, uv_layer)
            current_bottom = island_tools.get_island_center(right_island, uv_layer)
            uv_utils.translate_island(left_island, uv_layer, top_center - current_top)
            uv_utils.translate_island(right_island, uv_layer, bottom_center - current_bottom)
        else:
            left_center = Vector((unit_center.x - (scaled_gap * 0.5 + left_width * 0.5), unit_center.y))
            right_center = Vector((unit_center.x + (scaled_gap * 0.5 + right_width * 0.5), unit_center.y))
            current_left = island_tools.get_island_center(left_island, uv_layer)
            current_right = island_tools.get_island_center(right_island, uv_layer)
            uv_utils.translate_island(left_island, uv_layer, left_center - current_left)
            uv_utils.translate_island(right_island, uv_layer, right_center - current_right)


def _pack_symmetry(context, operator):
    settings = uv_utils.get_settings(context)
    obj = uv_utils.get_active_mesh_object(context)
    validation_bm = island_tools.get_active_bmesh(context)
    validation_uv_layer = island_tools.get_active_uv_layer(validation_bm, obj)
    island_tools.validate_uv_selection_scope(
        context,
        validation_bm,
        validation_uv_layer,
        refresh_invalid_sync=True,
    )

    uv_utils.ensure_destructive_ready(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    islands = island_tools.get_selected_uv_islands_for_context(context, bm, uv_layer)
    if not islands:
        operator.report({'INFO'}, 'Select one or more UV islands for Pack Symmetry.')
        return {'CANCELLED'}

    selection = uv_utils.store_uv_selection_state(bm, uv_layer)
    uv_snapshot = _uv_snapshot(all_islands, uv_layer)
    unselected_snapshot = _uv_snapshot(_scope_static_islands(all_islands, islands), uv_layer)

    axis_kind = 'V' if settings.symmetry_axis == 'V_HALF' else 'U'
    gap = max(settings.margin * 4.0, 0.002)

    try:
        changed_density, _target_density = _normalize_selected_density(context, islands, obj, uv_layer)
        _pre_rotate_islands(islands, uv_layer, settings.rotation_mode)
        static_islands = _scope_static_islands(all_islands, islands)
        units, pair_count, single_count = _build_symmetry_units(islands, uv_layer, axis_kind, gap)
        for index, unit in enumerate(units):
            unit['index'] = index
        layout_scale, placements = _solve_symmetry_exact_layout(
            units,
            static_islands,
            uv_layer,
            axis_kind,
            settings.margin,
            gap,
        )
        if placements is None:
            _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
            operator.report(
                {'INFO'},
                'Pack Symmetry could not find enough free 0-1 space for the selected UV islands.',
            )
            return {'CANCELLED'}

        _place_symmetry_units_exact(units, placements, layout_scale, uv_layer, axis_kind, gap)

        if not _uv_snapshot_matches(bm, uv_layer, unselected_snapshot):
            _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
            operator.report({'INFO'}, 'Pack Symmetry touched unselected UVs, so the operation was rolled back.')
            return {'CANCELLED'}
    except Exception as exc:
        bm = island_tools.get_active_bmesh(context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
        operator.report({'INFO'}, f'Pack Symmetry was rolled back: {exc}')
        return {'CANCELLED'}
    finally:
        try:
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            uv_utils.restore_uv_selection_state(bm, uv_layer, selection)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except Exception as exc:
            operator.report({'INFO'}, f'Could not restore UV selection state: {exc}')

    density_note = ' with selected density unified' if changed_density else ''
    scope_note = ' while avoiding unselected UV shapes'
    operator.report(
        {'INFO'},
        f'Pack Symmetry placed {len(islands)} selected island(s): {pair_count} pair(s), {single_count} centered single(s){density_note}{scope_note}.',
    )
    return {'FINISHED'}


def _pack(context, operator, selected_only):
    settings = uv_utils.get_settings(context)
    obj = uv_utils.get_active_mesh_object(context)
    if selected_only:
        validation_bm = island_tools.get_active_bmesh(context)
        validation_uv_layer = island_tools.get_active_uv_layer(validation_bm, obj)
        island_tools.validate_uv_selection_scope(
            context,
            validation_bm,
            validation_uv_layer,
            refresh_invalid_sync=True,
        )
    uv_utils.ensure_destructive_ready(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    if selected_only:
        islands = island_tools.get_selected_uv_islands_for_context(
            context,
            bm,
            uv_layer,
        )
    else:
        islands = all_islands
    if not islands:
        operator.report({"ERROR"}, "No UV islands found to pack.")
        return {"CANCELLED"}
    original_island_count = len(islands)

    selection = uv_utils.store_uv_selection_state(bm, uv_layer)
    uv_snapshot = _uv_snapshot(all_islands, uv_layer)
    unselected_snapshot = _uv_snapshot(
        _scope_static_islands(all_islands, islands),
        uv_layer,
    )
    pin_snapshot = _pin_snapshot(all_islands, uv_layer) if selected_only else {}

    try:
        _pre_rotate_islands(islands, uv_layer, settings.rotation_mode)
        static_islands = _scope_static_islands(all_islands, islands) if selected_only else []
        if settings.pack_preserve_stacks:
            pack_islands, pack_records, stack_groups = _stack_preserve_plan(
                islands,
                uv_layer,
            )
        else:
            pack_islands = islands
            pack_records = [_island_loop_records(island, uv_layer) for island in pack_islands]
            stack_groups = []
        keep_current_scale = selected_only and settings.pack_selected_keep_current_scale_v2
        scale_to_fit = not keep_current_scale
        used_fallback = False
        used_static_packer = False
        used_stack_safe_packer = bool(stack_groups)
        overflowed_tile = False
        if selected_only:
            # Pack Selected uses Blender's exact CONCAVE island geometry. The
            # unselected islands are temporarily pinned and included in the
            # operator selection only as immovable blockers; selected stack
            # followers remain outside the operator and are restored afterward.
            _pin_static_islands_for_pack(pack_islands, static_islands, uv_layer)
            operator_islands = list(static_islands) + list(pack_islands)
            uv_utils.select_uv_islands(context, bm, uv_layer, operator_islands)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            uv_utils.run_uv_pack(
                context,
                settings.margin,
                rotate=settings.rotation_mode == "CARDINAL",
                scale=scale_to_fit,
                pin=True,
                pin_method="LOCKED",
                shape_method="CONCAVE",
            )
            used_static_packer = True
        else:
            # Pack Whole Mesh retains the established native route and its
            # explicit all-island selection.  It is intentionally outside the
            # selected-only boundary above.
            uv_utils.select_uv_islands(context, bm, uv_layer, pack_islands)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            try:
                uv_utils.run_uv_pack(
                    context,
                    settings.margin,
                    rotate=settings.rotation_mode == "CARDINAL",
                    scale=scale_to_fit,
                )
            except Exception:
                bm = island_tools.get_active_bmesh(context)
                uv_layer = island_tools.get_active_uv_layer(bm, obj)
                pack_islands = [
                    loops
                    for loops in (
                        _loops_from_records(bm, records) for records in pack_records
                    )
                    if loops
                ]
                fits = uv_utils.basic_pack_islands(
                    bm,
                    uv_layer,
                    pack_islands,
                    settings.margin,
                    scale_to_fit=scale_to_fit,
                )
                overflowed_tile = not fits
                used_fallback = True

        bm = island_tools.get_active_bmesh(context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        if selected_only:
            _restore_pin_snapshot(bm, uv_layer, pin_snapshot)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        preserved_stacks = _restore_stacked_groups(bm, uv_layer, stack_groups)
        if keep_current_scale and not overflowed_tile:
            overflowed_tile = not _islands_inside_tile(islands, uv_layer)

        if selected_only and not _uv_snapshot_matches(bm, uv_layer, unselected_snapshot):
            _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
            operator.report(
                {"ERROR"},
                "Pack Selected changed unselected UVs; the operation was rolled back.",
            )
            return {"CANCELLED"}
    except Exception as exc:
        bm = island_tools.get_active_bmesh(context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
        operator.report({"ERROR"}, f"Pack failed and was rolled back: {exc}")
        return {"CANCELLED"}
    finally:
        try:
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            if selected_only:
                _restore_pin_snapshot(bm, uv_layer, pin_snapshot)
            uv_utils.restore_uv_selection_state(bm, uv_layer, selection)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except Exception as exc:
            operator.report({"ERROR"}, f"Could not restore UV selection state: {exc}")

    suffix = ""
    if used_static_packer:
        suffix = " avoiding locked unselected islands"
    elif used_stack_safe_packer:
        suffix = " using stack-safe packer"
    elif used_fallback:
        suffix = " using internal packer"
    density_note = " at current scale" if keep_current_scale else " scaled to fill 0-1" if selected_only else ""
    stack_note = f" and kept {preserved_stacks} stacked island(s)" if preserved_stacks else ""
    scope_note = " while leaving unselected UVs untouched" if selected_only else ""
    if overflowed_tile:
        operator.report(
            {"WARNING"},
            "Keep Current Scale is enabled: packed islands extend outside the 0-1 tile.",
        )
    operator.report(
        {"INFO"},
        f"Packed {original_island_count} UV island(s){density_note}{stack_note}{scope_note}{suffix}.",
    )
    return {"FINISHED"}

# -----------------------------------------------------------------------------
# Pack V2 detached external-process runtime
# -----------------------------------------------------------------------------

_ACTIVE_PACK_V2_JOB = None
_PACK_V2_TIMER_RUNNING = False
_PACK_V2_TIMER_INTERVAL = 0.06
_PACK_V2_WAIT_INTERVAL = 0.20


def _pack_v2_face_world_area(obj, face):
    points = [obj.matrix_world @ loop.vert.co for loop in face.loops]
    if len(points) < 3:
        return 0.0
    origin = points[0]
    area = 0.0
    for index in range(1, len(points) - 1):
        area += (points[index] - origin).cross(points[index + 1] - origin).length * 0.5
    return float(area)


def _pack_v2_capture_snapshot(context, mode):
    uv_utils.ensure_destructive_ready(context)
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    if mode in {"selected", "symmetry"}:
        island_tools.validate_uv_selection_scope(context, bm, uv_layer)
    settings = uv_utils.get_settings(context)

    faces = []
    selected_points = []
    all_points = []
    world_area_by_face = {}
    for face in bm.faces:
        selected = bool(
            island_tools._face_uv_selected_for_context(context, bm, face, uv_layer)
        ) if mode in {"selected", "symmetry"} else True
        loops = []
        for loop in face.loops:
            uv = loop[uv_layer].uv
            value = (
                int(loop.vert.index),
                int(loop.edge.index),
                float(uv.x),
                float(uv.y),
            )
            loops.append(value)
            if not face.hide:
                all_points.append((float(uv.x), float(uv.y)))
                if selected:
                    selected_points.append((float(uv.x), float(uv.y)))
        faces.append((int(face.index), selected, bool(face.hide), tuple(loops)))
        world_area_by_face[int(face.index)] = _pack_v2_face_world_area(obj, face)

    points = all_points if mode == "whole" else selected_points
    if not points:
        raise RuntimeError(
            "Pack V2: select at least one UV island."
            if mode != "whole" else "Pack V2: no visible UV islands found."
        )
    min_u = min(point[0] for point in points)
    max_u = max(point[0] for point in points)
    min_v = min(point[1] for point in points)
    max_v = max(point[1] for point in points)
    center = ((min_u + max_u) * 0.5, (min_v + max_v) * 0.5)
    axis_kind = "V" if settings.symmetry_axis == "V_HALF" else "U"
    snapshot = {
        "schema": "pack-v2-snapshot-v1",
        "faces": tuple(faces),
        "world_area_by_face": world_area_by_face,
        "options": {
            "mode": str(mode),
            "margin": max(0.0, float(settings.margin)),
            "rotation_mode": str(settings.rotation_mode),
            "preserve_stacks": bool(settings.pack_preserve_stacks),
            "scale_to_fit": not (
                mode == "selected" and bool(settings.pack_selected_keep_current_scale_v2)
            ),
            "axis_kind": axis_kind,
        },
    }
    return obj, bm, uv_layer, snapshot, center


def _pack_v2_worker_environment():
    environment = dict(os.environ)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        environment[name] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _pack_v2_creation_flags():
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def _pack_v2_launch_thread(job):
    try:
        input_path = Path(job["input_path"])
        output_path = Path(job["output_path"])
        progress_path = Path(job["progress_path"])
        with input_path.open("wb") as handle:
            pickle.dump(job["snapshot"], handle, protocol=5)
        python_executable = pro_process_runtime.resolve_bundled_python(
            blender_binary=job["blender_binary"],
            blender_version=job["blender_version"],
        )
        worker_script = Path(__file__).with_name("pack_v2_worker.py").resolve()
        if not worker_script.is_file():
            raise RuntimeError("Pack V2 worker script is missing")
        command = [
                str(python_executable),
                str(worker_script),
                str(input_path),
                str(output_path),
                str(progress_path),
            ]
        from . import stack_tools
        for attempt in range(2):
            if job.get("cancelled"):
                return
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                env=_pack_v2_worker_environment(),
                creationflags=_pack_v2_creation_flags(),
            )
            if not stack_tools._v2_publish_process(job, process):
                return
            return_code = process.wait()
            job["worker_returncode"] = return_code
            if job.get("cancelled"):
                return
            # Pack applies one complete result on the owner thread. A native
            # Windows access violation before output is safe to restart once;
            # Python errors and completed results must retain their diagnostics.
            if (attempt == 0 and return_code in (3221225477, -1073741819)
                    and not output_path.is_file()):
                job["worker_retries"] = 1
                continue
            break
        if not output_path.is_file():
            raise RuntimeError("Pack V2 worker exited without a result (code %s)" % return_code)
        with output_path.open("rb") as handle:
            payload = pickle.load(handle)
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(str((payload or {}).get("error", "Pack V2 worker failed")))
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("schema") != "pack-v2-result-v1":
            raise RuntimeError("Pack V2 worker returned an invalid result")
        if return_code not in (0, None):
            raise RuntimeError("Pack V2 worker exited with code %s" % return_code)
        job["result"] = result
        job["state"] = "ready_apply"
    except BaseException as exc:
        if not job.get("cancelled"):
            job["error"] = "%s: %s" % (type(exc).__name__, str(exc))
            job["state"] = "failed"


def _pack_v2_read_progress(job):
    progress_path = Path(job.get("progress_path", ""))
    if progress_path.is_file():
        try:
            value = json.loads(progress_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                job["progress"] = value
        except (OSError, ValueError, TypeError):
            pass
    value = dict(job.get("progress") or {})
    percent = max(0.0, min(100.0, float(value.get("percent", 0.0) or 0.0)))
    done = max(0, int(value.get("done", 0) or 0))
    total = max(1, int(value.get("total", 1) or 1))
    elapsed = max(0.0, time.perf_counter() - float(job.get("started", time.perf_counter())))
    stage = str(value.get("stage", job.get("state", "snapshot")) or "snapshot")
    if job.get("state") == "ready_apply":
        percent = 100.0
        stage = "waiting_apply"
    return percent, elapsed, done, total, stage


def _pack_v2_update_progress(job):
    percent, elapsed, done, total, stage = _pack_v2_read_progress(job)
    text = f"{percent:.0f}%  •  {elapsed:.1f}s  •  {done}/{total}"
    overlay.set_fast_progress(
        {
            "object_name": job["object_name"],
            "uv_map_name": job["uv_map_name"],
            "center": job["center"],
            "text": text,
        }
    )
    try:
        workspace = getattr(bpy.context, "workspace", None)
        if workspace is not None:
            suffix = " — waiting to apply on source UV" if job.get("state") == "ready_apply" else ""
            workspace.status_text_set(f"Pack V2 [{stage}]: {text}{suffix}")
    except Exception:
        pass


def _pack_v2_source_valid(job):
    obj = bpy.data.objects.get(job.get("object_name", ""))
    if obj is None or obj.type != "MESH":
        return False
    try:
        for key, pointer_key in (("_object_ref", "object_pointer"), ("_data_ref", "data_pointer")):
            reference = job.get(key)
            if reference is not None and reference.as_pointer() != job[pointer_key]:
                return False
        identity = (obj.as_pointer() == job.get("object_pointer", obj.as_pointer())
                    and obj.data.as_pointer() == job.get("data_pointer", obj.data.as_pointer()))
        uv_name = job.get("uv_map_name")
        uv = obj.data.uv_layers.get(uv_name) if uv_name else None
        if uv_name and uv is None:
            return False
        # MeshUVLoopLayer RNA addresses can change at the Edit/Undo boundary.
        # The captured name plus complete UV/topology snapshot is authoritative.
        return identity
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def _pack_v2_source_context_matches(job, context):
    if not _pack_v2_source_valid(job):
        return False
    obj = bpy.data.objects.get(job.get("object_name", ""))
    if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
        return False
    active = getattr(context, "edit_object", None) or getattr(context, "object", None)
    if active is not obj:
        return False
    active_uv = getattr(getattr(obj.data, "uv_layers", None), "active", None)
    return getattr(active_uv, "name", None) == job.get("uv_map_name")


def _pack_v2_snapshot_unchanged(job, bm, uv_layer):
    source_faces = job["snapshot"].get("faces", ())
    if len(bm.faces) != len(source_faces):
        return False
    if len(bm.edges) != int(job.get("edge_count", len(bm.edges))):
        return False
    if len(bm.verts) != int(job.get("vert_count", len(bm.verts))):
        return False
    bm.faces.ensure_lookup_table()
    for record in source_faces:
        face_index, _selected, _hidden, source_loops = record
        if face_index < 0 or face_index >= len(bm.faces):
            return False
        loops = tuple(bm.faces[face_index].loops)
        if len(loops) != len(source_loops):
            return False
        for local_index, (source_vert, source_edge, source_u, source_v) in enumerate(source_loops):
            loop = loops[local_index]
            if int(loop.vert.index) != int(source_vert) or int(loop.edge.index) != int(source_edge):
                return False
            uv = loop[uv_layer].uv
            if abs(float(uv.x) - float(source_u)) > 1.0e-8 or abs(float(uv.y) - float(source_v)) > 1.0e-8:
                return False
    return True


def _pack_v2_apply_result(job):
    obj = bpy.data.objects.get(job.get("object_name", ""))
    if obj is None:
        return "invalid"
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        bm.edges.ensure_lookup_table()
        bm.edges.index_update()
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        uv_layer = bm.loops.layers.uv.get(job["uv_map_name"])
        if uv_layer is None:
            return "invalid"
        if not _pack_v2_snapshot_unchanged(job, bm, uv_layer):
            return "changed"
        writes = tuple((job.get("result") or {}).get("writes", ()))
        staged = []
        seen = set()
        allowed = job.get("allowed_write_keys")
        if allowed is None:
            return "invalid"
        for record in writes:
            if not isinstance(record, (tuple, list)) or len(record) != 4:
                return "invalid"
            face_index, local_index, u, v = record
            if (isinstance(face_index, bool) or isinstance(local_index, bool)
                    or not isinstance(face_index, int) or not isinstance(local_index, int)
                    or face_index < 0 or face_index >= len(bm.faces)):
                return "invalid"
            loops = tuple(bm.faces[face_index].loops)
            if local_index < 0 or local_index >= len(loops):
                return "invalid"
            key = (face_index, local_index)
            if key in seen or key not in allowed:
                return "invalid"
            u, v = float(u), float(v)
            if not math.isfinite(u) or not math.isfinite(v):
                return "invalid"
            seen.add(key)
            staged.append((loops[local_index], (u, v)))
        for loop, point in staged:
            loop[uv_layer].uv = point
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        try:
            bpy.ops.ed.undo_push(message="uv GPT Pack V2")
        except Exception:
            pass
        return "applied"
    except (ReferenceError, RuntimeError, TypeError, ValueError, OverflowError):
        return "invalid"


def _pack_v2_finish_job(job, terminal_state=None, terminal_error=None):
    global _ACTIVE_PACK_V2_JOB
    job["terminal_state"] = terminal_state or job.get("terminal_state", "cancelled")
    job["terminal_error"] = terminal_error
    from . import stack_tools
    stack_tools._v2_cancel_external_job(job, reason=terminal_error or job["terminal_state"])
    overlay.clear_fast_progress()
    try:
        workspace = getattr(bpy.context, "workspace", None)
        if workspace is not None:
            workspace.status_text_set(None)
    except Exception:
        pass
    work_dir = job.get("work_dir")
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)
    if _ACTIVE_PACK_V2_JOB is job:
        _ACTIVE_PACK_V2_JOB = None


def _pack_v2_background_timer():
    global _PACK_V2_TIMER_RUNNING
    job = _ACTIVE_PACK_V2_JOB
    if job is None:
        _PACK_V2_TIMER_RUNNING = False
        return None
    if not _pack_v2_source_valid(job):
        job["error"] = "Source object or mesh was removed/replaced."
        job["state"] = "discarded"
        _pack_v2_finish_job(job, "source_invalid", job["error"])
        _PACK_V2_TIMER_RUNNING = False
        return None
    _pack_v2_update_progress(job)
    if job.get("state") == "failed":
        print("UV GPT Pack V2 failed:", job.get("error"))
        _pack_v2_finish_job(job, "failed", job.get("error"))
        _PACK_V2_TIMER_RUNNING = False
        return None
    if job.get("state") != "ready_apply":
        return _PACK_V2_TIMER_INTERVAL
    if not _pack_v2_source_context_matches(job, bpy.context):
        return _PACK_V2_WAIT_INTERVAL
    state = _pack_v2_apply_result(job)
    if state == "applied":
        result = job.get("result") or {}
        print(
            "UV GPT Pack V2 applied: %s island(s), scale %.6f, exact stack followers %s."
            % (
                result.get("movable_count", 0),
                float(result.get("scale", 1.0) or 1.0),
                result.get("stack_followers", 0),
            )
        )
        _pack_v2_finish_job(job, "applied")
        _PACK_V2_TIMER_RUNNING = False
        return None
    if state in {"changed", "invalid"}:
        job["error"] = "Source changed or worker result was invalid."
        job["state"] = "discarded"
        print("UV GPT Pack V2 discarded: source UV/topology changed during background work.")
        _pack_v2_finish_job(job, state, job["error"])
        _PACK_V2_TIMER_RUNNING = False
        return None
    return _PACK_V2_WAIT_INTERVAL


def _ensure_pack_v2_timer():
    global _PACK_V2_TIMER_RUNNING
    if _PACK_V2_TIMER_RUNNING:
        return
    _PACK_V2_TIMER_RUNNING = True
    try:
        bpy.app.timers.register(_pack_v2_background_timer, first_interval=_PACK_V2_TIMER_INTERVAL)
    except ValueError:
        pass


def _pack_v2_start_job(context, mode):
    global _ACTIVE_PACK_V2_JOB
    if _ACTIVE_PACK_V2_JOB is not None:
        raise RuntimeError("A Pack V2 job is already running in the background.")
    from . import stack_tools, fast_v2_core
    if (stack_tools._ACTIVE_FAST_BACKGROUND_JOB is not None
            or stack_tools._ACTIVE_PRO_EXACT_V2_JOB is not None):
        raise RuntimeError("Wait for the running Stack operation before packing.")
    obj, bm, uv_layer, snapshot, center = _pack_v2_capture_snapshot(context, mode)
    scope_snapshot = dict(snapshot, schema=fast_v2_core.SCHEMA)
    allowed_keys = frozenset(key for island in fast_v2_core.build_uv_islands(scope_snapshot)
                             if mode == "whole" or island.selected for key in island.loop_keys)
    work_dir = tempfile.mkdtemp(prefix="uv_gpt_pack_v2_")
    job = {
        "kind": "pack_v2",
        "mode": mode,
        "object_name": str(obj.name),
        "data_name": str(getattr(obj.data, "name", "")),
        "_object_ref": obj,
        "_data_ref": obj.data,
        "object_pointer": obj.as_pointer(),
        "data_pointer": obj.data.as_pointer(),
        "allowed_write_keys": allowed_keys,
        "uv_map_name": str(getattr(uv_layer, "name", "")),
        "center": center,
        "snapshot": snapshot,
        "edge_count": len(bm.edges),
        "vert_count": len(bm.verts),
        "started": time.perf_counter(),
        "state": "launching",
        "progress": {"stage": "snapshot", "percent": 1.0, "done": 0, "total": 1},
        "work_dir": work_dir,
        "input_path": str(Path(work_dir) / "input.pkl"),
        "output_path": str(Path(work_dir) / "output.pkl"),
        "progress_path": str(Path(work_dir) / "progress.json"),
        "blender_binary": getattr(getattr(bpy, "app", None), "binary_path", None),
        "blender_version": getattr(getattr(bpy, "app", None), "version", None),
        "process": None,
        "pid": None,
        "_launch_lock": threading.Lock(),
        "result": None,
        "error": None,
        "cancelled": False,
    }
    thread = threading.Thread(
        target=_pack_v2_launch_thread,
        args=(job,),
        name="uv-gpt-pack-v2-launcher",
        daemon=True,
    )
    job["thread"] = thread
    _ACTIVE_PACK_V2_JOB = job
    thread.start()
    _pack_v2_update_progress(job)
    _ensure_pack_v2_timer()
    return job


class UVGPT_OT_pack_selected(bpy.types.Operator):
    bl_idname = "uv_gpt.pack_selected"
    bl_label = "Pack Selected"
    bl_description = "Pack selected UV islands in the Pack V2 external process"
    bl_options = {"REGISTER", "UNDO"}

    def _start_detached(self, context):
        try:
            _pack_v2_start_job(context, "selected")
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Pack Selected V2 is running in the background; it will apply when you return to the source UV map.")
        return {"FINISHED"}

    def invoke(self, context, event):
        del event
        return self._start_detached(context)

    def execute(self, context):
        return self._start_detached(context)


class UVGPT_OT_pack_symmetry(bpy.types.Operator):
    bl_idname = "uv_gpt.pack_symmetry"
    bl_label = "Pack Symmetry"
    bl_description = "Pack selected UV islands symmetrically in the Pack V2 external process"
    bl_options = {"REGISTER", "UNDO"}

    def _start_detached(self, context):
        try:
            _pack_v2_start_job(context, "symmetry")
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"INFO"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Pack Symmetry V2 is running in the background; it will apply when you return to the source UV map.")
        return {"FINISHED"}

    def invoke(self, context, event):
        del event
        return self._start_detached(context)

    def execute(self, context):
        return self._start_detached(context)


class UVGPT_OT_pack_whole_mesh(bpy.types.Operator):
    bl_idname = "uv_gpt.pack_whole_mesh"
    bl_label = "Pack Whole Mesh"
    bl_description = "Pack all UV islands in the Pack V2 external process"
    bl_options = {"REGISTER", "UNDO"}

    def _start_detached(self, context):
        try:
            _pack_v2_start_job(context, "whole")
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Pack Whole Mesh V2 is running in the background; it will apply when you return to the source UV map.")
        return {"FINISHED"}

    def invoke(self, context, event):
        del event
        return self._start_detached(context)

    def execute(self, context):
        return self._start_detached(context)


classes = (
    UVGPT_OT_pack_selected,
    UVGPT_OT_pack_symmetry,
    UVGPT_OT_pack_whole_mesh,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    job = _ACTIVE_PACK_V2_JOB
    if job is not None:
        _pack_v2_finish_job(job)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
