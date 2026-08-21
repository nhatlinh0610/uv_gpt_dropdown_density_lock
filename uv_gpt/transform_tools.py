import math

import bpy
import bmesh
from mathutils import Vector

from . import island_tools, uv_utils


def _selected_islands(context, uv_only=False):
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    if uv_only:
        islands = island_tools.get_selected_uv_islands_for_context(
            context,
            bm,
            uv_layer,
            refresh_invalid_sync=True,
        )
        uv_utils.ensure_destructive_ready(context)
        obj = uv_utils.get_active_mesh_object(context)
        bm = island_tools.get_active_bmesh(context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        islands = island_tools.get_selected_uv_islands_for_context(
            context,
            bm,
            uv_layer,
        )
    else:
        uv_utils.ensure_destructive_ready(context)
        obj = uv_utils.get_active_mesh_object(context)
        bm = island_tools.get_active_bmesh(context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        islands = island_tools.get_selected_uv_islands(bm, uv_layer)
    if not islands:
        raise RuntimeError("Select one or more UV islands.")
    return obj, bm, uv_layer, islands


def _selected_loops(islands):
    return [loop for island in islands for loop in island]


class UVGPT_OT_center_selected(bpy.types.Operator):
    bl_idname = "uv_gpt.center_selected"
    bl_label = "Center Selected"
    bl_description = "Move selected UV islands to the 0.5, 0.5 tile center"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            obj, bm, uv_layer, islands = _selected_islands(context, uv_only=True)
            loops = _selected_loops(islands)
            center = uv_utils.get_loops_center(loops, uv_layer)
            delta = Vector((0.5, 0.5)) - center
            for island in islands:
                uv_utils.translate_island(island, uv_layer, delta)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class UVGPT_OT_mirror_x(bpy.types.Operator):
    bl_idname = "uv_gpt.mirror_x"
    bl_label = "Mirror X"
    bl_description = "Mirror selected UVs across the selection center or U=0.5"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            settings = uv_utils.get_settings(context)
            obj, bm, uv_layer, islands = _selected_islands(context)
            loops = _selected_loops(islands)
            if settings.mirror_pivot == "TILE_U":
                pivot_u = 0.5
            else:
                pivot_u = uv_utils.get_loops_center(loops, uv_layer).x
            for loop in loops:
                uv = loop[uv_layer].uv
                uv.x = 2.0 * pivot_u - uv.x
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def _rotate_selected(context, angle):
    obj, bm, uv_layer, islands = _selected_islands(context)
    center = uv_utils.get_loops_center(_selected_loops(islands), uv_layer)
    for island in islands:
        uv_utils.rotate_island(island, uv_layer, center, angle)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


class UVGPT_OT_rotate_90(bpy.types.Operator):
    bl_idname = "uv_gpt.rotate_90"
    bl_label = "Rotate 90 deg"
    bl_description = "Rotate selected UV islands 90 degrees around the selection center"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            _rotate_selected(context, math.radians(90.0))
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class UVGPT_OT_rotate_180(bpy.types.Operator):
    bl_idname = "uv_gpt.rotate_180"
    bl_label = "Rotate 180 deg"
    bl_description = "Rotate selected UV islands 180 degrees around the selection center"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            _rotate_selected(context, math.radians(180.0))
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


classes = (
    UVGPT_OT_center_selected,
    UVGPT_OT_mirror_x,
    UVGPT_OT_rotate_90,
    UVGPT_OT_rotate_180,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
