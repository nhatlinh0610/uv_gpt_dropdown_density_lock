"""R1 real-mesh Symmetry validation on the locked cc.blend fixture.

The harness opens the exact fixture in a separate Blender process, copies the
target mesh/data into disposable in-memory objects, and never saves the source
blend.  The selected regions are derived from real mesh topology and UV seams;
no square or synthetic fixture is used.
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
TARGET_ACTIVE_UV_NAME = "UVMap.002"
EXPECTED_ACTIVE_UV_ISLANDS = 17
MAX_MULTI_ISLAND_REGION_FACES = 4000


def _arg_value(name, default):
    if "--" not in sys.argv:
        return default
    args = sys.argv[sys.argv.index("--") + 1 :]
    try:
        index = args.index(name)
    except ValueError:
        return default
    if index + 1 >= len(args):
        return default
    return args[index + 1]


_project_root_arg = _arg_value("--project-root", str(PROJECT_ROOT))
PROJECT_ROOT = Path(_project_root_arg).resolve()
_fixture_arg = _arg_value("--fixture", str(FIXTURE_PATH))
FIXTURE_PATH = Path(_fixture_arg).resolve()
_package_root_arg = _arg_value("--package-root", "")
PACKAGE_ROOT = Path(_package_root_arg).resolve() if _package_root_arg else None


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
    if PACKAGE_ROOT is not None:
        _assert(
            PACKAGE_ROOT.is_dir() and PACKAGE_ROOT.name == "uv_gpt",
            f"invalid extracted package root: {PACKAGE_ROOT}",
        )
        expected_names = {
            path.name for path in (PROJECT_ROOT / "uv_gpt").glob("*.py")
        }
        packaged_names = {path.name for path in PACKAGE_ROOT.glob("*.py")}
        _assert(
            packaged_names == expected_names,
            f"package source set mismatch: expected={sorted(expected_names)} "
            f"actual={sorted(packaged_names)}",
        )
        _assert(
            not any(PACKAGE_ROOT.rglob("*.pyc")),
            f"package contains bytecode cache: {PACKAGE_ROOT}",
        )
        excluded = {str(PROJECT_ROOT), str(PROJECT_ROOT / "tests")}
        sys.path = [
            item
            for item in sys.path
            if str(Path(item or ".").resolve()) not in excluded
        ]
        sys.path.insert(0, str(PACKAGE_ROOT.parent))
        importlib.invalidate_caches()
        import uv_gpt
        expected = (PACKAGE_ROOT / "__init__.py").resolve()
        loaded_from = Path(uv_gpt.__file__).resolve()
        _assert(
            loaded_from == expected,
            f"wrong packaged uv_gpt import path: {loaded_from}; expected {expected}",
        )
    else:
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
    _assert(
        active_name == TARGET_ACTIVE_UV_NAME,
        f"expected active UV map {TARGET_ACTIVE_UV_NAME!r}, found {active_name!r}",
    )
    layer = bm.loops.layers.uv.get(active_name)
    _assert(layer is not None, f"missing active UV layer {active_name!r}")
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


def _set_active_uv_map(obj):
    for index, layer in enumerate(obj.data.uv_layers):
        if layer.name == TARGET_ACTIVE_UV_NAME:
            obj.data.uv_layers.active_index = index
            return
    raise HarnessError(f"missing UV map {TARGET_ACTIVE_UV_NAME} on {obj.name}")


def _set_settings_active_map(obj):
    settings = bpy.context.scene.uv_gpt_settings
    active_layer = obj.data.uv_layers.active if obj.data.uv_layers else None
    _assert(active_layer is not None, f"{obj.name} has no active UV map")
    _assert(
        active_layer.name == TARGET_ACTIVE_UV_NAME,
        f"unexpected disposable active UV map: {active_layer.name!r}",
    )
    settings.active_uv_map = active_layer.name


def _duplicate_target(template, suffix):
    obj = template.copy()
    obj.data = template.data.copy()
    obj.name = f"R1Disposable_{suffix}"
    collection = template.users_collection[0] if template.users_collection else bpy.context.collection
    _assert(collection is not None, "could not find a collection for disposable object")
    collection.objects.link(obj)
    _activate_edit_object(obj)
    _set_active_uv_map(obj)
    bm = bmesh.from_edit_mesh(obj.data)
    _setup_bmesh(bm)
    return obj, bm, _active_uv_layer(obj, bm)


def _remove_disposable(obj):
    if obj is None:
        return
    try:
        if obj.name in bpy.data.objects:
            if obj.mode != "OBJECT":
                _activate_object_mode(obj)
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
    except Exception as exc:
        print(f"[R1][CLEANUP WARNING] {type(exc).__name__}: {exc}")


def _activate_object_mode(obj):
    if bpy.context.object is obj and obj.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    elif obj.mode != "OBJECT":
        _activate_edit_object(obj)
        bpy.ops.object.mode_set(mode="OBJECT")


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


def _select_regions(bm, uv_layer, regions):
    """Select complete real UV-island groups while preserving region topology."""
    _clear_selection(bm, uv_layer)
    history = getattr(bm, "select_history", None)
    for region in regions:
        for face in sorted(region, key=lambda item: int(item.index)):
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
    target_faces = sorted(regions[-1], key=lambda item: int(item.index))
    _assert(target_faces, "target region is empty")
    bm.faces.active = target_faces[-1]
    bmesh.update_edit_mesh(
        bpy.context.edit_object.data,
        loop_triangles=False,
        destructive=False,
    )


def _set_active_history_conflict(bm, anchor_region, target_region):
    """Keep target last in history while deliberately leaving active on anchor."""
    target_faces = sorted(target_region, key=lambda item: int(item.index))
    anchor_faces = sorted(anchor_region, key=lambda item: int(item.index))
    _assert(target_faces and anchor_faces, "active/history conflict regions are empty")
    history = getattr(bm, "select_history", None)
    history_active = getattr(history, "active", None) if history is not None else None
    _assert(
        getattr(history_active, "index", None) == target_faces[-1].index,
        "target region is not the active/last-selected history region",
    )
    bm.faces.active = anchor_faces[-1]
    bmesh.update_edit_mesh(
        bpy.context.edit_object.data,
        loop_triangles=False,
        destructive=False,
    )
    history_active = getattr(history, "active", None) if history is not None else None
    _assert(
        getattr(bm.faces.active, "index", None) == anchor_faces[-1].index,
        "fixture did not set stale active face in anchor region",
    )
    _assert(
        getattr(history_active, "index", None) == target_faces[-1].index,
        "setting stale active face changed target selection history",
    )


def _face_key(region):
    return tuple(sorted(int(face.index) for face in region))


def _island_key(island):
    return tuple(sorted(int(face.index) for face in {loop.face for loop in island}))


def _loop_key(face, local_index):
    return int(face.index), int(local_index)


def _region_loop_keys(region):
    return {
        _loop_key(face, local_index)
        for face in region
        for local_index, _loop in enumerate(face.loops)
    }


def _region_island_keys(region, face_to_island):
    return tuple(
        sorted({face_to_island[face] for face in region})
    )


def _build_real_candidates(bm, uv_layer):
    from uv_gpt import island_tools

    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    _assert(
        len(islands) == EXPECTED_ACTIVE_UV_ISLANDS,
        f"expected {EXPECTED_ACTIVE_UV_ISLANDS} UV islands, found {len(islands)}",
    )
    island_faces = {
        _island_key(island): tuple(
            sorted(island_tools.island_faces(island), key=lambda face: int(face.index))
        )
        for island in islands
    }
    face_to_island = {
        face: key
        for key, faces in island_faces.items()
        for face in faces
    }

    edge_pairs = set()
    island_adjacency = {key: set() for key in island_faces}
    for edge in bm.edges:
        linked = sorted(
            {face_to_island[face] for face in edge.link_faces if face in face_to_island}
        )
        for left_index, left in enumerate(linked):
            for right in linked[left_index + 1 :]:
                island_adjacency[left].add(right)
                island_adjacency[right].add(left)
        edge_faces = sorted(
            {int(face.index) for face in edge.link_faces if face in face_to_island}
        )
        for left_index, left in enumerate(edge_faces):
            for right in edge_faces[left_index + 1 :]:
                edge_pairs.add((left, right))

    candidates_by_face_key = {}
    for seed_key in sorted(island_faces):
        for neighbour_key in sorted(island_adjacency[seed_key]):
            face_ids = tuple(
                sorted(
                    {face.index for face in island_faces[seed_key]}
                    | {face.index for face in island_faces[neighbour_key]}
                )
            )
            if not face_ids or len(face_ids) > MAX_MULTI_ISLAND_REGION_FACES:
                continue
            candidates_by_face_key[face_ids] = {
                "face_ids": face_ids,
                "island_keys": tuple(sorted((seed_key, neighbour_key))),
                "multi_island": True,
            }

    candidates = [
        candidates_by_face_key[key]
        for key in sorted(candidates_by_face_key)
    ]

    _assert(
        candidates,
        "exact target topology did not provide a bounded seam-split region",
    )
    candidates.sort(key=lambda item: (len(item["face_ids"]), item["face_ids"]))

    def shares_mesh_edge(left, right):
        left_ids = set(left["face_ids"])
        right_ids = set(right["face_ids"])
        return any(
            (min(left_face, right_face), max(left_face, right_face)) in edge_pairs
            for left_face in left_ids
            for right_face in right_ids
        )

    pair = None
    for left_index, left in enumerate(candidates):
        left_ids = set(left["face_ids"])
        for right in candidates[left_index + 1 :]:
            if left_ids & set(right["face_ids"]):
                continue
            if shares_mesh_edge(left, right):
                continue
            pair = (left, right)
            break
        if pair is not None:
            break
    _assert(pair is not None, "could not derive two disjoint real topology regions")

    third = None
    first_ids = set(pair[0]["face_ids"]) | set(pair[1]["face_ids"])
    for candidate in candidates:
        candidate_ids = set(candidate["face_ids"])
        if first_ids & candidate_ids:
            continue
        if shares_mesh_edge(candidate, pair[0]) or shares_mesh_edge(candidate, pair[1]):
            continue
            third = candidate
            break

    if third is None:
        # The exact user object may have only two bounded adjacent seam-pairs,
        # while still exposing an independent UV island for the >2 ambiguity
        # gate.  Keep the accepted anchor/target multi-island pair intact and
        # use that deterministic single-island topology component as the third
        # selected region.
        single_candidates = [
            {
                "face_ids": tuple(face.index for face in island_faces[key]),
                "island_keys": (key,),
                "multi_island": False,
            }
            for key in sorted(island_faces)
            if len(island_faces[key]) <= MAX_MULTI_ISLAND_REGION_FACES
        ]
        for candidate in single_candidates:
            candidate_ids = set(candidate["face_ids"])
            if first_ids & candidate_ids:
                continue
            if shares_mesh_edge(candidate, pair[0]) or shares_mesh_edge(candidate, pair[1]):
                continue
            third = candidate
            break

    return {
        "pair": pair,
        "third": third,
        "face_to_island": face_to_island,
        "island_count": len(islands),
    }


def _map_region(bm, face_ids):
    _setup_bmesh(bm)
    return tuple(bm.faces[index] for index in face_ids)


def _snapshot_uv(bm, uv_layer):
    _setup_bmesh(bm)
    return {
        _loop_key(face, local_index): (
            float(loop[uv_layer].uv.x),
            float(loop[uv_layer].uv.y),
        )
        for face in bm.faces
        for local_index, loop in enumerate(face.loops)
    }


def _snapshot_selection(bm, uv_layer):
    _setup_bmesh(bm)
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            result[_loop_key(face, local_index)] = (
                bool(getattr(luv, "select", False)),
                bool(getattr(luv, "select_edge", False)),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
                getattr(face, "uv_select", None),
                getattr(loop, "uv_select_vert", None),
                getattr(loop, "uv_select_edge", None),
            )
    return result


def _snapshot_active(obj, bm):
    history = getattr(bm, "select_history", None)
    active_face = getattr(bm.faces, "active", None)
    history_active = getattr(history, "active", None) if history else None
    history_entries = tuple(
        (type(item).__name__, getattr(item, "index", None))
        for item in history
    ) if history is not None else ()
    active_uv = obj.data.uv_layers.active
    return {
        "active_face": getattr(active_face, "index", None),
        "history_entries": history_entries,
        "history_active": (
            type(history_active).__name__,
            getattr(history_active, "index", None),
        ),
        "active_uv": getattr(active_uv, "name", None),
    }


def _snapshot_state(obj, bm, uv_layer):
    return {
        "uv": _snapshot_uv(bm, uv_layer),
        "selection": _snapshot_selection(bm, uv_layer),
        "active": _snapshot_active(obj, bm),
    }


def _region_data(region, uv_layer):
    from uv_gpt import island_tools

    loops = [loop for face in region for loop in face.loops]
    min_u, max_u, min_v, max_v = island_tools.get_island_bounds(loops, uv_layer)
    return {
        "center": Vector(((min_u + max_u) * 0.5, (min_v + max_v) * 0.5)),
    }


def _mirror(point, axis):
    if axis == "V_HALF":
        return Vector((point.x, 1.0 - point.y))
    return Vector((1.0 - point.x, point.y))


def _expected_target_uv(before_uv, target_region, anchor_data, uv_layer, axis):
    target_data = _region_data(target_region, uv_layer)
    delta = _mirror(anchor_data["center"], axis) - target_data["center"]
    return {
        key: (
            float(before_uv[key][0] + delta.x),
            float(before_uv[key][1] + delta.y),
        )
        for key in _region_loop_keys(target_region)
    }


def _assert_target_only(before, after, target_keys, expected):
    for key, value in before["uv"].items():
        if key not in target_keys:
            _assert(
                after["uv"][key] == value,
                f"non-target loop changed: {key} {value} -> {after['uv'][key]}",
            )
    max_error = 0.0
    changed = False
    for key in target_keys:
        actual = after["uv"][key]
        expected_value = expected[key]
        max_error = max(
            max_error,
            abs(actual[0] - expected_value[0]),
            abs(actual[1] - expected_value[1]),
        )
        if actual != before["uv"][key]:
            changed = True
    _assert(changed, "Symmetry did not move any target loop")
    _assert(max_error <= 1.0e-6, f"target region transform error={max_error}")
    target_keys = tuple(target_keys)
    first_key = target_keys[0]
    first_delta = (
        after["uv"][first_key][0] - before["uv"][first_key][0],
        after["uv"][first_key][1] - before["uv"][first_key][1],
    )
    max_translation_error = 0.0
    for key in target_keys:
        delta = (
            after["uv"][key][0] - before["uv"][key][0],
            after["uv"][key][1] - before["uv"][key][1],
        )
        max_translation_error = max(
            max_translation_error,
            abs(delta[0] - first_delta[0]),
            abs(delta[1] - first_delta[1]),
        )
    first_before = before["uv"][first_key]
    first_after = after["uv"][first_key]
    max_pairwise_error = 0.0
    for key in target_keys:
        before_relative = (
            before["uv"][key][0] - first_before[0],
            before["uv"][key][1] - first_before[1],
        )
        after_relative = (
            after["uv"][key][0] - first_after[0],
            after["uv"][key][1] - first_after[1],
        )
        max_pairwise_error = max(
            max_pairwise_error,
            abs(after_relative[0] - before_relative[0]),
            abs(after_relative[1] - before_relative[1]),
        )
    _assert(
        max_translation_error <= 1.0e-6,
        f"target translation delta is not constant within float32 storage tolerance: {max_translation_error}",
    )
    _assert(
        max_pairwise_error <= 1.0e-6,
        f"target pairwise UV delta changed beyond float32 storage tolerance: {max_pairwise_error}",
    )
    print(
        "[R1][NUMERIC] target translation_variation=%r pairwise_error=%r"
        % (max_translation_error, max_pairwise_error)
    )
    _assert(
        after["selection"] == before["selection"],
        "Symmetry changed UV/mesh selection flags",
    )
    _assert(
        after["active"] == before["active"],
        "Symmetry changed active/history/UV-map state",
    )


def _invoke_symmetry_operator():
    try:
        return bpy.ops.uv_gpt.symmetry_auto_mirror(), ""
    except RuntimeError as exc:
        # Background Blender can surface an operator's reported CANCELLED
        # result as RuntimeError("Error: ...") even though execute() returned
        # the expected cancellation set.  Keep the report text for the oracle.
        return {"CANCELLED"}, str(exc)


def _map_candidate_regions(bm, candidate):
    return tuple(_map_region(bm, item["face_ids"]) for item in candidate)


def _print_candidate(label, candidate, face_to_island):
    print(
        "[R1][TOPOLOGY] %s faces=%d face_ids=%s island_keys=%s multi_island=%s"
        % (
            label,
            len(candidate["face_ids"]),
            candidate["face_ids"],
            candidate["island_keys"],
            candidate["multi_island"],
        )
    )


def _run_axis_case(uv_gpt, template, candidates, axis):
    from uv_gpt import island_tools, symmetry_pair

    obj = None
    try:
        obj, bm, uv_layer = _duplicate_target(template, f"axis_{axis}")
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        settings = bpy.context.scene.uv_gpt_settings
        settings.duplicate_before_operations = False
        _set_settings_active_map(obj)
        settings.symmetry_axis = axis
        settings.match_rotation = True
        settings.match_scale = True

        anchor_region, target_region = _map_candidate_regions(bm, candidates["pair"])
        _select_regions(bm, uv_layer, (anchor_region, target_region))
        _set_active_history_conflict(bm, anchor_region, target_region)
        selected_regions = island_tools.get_selected_uv_regions_for_context(
            bpy.context,
            bm,
            uv_layer,
        )
        _assert(
            {
                _face_key(region) for region in selected_regions
            }
            == {_face_key(anchor_region), _face_key(target_region)},
            f"{axis}: context region selection widened or lost faces",
        )
        before = _snapshot_state(obj, bm, uv_layer)
        anchor_data = symmetry_pair._region_data(anchor_region, uv_layer)
        expected = _expected_target_uv(
            before["uv"],
            target_region,
            anchor_data,
            uv_layer,
            axis,
        )
        result = bpy.ops.uv_gpt.symmetry_auto_mirror()
        _assert(result == {"FINISHED"}, f"{axis}: operator result={result}")
        bm = bmesh.from_edit_mesh(obj.data)
        _setup_bmesh(bm)
        uv_layer = _active_uv_layer(obj, bm)
        after = _snapshot_state(obj, bm, uv_layer)
        _assert_target_only(before, after, _region_loop_keys(target_region), expected)
        print(
            "[R1][BLENDER PASS] axis=%s target-only coherent multi-region transform; "
            "faces anchor=%d target=%d"
            % (axis, len(anchor_region), len(target_region))
        )
    finally:
        _remove_disposable(obj)


def _run_active_fallback_case(uv_gpt, template, candidates):
    """Use a valid active target only after removing selection history."""
    from uv_gpt import island_tools, symmetry_pair

    obj = None
    try:
        obj, bm, uv_layer = _duplicate_target(template, "active_fallback")
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        settings = bpy.context.scene.uv_gpt_settings
        settings.duplicate_before_operations = False
        _set_settings_active_map(obj)
        settings.symmetry_axis = "U_HALF"
        settings.match_rotation = True
        settings.match_scale = True

        anchor_region, target_region = _map_candidate_regions(bm, candidates["pair"])
        _select_regions(bm, uv_layer, (anchor_region, target_region))
        history = getattr(bm, "select_history", None)
        _assert(history is not None, "active fallback requires selection history")
        history.clear()
        target_faces = sorted(target_region, key=lambda item: int(item.index))
        _assert(target_faces, "active fallback target region is empty")
        bm.faces.active = target_faces[-1]
        bmesh.update_edit_mesh(
            bpy.context.edit_object.data,
            loop_triangles=False,
            destructive=False,
        )
        _assert(getattr(history, "active", None) is None, "history was not cleared")
        before = _snapshot_state(obj, bm, uv_layer)
        anchor_data = symmetry_pair._region_data(anchor_region, uv_layer)
        expected = _expected_target_uv(
            before["uv"],
            target_region,
            anchor_data,
            uv_layer,
            "U_HALF",
        )
        result = bpy.ops.uv_gpt.symmetry_auto_mirror()
        _assert(result == {"FINISHED"}, f"active fallback: operator result={result}")
        bm = bmesh.from_edit_mesh(obj.data)
        _setup_bmesh(bm)
        uv_layer = _active_uv_layer(obj, bm)
        after = _snapshot_state(obj, bm, uv_layer)
        _assert_target_only(before, after, _region_loop_keys(target_region), expected)
        print("[R1][BLENDER PASS] history absent + valid active target fallback")
    finally:
        _remove_disposable(obj)


def _run_invalid_target_case(uv_gpt, template, candidates):
    """Cancel zero-write when neither history nor active resolves a region."""
    third = candidates.get("third")
    _assert(third is not None, "invalid target case needs a third topology region")
    obj = None
    try:
        obj, bm, uv_layer = _duplicate_target(template, "invalid_target")
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        settings = bpy.context.scene.uv_gpt_settings
        settings.duplicate_before_operations = False
        _set_settings_active_map(obj)
        settings.symmetry_axis = "U_HALF"
        settings.match_rotation = True
        settings.match_scale = True

        pair_regions = _map_candidate_regions(bm, candidates["pair"])
        third_region = _map_region(bm, third["face_ids"])
        _select_regions(bm, uv_layer, pair_regions)
        history = getattr(bm, "select_history", None)
        _assert(history is not None, "invalid target case requires selection history")
        history.clear()
        third_faces = sorted(third_region, key=lambda item: int(item.index))
        _assert(third_faces, "invalid target third region is empty")
        bm.faces.active = third_faces[-1]
        bmesh.update_edit_mesh(
            bpy.context.edit_object.data,
            loop_triangles=False,
            destructive=False,
        )
        _assert(getattr(history, "active", None) is None, "history was not cleared")
        before = _snapshot_state(obj, bm, uv_layer)
        result, error_text = _invoke_symmetry_operator()
        _assert(result == {"CANCELLED"}, f"invalid target: operator result={result}")
        _assert(
            "target region must be resolved by the active/last-selected" in error_text,
            f"invalid target cancellation lacked actionable report: {error_text}",
        )
        bm = bmesh.from_edit_mesh(obj.data)
        _setup_bmesh(bm)
        after = _snapshot_state(obj, bm, _active_uv_layer(obj, bm))
        _assert(after == before, "invalid target cancellation wrote UV/selection state")
        print("[R1][BLENDER PASS] neither history nor active target cancelled zero-write")
    finally:
        _remove_disposable(obj)


def _run_ambiguity_case(uv_gpt, template, candidates):
    from uv_gpt import island_tools

    third = candidates.get("third")
    _assert(third is not None, "could not derive a third disjoint real topology region")
    obj = None
    try:
        obj, bm, uv_layer = _duplicate_target(template, "ambiguity")
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        settings = bpy.context.scene.uv_gpt_settings
        settings.duplicate_before_operations = False
        _set_settings_active_map(obj)
        settings.symmetry_axis = "U_HALF"
        settings.match_rotation = True
        settings.match_scale = True
        regions = _map_candidate_regions(
            bm,
            (candidates["pair"][0], candidates["pair"][1], third),
        )
        _select_regions(bm, uv_layer, regions)
        before = _snapshot_state(obj, bm, uv_layer)
        result, error_text = _invoke_symmetry_operator()
        _assert(result == {"CANCELLED"}, f"ambiguity: operator result={result}")
        _assert(
            "exactly two connected UV regions" in error_text,
            f"ambiguity cancellation lacked actionable report: {error_text}",
        )
        bm = bmesh.from_edit_mesh(obj.data)
        _setup_bmesh(bm)
        after = _snapshot_state(obj, bm, _active_uv_layer(obj, bm))
        _assert(after == before, "ambiguous >2 region selection wrote state")
        print("[R1][BLENDER PASS] >2 topology regions cancelled with zero write")
    finally:
        _remove_disposable(obj)


def _run_invalid_sync_case(uv_gpt, template, candidates):
    obj = None
    try:
        # Blender 5.0 computes uv_select_sync_valid while entering Edit Mode.
        # Enable Sync before creating the disposable edit object so the test
        # exercises the same invalid-state boundary observed on cc.blend.
        bpy.context.scene.tool_settings.use_uv_select_sync = True
        obj, bm, uv_layer = _duplicate_target(template, "invalid_sync")
        settings = bpy.context.scene.uv_gpt_settings
        settings.duplicate_before_operations = False
        _set_settings_active_map(obj)
        settings.symmetry_axis = "U_HALF"
        settings.match_rotation = True
        settings.match_scale = True
        bm = bmesh.from_edit_mesh(obj.data)
        _setup_bmesh(bm)
        sync_valid = getattr(bm, "uv_select_sync_valid", None)
        _assert(
            sync_valid is not True,
            f"Blender 5.0 fixture did not expose the expected invalid sync state: {sync_valid}",
        )
        before = _snapshot_state(obj, bm, uv_layer)
        result, error_text = _invoke_symmetry_operator()
        _assert(result == {"CANCELLED"}, f"invalid sync: operator result={result}")
        _assert(
            "UV Select Sync is not valid for Symmetry" in error_text,
            f"invalid sync cancellation lacked actionable report: {error_text}",
        )
        bm = bmesh.from_edit_mesh(obj.data)
        _setup_bmesh(bm)
        after = _snapshot_state(obj, bm, _active_uv_layer(obj, bm))
        _assert(after == before, "invalid UV Sync wrote UV/selection state")
        print("[R1][BLENDER PASS] invalid UV Sync cancelled with zero write")
    finally:
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        _remove_disposable(obj)


def _registration_smoke(uv_gpt):
    names = {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}
    _assert("symmetry_auto_mirror" in names, "Symmetry operator did not register")
    uv_gpt.unregister()
    names = {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}
    _assert("symmetry_auto_mirror" not in names, "Symmetry operator remained after unregister")
    uv_gpt.register()
    names = {name for name in dir(bpy.ops.uv_gpt) if not name.startswith("_")}
    _assert("symmetry_auto_mirror" in names, "Symmetry operator did not re-register")
    print("[R1][BLENDER PASS] register/unregister/re-register")


def main():
    _assert(FIXTURE_PATH.is_file(), f"fixture missing: {FIXTURE_PATH}")
    fixture_sha_before = _sha256(FIXTURE_PATH)
    _assert(
        fixture_sha_before == EXPECTED_FIXTURE_SHA,
        f"fixture SHA mismatch before run: {fixture_sha_before}",
    )
    print(f"[R1] Blender={bpy.app.version_string} fixture_sha_before={fixture_sha_before}")
    _assert(str(Path(bpy.data.filepath).resolve()) == str(FIXTURE_PATH), "wrong blend filepath")

    uv_gpt = _import_workspace_addon()
    uv_gpt.register()
    template = bpy.data.objects.get(TARGET_OBJECT_NAME)
    _assert(template is not None and template.type == "MESH", "target mesh missing")
    _assert(
        template.data.uv_layers.active is not None
        and template.data.uv_layers.active.name == TARGET_ACTIVE_UV_NAME,
        f"target active UV map is not {TARGET_ACTIVE_UV_NAME!r}",
    )
    print(
        "[R1][TARGET] object={!r} active_uv={!r} uv_maps={!r} polys={} loops={}".format(
            template.name,
            template.data.uv_layers.active.name,
            [layer.name for layer in template.data.uv_layers],
            len(template.data.polygons),
            len(template.data.loops),
        )
    )

    analysis_obj = None
    try:
        analysis_obj, analysis_bm, analysis_uv = _duplicate_target(template, "analysis")
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        candidates = _build_real_candidates(analysis_bm, analysis_uv)
        _print_candidate("anchor", candidates["pair"][0], candidates["face_to_island"])
        _print_candidate("target", candidates["pair"][1], candidates["face_to_island"])
        if candidates["third"] is not None:
            _print_candidate("third", candidates["third"], candidates["face_to_island"])
        else:
            raise HarnessError("real topology did not provide a third ambiguity region")
    finally:
        _remove_disposable(analysis_obj)

    try:
        _run_axis_case(uv_gpt, template, candidates, "U_HALF")
        _run_axis_case(uv_gpt, template, candidates, "V_HALF")
        _run_active_fallback_case(uv_gpt, template, candidates)
        _run_invalid_target_case(uv_gpt, template, candidates)
        _run_ambiguity_case(uv_gpt, template, candidates)
        _run_invalid_sync_case(uv_gpt, template, candidates)
        _registration_smoke(uv_gpt)
    finally:
        bpy.context.scene.tool_settings.use_uv_select_sync = False
        if getattr(uv_gpt, "_REGISTERED", False):
            uv_gpt.unregister()

    fixture_sha_after = _sha256(FIXTURE_PATH)
    _assert(
        fixture_sha_after == fixture_sha_before,
        f"fixture SHA changed in Blender process: {fixture_sha_before} -> {fixture_sha_after}",
    )
    print(f"[R1][PASS] real-mesh Symmetry repair complete; fixture_sha_after={fixture_sha_after}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[R1][FAIL] {type(exc).__name__}: {exc}")
        raise
