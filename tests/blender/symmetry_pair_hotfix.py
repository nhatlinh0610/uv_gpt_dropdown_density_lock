"""S1 focused Blender 5.0 background smoke for the two-island symmetry hotfix.

The fixture is created and tested entirely in memory.  No .blend file is saved.
"""

from __future__ import annotations

import math
import pathlib
import sys

import bmesh
import bpy
from mathutils import Vector


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


OLD_OPERATOR_NAMES = {
    "symmetry_set_reference",
    "symmetry_snap_selected",
    "symmetry_clear_reference",
}


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _assert_close(left, right, message, epsilon=1.0e-6):
    _assert(abs(float(left) - float(right)) <= epsilon, message)


def _operator_names():
    return {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}


def _active_uv_layer(bm, obj):
    active_name = obj.data.uv_layers.active.name if obj.data.uv_layers.active else None
    return bm.loops.layers.uv.get(active_name) if active_name else bm.loops.layers.uv.active


def _uv_state(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    uv_layer = _active_uv_layer(bm, obj)
    coordinates = {}
    selection = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            key = (face.index, local_index)
            coordinates[key] = (float(luv.uv.x), float(luv.uv.y))
            selection[key] = (
                bool(getattr(luv, "select", False)),
                bool(getattr(luv, "select_edge", False)),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
            )
    active_face = getattr(bm.faces, "active", None)
    history_active = getattr(getattr(bm, "select_history", None), "active", None)
    return {
        "coordinates": coordinates,
        "selection": selection,
        "active_face": getattr(active_face, "index", None),
        "history_active": (
            type(history_active).__name__,
            getattr(history_active, "index", None),
        ),
    }


def _island_key(island):
    return tuple(sorted({loop.face.index for loop in island}))


def _island_center(island, uv_layer):
    coordinates = [loop[uv_layer].uv for loop in island]
    min_u = min(value.x for value in coordinates)
    max_u = max(value.x for value in coordinates)
    min_v = min(value.y for value in coordinates)
    max_v = max(value.y for value in coordinates)
    return Vector(((min_u + max_u) * 0.5, (min_v + max_v) * 0.5))


def _set_uv_selection(luv, value):
    for attribute in ("select", "select_edge"):
        try:
            setattr(luv, attribute, value)
        except (AttributeError, TypeError):
            pass


def _set_pair_selection(bm, uv_layer, active_face_index=1):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.select_history.clear()
    for face_index, face in enumerate(bm.faces):
        selected = face_index < 2
        face.select_set(selected)
        try:
            face.uv_select = selected
        except (AttributeError, TypeError):
            pass
        for loop in face.loops:
            loop.vert.select_set(selected)
            loop.edge.select_set(selected)
            luv = loop[uv_layer]
            _set_uv_selection(luv, selected)
        if selected:
            bm.select_history.add(face)
    bm.faces.active = bm.faces[active_face_index]


def _create_fixture(axis):
    mesh = bpy.data.meshes.new(f"S1SymmetryMesh_{axis}")
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
        ((0, 1, 2, 3), (4, 5, 6, 7), (8, 9, 10, 11)),
    )
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(f"S1SymmetryObject_{axis}", mesh)
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
    anchor_uv = ((0.20, 0.20), (0.30, 0.20), (0.30, 0.40), (0.20, 0.40))
    if axis == "U":
        target_uv = ((0.65, 0.05), (0.75, 0.05), (0.75, 0.15), (0.65, 0.15))
    else:
        target_uv = ((0.65, 0.65), (0.75, 0.65), (0.75, 0.75), (0.65, 0.75))
    unselected_uv = ((0.05, 0.65), (0.15, 0.65), (0.15, 0.75), (0.05, 0.75))
    for face, values in zip(bm.faces, (anchor_uv, target_uv, unselected_uv)):
        for loop, value in zip(face.loops, values):
            loop[uv_layer].uv = Vector(value)

    _set_pair_selection(bm, uv_layer, active_face_index=0)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return obj


def _run_axis_case(uv_gpt, island_tools, axis):
    settings = bpy.context.scene.uv_gpt_settings
    settings.symmetry_axis = "U_HALF" if axis == "U" else "V_HALF"
    settings.match_rotation = False
    settings.match_scale = False
    settings.duplicate_before_operations = False

    obj = _create_fixture(axis)
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(bm, obj)
        selected_before = island_tools.get_selected_uv_islands(bm, uv_layer)
        _assert(
            {_island_key(island) for island in selected_before} == {(0,), (1,)},
            f"{axis}: expected exactly two selected islands",
        )
        history_active = getattr(getattr(bm, "select_history", None), "active", None)
        _assert(
            getattr(history_active, "index", None) == 1,
            f"{axis}: target was not last-selected in history",
        )
        regions_before = island_tools.get_selected_uv_regions_for_context(
            bpy.context,
            bm,
            uv_layer,
        )
        _assert(
            {tuple(sorted(face.index for face in region)) for region in regions_before}
            == {(0,), (1,)},
            f"{axis}: context region selection lost before operator",
        )
        baseline = _uv_state(obj)
        anchor_before = _island_center(selected_before[0], uv_layer)
        undo_push_result = bpy.ops.ed.undo_push(message=f"S1 symmetry {axis} baseline")
        _assert(
            undo_push_result == {"FINISHED"},
            f"{axis}: could not initialize the background undo system: {undo_push_result}",
        )

        result = bpy.ops.uv_gpt.symmetry_auto_mirror()
        _assert(result == {"FINISHED"}, f"{axis}: operator result was {result}")

        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(bm, obj)
        selected_after = island_tools.get_selected_uv_islands(bm, uv_layer)
        after = _uv_state(obj)
        anchor_after = next(island for island in selected_after if _island_key(island) == (0,))
        target_after = next(island for island in selected_after if _island_key(island) == (1,))

        anchor_keys = {(0, index) for index in range(4)}
        target_keys = {(1, index) for index in range(4)}
        changed_keys = {
            key for key, value in after["coordinates"].items() if value != baseline["coordinates"][key]
        }
        _assert(
            all(after["coordinates"][key] == baseline["coordinates"][key] for key in anchor_keys),
            f"{axis}: anchor coordinates changed",
        )
        _assert(changed_keys and changed_keys <= target_keys, f"{axis}: non-target UVs changed")
        _assert(after["selection"] == baseline["selection"], f"{axis}: selection flags changed")
        _assert(after["active_face"] == baseline["active_face"] == 0, f"{axis}: active face changed")
        _assert(
            after["history_active"] == baseline["history_active"],
            f"{axis}: active selection-history element changed",
        )

        expected = Vector((1.0 - anchor_before.x, anchor_before.y))
        if axis == "V":
            expected = Vector((anchor_before.x, 1.0 - anchor_before.y))
        actual = _island_center(target_after, uv_layer)
        _assert_close(actual.x, expected.x, f"{axis}: target U center is not mirrored")
        _assert_close(actual.y, expected.y, f"{axis}: target V center is not mirrored")
        _assert(
            _island_center(anchor_after, uv_layer) == anchor_before,
            f"{axis}: anchor center changed",
        )

        # Background Blender requires an explicit checkpoint for direct BMesh
        # writes before ed.undo() can restore the preceding in-memory state.
        post_undo_push_result = bpy.ops.ed.undo_push(message=f"S1 symmetry {axis} result")
        _assert(
            post_undo_push_result == {"FINISHED"},
            f"{axis}: could not checkpoint the post-operation state: {post_undo_push_result}",
        )
        undo_result = bpy.ops.ed.undo()
        _assert(undo_result == {"FINISHED"}, f"{axis}: undo result was {undo_result}")
        undone = _uv_state(obj)
        if undone != baseline:
            print(f"[S1][DIAGNOSTIC] {axis}: baseline={baseline}")
            print(f"[S1][DIAGNOSTIC] {axis}: undone={undone}")
            raise AssertionError(f"{axis}: undo did not restore UV/selection/active state")
        print(f"[S1][BLENDER PASS] axis={axis} target-only mirror, state preservation, undo")
    finally:
        if obj.name in bpy.data.objects:
            if obj.mode == "EDIT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(obj, do_unlink=True)


def _run_invalid_active_case():
    settings = bpy.context.scene.uv_gpt_settings
    settings.symmetry_axis = "U_HALF"
    settings.match_rotation = False
    settings.match_scale = False
    settings.duplicate_before_operations = False

    obj = _create_fixture("INVALID")
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        bm.select_history.clear()
        bm.faces.active = bm.faces[2]
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        baseline = _uv_state(obj)
        try:
            result = bpy.ops.uv_gpt.symmetry_auto_mirror()
        except RuntimeError as exc:
            _assert(
                "target region must be resolved by the active/last-selected" in str(exc),
                f"invalid active: unexpected error message: {exc}",
            )
            result = {"CANCELLED"}
        _assert(result == {"CANCELLED"}, f"invalid active: operator result was {result}")
        after = _uv_state(obj)
        _assert(
            after == baseline,
            "invalid active: cancelled operator wrote UV/selection/active state",
        )
        print("[S1][BLENDER PASS] invalid active target cancelled without writes")
    finally:
        if obj.name in bpy.data.objects:
            if obj.mode == "EDIT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.data.objects.remove(obj, do_unlink=True)


def main():
    import uv_gpt
    from uv_gpt import island_tools

    tool_settings = bpy.context.scene.tool_settings
    if hasattr(tool_settings, "use_uv_select_sync"):
        tool_settings.use_uv_select_sync = False

    uv_gpt.register()
    try:
        names = _operator_names()
        _assert("symmetry_auto_mirror" in names, "symmetry_auto_mirror is not registered")
        _assert(not (OLD_OPERATOR_NAMES & names), "obsolete symmetry operator is registered")
        _run_axis_case(uv_gpt, island_tools, "U")
        _run_axis_case(uv_gpt, island_tools, "V")
        _run_invalid_active_case()

        uv_gpt.unregister()
        names_after_unregister = _operator_names()
        _assert("symmetry_auto_mirror" not in names_after_unregister, "unregister left symmetry operator")
        uv_gpt.register()
        _assert("symmetry_auto_mirror" in _operator_names(), "re-register lost symmetry operator")
        _assert(not (OLD_OPERATOR_NAMES & _operator_names()), "re-register restored obsolete operator")
        print("[S1][BLENDER PASS] registration, unregistration, and re-registration")
    finally:
        if getattr(uv_gpt, "_REGISTERED", False):
            uv_gpt.unregister()

    print("[S1][PASS] symmetry_pair_hotfix completed without saving a blend file")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[S1][FAIL] {type(exc).__name__}: {exc}")
        raise
