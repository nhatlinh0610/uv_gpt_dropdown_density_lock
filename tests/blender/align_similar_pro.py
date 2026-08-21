"""PRO-02B Blender 5.0 read-only runtime harness.

The harness opens the locked cc.blend fixture in memory, selects only the two
known islands, runs Align Similar Pro once as warmup and three measured times,
and restores the in-memory baseline between runs.  It never saves the blend.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
import time


sys.dont_write_bytecode = True

import bmesh
import bpy
from mathutils import Vector


PACKET_ID = "PRO-02B"
DEFAULT_FIXTURE = Path(r"C:\Users\linhp\Downloads\cc.blend")
EXPECTED_FIXTURE_SHA = (
    "49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 577
MASTER_CANDIDATE_A = (602, 603, 604, 605)
MASTER_CANDIDATE_B = (9448, 9484, 9967, 17967)
WARMUP_RUNS = 1
MEASURED_RUNS = 3
SELECTION_EPSILON = 1.0e-12
COOPERATIVE_YIELD_EVERY = None


class HarnessError(RuntimeError):
    pass


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


SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = Path(
    _arg_value("--project-root", str(SCRIPT_PATH.parents[2]))
).resolve()
PACKAGE_ROOT_VALUE = _arg_value("--package-root", "")
PACKAGE_ROOT = Path(PACKAGE_ROOT_VALUE).resolve() if PACKAGE_ROOT_VALUE else None
FIXTURE_PATH = Path(_arg_value("--fixture", str(DEFAULT_FIXTURE))).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_02b_align_similar_pro.json"),
    )
).resolve()
if PACKAGE_ROOT is not None:
    if PACKAGE_ROOT.name != "uv_gpt" or not (PACKAGE_ROOT / "__init__.py").is_file():
        raise HarnessError("Invalid extracted package root: %s" % PACKAGE_ROOT)
    blocked_paths = {
        str(PROJECT_ROOT),
        str(PROJECT_ROOT / "tests"),
    }
    filtered_path = []
    for item in sys.path:
        try:
            resolved = str(Path(item or ".").resolve())
        except (OSError, RuntimeError, TypeError):
            resolved = None
        if resolved not in blocked_paths:
            filtered_path.append(item)
    sys.path = filtered_path
    sys.path.insert(0, str(PACKAGE_ROOT.parent))
elif str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def import_addon():
    for name in list(sys.modules):
        if name == "uv_gpt" or name.startswith("uv_gpt."):
            del sys.modules[name]
    importlib.invalidate_caches()
    import uv_gpt

    loaded_from = Path(uv_gpt.__file__).resolve()
    if PACKAGE_ROOT is not None:
        expected = (PACKAGE_ROOT / "__init__.py").resolve()
        if loaded_from != expected:
            raise HarnessError(
                "Package smoke imported wrong add-on path: %s" % loaded_from
            )
    if tuple(uv_gpt.bl_info.get("version", ())) != (1, 2, 6):
        raise HarnessError(
            "Package version mismatch: %s" % (uv_gpt.bl_info.get("version"),)
        )
    return uv_gpt


def package_metadata(uv_gpt):
    stack_tools = importlib.import_module("uv_gpt.stack_tools")
    return {
        "mode": "zip-import" if PACKAGE_ROOT is not None else "workspace",
        "loaded_from": str(Path(uv_gpt.__file__).resolve()),
        "version": list(uv_gpt.bl_info.get("version", ())),
        "operator_ids": [
            stack_tools.UVGPT_OT_align_to_selected.bl_idname,
            stack_tools.UVGPT_OT_align_similar_pro_fast.bl_idname,
            stack_tools.UVGPT_OT_align_similar_pro_exact.bl_idname,
        ],
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def clean_json(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Vector):
        return [clean_json(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return clean_json(value.to_dict())
    if hasattr(value, "__dict__"):
        return clean_json(vars(value))
    return str(value)


def setup_bmesh(bm):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()


def face_key(island):
    return tuple(sorted({int(loop.face.index) for loop in island}))


def loop_key(face, local_index):
    return int(face.index), int(local_index)


def island_loop_keys(island):
    keys = []
    for loop in island:
        for local_index, candidate in enumerate(loop.face.loops):
            if candidate is loop:
                keys.append(loop_key(loop.face, local_index))
                break
    return tuple(keys)


def snapshot_uv(bm, uv_layer):
    setup_bmesh(bm)
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            uv = loop[uv_layer].uv
            result[loop_key(face, local_index)] = (float(uv.x), float(uv.y))
    return result


def snapshot_selection(bm, uv_layer):
    setup_bmesh(bm)
    result = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            result[loop_key(face, local_index)] = (
                bool(getattr(luv, "select", False)),
                bool(getattr(luv, "select_edge", False)),
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
            )
    return result


def snapshot_active(obj, bm):
    active_face = getattr(bm.faces, "active", None)
    active_uv = getattr(getattr(obj, "data", None), "uv_layers", None)
    active_uv = getattr(active_uv, "active", None)
    return {
        "object": obj.name if obj is not None else None,
        "mode": obj.mode if obj is not None else None,
        "active_face": int(active_face.index) if active_face is not None else None,
        "active_uv": getattr(active_uv, "name", None),
        "selected_objects": tuple(
            sorted(item.name for item in bpy.context.selected_objects)
        ),
    }


def restore_uv(bm, uv_layer, snapshot):
    setup_bmesh(bm)
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            value = snapshot.get(loop_key(face, local_index))
            if value is not None:
                loop[uv_layer].uv = Vector(value)
    bmesh.update_edit_mesh(bpy.context.edit_object.data, loop_triangles=False, destructive=False)


def restore_selection(bm, uv_layer, snapshot):
    setup_bmesh(bm)
    for face in bm.faces:
        face.select_set(False)
        for loop in face.loops:
            loop.vert.select_set(False)
            loop.edge.select_set(False)
            try:
                loop[uv_layer].select = False
                loop[uv_layer].select_edge = False
            except Exception:
                pass
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            values = snapshot.get(loop_key(face, local_index))
            if values is None:
                continue
            uv_select, edge_select, face_select, vert_select, mesh_edge_select = values
            try:
                loop[uv_layer].select = uv_select
                loop[uv_layer].select_edge = edge_select
            except Exception:
                pass
            if face_select:
                face.select_set(True)
            if vert_select:
                loop.vert.select_set(True)
            if mesh_edge_select:
                loop.edge.select_set(True)
    bmesh.update_edit_mesh(bpy.context.edit_object.data, loop_triangles=False, destructive=False)


def max_delta(before, after, keys):
    result = 0.0
    for key in keys:
        lhs = before.get(key)
        rhs = after.get(key)
        if lhs is None or rhs is None:
            continue
        result = max(result, abs(lhs[0] - rhs[0]), abs(lhs[1] - rhs[1]))
    return result


def activate_object(obj):
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for item in bpy.context.view_layer.objects:
        item.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")


def prepare_case(obj, island_tools, uv_utils):
    settings = bpy.context.scene.uv_gpt_settings
    settings.duplicate_before_operations = False
    activate_object(obj)
    settings.active_uv_map = TARGET_UV_NAME
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    if len(islands) != EXPECTED_ISLAND_COUNT:
        raise HarnessError("Unexpected island count: %s" % len(islands))
    by_key = {face_key(island): island for island in islands}
    if MASTER_CANDIDATE_A not in by_key or MASTER_CANDIDATE_B not in by_key:
        raise HarnessError("Locked PRO pair is missing from the fixture")
    uv_before = snapshot_uv(bm, uv_layer)
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(
        bm,
        uv_layer,
        [by_key[MASTER_CANDIDATE_A], by_key[MASTER_CANDIDATE_B]],
    )
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    selected = island_tools.get_selected_uv_islands(bm, uv_layer)
    if {face_key(island) for island in selected} != {
        MASTER_CANDIDATE_A,
        MASTER_CANDIDATE_B,
    }:
        raise HarnessError(
            "Pair selection mismatch: %s"
            % sorted(face_key(island) for island in selected)
        )
    return bm, uv_layer, by_key, uv_before, snapshot_selection(bm, uv_layer)


def prepare_dedicated_case(obj, island_tools, uv_utils, selected_keys):
    settings = bpy.context.scene.uv_gpt_settings
    settings.duplicate_before_operations = False
    activate_object(obj)
    settings.active_uv_map = TARGET_UV_NAME
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    by_key = {face_key(island): island for island in islands}
    if not set(selected_keys).issubset(by_key):
        raise HarnessError(
            "Dedicated fixture selection keys missing: %s"
            % sorted(set(selected_keys) - set(by_key))
        )
    uv_before = snapshot_uv(bm, uv_layer)
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(
        bm,
        uv_layer,
        [by_key[key] for key in selected_keys],
    )
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    selected = island_tools.get_selected_uv_islands(bm, uv_layer)
    if {face_key(island) for island in selected} != set(selected_keys):
        raise HarnessError(
            "Dedicated selection mismatch for %s: got %s"
            % (obj.name, sorted(face_key(island) for island in selected))
        )
    return bm, uv_layer, by_key, uv_before, snapshot_selection(bm, uv_layer)


def run_dedicated_case(
    obj,
    island_tools,
    uv_utils,
    stack_tools,
    selected_keys,
    expected_master,
    allow_flipping=False,
):
    bm, uv_layer, by_key, baseline_uv, baseline_selection = prepare_dedicated_case(
        obj,
        island_tools,
        uv_utils,
        selected_keys,
    )
    settings = bpy.context.scene.uv_gpt_settings
    settings.stack_allow_flipping = bool(allow_flipping)
    before_uv = snapshot_uv(bm, uv_layer)
    before_active = snapshot_active(obj, bm)
    started = time.perf_counter()
    result = stack_tools.run_align_similar_pro(
        {
            "bpy_context": bpy.context,
            "detail_mappings": True,
            "cooperative_yield_every": COOPERATIVE_YIELD_EVERY,
            "correspondence_mode": "EXACT_ONLY",
            "mode": "EXACT_ONLY",
        }
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = snapshot_uv(bm, uv_layer)
    after_selection = snapshot_selection(bm, uv_layer)
    after_active = snapshot_active(obj, bm)
    if result["operator_result"] != ["FINISHED"]:
        raise HarnessError("Dedicated operator did not finish: %s" % result)
    if snapshot_selection(bm, uv_layer) != baseline_selection:
        raise HarnessError("Dedicated selection changed")
    if before_active != after_active:
        raise HarnessError("Dedicated active state changed")
    duplicate_targets = 0
    target_keys = set()
    mapping_max_delta = 0.0
    for group in result.get("groups", []):
        if expected_master is not None and tuple(group["master_key"]) != expected_master:
            raise HarnessError(
                "Dedicated UV-area master mismatch: %s" % group["master_key"]
            )
        for mapping_pairs in group.get("mapping_pairs", []):
            for pair in mapping_pairs:
                key = tuple(pair[0])
                master_key = tuple(pair[1])
                if key in target_keys:
                    duplicate_targets += 1
                target_keys.add(key)
                expected = before_uv[master_key]
                actual = after_uv[key]
                mapping_max_delta = max(
                    mapping_max_delta,
                    abs(expected[0] - actual[0]),
                    abs(expected[1] - actual[1]),
                )
    if duplicate_targets:
        raise HarnessError("Dedicated duplicate target assignment")
    master_delta = 0.0
    if expected_master is not None:
        master_keys = set(island_loop_keys(by_key[expected_master]))
        master_delta = max_delta(before_uv, after_uv, master_keys)
    if result.get("aligned_exact", 0):
        if mapping_max_delta > 1.0e-7:
            raise HarnessError("Dedicated exact mapped UV delta is %s" % mapping_max_delta)
        if master_delta > SELECTION_EPSILON:
            raise HarnessError("Dedicated master UV changed by %s" % master_delta)
    unselected_keys = set(before_uv) - set().union(
        *(set(island_loop_keys(by_key[key])) for key in selected_keys)
    )
    unselected_delta = max_delta(before_uv, after_uv, unselected_keys)
    if unselected_delta > SELECTION_EPSILON:
        raise HarnessError("Dedicated unselected UV changed")
    if not result.get("aligned_exact", 0) and max_delta(
        before_uv, after_uv, set(before_uv)
    ) > SELECTION_EPSILON:
        raise HarnessError("Dedicated rejection wrote UVs")
    return {
        "object": obj.name,
        "selected_keys": [list(key) for key in selected_keys],
        "allow_flipping": bool(allow_flipping),
        "elapsed_ms": elapsed_ms,
        "result": clean_json(result),
        "mapping_max_delta": mapping_max_delta,
        "master_delta": master_delta,
        "unselected_delta": unselected_delta,
        "duplicate_targets": duplicate_targets,
        "uv_changed": max_delta(before_uv, after_uv, set(before_uv)),
    }


def run_one(
    obj,
    island_tools,
    uv_utils,
    stack_tools,
    baseline_uv,
    baseline_selection,
    by_key,
    run_kind,
    run_index,
):
    bm = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("Locked UV layer disappeared")
    restore_uv(bm, uv_layer, baseline_uv)
    restore_selection(bm, uv_layer, baseline_selection)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    before_uv = snapshot_uv(bm, uv_layer)
    before_selection = snapshot_selection(bm, uv_layer)
    before_active = snapshot_active(obj, bm)
    selected_keys = set(island_loop_keys(by_key[MASTER_CANDIDATE_A])) | set(
        island_loop_keys(by_key[MASTER_CANDIDATE_B])
    )
    started = time.perf_counter()
    result = stack_tools.run_align_similar_pro(
        {
            "bpy_context": bpy.context,
            "detail_mappings": True,
            "cooperative_yield_every": COOPERATIVE_YIELD_EVERY,
            "correspondence_mode": "EXACT_ONLY",
            "mode": "EXACT_ONLY",
        }
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = snapshot_uv(bm, uv_layer)
    after_selection = snapshot_selection(bm, uv_layer)
    after_active = snapshot_active(obj, bm)

    if result["operator_result"] != ["FINISHED"]:
        raise HarnessError("Align Similar Pro did not finish: %s" % result)
    if before_selection != after_selection:
        raise HarnessError("UV/mesh selection changed")
    if before_active != after_active:
        raise HarnessError("Active object/face/mode changed")

    target_keys = set()
    duplicate_targets = 0
    mapping_max_delta = 0.0
    master_delta = 0.0
    for group in result.get("groups", []):
        if tuple(group["master_key"]) != MASTER_CANDIDATE_B:
            raise HarnessError(
                "Expected UV-area master B, got %s" % group["master_key"]
            )
        for mapping_pairs in group.get("mapping_pairs", []):
            candidate_keys = []
            for pair in mapping_pairs:
                candidate_key = tuple(pair[0])
                master_key = tuple(pair[1])
                candidate_keys.append(candidate_key)
                if candidate_key in target_keys:
                    duplicate_targets += 1
                target_keys.add(candidate_key)
                expected = before_uv[master_key]
                actual = after_uv[candidate_key]
                mapping_max_delta = max(
                    mapping_max_delta,
                    abs(expected[0] - actual[0]),
                    abs(expected[1] - actual[1]),
                )
            if len(candidate_keys) != len(set(candidate_keys)):
                duplicate_targets += 1
    if duplicate_targets:
        raise HarnessError("Duplicate candidate loop assignment detected")

    master_keys = set(island_loop_keys(by_key[MASTER_CANDIDATE_B]))
    master_delta = max_delta(before_uv, after_uv, master_keys)
    unselected_keys = set(before_uv) - selected_keys
    unselected_delta = max_delta(before_uv, after_uv, unselected_keys)
    if result.get("aligned_exact", 0):
        if mapping_max_delta > 1.0e-7:
            raise HarnessError("Exact mapped UV delta is %s" % mapping_max_delta)
        if master_delta > SELECTION_EPSILON:
            raise HarnessError("UV-area master UV changed by %s" % master_delta)
        if unselected_delta > SELECTION_EPSILON:
            raise HarnessError("Unselected UV changed by %s" % unselected_delta)
    else:
        if max_delta(before_uv, after_uv, set(before_uv)) > SELECTION_EPSILON:
            raise HarnessError("Rejected Pro pair wrote UVs")

    return {
        "run_kind": run_kind,
        "run_index": run_index,
        "elapsed_ms": elapsed_ms,
        "result": clean_json(result),
        "mapping_max_delta": mapping_max_delta,
        "master_delta": master_delta,
        "unselected_delta": unselected_delta,
        "selection_unchanged": before_selection == after_selection,
        "active_unchanged": before_active == after_active,
        "duplicate_targets": duplicate_targets,
    }


def main_dedicated():
    if not FIXTURE_PATH.is_file():
        raise HarnessError("Dedicated fixture missing: %s" % FIXTURE_PATH)
    fixture_sha = sha256_file(FIXTURE_PATH)
    uv_gpt = import_addon()

    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        cases = (
            ("PROExact", ((0,), (1,), (2,), (3,)), (0,), False),
            ("PROExact", ((0,), (1,), (2,), (3,)), (0,), True),
            ("PROHole", ((0, 1, 2, 3), (4, 5, 6, 7)), (0, 1, 2, 3), False),
            ("PROInterior", ((0, 1, 2, 3), (4, 5, 6, 7)), (0, 1, 2, 3), False),
            ("PROSeam", ((0, 1, 2, 3, 4, 5, 6, 7, 8), (9, 10, 11, 12, 13, 14, 15, 16, 17)), (0, 1, 2, 3, 4, 5, 6, 7, 8), False),
            ("PRONonIso", ((0,), (1, 2)), None, False),
        )
        case_results = []
        for object_name, selected_keys, expected_master, allow_flipping in cases:
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                raise HarnessError("Dedicated object missing: %s" % object_name)
            case_results.append(
                run_dedicated_case(
                    obj,
                    island_tools,
                    uv_utils,
                    stack_tools,
                    selected_keys,
                    expected_master,
                    allow_flipping=allow_flipping,
                )
            )
        if case_results[0]["result"]["aligned_exact"] < 2:
            raise HarnessError("Dedicated exact/cyclic cases did not align")
        if case_results[1]["result"]["aligned_exact"] < 3:
            raise HarnessError("Dedicated reflection case did not align with flipping")
        if not any(
            item.get("reason") == "reflection_not_allowed"
            for item in case_results[0]["result"].get("topology_rejections", [])
        ):
            raise HarnessError("Dedicated reflection-off rejection was not observed")
        if case_results[4]["result"]["aligned_exact"] != 1:
            raise HarnessError("Dedicated seam/shared-vertex case did not align")
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "mode": "dedicated",
            "package": package_metadata(uv_gpt),
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "cases": case_results,
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print("PRO-02B dedicated runtime passed: %s" % RESULT_PATH)
    finally:
        uv_gpt.unregister()


def main():
    if _arg_value("--mode", "cc") == "dedicated":
        return main_dedicated()
    if not FIXTURE_PATH.is_file():
        raise HarnessError("Fixture missing: %s" % FIXTURE_PATH)
    fixture_sha = sha256_file(FIXTURE_PATH)
    if fixture_sha != EXPECTED_FIXTURE_SHA:
        raise HarnessError("Fixture SHA mismatch: %s" % fixture_sha)
    if FIXTURE_SHA_BEFORE_EXTERNAL and fixture_sha != FIXTURE_SHA_BEFORE_EXTERNAL:
        raise HarnessError("External fixture SHA mismatch: %s" % fixture_sha)

    uv_gpt = import_addon()

    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH":
            raise HarnessError("Target object missing: %s" % TARGET_OBJECT_NAME)
        activate_object(obj)
        bm, uv_layer, by_key, baseline_uv, baseline_selection = prepare_case(
            obj,
            island_tools,
            uv_utils,
        )
        runs = []
        for run_index in range(WARMUP_RUNS):
            runs.append(
                run_one(
                    obj,
                    island_tools,
                    uv_utils,
                    stack_tools,
                    baseline_uv,
                    baseline_selection,
                    by_key,
                    "warmup",
                    run_index,
                )
            )
        for run_index in range(MEASURED_RUNS):
            runs.append(
                run_one(
                    obj,
                    island_tools,
                    uv_utils,
                    stack_tools,
                    baseline_uv,
                    baseline_selection,
                    by_key,
                    "measured",
                    run_index,
                )
            )
        measured = [run for run in runs if run["run_kind"] == "measured"]
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "package": package_metadata(uv_gpt),
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "object": TARGET_OBJECT_NAME,
            "uv_map": TARGET_UV_NAME,
            "island_count": EXPECTED_ISLAND_COUNT,
            "locked_pair": {
                "A": list(MASTER_CANDIDATE_A),
                "B": list(MASTER_CANDIDATE_B),
            },
            "warmup_runs": WARMUP_RUNS,
            "measured_runs": MEASURED_RUNS,
            "runs": runs,
            "measured_elapsed_ms": [run["elapsed_ms"] for run in measured],
            "measured_mapping_max_delta": [
                run["mapping_max_delta"] for run in measured
            ],
            "measured_master_delta": [run["master_delta"] for run in measured],
            "measured_unselected_delta": [
                run["unselected_delta"] for run in measured
            ],
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print("PRO-02B runtime passed: %s" % RESULT_PATH)
    finally:
        uv_gpt.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("PRO-02B runtime failed: %s" % exc)
        raise SystemExit(1)
