import bpy
import bmesh
from mathutils import Vector

from . import island_tools, uv_utils


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


def _region_loops(region):
    return [loop for face in region for loop in face.loops]


def _bounds_center(loops, uv_layer):
    min_u, max_u, min_v, max_v = island_tools.get_island_bounds(loops, uv_layer)
    return Vector(((min_u + max_u) * 0.5, (min_v + max_v) * 0.5))


def _region_data(region, uv_layer):
    """Return the only geometry needed by position-only Symmetry."""
    return {"center": _bounds_center(_region_loops(region), uv_layer)}


def _island_data(island, uv_layer):
    """Compatibility wrapper for callers/tests that pass one island of loops."""
    return {"center": _bounds_center(island, uv_layer)}


def _apply_from_reference(context, target_region, ref_data, uv_layer):
    settings = uv_utils.get_settings(context)
    axis_kind, axis_value = _axis_from_settings(settings)
    target_loops = _region_loops(target_region)
    target_center = _bounds_center(target_loops, uv_layer)
    desired_target_center = _mirror_point(
        ref_data["center"],
        axis_kind,
        axis_value,
    )
    delta = desired_target_center - target_center
    uv_utils.translate_island(target_loops, uv_layer, delta)


def _resolve_selected_pair(context, bm, uv_layer):
    regions = island_tools.get_selected_uv_regions_for_context(
        context,
        bm,
        uv_layer,
    )
    if len(regions) != 2:
        raise RuntimeError(
            "Select exactly two connected UV regions; found "
            f"{len(regions)} selected region(s). The anchor must be first and "
            "the target must be active/last-selected."
        )

    _target_source, target_index = island_tools.resolve_selected_region_target(
        bm,
        regions,
    )
    if target_index is None:
        raise RuntimeError(
            "The target region must be resolved by the active/last-selected "
            "selected face; Symmetry was cancelled without writing UVs."
        )

    anchor_index = 1 - target_index
    return regions[anchor_index], regions[target_index]


class UVGPT_OT_symmetry_auto_mirror(bpy.types.Operator):
    bl_idname = "uv_gpt.symmetry_auto_mirror"
    bl_label = "Mirror Target Position"
    bl_description = (
        "Translate the active/last-selected target so its position mirrors the "
        "first selected anchor across the chosen U or V axis"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            obj = uv_utils.get_active_mesh_object(context)
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            _axis_from_settings(uv_utils.get_settings(context))
            _resolve_selected_pair(context, bm, uv_layer)

            # Validate selection and axis before the optional UV-map duplication
            # performed by ensure_destructive_ready().  This keeps an invalid
            # active target from causing any symmetry UV write.
            uv_utils.ensure_destructive_ready(context)
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            anchor_region, target_region = _resolve_selected_pair(
                context,
                bm,
                uv_layer,
            )
            _apply_from_reference(
                context,
                target_region,
                _region_data(anchor_region, uv_layer),
                uv_layer,
            )
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        axis_label = "U" if uv_utils.get_settings(context).symmetry_axis == "U_HALF" else "V"
        self.report(
            {"INFO"},
            f"Mirrored the active target position across the {axis_label} axis.",
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
