"""R2 real-mesh Pack/Center scope validation on the locked cc.blend fixture.

The harness opens the exact fixture in a separate Blender process, derives a
small connected seam-spanning UV part from the exact target's real UV layout, and
operates only on disposable in-memory object/data copies.  It never saves the
source blend and uses a full-loop complement oracle for every operation.
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
import sys


sys.dont_write_bytecode = True

import bmesh
import bpy
from mathutils import Vector


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = Path(r"C:\Users\linhp\Downloads\cc.blend").resolve()
EXPECTED_FIXTURE_SHA = (
    "5CB51356284D731990D5F5CA481EDB64ACD4452B47802CAAE5EA5DB307C5D3B6"
)
TARGET_OBJECT_NAME = "body pussy -4-2 base chon A big tit done"
MAX_CANDIDATE_FACES = 4000


class HarnessError(RuntimeError):
    pass


def _assert(condition, message):
    if not condition:
        raise HarnessError(message)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _import_workspace_addon():
    for name in list(sys.modules):
        if name == "uv_gpt" or name.startswith("uv_gpt."):
            del sys.modules[name]
    importlib.invalidate_caches()
    project_text = str(PROJECT_ROOT)
    sys.path = [item for item in sys.path if str(item or ".") != project_text]
    sys.path.insert(0, project_text)
    import uv_gpt

    expected = (PROJECT_ROOT / "uv_gpt" / "__init__.py").resolve()
    loaded_from = Path(uv_gpt.__file__).resolve()
    _assert(
        loaded_from == expected,
        f"wrong uv_gpt import path: {loaded_from}; expected {expected}",
    )
    _assert(
        tuple(uv_gpt.bl_info.get("version", ())) == (1, 2, 6),
        f"unexpected add-on version: {uv_gpt.bl_info.get('version')}",
    )
    return uv_gpt


def _setup_bmesh(bm):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()


def _active_uv_layer(obj, bm):
    active_name = obj.data.uv_layers.active.name if obj.data.uv_layers.active else None
    _assert(active_name, f"target {TARGET_OBJECT_NAME} has no active UV map")
    layer = bm.loops.layers.uv.get(active_name)
    _assert(layer is not None, f"active UV map {active_name!r} is missing from BMesh")
    return layer


def _activate_edit_object(obj):
    current = bpy.context.object
    if current is not None and current.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for candidate in bpy.context.view_layer.objects:
        candidate.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")


def _activate_object_mode(obj):
    if bpy.context.object is obj and obj.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    elif obj.mode != "OBJECT":
        _activate_edit_object(obj)
        bpy.ops.object.mode_set(mode="OBJECT")


def _set_active_uv_map(obj, name=None):
    target_name = name or (obj.data.uv_layers.active.name if obj.data.uv_layers.active else None)
    _assert(target_name, f"target {obj.name} has no discoverable active UV map")
    for index, layer in enumerate(obj.data.uv_layers):
        if layer.name == target_name:
            obj.data.uv_layers.active_index = index
            return target_name
    raise HarnessError(f"missing discovered UV map {target_name!r} on {obj.name}")


def _duplicate_target(template, suffix):
    obj = template.copy()
    obj.data = template.data.copy()
    obj.name = f"R2Disposable_{suffix}"
    collection = template.users_collection[0] if template.users_collection else bpy.context.collection
    _assert(collection is not None, "could not find a collection for disposable object")
    collection.objects.link(obj)
    _activate_edit_object(obj)
    _set_active_uv_map(obj, template.data.uv_layers.active.name)
    bm = bmesh.from_edit_mesh(obj.data)
    _setup_bmesh(bm)
    return obj, bm, _active_uv_layer(obj, bm)


def _remove_disposable(obj):
    if obj is None:
        return
    try:
        if obj.name in bpy.data.objects:
            _activate_object_mode(obj)
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
    except Exception as exc:
        print(f"[R2][CLEANUP WARNING] {type(exc).__name__}: {exc}")


def _set_optional(owner, name, value):
    try:
        setattr(owner, name, bool(value))
    except (AttributeError, TypeError, RuntimeError):
        pass


def _set_loop_flag(loop, name, value):
    setter = getattr(loop, f"{name}_set", None)
    if setter is not None:
        try:
            setter(bool(value))
        except (AttributeError, TypeError, RuntimeError):
            pass


def _clear_selection(bm, uv_layer):
    _setup_bmesh(bm)
    history = getattr(bm, "select_history", None)
    if history is not None:
        history.clear()
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


def _select_region(bm, uv_layer, face_ids):
    """Select a real topology part through UV flags, then make it active."""
    _clear_selection(bm, uv_layer)
    history = getattr(bm, "select_history", None)
    faces = [bm.faces[index] for index in face_ids]
    for face in faces:
        face.select_set(True)
        _set_optional(face, "uv_select", True)
        if history is not None:
            history.add(face)
        for loop in face.loops:
            loop.vert.select_set(True)
            loop.edge.select_set(True)
            luv = loop[uv_layer]
            _set_optional(luv, "select", True)
            _set_optional(luv, "select_edge", True)
            _set_loop_flag(loop, "uv_select_vert", True)
            _set_loop_flag(loop, "uv_select_edge", True)
    _assert(faces, "selected real region is empty")
    bm.faces.active = faces[-1]
    bmesh.update_edit_mesh(
        bpy.context.edit_object.data,
        loop_triangles=False,
        destructive=False,
    )


def _add_mesh_selection_noise(bm, uv_layer, selected_ids):
    """Set mesh-only flags outside UV scope to guard against legacy leakage."""
    selected_ids = set(selected_ids)
    noise_face = next(face for face in bm.faces if face.index not in selected_ids)
    noise_face.select_set(False)
    _set_optional(noise_face, "uv_select", False)
    for loop in noise_face.loops:
        loop.vert.select_set(True)
        loop.edge.select_set(True)
        _set_optional(loop[uv_layer], "select", False)
        _set_optional(loop[uv_layer], "select_edge", False)
        _set_loop_flag(loop, "uv_select_vert", False)
        _set_loop_flag(loop, "uv_select_edge", False)


def _face_key(face_ids):
    return tuple(sorted(int(index) for index in face_ids))


def _island_key(island):
    return tuple(sorted({int(loop.face.index) for loop in island}))


def _island_bounds(island, uv_layer):
    points = [loop[uv_layer].uv for loop in island]
    return (
        min(point.x for point in points),
        max(point.x for point in points),
        min(point.y for point in points),
        max(point.y for point in points),
    )


def _build_real_candidates(bm, uv_layer, island_tools):
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    _assert(islands, "exact target active UV map has no UV islands")

    island_faces = {}
    island_by_key = {}
    for island in islands:
        key = _island_key(island)
        faces = tuple(sorted(island_tools.island_faces(island), key=lambda item: int(item.index)))
        island_faces[key] = faces
        island_by_key[key] = island
    face_to_island = {
        face: key
        for key, faces in island_faces.items()
        for face in faces
    }

    adjacency = {key: set() for key in island_faces}
    for edge in bm.edges:
        linked = sorted(
            {face_to_island[face] for face in edge.link_faces if face in face_to_island}
        )
        for left_index, left in enumerate(linked):
            for right in linked[left_index + 1 :]:
                adjacency[left].add(right)
                adjacency[right].add(left)

    candidates = []
    for left in sorted(adjacency):
        for right in sorted(adjacency[left]):
            if left >= right:
                continue
            face_ids = tuple(
                sorted(
                    {face.index for face in island_faces[left]}
                    | {face.index for face in island_faces[right]}
                )
            )
            if not face_ids or len(face_ids) > MAX_CANDIDATE_FACES:
                continue
            area = 0.0
            for key in (left, right):
                min_u, max_u, min_v, max_v = _island_bounds(island_by_key[key], uv_layer)
                area += max(max_u - min_u, 1e-8) * max(max_v - min_v, 1e-8)
            candidates.append(
                {
                    "face_ids": face_ids,
                    "island_keys": (left, right),
                    "area": area,
                    "multi_island": True,
                }
            )

    if candidates:
        candidates.sort(key=lambda item: (len(item["face_ids"]), item["face_ids"]))
        selected = candidates[0]
    else:
        print(
            "[R2][DIAGNOSTIC] no adjacent multi-island real region was available; "
            "using the smallest real UV island"
        )
        single = sorted(island_faces, key=lambda key: (len(island_faces[key]), key))[0]
        selected = {
            "face_ids": tuple(face.index for face in island_faces[single]),
            "island_keys": (single,),
            "area": 0.0,
            "multi_island": False,
        }

    return {
        "selected": selected,
        "face_to_island": face_to_island,
        "island_count": len(islands),
    }


def _loop_key(face, local_index):
    return int(face.index), int(local_index)


def _uv_snapshot(bm, uv_layer):
    _setup_bmesh(bm)
    return {
        _loop_key(face, local_index): (
            float(loop[uv_layer].uv.x),
            float(loop[uv_layer].uv.y),
        )
        for face in bm.faces
        for local_index, loop in enumerate(face.loops)
    }


def _all_uv_snapshot(obj, bm):
    """Snapshot every UV map so refresh cannot hide a map-local mutation."""
    _setup_bmesh(bm)
    snapshot = {}
    for uv_map in obj.data.uv_layers:
        layer = bm.loops.layers.uv.get(uv_map.name)
        _assert(layer is not None, f"missing BMesh layer for UV map {uv_map.name!r}")
        for face in bm.faces:
            for local_index, loop in enumerate(face.loops):
                uv = loop[layer].uv
                snapshot[(uv_map.name, face.index, local_index)] = (
                    float(uv.x),
                    float(uv.y),
                )
    return snapshot


def _optional_bool(owner, name):
    value = getattr(owner, name, None)
    return None if value is None else bool(value)


def _selection_state(obj, bm, uv_layer):
    _setup_bmesh(bm)
    loops = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            loops[_loop_key(face, local_index)] = (
                _optional_bool(luv, "select"),
                _optional_bool(luv, "select_edge"),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
                _optional_bool(face, "uv_select"),
                _optional_bool(loop, "uv_select_vert"),
                _optional_bool(loop, "uv_select_edge"),
            )
    history = getattr(bm, "select_history", None)
    history_entries = tuple(
        (type(element).__name__, int(element.index))
        for element in history
        if getattr(element, "index", None) is not None
    ) if history is not None else ()
    history_active = getattr(history, "active", None) if history is not None else None
    active_face = getattr(bm.faces, "active", None)
    active_layer = obj.data.uv_layers.active if obj.data.uv_layers else None
    return {
        "loops": loops,
        "faces": tuple((int(face.index), bool(face.select)) for face in bm.faces),
        "edges": tuple((int(edge.index), bool(edge.select)) for edge in bm.edges),
        "verts": tuple((int(vert.index), bool(vert.select)) for vert in bm.verts),
        "history": history_entries,
        "history_active": (
            type(history_active).__name__,
            int(history_active.index) if history_active is not None else None,
        ),
        "active_face": int(active_face.index) if active_face is not None else None,
        "active_uv_map": active_layer.name if active_layer is not None else None,
    }


def _selected_loop_keys(islands, island_tools):
    return {
        _loop_key(face, local_index)
        for island in islands
        for face in island_tools.island_faces(island)
        for local_index, _loop in enumerate(face.loops)
    }


def _selected_scope(bm, uv_layer, island_tools):
    islands = island_tools.get_selected_uv_islands_for_context(
        bpy.context,
        bm,
        uv_layer,
    )
    keys = {_island_key(island) for island in islands}
    return islands, keys


def _offset_selected(islands, uv_layer, delta):
    for island in islands:
        for loop in island:
            loop[uv_layer].uv += delta


def _settings():
    settings = bpy.context.scene.uv_gpt_settings
    bpy.context.scene.tool_settings.use_uv_select_sync = False
    settings.duplicate_before_operations = False
    try:
        settings.active_uv_map = "NONE"
    except (AttributeError, TypeError, ValueError):
        pass
    settings.margin = 0.003
    settings.rotation_mode = "NONE"
    settings.pack_preserve_stacks = False
    settings.pack_selected_lock_density = False
    return settings


def _set_settings_active_map(obj):
    settings = bpy.context.scene.uv_gpt_settings
    active_layer = obj.data.uv_layers.active if obj.data.uv_layers else None
    _assert(active_layer is not None, f"{obj.name} has no active UV map for settings")
    try:
        settings.active_uv_map = active_layer.name
    except (AttributeError, TypeError, ValueError) as exc:
        raise HarnessError(
            f"could not bind settings to discovered active UV map {active_layer.name!r}"
        ) from exc


def _invoke(call):
    try:
        return call(), ""
    except RuntimeError as exc:
        return {"CANCELLED"}, str(exc)


def _prepare_selected_case(template, candidate, suffix, island_tools):
    obj, bm, uv_layer = _duplicate_target(template, suffix)
    _set_settings_active_map(obj)
    _select_region(bm, uv_layer, candidate["face_ids"])
    _add_mesh_selection_noise(bm, uv_layer, candidate["face_ids"])
    selected_islands, selected_keys = _selected_scope(bm, uv_layer, island_tools)
    expected_keys = set(candidate["island_keys"])
    _assert(
        selected_keys == expected_keys,
        f"context selection mismatch: expected {expected_keys}, found {selected_keys}",
    )
    _offset_selected(selected_islands, uv_layer, Vector((0.37, -0.29)))
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return obj, bm, uv_layer, selected_islands, selected_keys


def _assert_target_only(before, after, selected_keys, label):
    all_keys = set(before)
    complement = all_keys - set(selected_keys)
    changed_selected = {
        key for key in selected_keys if after[key] != before[key]
    }
    changed_complement = {
        key for key in complement if after[key] != before[key]
    }
    if changed_complement:
        print(
            f"[R2][DIAGNOSTIC] {label}: complement_changed={len(changed_complement)} "
            f"first_keys={sorted(changed_complement)[:8]} selected_keys={len(selected_keys)} "
            f"all_keys={len(all_keys)}"
        )
    _assert(changed_selected, f"{label}: selected UVs did not change")
    _assert(
        all(after[key] == before[key] for key in complement),
        f"{label}: unselected complement changed",
    )
    return changed_selected, complement


def _run_pack_case(template, candidate, island_tools, mode):
    settings = _settings()
    settings.pack_selected_unselected_mode = mode
    obj = bm = uv_layer = None
    try:
        obj, bm, uv_layer, selected_islands, selected_keys = _prepare_selected_case(
            template,
            candidate,
            f"pack_{mode}",
            island_tools,
        )
        selected_loop_keys = _selected_loop_keys(selected_islands, island_tools)
        before = _uv_snapshot(bm, uv_layer)
        before_state = _selection_state(obj, bm, uv_layer)
        result, error_text = _invoke(lambda: bpy.ops.uv_gpt.pack_selected())
        _assert(result == {"FINISHED"}, f"Pack Selected {mode} result={result}; {error_text}")
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(obj, bm)
        after = _uv_snapshot(bm, uv_layer)
        after_state = _selection_state(obj, bm, uv_layer)
        changed, complement = _assert_target_only(
            before,
            after,
            selected_loop_keys,
            f"Pack Selected {mode}",
        )
        _assert(after_state == before_state, f"Pack Selected {mode}: state not restored")
        print(
            f"[R2][BLENDER PASS] Pack Selected {mode}: selected_loops={len(changed)} "
            f"complement_exact={len(complement)} state_restored"
        )
    finally:
        _remove_disposable(obj)


def _run_center_case(template, candidate, island_tools):
    _settings()
    obj = bm = uv_layer = None
    try:
        obj, bm, uv_layer, selected_islands, selected_keys = _prepare_selected_case(
            template,
            candidate,
            "center",
            island_tools,
        )
        selected_loop_keys = _selected_loop_keys(selected_islands, island_tools)
        before = _uv_snapshot(bm, uv_layer)
        before_state = _selection_state(obj, bm, uv_layer)
        result, error_text = _invoke(lambda: bpy.ops.uv_gpt.center_selected())
        _assert(result == {"FINISHED"}, f"Center Selected result={result}; {error_text}")
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(obj, bm)
        after = _uv_snapshot(bm, uv_layer)
        after_state = _selection_state(obj, bm, uv_layer)
        changed, complement = _assert_target_only(
            before,
            after,
            selected_loop_keys,
            "Center Selected",
        )
        selected_points = [
            loop[uv_layer].uv
            for island in selected_islands
            for loop in island
        ]
        min_u = min(point.x for point in selected_points)
        max_u = max(point.x for point in selected_points)
        min_v = min(point.y for point in selected_points)
        max_v = max(point.y for point in selected_points)
        _assert(abs((min_u + max_u) * 0.5 - 0.5) <= 1e-6, "Center U is not 0.5")
        _assert(abs((min_v + max_v) * 0.5 - 0.5) <= 1e-6, "Center V is not 0.5")
        _assert(after_state == before_state, "Center Selected: state not restored")
        print(
            f"[R2][BLENDER PASS] Center Selected: selected_loops={len(changed)} "
            f"complement_exact={len(complement)} state_restored"
        )
    finally:
        _remove_disposable(obj)


def _run_injected_rollback_case(template, candidate, island_tools, mutate_then_raise):
    import uv_gpt.pack_tools as pack_tools
    import uv_gpt.uv_utils as uv_utils

    _settings().pack_selected_unselected_mode = "IGNORE_UNSELECTED"
    obj = bm = uv_layer = None
    original_backend = uv_utils.basic_pack_islands
    try:
        obj, bm, uv_layer, selected_islands, selected_keys = _prepare_selected_case(
            template,
            candidate,
            "rollback_raise" if mutate_then_raise else "rollback_complement",
            island_tools,
        )
        before = _uv_snapshot(bm, uv_layer)
        before_state = _selection_state(obj, bm, uv_layer)
        selected_face_ids = set(candidate["face_ids"])

        def injected_backend(bm_arg, uv_layer_arg, islands_arg, margin, scale_to_fit=True):
            complement_face = next(
                face for face in bm_arg.faces if face.index not in selected_face_ids
            )
            complement_loop = next(iter(complement_face.loops))
            complement_loop[uv_layer_arg].uv.x += 0.123456
            if mutate_then_raise:
                raise RuntimeError("injected backend failure")
            return True

        uv_utils.basic_pack_islands = injected_backend
        result, error_text = _invoke(lambda: bpy.ops.uv_gpt.pack_selected())
        _assert(result == {"CANCELLED"}, f"injected rollback result={result}; {error_text}")
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(obj, bm)
        after = _uv_snapshot(bm, uv_layer)
        after_state = _selection_state(obj, bm, uv_layer)
        _assert(after == before, "injected Pack Selected failure did not restore all UVs")
        _assert(after_state == before_state, "injected Pack Selected failure did not restore state")
        label = "exception" if mutate_then_raise else "complement mutation"
        print(f"[R2][BLENDER PASS] atomic rollback ({label}): UVs/state exact")
    finally:
        uv_utils.basic_pack_islands = original_backend
        _remove_disposable(obj)


def _run_invalid_sync_case(template, candidate, island_tools):
    bpy.context.scene.tool_settings.use_uv_select_sync = True
    original_refresh = island_tools.refresh_uv_selection_scope
    try:
        for operation_name, operation in (
            ("Pack", lambda: bpy.ops.uv_gpt.pack_selected()),
            ("Center", lambda: bpy.ops.uv_gpt.center_selected()),
        ):
            settings = _settings()
            settings.pack_selected_unselected_mode = "IGNORE_UNSELECTED"
            bpy.context.scene.tool_settings.use_uv_select_sync = True
            obj = bm = uv_layer = None
            try:
                # Blender 5.0 exposes the exact open-file boundary when Sync is
                # enabled before entering Edit Mode on the disposable copy.
                obj, bm, uv_layer = _duplicate_target(
                    template,
                    f"invalid_sync_{operation_name.lower()}",
                )
                _set_settings_active_map(obj)
                _select_region(bm, uv_layer, candidate["face_ids"])
                sync_valid = getattr(bm, "uv_select_sync_valid", None)
                _assert(
                    sync_valid is not True,
                    f"{operation_name}: fixture did not expose invalid sync state: {sync_valid}",
                )
                before = _uv_snapshot(bm, uv_layer)
                before_all = _all_uv_snapshot(obj, bm)
                before_state = _selection_state(obj, bm, uv_layer)
                selected_face_ids = set(candidate["face_ids"])
                selected_loop_keys = {
                    _loop_key(face, local_index)
                    for face in bm.faces
                    if face.index in selected_face_ids
                    for local_index, _loop in enumerate(face.loops)
                }

                refresh_calls = 0
                refresh_after_all = None
                refresh_after_state = None

                def tracking_refresh(context, bm_arg, uv_layer_arg):
                    nonlocal refresh_calls, refresh_after_all, refresh_after_state
                    refresh_calls += 1
                    result = original_refresh(context, bm_arg, uv_layer_arg)
                    refresh_after_all = _all_uv_snapshot(obj, bm_arg)
                    refresh_after_state = _selection_state(obj, bm_arg, uv_layer_arg)
                    return result

                island_tools.refresh_uv_selection_scope = tracking_refresh
                result, error_text = _invoke(operation)
                _assert(
                    result == {"FINISHED"},
                    f"invalid-sync {operation_name} result={result}; {error_text}",
                )
                _assert(refresh_calls == 1, f"invalid-sync {operation_name} refresh count={refresh_calls}")
                _assert(
                    refresh_after_all == before_all,
                    f"invalid-sync {operation_name} changed UV coordinates during refresh",
                )
                _assert(
                    refresh_after_state == before_state,
                    f"invalid-sync {operation_name} changed selection/active/history during refresh",
                )
                _assert(
                    obj.mode == "EDIT" and bpy.context.object is obj,
                    f"invalid-sync {operation_name} changed object/mode during refresh",
                )
                _assert(
                    getattr(bm, "uv_select_sync_valid", None) is True,
                    f"invalid-sync {operation_name} did not establish valid sync",
                )

                bm = bmesh.from_edit_mesh(obj.data)
                uv_layer = _active_uv_layer(obj, bm)
                after = _uv_snapshot(bm, uv_layer)
                after_state = _selection_state(obj, bm, uv_layer)
                changed, complement = _assert_target_only(
                    before,
                    after,
                    selected_loop_keys,
                    f"invalid-sync {operation_name}",
                )
                _assert(after_state == before_state, f"invalid-sync {operation_name} state not restored")
                print(
                    f"[R2][BLENDER PASS] invalid UV Sync {operation_name}: "
                    f"refresh_once coords_exact={len(before_all)} "
                    f"selected_loops={len(changed)} complement_exact={len(complement)} "
                    f"state_restored"
                )
            finally:
                island_tools.refresh_uv_selection_scope = original_refresh
                _remove_disposable(obj)
    finally:
        island_tools.refresh_uv_selection_scope = original_refresh
        bpy.context.scene.tool_settings.use_uv_select_sync = False


def _run_whole_mesh_case(template, island_tools):
    _settings()
    obj = bm = uv_layer = None
    try:
        obj, bm, uv_layer = _duplicate_target(template, "whole_mesh")
        _set_settings_active_map(obj)
        all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        _assert(all_islands, "Pack Whole Mesh lost the exact target UV layout")
        for island in all_islands:
            for loop in island:
                loop[uv_layer].uv += Vector((0.217, -0.163))
        _clear_selection(bm, uv_layer)
        _select_region(bm, uv_layer, tuple(face.index for face in island_tools.island_faces(all_islands[0])))
        before = _uv_snapshot(bm, uv_layer)
        before_state = _selection_state(obj, bm, uv_layer)
        result, error_text = _invoke(lambda: bpy.ops.uv_gpt.pack_whole_mesh())
        _assert(result == {"FINISHED"}, f"Pack Whole Mesh result={result}; {error_text}")
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = _active_uv_layer(obj, bm)
        after = _uv_snapshot(bm, uv_layer)
        changed = {key for key in before if after[key] != before[key]}
        changed_faces = {key[0] for key in changed}
        _assert(
            len(changed_faces) > 1,
            "Pack Whole Mesh did not demonstrate an intentional broader write",
        )
        after_state = _selection_state(obj, bm, uv_layer)
        _assert(after_state == before_state, "Pack Whole Mesh state not restored")
        print(
            f"[R2][BLENDER PASS] Pack Whole Mesh intentional broader write: "
            f"changed_loops={len(changed)} changed_faces={len(changed_faces)} state_restored"
        )
    finally:
        _remove_disposable(obj)


def _registration_smoke(uv_gpt):
    uv_gpt.unregister()
    names_after_unregister = {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}
    _assert("pack_selected" not in names_after_unregister, "Pack operator remained registered")
    _assert("center_selected" not in names_after_unregister, "Center operator remained registered")
    uv_gpt.register()
    names_after_register = {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}
    _assert("pack_selected" in names_after_register, "Pack operator did not re-register")
    _assert("center_selected" in names_after_register, "Center operator did not re-register")
    print("[R2][BLENDER PASS] register/unregister/re-register")


def main():
    _assert(FIXTURE_PATH.exists(), f"fixture missing: {FIXTURE_PATH}")
    fixture_before = _sha256(FIXTURE_PATH)
    baseline_matches = fixture_before == EXPECTED_FIXTURE_SHA
    if not baseline_matches:
        print(
            "[R2][BLOCKER] fixture baseline SHA mismatch; expected={!r} observed={!r}. "
            "Continuing read-only behavioral gates without overwriting the fixture.".format(
                EXPECTED_FIXTURE_SHA,
                fixture_before,
            )
        )
    print(f"[R2][BLENDER] Blender={bpy.app.version_string} fixture_sha_before={fixture_before}")

    uv_gpt = _import_workspace_addon()
    from uv_gpt import island_tools

    template = bpy.data.objects.get(TARGET_OBJECT_NAME)
    _assert(template is not None and template.type == "MESH", f"missing target {TARGET_OBJECT_NAME}")
    _activate_object_mode(template)
    _set_active_uv_map(template)
    uv_gpt.register()
    try:
        _settings()
        bm = bmesh.new()
        bm.from_mesh(template.data)
        try:
            _setup_bmesh(bm)
            uv_layer = _active_uv_layer(template, bm)
            candidates = _build_real_candidates(bm, uv_layer, island_tools)
        finally:
            bm.free()

        candidate = candidates["selected"]
        print(
            f"[R2][TOPOLOGY] all_islands={candidates['island_count']} "
            f"selected_face_key={_face_key(candidate['face_ids'])} "
            f"selected_island_keys={candidate['island_keys']} "
            f"multi_island={candidate['multi_island']}"
        )

        _run_pack_case(template, candidate, island_tools, "LOCK_UNSELECTED")
        _run_pack_case(template, candidate, island_tools, "IGNORE_UNSELECTED")
        _run_center_case(template, candidate, island_tools)
        _run_injected_rollback_case(template, candidate, island_tools, mutate_then_raise=False)
        _run_injected_rollback_case(template, candidate, island_tools, mutate_then_raise=True)
        _run_invalid_sync_case(template, candidate, island_tools)
        _run_whole_mesh_case(template, island_tools)
        _registration_smoke(uv_gpt)
    finally:
        try:
            uv_gpt.unregister()
        except Exception:
            pass
        bpy.context.scene.tool_settings.use_uv_select_sync = False

    fixture_after = _sha256(FIXTURE_PATH)
    _assert(
        fixture_after == fixture_before,
        f"fixture changed during R2 run: before={fixture_before} after={fixture_after}",
    )
    if baseline_matches:
        print(f"[R2][PASS] real-mesh Pack/Center repair complete; fixture_sha_after={fixture_after}")
    else:
        print(
            "[R2][BEHAVIOR PASS] real-mesh Pack/Center gates complete; "
            f"fixture_sha_unchanged={fixture_after}"
        )
        print(
            "[R2][BLOCKED] expected fixture SHA was not present; "
            "behavior is evidenced on the exact current target only."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[R2][FAIL] {type(exc).__name__}: {exc}")
        raise
