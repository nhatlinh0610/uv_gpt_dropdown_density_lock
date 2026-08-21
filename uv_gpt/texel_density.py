import math

import bpy
import bmesh
from mathutils import Vector

from . import island_tools, overlay, uv_utils


def texture_size_from_settings(settings):
    if settings.texture_size_mode == "CUSTOM":
        return max(1, int(settings.custom_texture_size))
    return int(settings.texture_size_mode)


def cm_per_unit(context):
    scale_length = getattr(context.scene.unit_settings, "scale_length", 1.0) or 1.0
    return scale_length * 100.0


def px_cm_to_px_unit(context, px_cm):
    return px_cm * cm_per_unit(context)


def px_unit_to_px_cm(context, px_unit):
    value = cm_per_unit(context)
    return px_unit / value if value else px_unit


def target_px_cm(context):
    settings = uv_utils.get_settings(context)
    if settings.input_unit == "PX_UNIT":
        return px_unit_to_px_cm(context, settings.target_value)
    return settings.target_value


def set_target_from_px_cm(context, px_cm):
    settings = uv_utils.get_settings(context)
    if settings.input_unit == "PX_UNIT":
        settings.target_value = px_cm_to_px_unit(context, px_cm)
    else:
        settings.target_value = px_cm


def _format_number(value):
    if abs(value) >= 100.0:
        return f"{value:.0f}"
    if abs(value) >= 10.0:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_td_label(context, px_cm):
    settings = uv_utils.get_settings(context)
    px_unit = px_cm_to_px_unit(context, px_cm)
    if settings.display_unit == "PX_CM":
        return f"{_format_number(px_cm)} px/cm"
    if settings.display_unit == "PX_UNIT":
        return f"{_format_number(px_unit)} px/unit"
    return f"{_format_number(px_unit)} px/unit | {_format_number(px_cm)} px/cm"


def _face_world_area_units2(obj, face):
    matrix = obj.matrix_world
    points = [matrix @ loop.vert.co for loop in face.loops]
    if len(points) < 3:
        return 0.0
    origin = points[0]
    area = 0.0
    for index in range(1, len(points) - 1):
        area += (points[index] - origin).cross(points[index + 1] - origin).length * 0.5
    return area


def _island_mesh_area_cm2(context, obj, island):
    faces = island_tools.island_faces(island)
    unit = cm_per_unit(context)
    return sum(_face_world_area_units2(obj, face) for face in faces) * unit * unit


def calculate_island_px_cm(context, obj, island, uv_layer):
    settings = uv_utils.get_settings(context)
    texture_size = texture_size_from_settings(settings)
    uv_area = island_tools.get_island_area(island, uv_layer)
    mesh_area_cm2 = _island_mesh_area_cm2(context, obj, island)
    if uv_area <= 0.0 or mesh_area_cm2 <= 0.0:
        return 0.0
    return texture_size * math.sqrt(uv_area / mesh_area_cm2)


def _labels_for_islands(context, selected_only):
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = (
        island_tools.get_selected_uv_islands(bm, uv_layer)
        if selected_only
        else island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    )
    if selected_only and not islands:
        raise RuntimeError("Select one or more UV islands.")
    if not islands:
        raise RuntimeError("No UV islands found.")
    labels = []
    active_uv_name = obj.data.uv_layers.active.name if obj.data.uv_layers.active else ""
    for island in islands:
        px_cm = calculate_island_px_cm(context, obj, island, uv_layer)
        center = island_tools.get_island_center(island, uv_layer)
        labels.append(
            {
                "object_name": obj.name,
                "uv_map_name": active_uv_name,
                "face_indices": [face.index for face in island_tools.island_faces(island)],
                "center": (center.x, center.y),
                "px_cm": px_cm,
                "px_unit": px_cm_to_px_unit(context, px_cm),
                "text": format_td_label(context, px_cm),
                "td_dynamic": True,
                "selected": selected_only,
            }
        )
    return labels


def _selected_average_px_cm(context):
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_selected_uv_islands(bm, uv_layer)
    if not islands:
        raise RuntimeError("Select one or more UV islands.")
    values = [calculate_island_px_cm(context, obj, island, uv_layer) for island in islands]
    values = [value for value in values if value > 0.0]
    if not values:
        raise RuntimeError("Selected UV islands have no measurable texel density.")
    return sum(values) / len(values)


def apply_px_cm_to_islands(context, islands, px_cm):
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    changed = 0
    for island in islands:
        current = calculate_island_px_cm(context, obj, island, uv_layer)
        if current <= 0.0:
            continue
        scale = px_cm / current
        center = island_tools.get_island_center(island, uv_layer)
        uv_utils.scale_island(island, uv_layer, center, scale)
        changed += 1
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return changed


def apply_px_cm_to_selection(context, px_cm):
    uv_utils.ensure_destructive_ready(context)
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_selected_uv_islands(bm, uv_layer)
    if not islands:
        raise RuntimeError("Select one or more UV islands.")
    return apply_px_cm_to_islands(context, islands, px_cm)


def _face_uv_selected(face, uv_layer):
    if face.hide:
        return False
    if _face_has_uv_selection(face, uv_layer):
        return True
    return bool(face.select)


def _face_has_uv_selection(face, uv_layer):
    for loop in face.loops:
        luv = loop[uv_layer]
        if getattr(luv, "select", False) or getattr(luv, "select_edge", False):
            return True
    return False


def _selected_reference_face(bm, uv_layer):
    active_face = getattr(bm.faces, "active", None)
    if isinstance(active_face, bmesh.types.BMFace) and _face_has_uv_selection(active_face, uv_layer):
        return active_face

    history = getattr(bm, "select_history", None)
    active = getattr(history, "active", None) if history else None
    if isinstance(active, bmesh.types.BMFace) and _face_has_uv_selection(active, uv_layer):
        return active

    selected = [face for face in bm.faces if _face_has_uv_selection(face, uv_layer)]
    if len(selected) == 1:
        return selected[0]
    if len(selected) > 1:
        raise RuntimeError("Select only one quad face to use as the density square.")

    if isinstance(active_face, bmesh.types.BMFace) and _face_uv_selected(active_face, uv_layer):
        return active_face
    if isinstance(active, bmesh.types.BMFace) and _face_uv_selected(active, uv_layer):
        return active

    if not selected:
        selected = [face for face in bm.faces if _face_uv_selected(face, uv_layer)]
    if not selected:
        raise RuntimeError("Select one quad face to use as the density square.")
    if len(selected) > 1:
        raise RuntimeError("Select only one quad face to use as the density square.")
    return selected[0]


def _face_uv_center(face, uv_layer):
    center = Vector((0.0, 0.0))
    loops = list(face.loops)
    for loop in loops:
        center += loop[uv_layer].uv
    return center / max(1, len(loops))


def _square_axes(face, uv_layer):
    loops = list(face.loops)
    edge = loops[1][uv_layer].uv - loops[0][uv_layer].uv
    if edge.length_squared <= 1e-12:
        return Vector((1.0, 0.0)), Vector((0.0, 1.0))
    axis_u = edge.normalized()
    axis_v = Vector((-axis_u.y, axis_u.x))
    return axis_u, axis_v


def _square_face_to_px_cm(context, obj, face, uv_layer, px_cm):
    loops = list(face.loops)
    if len(loops) != 4:
        raise RuntimeError("The density square needs one selected quad face.")

    side = _target_square_side(context, obj, face, px_cm)
    center = _face_uv_center(face, uv_layer)
    axis_u, axis_v = _square_axes(face, uv_layer)
    _assign_square_face_uvs(face, uv_layer, center, axis_u, axis_v, side)
    return side


def _target_square_side(context, obj, face, px_cm):
    texture_size = texture_size_from_settings(uv_utils.get_settings(context))
    mesh_area_cm2 = _face_world_area_units2(obj, face) * cm_per_unit(context) ** 2
    if mesh_area_cm2 <= 0.0:
        raise RuntimeError("Selected face has no measurable mesh area.")

    uv_area = mesh_area_cm2 * (px_cm / texture_size) ** 2
    side = math.sqrt(max(uv_area, 0.0))
    if side <= 0.0:
        raise RuntimeError("Target density produced an empty UV square.")
    return side


def _assign_square_face_uvs(face, uv_layer, center, axis_u, axis_v, side):
    loops = list(face.loops)
    half = side * 0.5
    offsets = (
        (-half, -half),
        (half, -half),
        (half, half),
        (-half, half),
    )
    for loop, (offset_u, offset_v) in zip(loops, offsets):
        loop[uv_layer].uv = center + axis_u * offset_u + axis_v * offset_v


def square_selected_face_to_px_cm(context, px_cm, ensure_ready=True):
    if ensure_ready:
        uv_utils.ensure_destructive_ready(context)
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    face = _selected_reference_face(bm, uv_layer)
    side = _square_face_to_px_cm(context, obj, face, uv_layer, px_cm)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return side


def _face_from_edge(face, edge):
    for linked in edge.link_faces:
        if linked is not face and not linked.hide:
            return linked
    return None


def _face_uv_from_vertex_map(face, vertex_uvs):
    center = Vector((0.0, 0.0))
    count = 0
    for loop in face.loops:
        uv = vertex_uvs.get(loop.vert)
        if uv is None:
            continue
        center += uv
        count += 1
    return center / max(1, count)


def _apply_vertex_uvs_to_face(face, uv_layer, vertex_uvs):
    for loop in face.loops:
        uv = vertex_uvs.get(loop.vert)
        if uv is None:
            return False
        loop[uv_layer].uv = uv.copy()
    return True


def _seed_grid_face(face, uv_layer, center, axis_u, axis_v, side, vertex_uvs):
    _assign_square_face_uvs(face, uv_layer, center, axis_u, axis_v, side)
    for loop in face.loops:
        vertex_uvs[loop.vert] = loop[uv_layer].uv.copy()


def _assign_neighbor_grid_face(source_face, target_face, shared_edge, uv_layer, vertex_uvs):
    if len(target_face.loops) != 4:
        return False
    shared_loop = None
    for loop in target_face.loops:
        if loop.edge is shared_edge:
            shared_loop = loop
            break
    if shared_loop is None:
        return False

    vert_a = shared_loop.vert
    vert_b = shared_loop.link_loop_next.vert
    uv_a = vertex_uvs.get(vert_a)
    uv_b = vertex_uvs.get(vert_b)
    if uv_a is None or uv_b is None:
        return False

    edge_vec = uv_b - uv_a
    edge_len = edge_vec.length
    if edge_len <= 1e-12:
        return False

    perp = Vector((-edge_vec.y, edge_vec.x)).normalized()
    edge_center = (uv_a + uv_b) * 0.5
    source_center = _face_uv_from_vertex_map(source_face, vertex_uvs)
    source_side = (source_center - edge_center).dot(perp)
    offset = (-perp if source_side >= 0.0 else perp) * edge_len

    next_vert = shared_loop.link_loop_next.link_loop_next.vert
    prev_vert = shared_loop.link_loop_prev.vert
    new_uvs = {
        next_vert: uv_b + offset,
        prev_vert: uv_a + offset,
    }
    for vert, uv in new_uvs.items():
        vertex_uvs.setdefault(vert, uv.copy())

    return _apply_vertex_uvs_to_face(target_face, uv_layer, vertex_uvs)


def _grid_component(seed_face, uv_layer, center, axis_u, axis_v, side, vertex_uvs, assigned_faces):
    if len(seed_face.loops) != 4:
        return 0
    _seed_grid_face(seed_face, uv_layer, center, axis_u, axis_v, side, vertex_uvs)
    assigned_faces.add(seed_face)
    queue = [seed_face]
    gridded = 1

    while queue:
        face = queue.pop(0)
        for loop in face.loops:
            edge = loop.edge
            neighbor = _face_from_edge(face, edge)
            if neighbor is None or neighbor in assigned_faces:
                continue
            if len(neighbor.loops) != 4:
                continue
            if not _assign_neighbor_grid_face(face, neighbor, edge, uv_layer, vertex_uvs):
                continue
            assigned_faces.add(neighbor)
            queue.append(neighbor)
            gridded += 1
    return gridded


def _assigned_faces_bounds(faces, uv_layer):
    us = []
    vs = []
    for face in faces:
        for loop in face.loops:
            uv = loop[uv_layer].uv
            us.append(uv.x)
            vs.append(uv.y)
    if not us:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(us), max(us), min(vs), max(vs))


def grid_whole_mesh_from_selected_face(context, px_cm, ensure_ready=True):
    if ensure_ready:
        uv_utils.ensure_destructive_ready(context)
    obj = uv_utils.get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    reference_face = _selected_reference_face(bm, uv_layer)
    if len(reference_face.loops) != 4:
        raise RuntimeError("The density grid needs one selected quad face.")

    side = _target_square_side(context, obj, reference_face, px_cm)
    vertex_uvs = {}
    assigned_faces = set()
    center = _face_uv_center(reference_face, uv_layer)
    axis_u, axis_v = _square_axes(reference_face, uv_layer)
    gridded = _grid_component(
        reference_face,
        uv_layer,
        center,
        axis_u,
        axis_v,
        side,
        vertex_uvs,
        assigned_faces,
    )
    components = 1 if gridded else 0

    margin = max(side * 0.25, 0.001)
    _min_u, max_u, min_v, _max_v = _assigned_faces_bounds(assigned_faces, uv_layer)
    next_u = max_u + margin + side * 0.5
    visible_faces = [face for face in bm.faces if not face.hide]
    skipped_non_quads = len([face for face in visible_faces if len(face.loops) != 4])

    for face in visible_faces:
        if face in assigned_faces or len(face.loops) != 4:
            continue
        component_center = Vector((next_u, min_v + side * 0.5))
        added = _grid_component(
            face,
            uv_layer,
            component_center,
            Vector((1.0, 0.0)),
            Vector((0.0, 1.0)),
            side,
            vertex_uvs,
            assigned_faces,
        )
        if not added:
            continue
        components += 1
        _min_u, max_u, min_v, _max_v = _assigned_faces_bounds(assigned_faces, uv_layer)
        next_u = max_u + margin + side * 0.5

    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return {
        "faces": len(assigned_faces),
        "components": components,
        "skipped_non_quads": skipped_non_quads,
        "side": side,
    }


class UVGPT_OT_show_selected_td(bpy.types.Operator):
    bl_idname = "uv_gpt.show_selected_td"
    bl_label = "Show Selected TD"
    bl_description = "Draw texel density labels for selected UV islands in the UV Editor"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            labels = _labels_for_islands(context, selected_only=True)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        overlay.set_td_labels(labels)
        self.report({"INFO"}, f"Showing {len(labels)} selected texel density label(s).")
        return {"FINISHED"}


class UVGPT_OT_show_all_td(bpy.types.Operator):
    bl_idname = "uv_gpt.show_all_td"
    bl_label = "Show All TD"
    bl_description = "Draw texel density labels for every UV island in the UV Editor"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            labels = _labels_for_islands(context, selected_only=False)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        overlay.set_td_labels(labels)
        self.report({"INFO"}, f"Showing {len(labels)} texel density label(s).")
        return {"FINISHED"}


class UVGPT_OT_hide_td_overlay(bpy.types.Operator):
    bl_idname = "uv_gpt.hide_td_overlay"
    bl_label = "Hide TD Overlay"
    bl_description = "Clear texel density labels"
    bl_options = {"REGISTER"}

    def execute(self, context):
        overlay.clear_td_labels()
        self.report({"INFO"}, "Texel density overlay hidden.")
        return {"FINISHED"}


class UVGPT_OT_set_target_from_selected(bpy.types.Operator):
    bl_idname = "uv_gpt.set_target_from_selected"
    bl_label = "Set Target From Selected"
    bl_description = "Use the average selected texel density as the target"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            px_cm = _selected_average_px_cm(context)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        set_target_from_px_cm(context, px_cm)
        if overlay.td_overlay_active():
            try:
                overlay.set_td_labels(_labels_for_islands(context, selected_only=True))
            except RuntimeError:
                pass
        self.report({"INFO"}, f"Target TD set to {format_td_label(context, px_cm)}.")
        return {"FINISHED"}


class UVGPT_OT_square_selected_face_td(bpy.types.Operator):
    bl_idname = "uv_gpt.square_selected_face_td"
    bl_label = "Square Face to TD"
    bl_description = "Make the selected quad UV face square at the target texel density"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            px_cm = target_px_cm(context)
            square_selected_face_to_px_cm(context, px_cm)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Squared face to {format_td_label(context, px_cm)}.")
        return {"FINISHED"}


class UVGPT_OT_square_face_apply_td_whole_mesh(bpy.types.Operator):
    bl_idname = "uv_gpt.square_face_apply_td_whole_mesh"
    bl_label = "Grid Mesh From Face TD"
    bl_description = "Use the selected quad face as the density cell and turn the whole quad mesh into a UV grid"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            px_cm = target_px_cm(context)
            result = grid_whole_mesh_from_selected_face(context, px_cm)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        skipped = result["skipped_non_quads"]
        skipped_note = f" Skipped {skipped} non-quad face(s)." if skipped else ""
        self.report(
            {"INFO"},
            (
                f"Gridded {result['faces']} quad face(s) in "
                f"{result['components']} component(s) at {format_td_label(context, px_cm)}."
                f"{skipped_note}"
            ),
        )
        return {"FINISHED"}


class UVGPT_OT_apply_td_selected(bpy.types.Operator):
    bl_idname = "uv_gpt.apply_td_selected"
    bl_label = "Apply to Selected"
    bl_description = "Scale selected UV islands to the target texel density"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            changed = apply_px_cm_to_selection(context, target_px_cm(context))
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Applied target TD to {changed} selected island(s).")
        return {"FINISHED"}


class UVGPT_OT_apply_td_whole_mesh(bpy.types.Operator):
    bl_idname = "uv_gpt.apply_td_whole_mesh"
    bl_label = "Apply to Whole Mesh"
    bl_description = "Scale all UV islands to the target texel density"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            uv_utils.ensure_destructive_ready(context)
            obj = uv_utils.get_active_mesh_object(context)
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
            changed = apply_px_cm_to_islands(context, islands, target_px_cm(context))
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Applied target TD to {changed} island(s).")
        return {"FINISHED"}


classes = (
    UVGPT_OT_show_selected_td,
    UVGPT_OT_show_all_td,
    UVGPT_OT_hide_td_overlay,
    UVGPT_OT_set_target_from_selected,
    UVGPT_OT_square_selected_face_td,
    UVGPT_OT_square_face_apply_td_whole_mesh,
    UVGPT_OT_apply_td_selected,
    UVGPT_OT_apply_td_whole_mesh,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
