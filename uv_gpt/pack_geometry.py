"""Pure helpers for Pack Selected / Pack Symmetry.

No Blender imports: this keeps shape pairing and 2D collision logic testable.
"""

import math

_EPS = 1.0e-10


def shape_distance(left_feature, right_feature):
    """Dimensionless distance between normalized island-shape features."""
    left_aspect, left_radial = left_feature
    right_aspect, right_radial = right_feature
    left_aspect = max(float(left_aspect), _EPS)
    right_aspect = max(float(right_aspect), _EPS)
    aspect_error = abs(math.log(left_aspect / right_aspect))

    count = min(len(left_radial), len(right_radial))
    if count:
        radial_error = math.sqrt(
            sum((float(left_radial[i]) - float(right_radial[i])) ** 2 for i in range(count))
            / count
        )
    else:
        radial_error = 0.0 if len(left_radial) == len(right_radial) else 1.0
    return 0.35 * aspect_error + radial_error


def pair_similar_records(records, max_distance=0.20):
    """Pair records inside identical topology buckets using cached shape distances.

    The previous implementation repeatedly rescanned every remaining pair after
    each match, making a large same-topology bucket effectively cubic.  Here
    each pair distance is computed once, candidate edges are sorted once, and
    the same "closest available pair first" greedy rule is applied by skipping
    edges whose endpoint has already been matched.
    """
    buckets = {}
    for record in records:
        buckets.setdefault(tuple(record["topology"]), []).append(record)

    pairs = []
    singles = []
    limit = float(max_distance)
    for _topology, members in buckets.items():
        member_count = len(members)
        if member_count < 2:
            singles.extend(members)
            continue

        candidates = []
        for i in range(member_count - 1):
            left_feature = members[i]["feature"]
            for j in range(i + 1, member_count):
                distance = shape_distance(left_feature, members[j]["feature"])
                if distance <= limit:
                    candidates.append((distance, i, j))
        candidates.sort()

        matched = [False] * member_count
        for _distance, i, j in candidates:
            if matched[i] or matched[j]:
                continue
            matched[i] = True
            matched[j] = True
            pairs.append((members[i], members[j]))

        singles.extend(members[index] for index, used in enumerate(matched) if not used)
    return pairs, singles


def polygon_bounds(poly):
    xs = [point[0] for point in poly]
    ys = [point[1] for point in poly]
    return (min(xs), max(xs), min(ys), max(ys)) if poly else (0.0, 0.0, 0.0, 0.0)


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return (
        min(a[0], b[0]) - _EPS <= p[0] <= max(a[0], b[0]) + _EPS
        and min(a[1], b[1]) - _EPS <= p[1] <= max(a[1], b[1]) + _EPS
        and abs(_orient(a, b, p)) <= _EPS
    )


def segments_intersect(a, b, c, d):
    o1 = _orient(a, b, c)
    o2 = _orient(a, b, d)
    o3 = _orient(c, d, a)
    o4 = _orient(c, d, b)
    if ((o1 > _EPS and o2 < -_EPS) or (o1 < -_EPS and o2 > _EPS)) and (
        (o3 > _EPS and o4 < -_EPS) or (o3 < -_EPS and o4 > _EPS)
    ):
        return True
    return (
        _on_segment(a, b, c)
        or _on_segment(a, b, d)
        or _on_segment(c, d, a)
        or _on_segment(c, d, b)
    )


def point_in_polygon(point, poly):
    if len(poly) < 3:
        return False
    inside = False
    x, y = point
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if _on_segment(poly[j], poly[i], point):
            return True
        crosses = ((yi > y) != (yj > y))
        if crosses:
            x_hit = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_hit:
                inside = not inside
        j = i
    return inside


def _point_segment_distance(p, a, b):
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    denom = dx * dx + dy * dy
    if denom <= _EPS:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / denom
    t = max(0.0, min(1.0, t))
    qx = a[0] + t * dx
    qy = a[1] + t * dy
    return math.hypot(p[0] - qx, p[1] - qy)


def _segment_distance(a, b, c, d):
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        _point_segment_distance(a, c, d),
        _point_segment_distance(b, c, d),
        _point_segment_distance(c, a, b),
        _point_segment_distance(d, a, b),
    )


def polygons_conflict(left, right, margin=0.0):
    """True when polygons overlap/touch, or are closer than ``margin``."""
    if len(left) < 3 or len(right) < 3:
        return False
    lminx, lmaxx, lminy, lmaxy = polygon_bounds(left)
    rminx, rmaxx, rminy, rmaxy = polygon_bounds(right)
    margin = max(0.0, float(margin))
    if (
        lmaxx + margin < rminx
        or rmaxx + margin < lminx
        or lmaxy + margin < rminy
        or rmaxy + margin < lminy
    ):
        return False

    left_edges = list(zip(left, left[1:] + left[:1]))
    right_edges = list(zip(right, right[1:] + right[:1]))
    for a, b in left_edges:
        for c, d in right_edges:
            if segments_intersect(a, b, c, d):
                return True
    if point_in_polygon(left[0], right) or point_in_polygon(right[0], left):
        return True
    if margin > 0.0:
        for a, b in left_edges:
            for c, d in right_edges:
                if _segment_distance(a, b, c, d) < margin - _EPS:
                    return True
    return False


def transform_polygons(polygons, scale, tx, ty):
    return tuple(
        tuple((float(x) * scale + tx, float(y) * scale + ty) for x, y in polygon)
        for polygon in polygons
    )


def polygons_fit_tile(polygons, margin=0.0):
    low = float(margin) - _EPS
    high = 1.0 - float(margin) + _EPS
    for polygon in polygons:
        for x, y in polygon:
            if x < low or x > high or y < low or y > high:
                return False
    return True


def polygon_sets_conflict(left_set, right_set, margin=0.0):
    for left in left_set:
        for right in right_set:
            if polygons_conflict(left, right, margin=margin):
                return True
    return False
