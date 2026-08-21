"""AS-02 focused Blender fixture harness for scale/flip toggle behavior."""

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


PACKET_ID = "AS-02"
SCHEMA_VERSION = "as-02-align-similar-selected-focused-v1"
TARGET_OBJECT_NAME = "AS_02_Focus"
TARGET_UV_NAME = "UVMap.001"
WARMUP_RUNS = 1
MEASURED_RUNS = 3
SELECTION_EPSILON = 1.0e-12
EXPECTED_MEMBER_KEYS = {
    "scale_on_flip_on": {(1,), (2,), (3,)},
    "scale_on_flip_off": {(1,), (3,)},
    "scale_off_flip_on": {(2,), (3,)},
    "scale_off_flip_off": {(3,)},
}


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
    return args[index + 1] if index + 1 < len(args) else default


def _bool_arg(name):
    value = _arg_value(name, "")
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise HarnessError("Missing or invalid boolean argument %s=%s" % (name, value))


SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = Path(
    _arg_value("--project-root", str(SCRIPT_PATH.parents[2]))
).resolve()
FIXTURE_PATH = Path(
    _arg_value(
        "--fixture",
        str(PROJECT_ROOT / ".test_runtime" / "as_02_focus.blend"),
    )
).resolve()
PACKAGE_ARGUMENT = _arg_value("--package-zip", "")
PACKAGE_ZIP_PATH = Path(PACKAGE_ARGUMENT).resolve() if PACKAGE_ARGUMENT else None
RESULT_ARGUMENT = _arg_value("--result", "")
RESULT_PATH = (
    Path(RESULT_ARGUMENT).resolve()
    if RESULT_ARGUMENT
    else PROJECT_ROOT / "benchmarks" / "as_02_focus.json"
)
EXPECTED_FIXTURE_SHA_ARGUMENT = _arg_value("--fixture-sha-before", "").upper()
MATCH_SCALE = _bool_arg("--match-scale")
ALLOW_FLIPPING = _bool_arg("--allow-flipping")
CONFIGURATION = "scale_%s_flip_%s" % (
    "on" if MATCH_SCALE else "off",
    "on" if ALLOW_FLIPPING else "off",
)

if PACKAGE_ZIP_PATH is not None:
    sys.path.insert(0, str(PACKAGE_ZIP_PATH))
else:
    sys.path.insert(0, str(PROJECT_ROOT))


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


def face_key(island_or_face):
    loops = island_or_face.loops if hasattr(island_or_face, "loops") else island_or_face
    return tuple(sorted({int(loop.face.index) for loop in loops}))


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
    return {
        loop_key(face, local_index): (
            float(loop[uv_layer].uv.x),
            float(loop[uv_layer].uv.y),
        )
        for face in bm.faces
        for local_index, loop in enumerate(face.loops)
    }


def snapshot_selection(bm, uv_layer):
    setup_bmesh(bm)
    return {
        loop_key(face, local_index): (
            bool(getattr(loop[uv_layer], "select", False)),
            bool(getattr(loop[uv_layer], "select_edge", False)),
            bool(face.select),
            bool(loop.vert.select),
            bool(loop.edge.select),
        )
        for face in bm.faces
        for local_index, loop in enumerate(face.loops)
    }


def restore_state(obj, uv_utils, uv_snapshot, selection_snapshot):
    bm = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("Focused fixture UV layer disappeared while restoring")
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            value = uv_snapshot.get(loop_key(face, local_index))
            if value is not None:
                loop[uv_layer].uv = Vector(value)
    uv_utils.restore_uv_selection(bm, uv_layer, selection_snapshot)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    return bm, uv_layer


def active_state(obj, bm, uv_layer):
    active = getattr(bm.faces, "active", None)
    active_key = list(face_key(active)) if active is not None else None
    active_object = bpy.context.view_layer.objects.active
    active_uv = getattr(obj.data.uv_layers, "active", None)
    return {
        "active_object": getattr(active_object, "name", None),
        "context_object": getattr(getattr(bpy.context, "object", None), "name", None),
        "edit_object": getattr(getattr(bpy.context, "edit_object", None), "name", None),
        "mode": str(getattr(obj, "mode", None)),
        "active_face_key": active_key,
        "active_uv_layer": getattr(active_uv, "name", None),
        "bmesh_uv_layer": getattr(uv_layer, "name", None),
        "use_uv_select_sync": bool(
            getattr(getattr(bpy.context.scene, "tool_settings", None), "use_uv_select_sync", False)
        ),
    }


def islands_by_key(islands):
    return {face_key(island): island for island in islands}


def island_delta(before, after, island):
    values = []
    for key in island_loop_keys(island):
        lhs = before.get(key)
        rhs = after.get(key)
        if lhs is None or rhs is None:
            raise HarnessError("Focused fixture UV snapshot key disappeared: %s" % (key,))
        values.append(max(abs(lhs[0] - rhs[0]), abs(lhs[1] - rhs[1])))
    return max(values, default=0.0)


def settings_snapshot(context):
    settings = context.scene.uv_gpt_settings
    return {
        "stack_match_scale": bool(settings.stack_match_scale),
        "stack_allow_flipping": bool(settings.stack_allow_flipping),
        "stack_similarity_tolerance": float(settings.stack_similarity_tolerance),
        "active_uv_map_setting": str(settings.active_uv_map),
        "duplicate_before_operations": bool(settings.duplicate_before_operations),
    }


def timing_summary(runs):
    values = sorted(float(item["elapsed_ms"]) for item in runs)
    if not values:
        return {"count": 0, "min_ms": None, "median_ms": None, "p95_ms": None, "runs_ms": []}
    median = values[len(values) // 2]
    p95 = values[min(len(values) - 1, int(math.ceil((len(values) - 1) * 0.95)))]
    return {
        "count": len(values),
        "min_ms": values[0],
        "median_ms": median,
        "p95_ms": p95,
        "runs_ms": [float(item["elapsed_ms"]) for item in runs],
    }


def validate_coverage(evidence, selected_keys):
    selected_set = set(selected_keys)
    seen = []
    for group in evidence.get("groups", []):
        representative = tuple(group["representative_key"])
        members = [tuple(key) for key in group.get("member_keys", [])]
        if representative in members:
            raise HarnessError("Focused group contains its representative as a member")
        if len(members) != len(set(members)):
            raise HarnessError("Focused group assigns one member more than once")
        seen.append(representative)
        seen.extend(members)
    if len(seen) != len(set(seen)) or set(seen) != selected_set:
        raise HarnessError(
            "Focused groups do not cover selected islands exactly: seen=%s selected=%s"
            % (sorted(set(seen)), sorted(selected_set))
        )
    expected_groups = sum(len(group.get("member_keys", [])) >= 1 for group in evidence.get("groups", []))
    expected_aligned = sum(len(group.get("member_keys", [])) for group in evidence.get("groups", []))
    if int(evidence.get("group_count", -1)) != expected_groups:
        raise HarnessError("Focused group_count mismatch")
    if int(evidence.get("aligned_count", -1)) != expected_aligned:
        raise HarnessError("Focused aligned_count mismatch")


def run_one(obj, uv_utils, island_tools, stack_tools, baseline_uv, baseline_selection, baseline_keys, run_kind, run_index):
    bm, uv_layer = restore_state(obj, uv_utils, baseline_uv, baseline_selection)
    all_before = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected_before = [
        island for island in all_before if stack_tools._island_is_selected(island, uv_layer)
    ]
    selected_keys = tuple(sorted(face_key(island) for island in selected_before))
    if selected_keys != baseline_keys:
        raise HarnessError("Focused baseline restore changed selected island membership")
    before_uv = snapshot_uv(bm, uv_layer)
    before_selection = snapshot_selection(bm, uv_layer)
    before_state = active_state(obj, bm, uv_layer)
    settings = settings_snapshot(bpy.context)
    started = time.perf_counter()
    evidence = stack_tools.run_match_03({"bpy_context": bpy.context})
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    bm_after = bmesh.from_edit_mesh(obj.data)
    setup_bmesh(bm_after)
    uv_layer_after = bm_after.loops.layers.uv.get(TARGET_UV_NAME)
    all_after = island_tools.get_uv_islands(bm_after, uv_layer_after, selected_only=False)
    after_selection = snapshot_selection(bm_after, uv_layer_after)
    after_uv = snapshot_uv(bm_after, uv_layer_after)
    after_state = active_state(obj, bm_after, uv_layer_after)
    before_by_key = islands_by_key(all_before)
    after_by_key = islands_by_key(all_after)
    selected_set = set(selected_keys)
    unselected_keys = tuple(sorted(set(before_by_key) - selected_set))
    validate_coverage(evidence, selected_keys)

    apply_records = [
        {
            "member_key": tuple(item["member_key"]),
            "representative_key": tuple(item["representative_key"]),
            "score": float(item["score"]),
        }
        for item in evidence.get("apply_records", [])
    ]
    member_keys = [item["member_key"] for item in apply_records]
    if len(member_keys) != len(set(member_keys)):
        raise HarnessError("Focused apply evidence contains duplicate assignments")
    actual_member_keys = set(member_keys)
    representative_keys = [tuple(item["representative_key"]) for item in evidence.get("groups", [])]
    representative_deltas = {
        str(key): island_delta(baseline_uv, after_uv, after_by_key[key])
        for key in representative_keys
    }
    unselected_deltas = {
        str(key): island_delta(baseline_uv, after_uv, after_by_key[key])
        for key in unselected_keys
    }
    changed_keys = {
        key
        for key in after_by_key
        if island_delta(baseline_uv, after_uv, after_by_key[key]) > SELECTION_EPSILON
    }
    changed_nonmembers = sorted(changed_keys - actual_member_keys)
    tolerance = float(settings["stack_similarity_tolerance"])
    quality_failures = [
        item for item in apply_records if not math.isfinite(item["score"]) or item["score"] > tolerance
    ]
    return {
        "run_kind": run_kind,
        "run_index": run_index,
        "elapsed_ms": elapsed_ms,
        "settings": settings,
        "operator_result": evidence.get("operator_result", []),
        "aligned_count": int(evidence.get("aligned_count", 0)),
        "group_count": int(evidence.get("group_count", 0)),
        "full_fits": int(getattr(evidence.get("diagnostics"), "full_fits", 0)),
        "groups": evidence.get("groups", []),
        "actual_member_keys": [list(key) for key in sorted(actual_member_keys)],
        "correctness": {
            "selection_snapshot_unchanged": before_selection == after_selection,
            "unselected_max_delta": max(unselected_deltas.values(), default=0.0),
            "representatives_max_delta": max(representative_deltas.values(), default=0.0),
            "changed_nonmembers": [list(key) for key in changed_nonmembers],
            "quality_failures": quality_failures,
            "active_state_unchanged": before_state == after_state,
            "no_duplicate_assignment": len(member_keys) == len(set(member_keys)),
        },
    }


def run_harness():
    if not FIXTURE_PATH.is_file():
        raise HarnessError("Focused fixture missing: %s" % FIXTURE_PATH)
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if EXPECTED_FIXTURE_SHA_ARGUMENT and fixture_sha_before != EXPECTED_FIXTURE_SHA_ARGUMENT:
        raise HarnessError("Focused fixture SHA mismatch before Blender: %s" % fixture_sha_before)
    if Path(bpy.data.filepath).resolve() != FIXTURE_PATH:
        raise HarnessError("Blender opened a different focused fixture: %s" % bpy.data.filepath)

    addon = importlib.import_module("uv_gpt")
    island_tools = importlib.import_module("uv_gpt.island_tools")
    stack_tools = importlib.import_module("uv_gpt.stack_tools")
    uv_utils = importlib.import_module("uv_gpt.uv_utils")
    registered = False
    try:
        addon.register()
        registered = True
        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
            raise HarnessError("Focused mesh is not active in Edit Mode")
        bpy.context.view_layer.objects.active = obj
        obj.data.uv_layers.active_index = obj.data.uv_layers.find(TARGET_UV_NAME)
        settings = bpy.context.scene.uv_gpt_settings
        settings.active_uv_map = TARGET_UV_NAME
        settings.duplicate_before_operations = False
        settings.stack_match_scale = MATCH_SCALE
        settings.stack_allow_flipping = ALLOW_FLIPPING
        settings.stack_similarity_tolerance = 0.01

        bm = island_tools.get_active_bmesh(bpy.context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        selected = [island for island in all_islands if stack_tools._island_is_selected(island, uv_layer)]
        selected_keys = tuple(sorted(face_key(island) for island in selected))
        if len(all_islands) != 5 or selected_keys != ((0,), (1,), (2,), (3,)):
            raise HarnessError("Unexpected focused fixture islands: %s / %s" % (len(all_islands), selected_keys))
        baseline_uv = snapshot_uv(bm, uv_layer)
        baseline_selection = snapshot_selection(bm, uv_layer)
        baseline_keys = selected_keys
        runs = []
        for index in range(WARMUP_RUNS):
            runs.append(run_one(obj, uv_utils, island_tools, stack_tools, baseline_uv, baseline_selection, baseline_keys, "warmup", index + 1))
        measured = []
        for index in range(MEASURED_RUNS):
            item = run_one(obj, uv_utils, island_tools, stack_tools, baseline_uv, baseline_selection, baseline_keys, "measured", index + 1)
            runs.append(item)
            measured.append(item)

        expected_members = EXPECTED_MEMBER_KEYS[CONFIGURATION]
        for index, item in enumerate(measured, 1):
            if item["operator_result"] != ["FINISHED"]:
                raise HarnessError("Focused measured run %d did not finish" % index)
            if set(tuple(key) for key in item["actual_member_keys"]) != expected_members:
                raise HarnessError(
                    "Focused %s member mismatch: actual=%s expected=%s"
                    % (CONFIGURATION, item["actual_member_keys"], sorted(expected_members))
                )
            correctness = item["correctness"]
            if not correctness["selection_snapshot_unchanged"]:
                raise HarnessError("Focused measured run %d changed selection" % index)
            if correctness["unselected_max_delta"] > SELECTION_EPSILON:
                raise HarnessError("Focused measured run %d changed unselected UVs" % index)
            if correctness["representatives_max_delta"] > SELECTION_EPSILON:
                raise HarnessError("Focused measured run %d changed a representative" % index)
            if correctness["changed_nonmembers"] or not correctness["active_state_unchanged"]:
                raise HarnessError("Focused measured run %d changed protected state" % index)
            if correctness["quality_failures"] or not correctness["no_duplicate_assignment"]:
                raise HarnessError("Focused measured run %d failed safety/quality" % index)
            if item["full_fits"] <= 0:
                raise HarnessError("Focused measured run %d had no full fits" % index)

        signatures = [
            (item["groups"], item["actual_member_keys"], item["aligned_count"], item["group_count"])
            for item in measured
        ]
        if any(signature != signatures[0] for signature in signatures[1:]):
            raise HarnessError("Focused measured grouping is not deterministic")
        fixture_sha_after = sha256_file(FIXTURE_PATH)
        if fixture_sha_after != fixture_sha_before:
            raise HarnessError("Focused fixture SHA changed in Blender")
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "schema": SCHEMA_VERSION,
            "fixture": {
                "path": str(FIXTURE_PATH),
                "sha256_before": fixture_sha_before,
                "sha256_after": fixture_sha_after,
                "raw_islands": len(all_islands),
                "selected_islands": len(selected),
                "selected_keys": [list(key) for key in selected_keys],
            },
            "configuration": {
                "name": CONFIGURATION,
                "stack_match_scale": MATCH_SCALE,
                "stack_allow_flipping": ALLOW_FLIPPING,
                "expected_member_keys": [list(key) for key in sorted(expected_members)],
            },
            "package": {
                "mode": "zip-import" if PACKAGE_ZIP_PATH is not None else "source-import",
                "zip_path": str(PACKAGE_ZIP_PATH) if PACKAGE_ZIP_PATH is not None else None,
                "loaded_from": str(getattr(addon, "__file__", "")),
            },
            "warmup_runs": WARMUP_RUNS,
            "measured_runs": MEASURED_RUNS,
            "timing": timing_summary(measured),
            "runs": runs,
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(clean_json(result), indent=2, sort_keys=True), encoding="utf-8", newline="\n")
        print(
            "AS-02 focused %s: aligned=%d groups=%d full_fits=%d timing=%s"
            % (CONFIGURATION, measured[0]["aligned_count"], measured[0]["group_count"], measured[0]["full_fits"], result["timing"]["runs_ms"]),
            flush=True,
        )
        return result
    finally:
        if registered:
            addon.unregister()


def main():
    try:
        run_harness()
    except Exception as exc:
        failure = {
            "status": "failed",
            "packet": PACKET_ID,
            "schema": SCHEMA_VERSION,
            "configuration": CONFIGURATION,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
        try:
            RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULT_PATH.write_text(json.dumps(clean_json(failure), indent=2, sort_keys=True), encoding="utf-8", newline="\n")
        except Exception:
            pass
        import traceback

        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
