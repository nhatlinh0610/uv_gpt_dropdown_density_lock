"""S2 Blender 5.0 background smoke for Pack/Center UV scope.

The seam fixture is created in memory.  It is never saved to a .blend file.
"""

from __future__ import annotations

import pathlib
import sys

import bmesh
import bpy
from mathutils import Vector


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _active_uv_layer(bm, obj):
    active_name = obj.data.uv_layers.active.name if obj.data.uv_layers.active else None
    return bm.loops.layers.uv.get(active_name) if active_name else bm.loops.layers.uv.active


def _set_optional(owner, attribute, value):
    try:
        setattr(owner, attribute, bool(value))
    except (AttributeError, TypeError, RuntimeError):
        pass


def _set_loop_flag(loop, attribute, value):
    setter = getattr(loop, f"{attribute}_set", None)
    if setter is not None:
        setter(bool(value))


def _clear_selection(bm, uv_layer):
    bm.select_history.clear()
    for face in bm.faces:
        face.select_set(False)
        _set_optional(face, "uv_select", False)
        for loop in face.loops:
            loop.vert.select_set(False)
            loop.edge.select_set(False)
            luv = loop[uv_layer]
            _set_optional(luv, "select", False)
            _set_optional(luv, "select_edge", False)
            _set_loop_flag(loop, "uv_select_vert", False)
            _set_loop_flag(loop, "uv_select_edge", False)


def _select_face_uv(bm, uv_layer, face_index):
    _clear_selection(bm, uv_layer)
    face = bm.faces[face_index]
    face.select_set(True)
    _set_optional(face, "uv_select", True)
    for loop in face.loops:
        loop.vert.select_set(True)
        loop.edge.select_set(True)
        luv = loop[uv_layer]
        _set_optional(luv, "select", True)
        _set_optional(luv, "select_edge", True)
        _set_loop_flag(loop, "uv_select_vert", True)
        _set_loop_flag(loop, "uv_select_edge", True)
    bm.select_history.add(face)
    bm.faces.active = face


def _coordinates(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    uv_layer = _active_uv_layer(bm, obj)
    return {
        (face.index, local_index): (
            float(loop[uv_layer].uv.x),
            float(loop[uv_layer].uv.y),
        )
        for face in bm.faces
        for local_index, loop in enumerate(face.loops)
    }


def _selection_state(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    uv_layer = _active_uv_layer(bm, obj)
    loops = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            loops[(face.index, local_index)] = (
                getattr(luv, "select", None),
                getattr(luv, "select_edge", None),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
                getattr(face, "uv_select", None),
                getattr(loop, "uv_select_vert", None),
                getattr(loop, "uv_select_edge", None),
            )
    history = getattr(bm, "select_history", None)
    history_entries = tuple(
        (type(element).__name__, getattr(element, "index", None))
        for element in history
    ) if history is not None else ()
    history_active = getattr(history, "active", None) if history is not None else None
    active_face = getattr(bm.faces, "active", None)
    return {
        "loops": loops,
        "history": history_entries,
        "history_active": (
            type(history_active).__name__,
            getattr(history_active, "index", None),
        ),
        "active_face": getattr(active_face, "index", None),
    }


def _island_key(island):
    return tuple(sorted({loop.face.index for loop in island}))


def _create_fixture(name, overlapping=False):
    mesh = bpy.data.meshes.new(f"S2PackMesh_{name}")
    mesh.from_pydata(
        (
            (-1.0, -1.0, 0.0),
            (1.0, -1.0, 0.0),
            (1.0, 1.0, 0.0),
            (-1.0, 1.0, 0.0),
            (3.0, -1.0, 0.0),
            (5.0, -1.0, 0.0),
            (5.0, 1.0, 0.0),
            (3.0, 1.0, 0.0),
            (7.0, -1.0, 0.0),
            (9.0, -1.0, 0.0),
            (9.0, 1.0, 0.0),
            (7.0, 1.0, 0.0),
        ),
        (),
        ((0, 1, 2, 3), (2, 1, 4, 5), (6, 7, 8, 9)),
    )
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(f"S2PackObject_{name}", mesh)
    bpy.context.collection.objects.link(obj)
    for selected_object in list(bpy.context.selected_objects):
        selected_object.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    uv_layer = _active_uv_layer(bm, obj)
    if overlapping:
        values = (
            ((0.40, 0.10), (0.55, 0.10), (0.55, 0.25), (0.40, 0.25)),
            ((0.40, 0.10), (0.55, 0.10), (0.55, 0.25), (0.40, 0.25)),
            ((0.40, 0.10), (0.55, 0.10), (0.55, 0.25), (0.40, 0.25)),
        )
    else:
        values = (
            ((0.38, 0.08), (0.53, 0.08), (0.53, 0.23), (0.38, 0.23)),
            ((0.68, 0.08), (0.83, 0.08), (0.83, 0.23), (0.68, 0.23)),
            ((0.05, 0.68), (0.20, 0.68), (0.20, 0.83), (0.05, 0.83)),
        )
    for face, face_values in zip(bm.faces, values):
        for loop, value in zip(face.loops, face_values):
            loop[uv_layer].uv = Vector(value)

    _select_face_uv(bm, uv_layer, 0)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return obj


def _settings():
    settings = bpy.context.scene.uv_gpt_settings
    bpy.context.scene.tool_settings.use_uv_select_sync = False
    settings.duplicate_before_operations = False
    settings.margin = 0.01
    settings.rotation_mode = "NONE"
    settings.pack_preserve_stacks = False
    settings.pack_selected_lock_density = False
    return settings


def _assert_three_separate_islands(uv_gpt, island_tools, obj):
    bm = bmesh.from_edit_mesh(obj.data)
    uv_layer = _active_uv_layer(bm, obj)
    all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    _assert(
        {_island_key(island) for island in all_islands} == {(0,), (1,), (2,)},
        "seam fixture did not produce three UV islands",
    )
    selected = island_tools.get_selected_uv_islands_for_context(
        bpy.context,
        bm,
        uv_layer,
    )
    _assert(
        {_island_key(island) for island in selected} == {(0,)},
        "UV-only predicate widened one selected island to the seam neighbor",
    )


def _run_selected_pack_case(uv_gpt, island_tools, mode):
    settings = _settings()
    settings.pack_selected_unselected_mode = mode
    obj = _create_fixture(f"selected_{mode}")
    try:
        _assert_three_separate_islands(uv_gpt, island_tools, obj)
        before_coordinates = _coordinates(obj)
        before_selection = _selection_state(obj)
        result = bpy.ops.uv_gpt.pack_selected()
        _assert(result == {"FINISHED"}, f"Pack Selected {mode} result: {result}")
        after_coordinates = _coordinates(obj)
        after_selection = _selection_state(obj)
        target_keys = {(0, index) for index in range(4)}
        unselected_keys = set(before_coordinates) - target_keys
        _assert(
            any(after_coordinates[key] != before_coordinates[key] for key in target_keys),
            f"Pack Selected {mode} did not move its selected target",
        )
        _assert(
            all(after_coordinates[key] == before_coordinates[key] for key in unselected_keys),
            f"Pack Selected {mode} changed an unselected UV loop",
        )
        if after_selection != before_selection:
            print(f"[S2][DIAGNOSTIC] {mode} selection before={before_selection}")
            print(f"[S2][DIAGNOSTIC] {mode} selection after={after_selection}")
            raise AssertionError(
                f"Pack Selected {mode} did not restore UV/mesh selection and active history"
            )
        print(f"[S2][BLENDER PASS] Pack Selected {mode}: target-only, unselected exact, selection restored")
    finally:
        if obj.name in bpy.data.objects:
            if obj.mode == "EDIT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(obj, do_unlink=True)


def _run_center_case(uv_gpt, island_tools):
    _settings()
    obj = _create_fixture("center")
    try:
        _assert_three_separate_islands(uv_gpt, island_tools, obj)
        before_coordinates = _coordinates(obj)
        before_selection = _selection_state(obj)
        result = bpy.ops.uv_gpt.center_selected()
        _assert(result == {"FINISHED"}, f"Center Selected result: {result}")
        after_coordinates = _coordinates(obj)
        after_selection = _selection_state(obj)
        target_keys = {(0, index) for index in range(4)}
        unselected_keys = set(before_coordinates) - target_keys
        _assert(
            any(after_coordinates[key] != before_coordinates[key] for key in target_keys),
            "Center Selected did not move its selected target",
        )
        _assert(
            all(after_coordinates[key] == before_coordinates[key] for key in unselected_keys),
            "Center Selected changed an unselected UV loop",
        )
        _assert(
            after_selection == before_selection,
            "Center Selected changed UV/mesh selection or active history",
        )
        print("[S2][BLENDER PASS] Center Selected: target-only, unselected exact, selection preserved")
    finally:
        if obj.name in bpy.data.objects:
            if obj.mode == "EDIT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(obj, do_unlink=True)


def _run_whole_mesh_case(uv_gpt, island_tools):
    settings = _settings()
    settings.pack_selected_unselected_mode = "LOCK_UNSELECTED"
    obj = _create_fixture("whole", overlapping=True)
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(bm, obj)
        before_coordinates = _coordinates(obj)
        before_selection = _selection_state(obj)
        all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        _assert(len(all_islands) == 3, "whole-mesh fixture island count is not three")
        result = bpy.ops.uv_gpt.pack_whole_mesh()
        _assert(result == {"FINISHED"}, f"Pack Whole Mesh result: {result}")
        after_coordinates = _coordinates(obj)
        after_selection = _selection_state(obj)
        changed_faces = {
            face_index
            for (face_index, local_index), value in after_coordinates.items()
            if value != before_coordinates[(face_index, local_index)]
        }
        _assert(changed_faces == {0, 1, 2}, "Pack Whole Mesh did not operate on all UV islands")
        _assert(
            after_selection == before_selection,
            "Pack Whole Mesh did not restore the original selection/history",
        )
        print("[S2][BLENDER PASS] Pack Whole Mesh: all three islands intentionally in scope")
    finally:
        if obj.name in bpy.data.objects:
            if obj.mode == "EDIT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(obj, do_unlink=True)


def _run_sync_case(uv_gpt, island_tools):
    _settings()
    obj = _create_fixture("sync")
    try:
        _assert_three_separate_islands(uv_gpt, island_tools, obj)
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(bm, obj)
        sync_valid = getattr(bm, "uv_select_sync_valid", None)
        before_coordinates = _coordinates(obj)
        before_selection = _selection_state(obj)
        bpy.context.scene.tool_settings.use_uv_select_sync = True
        sync_error = ""
        try:
            result = bpy.ops.uv_gpt.pack_selected()
        except RuntimeError as exc:
            result = {"CANCELLED"}
            sync_error = str(exc)
        after_coordinates = _coordinates(obj)
        after_selection = _selection_state(obj)
        if sync_valid is True:
            _assert(result == {"FINISHED"}, f"valid UV Select Sync result: {result}")
            target_keys = {(0, index) for index in range(4)}
            _assert(
                all(
                    after_coordinates[key] == before_coordinates[key]
                    for key in set(before_coordinates) - target_keys
                ),
                "valid UV Select Sync widened selected-only Pack scope",
            )
            print("[S2][BLENDER PASS] UV Select Sync valid path: UV scope supported")
        else:
            _assert(result == {"CANCELLED"}, f"invalid UV Select Sync result: {result}")
            _assert("UV Select Sync" in sync_error, "invalid sync cancellation lacked clear message")
            _assert(after_coordinates == before_coordinates, "invalid sync path wrote UVs")
            _assert(after_selection == before_selection, "invalid sync path changed selection")
            center_error = ""
            try:
                center_result = bpy.ops.uv_gpt.center_selected()
            except RuntimeError as exc:
                center_result = {"CANCELLED"}
                center_error = str(exc)
            _assert(center_result == {"CANCELLED"}, "invalid sync Center Selected did not cancel")
            _assert("UV Select Sync" in center_error, "Center invalid sync lacked clear message")
            _assert(_coordinates(obj) == before_coordinates, "invalid sync Center wrote UVs")
            _assert(_selection_state(obj) == before_selection, "invalid sync Center changed selection")
            print("[S2][BLENDER PASS] UV Select Sync invalid path: Pack/Center safe cancellation, no writes")
    finally:
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        if obj.name in bpy.data.objects:
            if obj.mode == "EDIT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(obj, do_unlink=True)


def _run_registration_smoke(uv_gpt):
    uv_gpt.unregister()
    names_after_unregister = {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}
    _assert("pack_selected" not in names_after_unregister, "Pack operator remained registered")
    _assert("center_selected" not in names_after_unregister, "Center operator remained registered")
    uv_gpt.register()
    names_after_register = {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}
    _assert("pack_selected" in names_after_register, "Pack operator did not re-register")
    _assert("center_selected" in names_after_register, "Center operator did not re-register")
    print("[S2][BLENDER PASS] register/unregister clean")


def main():
    import uv_gpt
    from uv_gpt import island_tools

    uv_gpt.register()
    _run_selected_pack_case(uv_gpt, island_tools, "LOCK_UNSELECTED")
    _run_selected_pack_case(uv_gpt, island_tools, "IGNORE_UNSELECTED")
    _run_center_case(uv_gpt, island_tools)
    _run_whole_mesh_case(uv_gpt, island_tools)
    _run_sync_case(uv_gpt, island_tools)
    _run_registration_smoke(uv_gpt)
    uv_gpt.unregister()
    print("[S2][BLENDER PASS] pack_selected_center_hotfix complete; no .blend saved")


if __name__ == "__main__":
    main()
