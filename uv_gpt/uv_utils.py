import math

import bpy
import bmesh
from bpy.types import Operator
from mathutils import Vector

from . import island_tools


BAKE_UV_NAME = "Bake_Optimized"


def get_settings(context):
    return context.scene.uv_gpt_settings


def get_active_mesh_object(context):
    obj = getattr(context, "edit_object", None) or getattr(context, "object", None)
    if not obj or obj.type != "MESH":
        raise RuntimeError("Select a mesh object.")
    if obj.mode != "EDIT":
        raise RuntimeError("uv GPT works on UVs in Edit Mode.")
    return obj


def set_active_uv_map(context, name):
    obj = get_active_mesh_object(context)
    if not obj.data.uv_layers or not name or name == "NONE":
        return False
    for index, layer in enumerate(obj.data.uv_layers):
        if layer.name == name:
            obj.data.uv_layers.active_index = index
            try:
                bm = bmesh.from_edit_mesh(obj.data)
                bm_layer = bm.loops.layers.uv.get(name)
                if bm_layer is not None:
                    bm.loops.layers.uv.active = bm_layer
            except Exception:
                pass
            return True
    return False


def duplicate_to_bake_optimized(context):
    obj = get_active_mesh_object(context)
    bm = island_tools.get_active_bmesh(context)
    source_layer = island_tools.get_active_uv_layer(bm, obj)
    target_layer = bm.loops.layers.uv.get(BAKE_UV_NAME)
    if target_layer is None:
        target_layer = bm.loops.layers.uv.new(BAKE_UV_NAME)

    for face in bm.faces:
        for loop in face.loops:
            source = loop[source_layer]
            target = loop[target_layer]
            target.uv = source.uv.copy()
            for attr in ("select", "select_edge", "pin_uv"):
                try:
                    setattr(target, attr, getattr(source, attr))
                except Exception:
                    pass

    try:
        bm.loops.layers.uv.active = target_layer
    except Exception:
        pass
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    set_active_uv_map(context, BAKE_UV_NAME)
    return BAKE_UV_NAME


def ensure_destructive_ready(context):
    settings = get_settings(context)
    if settings.duplicate_before_operations:
        return duplicate_to_bake_optimized(context)
    if settings.active_uv_map and settings.active_uv_map != "NONE":
        set_active_uv_map(context, settings.active_uv_map)
    return None


def report_error(operator, message):
    if operator:
        operator.report({"ERROR"}, message)


def report_info(operator, message):
    if operator:
        operator.report({"INFO"}, message)


def selected_or_error(bm, uv_layer):
    islands = island_tools.get_selected_uv_islands(bm, uv_layer)
    if not islands:
        raise RuntimeError("Select one or more UV islands.")
    return islands


def store_uv_selection(bm, uv_layer):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    snapshot = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            snapshot[(face.index, local_index)] = (
                bool(getattr(luv, "select", False)),
                bool(getattr(luv, "select_edge", False)),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
            )
    return snapshot


def restore_uv_selection(bm, uv_layer, snapshot):
    if not snapshot:
        return
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    for face in bm.faces:
        face.select_set(False)
        for loop in face.loops:
            loop.vert.select_set(False)
            loop.edge.select_set(False)
            luv = loop[uv_layer]
            try:
                luv.select = False
                luv.select_edge = False
            except Exception:
                pass

    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            values = snapshot.get((face.index, local_index))
            if values is None:
                continue
            uv_select, edge_select, face_select, vert_select, mesh_edge_select = values
            luv = loop[uv_layer]
            try:
                luv.select = uv_select
                luv.select_edge = edge_select
            except Exception:
                pass
            if face_select:
                face.select_set(True)
            if vert_select:
                loop.vert.select_set(True)
            if mesh_edge_select:
                loop.edge.select_set(True)


def _optional_bool(value_owner, attribute):
    value = getattr(value_owner, attribute, None)
    return None if value is None else bool(value)


def _selection_element_key(element):
    if isinstance(element, bmesh.types.BMFace):
        return ("FACE", int(element.index))
    if isinstance(element, bmesh.types.BMEdge):
        return ("EDGE", int(element.index))
    if isinstance(element, bmesh.types.BMVert):
        return ("VERT", int(element.index))
    return None


def _selection_element_from_key(bm, key):
    if not key:
        return None
    element_type, index = key
    collection = {
        "FACE": bm.faces,
        "EDGE": bm.edges,
        "VERT": bm.verts,
    }.get(element_type)
    if collection is None or index < 0 or index >= len(collection):
        return None
    return collection[index]


def _set_optional_bool(value_owner, attribute, value):
    if value is None:
        return
    try:
        setattr(value_owner, attribute, bool(value))
    except (AttributeError, TypeError, RuntimeError):
        pass


def _set_mesh_selection(element, value):
    try:
        element.select = bool(value)
    except (AttributeError, TypeError, RuntimeError):
        element.select_set(bool(value))


def _set_loop_uv_flag(loop, attribute, value):
    setter = getattr(loop, f"{attribute}_set", None)
    if setter is not None:
        try:
            setter(bool(value))
        except (AttributeError, TypeError, RuntimeError):
            pass


def store_uv_selection_state(bm, uv_layer):
    """Snapshot mesh, UV-loop, active-face, and select-history state."""
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()

    faces = {face.index: bool(face.select) for face in bm.faces}
    verts = {vert.index: bool(vert.select) for vert in bm.verts}
    edges = {edge.index: bool(edge.select) for edge in bm.edges}
    loops = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            loops[(face.index, local_index)] = (
                _optional_bool(luv, "select"),
                _optional_bool(luv, "select_edge"),
                bool(face.select),
                _optional_bool(face, "uv_select"),
                _optional_bool(loop, "uv_select_vert"),
                _optional_bool(loop, "uv_select_edge"),
            )

    history = getattr(bm, "select_history", None)
    history_entries = []
    if history is not None:
        for element in history:
            key = _selection_element_key(element)
            if key is not None:
                history_entries.append(key)
    history_active = _selection_element_key(
        getattr(history, "active", None) if history is not None else None
    )
    return {
        "faces": faces,
        "verts": verts,
        "edges": edges,
        "loops": loops,
        "history": tuple(history_entries),
        "history_active": history_active,
        "active_face": _selection_element_key(getattr(bm.faces, "active", None)),
    }


def _clear_uv_selection_state(bm, uv_layer):
    for face in bm.faces:
        _set_mesh_selection(face, False)
        _set_optional_bool(face, "uv_select", False)
        for loop in face.loops:
            _set_mesh_selection(loop.vert, False)
            _set_mesh_selection(loop.edge, False)
            luv = loop[uv_layer]
            _set_optional_bool(luv, "select", False)
            _set_optional_bool(luv, "select_edge", False)
            _set_loop_uv_flag(loop, "uv_select_vert", False)
            _set_loop_uv_flag(loop, "uv_select_edge", False)


def _apply_uv_selection_state(bm, uv_layer, snapshot):
    for (face_index, local_index), values in snapshot["loops"].items():
        loop = _loop_from_face_record(bm, face_index, local_index)
        if loop is None:
            continue
        (
            uv_select,
            uv_edge_select,
            face_select,
            face_uv_select,
            uv_vert_select,
            loop_uv_edge_select,
        ) = values
        face = loop.face
        luv = loop[uv_layer]
        _set_mesh_selection(face, face_select)
        _set_optional_bool(luv, "select", uv_select)
        _set_optional_bool(luv, "select_edge", uv_edge_select)
        _set_optional_bool(face, "uv_select", face_uv_select)
        if uv_vert_select is not None:
            _set_loop_uv_flag(loop, "uv_select_vert", uv_vert_select)
        if loop_uv_edge_select is not None:
            _set_loop_uv_flag(loop, "uv_select_edge", loop_uv_edge_select)


def _apply_mesh_selection_state(bm, snapshot):
    for face in bm.faces:
        _set_mesh_selection(face, bool(snapshot.get("faces", {}).get(face.index, False)))
    for edge in bm.edges:
        _set_mesh_selection(edge, bool(snapshot.get("edges", {}).get(edge.index, False)))
    # Blender may clear linked vertices while an edge/face is deselected. Apply
    # vertices last so shared seam vertices finish at their exact snapshot bit.
    for vert in bm.verts:
        _set_mesh_selection(vert, bool(snapshot.get("verts", {}).get(vert.index, False)))


def _loop_from_face_record(bm, face_index, local_index):
    if face_index < 0 or face_index >= len(bm.faces):
        return None
    face = bm.faces[face_index]
    loops = list(face.loops)
    if local_index < 0 or local_index >= len(loops):
        return None
    return loops[local_index]


def restore_uv_selection_state(bm, uv_layer, snapshot):
    """Restore a selection snapshot, including active face/history when supported."""
    if not snapshot:
        return
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()

    _clear_uv_selection_state(bm, uv_layer)
    _apply_uv_selection_state(bm, uv_layer, snapshot)
    _apply_mesh_selection_state(bm, snapshot)

    history = getattr(bm, "select_history", None)
    if history is not None:
        try:
            history.clear()
            for key in snapshot.get("history", ()):
                element = _selection_element_from_key(bm, key)
                if element is not None:
                    history.add(element)
            history_active = _selection_element_from_key(
                bm,
                snapshot.get("history_active"),
            )
            if history_active is not None:
                try:
                    history.active = history_active
                except (AttributeError, TypeError, RuntimeError):
                    pass
        except (AttributeError, TypeError, RuntimeError):
            pass

    active_face = _selection_element_from_key(bm, snapshot.get("active_face"))
    if active_face is not None and isinstance(active_face, bmesh.types.BMFace):
        try:
            bm.faces.active = active_face
        except (AttributeError, TypeError, RuntimeError):
            pass

    # Select-history insertion can reselect an element in Blender. Reapply the
    # exact flags once more so restoration is deterministic after that API call.
    _apply_uv_selection_state(bm, uv_layer, snapshot)
    _apply_mesh_selection_state(bm, snapshot)


def set_all_uv_selection(bm, uv_layer, value):
    for face in bm.faces:
        face.select_set(value)
        for loop in face.loops:
            loop.vert.select_set(value)
            loop.edge.select_set(value)
            luv = loop[uv_layer]
            try:
                luv.select = value
                luv.select_edge = value
            except Exception:
                pass


def select_islands(bm, uv_layer, islands):
    target_loops = {loop for island in islands for loop in island}
    for face in bm.faces:
        face_selected = False
        for loop in face.loops:
            selected = loop in target_loops
            if selected:
                face_selected = True
                loop.vert.select_set(True)
                loop.edge.select_set(True)
            luv = loop[uv_layer]
            try:
                luv.select = selected
                luv.select_edge = selected
            except Exception:
                pass
        face.select_set(face_selected)


def select_uv_islands(context, bm, uv_layer, islands):
    """Select only the requested UV islands using Blender's UV-only flags."""
    target_loops = {loop for island in islands for loop in island}
    target_faces = {loop.face for loop in target_loops}
    _clear_uv_selection_state(bm, uv_layer)

    for face in target_faces:
        face.select_set(True)
        _set_optional_bool(face, "uv_select", True)
    for loop in target_loops:
        loop.vert.select_set(True)
        loop.edge.select_set(True)
        luv = loop[uv_layer]
        _set_optional_bool(luv, "select", True)
        _set_optional_bool(luv, "select_edge", True)
        _set_loop_uv_flag(loop, "uv_select_vert", True)
        _set_loop_uv_flag(loop, "uv_select_edge", True)


def translate_island(island, uv_layer, delta):
    for loop in island:
        loop[uv_layer].uv += delta


def scale_island(island, uv_layer, center, scale):
    for loop in island:
        uv = loop[uv_layer].uv
        loop[uv_layer].uv = center + (uv - center) * scale


def rotate_island(island, uv_layer, center, angle):
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    for loop in island:
        uv = loop[uv_layer].uv
        offset = uv - center
        loop[uv_layer].uv = Vector(
            (
                center.x + offset.x * cos_a - offset.y * sin_a,
                center.y + offset.x * sin_a + offset.y * cos_a,
            )
        )


def get_loops_bounds(loops, uv_layer):
    if not loops:
        return (0.0, 0.0, 0.0, 0.0)
    us = [loop[uv_layer].uv.x for loop in loops]
    vs = [loop[uv_layer].uv.y for loop in loops]
    return (min(us), max(us), min(vs), max(vs))


def get_loops_center(loops, uv_layer):
    min_u, max_u, min_v, max_v = get_loops_bounds(loops, uv_layer)
    return Vector(((min_u + max_u) * 0.5, (min_v + max_v) * 0.5))


def keep_islands_inside_tile(islands, uv_layer):
    loops = [loop for island in islands for loop in island]
    min_u, max_u, min_v, max_v = get_loops_bounds(loops, uv_layer)
    delta = Vector((0.0, 0.0))
    if min_u < 0.0:
        delta.x = -min_u
    elif max_u > 1.0:
        delta.x = 1.0 - max_u
    if min_v < 0.0:
        delta.y = -min_v
    elif max_v > 1.0:
        delta.y = 1.0 - max_v
    if delta.length_squared > 0.0:
        for island in islands:
            translate_island(island, uv_layer, delta)


def _find_image_editor_context(context):
    area = getattr(context, "area", None)
    if area and area.type == "IMAGE_EDITOR":
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        return area, region, area.spaces.active
    screen = getattr(context, "screen", None)
    if screen:
        for area in screen.areas:
            if area.type == "IMAGE_EDITOR":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return area, region, area.spaces.active
    return None, None, None


def _uv_context_override(context):
    obj = get_active_mesh_object(context)
    area, region, space_data = _find_image_editor_context(context)
    override = {
        "active_object": obj,
        "object": obj,
        "edit_object": obj,
        "selected_objects": [obj],
        "selected_editable_objects": [obj],
    }
    if area and region:
        override.update({"area": area, "region": region, "space_data": space_data})
    return override


def run_uv_pack(context, margin, rotate=True, scale=True):
    override = _uv_context_override(context)
    with context.temp_override(**override):
        try:
            bpy.ops.uv.pack_islands(
                rotate_method="ANY" if rotate else "NONE",
                scale=scale,
                margin=margin,
            )
            return
        except TypeError:
            if not scale:
                raise
            try:
                bpy.ops.uv.pack_islands(rotate=rotate, margin=margin)
            except TypeError:
                bpy.ops.uv.pack_islands(margin=margin)


def run_uv_paste(context):
    override = _uv_context_override(context)
    with context.temp_override(**override):
        bpy.ops.uv.paste()


def run_uv_copy(context):
    override = _uv_context_override(context)
    with context.temp_override(**override):
        bpy.ops.uv.copy()


def _layout_rectangles(rectangles, scale, margin):
    x = margin
    y = margin
    row_height = 0.0
    placements = {}
    max_width = 1.0 - margin
    for item in rectangles:
        width = item["width"] * scale
        height = item["height"] * scale
        if x + width > max_width and x > margin:
            x = margin
            y += row_height + margin
            row_height = 0.0
        placements[item["index"]] = Vector((x, y))
        x += width + margin
        row_height = max(row_height, height)
    return placements, y + row_height + margin


def _rectangles_fit(rectangles, scale, margin):
    if not rectangles:
        return True
    for item in rectangles:
        if item["width"] * scale + 2.0 * margin > 1.0:
            return False
        if item["height"] * scale + 2.0 * margin > 1.0:
            return False
    _placements, height = _layout_rectangles(rectangles, scale, margin)
    return height <= 1.0


def basic_pack_islands(bm, uv_layer, islands, margin, scale_to_fit=True):
    rectangles = []
    for index, island in enumerate(islands):
        min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
        rectangles.append(
            {
                "index": index,
                "min": Vector((min_u, min_v)),
                "width": max(max_u - min_u, 1e-6),
                "height": max(max_v - min_v, 1e-6),
            }
        )
    rectangles.sort(key=lambda item: item["height"], reverse=True)

    if scale_to_fit:
        high = 1.0
        while _rectangles_fit(rectangles, high, margin) and high < 1024.0:
            high *= 2.0
        low = 0.0
        for _ in range(36):
            mid = (low + high) * 0.5
            if _rectangles_fit(rectangles, mid, margin):
                low = mid
            else:
                high = mid
        scale = low
        fits = True
    else:
        scale = 1.0
        fits = _rectangles_fit(rectangles, scale, margin)

    placements, _height = _layout_rectangles(rectangles, scale, margin)
    rect_by_index = {item["index"]: item for item in rectangles}
    for index, island in enumerate(islands):
        item = rect_by_index[index]
        target_min = placements[index]
        source_min = item["min"]
        for loop in island:
            uv = loop[uv_layer].uv
            loop[uv_layer].uv = target_min + (uv - source_min) * scale
    return fits


class UVGPT_OT_duplicate_to_bake_optimized(Operator):
    bl_idname = "uv_gpt.duplicate_to_bake_optimized"
    bl_label = "Duplicate to Bake_Optimized"
    bl_description = "Duplicate the active UV map into Bake_Optimized and make it active"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            duplicate_to_bake_optimized(context)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Duplicated active UV map to Bake_Optimized.")
        return {"FINISHED"}


classes = (UVGPT_OT_duplicate_to_bake_optimized,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
