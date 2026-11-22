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
    n = len(poly)
    if n < 3:
        return False
    inside = False
    x, y = point
    eps = _EPS
    xj, yj = poly[n - 1]
    for xi, yi in poly:
        if (
            (xi if xi < xj else xj) - eps <= x <= (xj if xi < xj else xi) + eps
            and (yi if yi < yj else yj) - eps <= y <= (yj if yi < yj else yi) + eps
            and abs((xi - xj) * (y - yj) - (yi - yj) * (x - xj)) <= eps
        ):
            return True
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        xj, yj = xi, yi
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


def convex_hull(points):
    """Return 2D convex hull vertices in CCW order using Andrew's monotone chain."""
    unique = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(unique) <= 2:
        return tuple(unique)

    lower = []
    for p in unique:
        while len(lower) >= 2 and _orient(lower[-2], lower[-1], p) <= _EPS:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(unique):
        while len(upper) >= 2 and _orient(upper[-2], upper[-1], p) <= _EPS:
            upper.pop()
        upper.append(p)

    return tuple(lower[:-1] + upper[:-1])


def min_area_rect_angle(points):
    """Return rotation angle (radians) aligning points to their min-area AABB."""
    hull = convex_hull(points)
    if len(hull) < 3:
        return 0.0
    best_area = float("inf")
    best_angle = 0.0
    n = len(hull)
    for i in range(n):
        a = hull[i]
        b = hull[(i + 1) % n]
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        if math.hypot(dx, dy) <= _EPS:
            continue
        angle = -math.atan2(dy, dx)
        c = math.cos(angle)
        s = math.sin(angle)
        min_x = min_y = float("inf")
        max_x = max_y = -float("inf")
        for px, py in hull:
            rx = c * px - s * py
            ry = s * px + c * py
            if rx < min_x:
                min_x = rx
            if rx > max_x:
                max_x = rx
            if ry < min_y:
                min_y = ry
            if ry > max_y:
                max_y = ry
        w = max_x - min_x
        h = max_y - min_y
        area = w * h
        if area < best_area - 1.0e-12:
            best_area = area
            # Prefer wider-than-tall horizontal canonical alignment.
            best_angle = angle + (math.pi * 0.5 if h > w else 0.0)
    return best_angle


def min_square_span_angle(points):
    """Return rotation angle (radians) minimizing max(width, height) for square tile fit."""
    hull = convex_hull(points)
    if len(hull) < 3:
        return 0.0

    def _span(rad):
        c = math.cos(rad)
        s = math.sin(rad)
        min_x = min_y = float("inf")
        max_x = max_y = -float("inf")
        for px, py in hull:
            rx = c * px - s * py
            ry = s * px + c * py
            if rx < min_x:
                min_x = rx
            if rx > max_x:
                max_x = rx
            if ry < min_y:
                min_y = ry
            if ry > max_y:
                max_y = ry
        return max(max_x - min_x, max_y - min_y)

    # max(w, h) has exact 90-degree periodicity; sweep 0..89 deg then refine with ternary search.
    best_rad = 0.0
    best_span = float("inf")
    for deg in range(90):
        rad = math.radians(deg)
        sp = _span(rad)
        if sp < best_span:
            best_span = sp
            best_rad = rad
    lo = best_rad - math.radians(1.5)
    hi = best_rad + math.radians(1.5)
    for _ in range(16):
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if _span(m1) < _span(m2):
            hi = m2
        else:
            lo = m1
    return (0.5 * (lo + hi)) % (2.0 * math.pi)



def _point_on_polygon_boundary(point, poly, tol=1.0e-5):
    if len(poly) < 2:
        return False
    for i in range(len(poly)):
        a = poly[i]
        b = poly[(i + 1) % len(poly)]
        if _point_segment_distance(point, a, b) <= tol:
            return True
    return False


def _point_in_shape_strict(point, shape, boundary_tol=1.0e-5):
    for poly in shape:
        if _point_on_polygon_boundary(point, poly, tol=boundary_tol):
            return False
    inside = False
    for poly in shape:
        if point_in_polygon(point, poly):
            inside = not inside
    return inside


def _interior_probe_points(shape):
    probes = []
    for poly in shape:
        if len(poly) < 3:
            continue
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        if _point_in_shape_strict((cx, cy), shape):
            probes.append((cx, cy))
        # Inward normal probes from polygon edges handle non-convex C/U crescents
        # as well as narrow ribbons where large length-fractions overshoot.
        edge_items = []
        for i in range(len(poly)):
            a = poly[i]
            b = poly[(i + 1) % len(poly)]
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length > 1.0e-7:
                edge_items.append((length, a, b))
        edge_items.sort(reverse=True)
        for length, a, b in edge_items[:10]:
            mx = (a[0] + b[0]) * 0.5
            my = (a[1] + b[1]) * 0.5
            nx = -(b[1] - a[1]) / length
            ny = (b[0] - a[0]) / length
            found = False
            steps = [length * f for f in (0.01, 0.002, 0.05, 0.0004, 0.0001)] + [2.0e-5]
            for step in steps:
                if step <= 1.1e-5:
                    continue
                for sign in (1.0, -1.0):
                    cand = (mx + nx * step * sign, my + ny * step * sign)
                    if _point_in_shape_strict(cand, shape):
                        probes.append(cand)
                        found = True
                        break
                if found:
                    break
            if len(probes) >= 10:
                break
    return tuple(probes)


def _sample_boundary_points_for_overlap(shape, target_bounds=None, max_pts=192):
    pts = []
    if target_bounds is not None:
        minx, maxx, miny, maxy = target_bounds
        for poly in shape:
            n = len(poly)
            if n < 3:
                continue
            for i in range(n):
                a = poly[i]
                if minx <= a[0] <= maxx and miny <= a[1] <= maxy:
                    pts.append(a)
                b = poly[(i + 1) % n]
                mx = (a[0] + b[0]) * 0.5
                my = (a[1] + b[1]) * 0.5
                if minx <= mx <= maxx and miny <= my <= maxy:
                    pts.append((mx, my))
                if len(pts) >= max_pts:
                    return tuple(pts)
        if pts:
            return tuple(pts)
    for poly in shape:
        n = len(poly)
        if n < 3:
            continue
        step = max(1, n // max(1, max_pts // 2))
        for i in range(0, n, step):
            a = poly[i]
            b = poly[(i + 1) % n]
            pts.append(a)
            pts.append(((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5))
            if len(pts) >= max_pts:
                return tuple(pts)
    return tuple(pts)


def shapes_strictly_overlap(left, right):
    """True when two 2D polygon sets share positive overlapping area or cross strictly.

    Two non-overlapping shapes merely touching flush at a shared boundary (gap=0)
    return False so adjacent non-stacked islands are not falsely locked together.
    """
    if not left or not right:
        return False
    lpts = [p for poly in left for p in poly]
    rpts = [p for poly in right for p in poly]
    if not lpts or not rpts:
        return False
    lb = polygon_bounds(lpts)
    rb = polygon_bounds(rpts)
    if (
        min(lb[1], rb[1]) <= max(lb[0], rb[0]) + 1.0e-5
        or min(lb[3], rb[3]) <= max(lb[2], rb[2]) + 1.0e-5
    ):
        return False

    for lp in left:
        for rp in right:
            le = list(zip(lp, lp[1:] + lp[:1]))
            re = list(zip(rp, rp[1:] + rp[:1]))
            for a, b in le:
                minax = min(a[0], b[0])
                maxax = max(a[0], b[0])
                minay = min(a[1], b[1])
                maxay = max(a[1], b[1])
                for c, d in re:
                    if (
                        maxax <= min(c[0], d[0]) + 1.0e-5
                        or max(c[0], d[0]) <= minax + 1.0e-5
                        or maxay <= min(c[1], d[1]) + 1.0e-5
                        or max(c[1], d[1]) <= minay + 1.0e-5
                    ):
                        continue
                    o1 = _orient(a, b, c)
                    o2 = _orient(a, b, d)
                    o3 = _orient(c, d, a)
                    o4 = _orient(c, d, b)
                    if (o1 * o2 < 0.0) and (o3 * o4 < 0.0):
                        # Require strict non-grazing crossing beyond float32 boundary noise.
                        len_ab = math.hypot(b[0] - a[0], b[1] - a[1])
                        len_cd = math.hypot(d[0] - c[0], d[1] - c[1])
                        if (
                            min(abs(o1), abs(o2)) > 1.0e-5 * len_ab
                            and min(abs(o3), abs(o4)) > 1.0e-5 * len_cd
                        ):
                            return True

    for probe in _interior_probe_points(left):
        if _point_in_shape_strict(probe, right):
            return True
    for probe in _interior_probe_points(right):
        if _point_in_shape_strict(probe, left):
            return True
    for pt in _sample_boundary_points_for_overlap(left, target_bounds=rb):
        if _point_in_shape_strict(pt, right):
            return True
    for pt in _sample_boundary_points_for_overlap(right, target_bounds=lb):
        if _point_in_shape_strict(pt, left):
            return True
    return False
