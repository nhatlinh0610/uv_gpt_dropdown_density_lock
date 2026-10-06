import traceback
import math

import bpy
import bmesh
from mathutils import Vector

from . import island_tools, uv_utils


_EPSILON = 1.0e-12
_UV_KEY_DIGITS = 10


def _axis_from_settings(settings):
    axis = settings.symmetry_axis
    if axis == "U_HALF":
        return "U", 0.5
    if axis == "V_HALF":
        return "V", 0.5
    raise RuntimeError("Choose the U or V symmetry axis.")


def _mirror_point(point, axis_kind, axis_value):
    if axis_kind == "V":
        return Vector((point.x, 2.0 * axis_value - point.y))
    return Vector((2.0 * axis_value - point.x, point.y))


def _mirror_single_region(region, uv_layer, axis_kind, axis_value):
    """Reflect every loop of one region across the selected tile half-axis.

    A single-region mirror is a direct geometric reflection.  It preserves
    the island's scale and the coordinate orthogonal to the chosen axis; no
    centroid alignment, PCA rotation, or similarity scale is applied.
    """
    if axis_kind not in {"U", "V"}:
        raise RuntimeError("Choose the U or V symmetry axis.")
    for loop in _region_loops(region):
        uv = loop[uv_layer].uv
        mirrored = _mirror_point(uv, axis_kind, axis_value)
        uv.x = mirrored.x
        uv.y = mirrored.y


def _mirror_direction(direction, axis_kind):
    """Mirror a direction vector across the selected UV symmetry axis."""
    if axis_kind == "V":
        return Vector((direction.x, -direction.y))
    return Vector((-direction.x, direction.y))


def _region_loops(region):
    return [loop for face in region for loop in face.loops]


def _bounds_center(loops, uv_layer):
    min_u, max_u, min_v, max_v = island_tools.get_island_bounds(loops, uv_layer)
    return Vector(((min_u + max_u) * 0.5, (min_v + max_v) * 0.5))


def _unique_uv_points(loops, uv_layer):
    """Return unique UV positions so shared face corners are not over-weighted."""
    points = []
    seen = set()
    for loop in loops:
        uv = loop[uv_layer].uv
        key = (
            round(float(uv.x), _UV_KEY_DIGITS),
            round(float(uv.y), _UV_KEY_DIGITS),
        )
        if key in seen:
            continue
        seen.add(key)
        points.append(Vector((float(uv.x), float(uv.y))))
    return points


def _centroid(points):
    if not points:
        raise RuntimeError("The selected UV region contains no UV coordinates.")
    center = Vector((0.0, 0.0))
    for point in points:
        center += point
    return center / len(points)


def _rms_radius(points, center):
    """Rotation-independent size used for uniform scale matching."""
    if not points:
        return 0.0
    radius_sq = sum((point - center).length_squared for point in points) / len(points)
    return math.sqrt(max(radius_sq, 0.0))


def _principal_axis(points, center):
    """Return a stable signed PCA axis for UV-island orientation."""
    if len(points) < 2:
        return None

    xx = yy = xy = 0.0
    for point in points:
        delta = point - center
        xx += delta.x * delta.x
        yy += delta.y * delta.y
        xy += delta.x * delta.y

    total_variance = xx + yy
    if total_variance <= _EPSILON:
        return None

    anisotropy = math.hypot(xx - yy, 2.0 * xy) / total_variance
    if anisotropy < 1.0e-5:
        # Circular/square-like islands do not have a reliable main direction.
        return None

    angle = 0.5 * math.atan2(2.0 * xy, xx - yy)
    axis = Vector((math.cos(angle), math.sin(angle)))

    # PCA is undirected (+/-). Resolve the sign deterministically.
    projections = [(point - center).dot(axis) for point in points]
    skew = sum(value * value * value for value in projections)
    skew_scale = sum(abs(value) ** 3 for value in projections)

    if skew_scale > _EPSILON and abs(skew) > 1.0e-8 * skew_scale:
        if skew < 0.0:
            axis.negate()
    else:
        farthest = max(points, key=lambda point: (point - center).length_squared)
        if (farthest - center).dot(axis) < 0.0:
            axis.negate()

    return axis


def _transform_data(loops, uv_layer):
    points = _unique_uv_points(loops, uv_layer)
    center = _centroid(points)
    bounds_center = _bounds_center(loops, uv_layer)
    return {
        "center": bounds_center,
        "bounds_center": bounds_center,
        "centroid": center,
        "rms_radius": _rms_radius(points, center),
        "principal_axis": _principal_axis(points, center),
    }


def _region_data(region, uv_layer):
    return _transform_data(_region_loops(region), uv_layer)


def _island_data(island, uv_layer):
    """Compatibility wrapper for callers/tests that pass one island of loops."""
    return _transform_data(island, uv_layer)


def _rotation_angle(source_axis, desired_axis):
    """Signed 2D angle that rotates source_axis onto desired_axis."""
    if source_axis is None or desired_axis is None:
        return 0.0
    cross = source_axis.x * desired_axis.y - source_axis.y * desired_axis.x
    dot = source_axis.dot(desired_axis)
    return math.atan2(cross, dot)


def _wrap_angle(angle):
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _nearest_uv_axis_rotation(principal_axis):
    """Return the smallest turn that aligns the island's main axis to U or V."""
    if principal_axis is None:
        return 0.0, None

    current_angle = math.atan2(principal_axis.y, principal_axis.x)
    candidates = (
        (0.0, "U"),
        (0.5 * math.pi, "V"),
        (math.pi, "U"),
        (-0.5 * math.pi, "V"),
    )

    best_angle = 0.0
    best_label = None
    best_abs = float("inf")
    for target_angle, label in candidates:
        delta = _wrap_angle(target_angle - current_angle)
        score = abs(delta)
        if score < best_abs:
            best_abs = score
            best_angle = delta
            best_label = label

    return best_angle, best_label


def _apply_similarity_transform(loops, uv_layer, pivot, angle, scale):
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    for loop in loops:
        uv = loop[uv_layer].uv
        local_x = uv.x - pivot.x
        local_y = uv.y - pivot.y

        rotated_x = local_x * cos_a - local_y * sin_a
        rotated_y = local_x * sin_a + local_y * cos_a

        uv.x = pivot.x + rotated_x * scale
        uv.y = pivot.y + rotated_y * scale


def _center_and_align_single_region(region, uv_layer):
    """Center one region at 0.5/0.5 and rotate it to the nearest U/V axis.

    Single-island mode intentionally NEVER scales or flips the island.
    """
    loops = _region_loops(region)
    data = _transform_data(loops, uv_layer)

    angle, aligned_axis = _nearest_uv_axis_rotation(data["principal_axis"])
    if abs(angle) > _EPSILON:
        _apply_similarity_transform(
            loops,
            uv_layer,
            data["centroid"],
            angle,
            1.0,
        )

    # Center after rotation because the axis-aligned bounds can change.
    current_center = _bounds_center(loops, uv_layer)
    tile_center = Vector((0.5, 0.5))
    uv_utils.translate_island(loops, uv_layer, tile_center - current_center)

    return angle, aligned_axis


def _apply_from_reference(context, target_region, ref_data, uv_layer):
    """Mirror position and match rotation + uniform scale without UV flipping."""
    settings = uv_utils.get_settings(context)
    axis_kind, axis_value = _axis_from_settings(settings)
    target_loops = _region_loops(target_region)
    target_data = _transform_data(target_loops, uv_layer)

    target_radius = target_data["rms_radius"]
    ref_radius = ref_data.get("rms_radius")
    if ref_radius is None or target_radius <= _EPSILON or ref_radius <= _EPSILON:
        scale = 1.0
    else:
        scale = ref_radius / target_radius

    desired_axis = None
    ref_axis = ref_data.get("principal_axis")
    if ref_axis is not None:
        desired_axis = _mirror_direction(ref_axis, axis_kind)

    angle = _rotation_angle(target_data["principal_axis"], desired_axis)
    _apply_similarity_transform(
        target_loops,
        uv_layer,
        target_data["centroid"],
        angle,
        scale,
    )

    ref_bounds_center = ref_data.get("bounds_center", ref_data.get("center"))
    if ref_bounds_center is None:
        raise RuntimeError("Reference UV data is missing its center.")

    desired_target_center = _mirror_point(
        ref_bounds_center,
        axis_kind,
        axis_value,
    )
    transformed_target_center = _bounds_center(target_loops, uv_layer)
    uv_utils.translate_island(
        target_loops,
        uv_layer,
        desired_target_center - transformed_target_center,
    )

    return angle, scale


def _selected_regions(context, bm, uv_layer):
    regions = island_tools.get_selected_uv_regions_for_context(
        context,
        bm,
        uv_layer,
    )
    if len(regions) not in {1, 2}:
        raise RuntimeError(
            "Select one UV region to mirror, or two UV regions to mirror; "
            f"found {len(regions)} selected region(s)."
        )
    return regions


def _resolve_selected_pair(context, bm, uv_layer, regions=None):
    if regions is None:
        regions = _selected_regions(context, bm, uv_layer)
    if len(regions) != 2:
        raise RuntimeError(
            "Pair symmetry needs exactly two UV regions. Select one region for "
            "single-region mirroring, or exactly two for anchor/target symmetry."
        )

    _target_source, target_index = island_tools.resolve_selected_region_target(
        bm,
        regions,
    )
    if target_index is None:
        raise RuntimeError(
            "Could not resolve the target region from the active/last-selected face."
        )

    anchor_index = 1 - target_index
    return regions[anchor_index], regions[target_index]


class UVGPT_OT_symmetry_auto_mirror(bpy.types.Operator):
    bl_idname = "uv_gpt.symmetry_auto_mirror"
    bl_label = "Mirror Symmetry"
    bl_description = (
        "One region: reflect every UV across the selected U=0.5 or V=0.5 axis "
        "without scaling. "
        "Two regions: mirror position and match target rotation + uniform scale"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            obj = uv_utils.get_active_mesh_object(context)
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)

            # Resolve selection only once before ensure_destructive_ready().
            # Duplicating/switching the UV map can temporarily disturb UV
            # selection state, which previously caused a valid 1-island
            # selection to be re-read as invalid.
            regions = _selected_regions(context, bm, uv_layer)
            selection_count = len(regions)

            anchor_region = target_region = None
            axis_kind = axis_value = None
            if selection_count == 1:
                # Validate the single-region axis before any destructive setup
                # so an invalid setting cannot partially mutate the UVs.
                axis_kind, axis_value = _axis_from_settings(
                    uv_utils.get_settings(context)
                )
            if selection_count == 2:
                _axis_from_settings(uv_utils.get_settings(context))
                anchor_region, target_region = _resolve_selected_pair(
                    context,
                    bm,
                    uv_layer,
                    regions,
                )

            uv_utils.ensure_destructive_ready(context)
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)

            if selection_count == 1:
                _mirror_single_region(
                    regions[0],
                    uv_layer,
                    axis_kind,
                    axis_value,
                )
                bmesh.update_edit_mesh(
                    obj.data,
                    loop_triangles=False,
                    destructive=False,
                )
                self.report(
                    {"INFO"},
                    f"Mirrored UV region across {axis_kind}={axis_value:g}.",
                )
                return {"FINISHED"}

            angle, scale = _apply_from_reference(
                context,
                target_region,
                _region_data(anchor_region, uv_layer),
                uv_layer,
            )
            bmesh.update_edit_mesh(
                obj.data,
                loop_triangles=False,
                destructive=False,
            )

        except RuntimeError as exc:
            # User/selection issues are deliberately calm INFO notifications.
            self.report({"INFO"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            # Keep the UI non-blocking while preserving a traceback for debugging.
            traceback.print_exc()
            self.report({"INFO"}, f"Symmetry skipped: {exc}")
            return {"CANCELLED"}

        axis_label = (
            "U"
            if uv_utils.get_settings(context).symmetry_axis == "U_HALF"
            else "V"
        )
        self.report(
            {"INFO"},
            "Mirrored target transform across the "
            f"{axis_label} axis (rotation {math.degrees(angle):.2f} deg, "
            f"scale {scale:.4f}).",
        )
        return {"FINISHED"}


classes = (
    UVGPT_OT_symmetry_auto_mirror,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
