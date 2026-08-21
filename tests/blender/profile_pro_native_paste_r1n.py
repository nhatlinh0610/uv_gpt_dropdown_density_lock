"""MC4-R1N native UV copy/paste A/B oracle on the disposable fixture.

This is deliberately an evidence harness, not a Pro implementation.  It runs
the existing synchronous Pro path first, then reproduces the native Blender
``uv.copy``/``uv.paste`` pattern on every synchronously accepted member and on
one same-face-count rejected member.  The fixture is opened in memory only and
is never saved.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import traceback

sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "MC4-R1N-NATIVE-PASTE-FEASIBILITY"
EXPECTED_FIXTURE_SHA = "EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8"
EXPECTED_COUNTS = (2, 3, 1, 1, 1, 0)
TARGET_UV_NAME = "UVMap.001"
DELTA_TOLERANCE = 1.0e-7
SELECTION_TOLERANCE = 1.0e-12
CORRESPONDENCE_MODE = "EXACT_ONLY"


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


SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = Path(_arg_value("--project-root", str(SCRIPT_PATH.parents[2]))).resolve()
FIXTURE_PATH = Path(
    _arg_value(
        "--fixture",
        str(PROJECT_ROOT / "benchmarks" / "pro_02b_dedicated_fixture.blend"),
    )
).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(Path(__import__("tempfile").gettempdir()) / "uv_gpt_mc4r1n_result.json"),
    )
).resolve()


def _load_common():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("mc4r1n_common", path)
    if spec is None or spec.loader is None:
        raise HarnessError("could not load existing dedicated harness helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COMMON = _load_common()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _key(value):
    return tuple(int(item) for item in value)


def _keys(values):
    return tuple(_key(value) for value in values)


def _sorted_keys(values):
    return tuple(sorted((_key(value) for value in values)))


def _operator_info(name, uv_utils):
    """Run one native operator with the same context seam as the add-on."""

    override = uv_utils._uv_context_override(bpy.context)
    try:
        with bpy.context.temp_override(**override):
            result = getattr(bpy.ops.uv, name)()
        return {"return": sorted(str(item) for item in result), "error": None}
    except Exception as exc:
        return {
            "return": None,
            "error": "%s: %s" % (type(exc).__name__, str(exc)),
        }


def _is_finished(operator_info):
    return operator_info.get("error") is None and operator_info.get("return") == [
        "FINISHED"
    ]


def _uv_digest(values):
    payload = repr(tuple(sorted((tuple(key), tuple(value)) for key, value in values.items())))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()


def _result_digest(result):
    canonical = {
        "aligned_exact": result.get("aligned_exact", 0),
        "group_count": result.get("group_count", 0),
        "groups": result.get("groups", []),
        "topology_rejections": result.get("topology_rejections", []),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest().upper()


def _max_delta(before, after, keys):
    maximum = 0.0
    for key in keys:
        lhs = before.get(key)
        rhs = after.get(key)
        if lhs is None or rhs is None:
            continue
        maximum = max(
            maximum,
            abs(float(lhs[0]) - float(rhs[0])),
            abs(float(lhs[1]) - float(rhs[1])),
        )
    return maximum


def _island_state(obj, island_tools):
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    by_key = {COMMON.face_key(island): island for island in islands}
    return bm, uv_layer, by_key


def _select_keys(obj, island_tools, uv_utils, selected_keys):
    bm, uv_layer, by_key = _island_state(obj, island_tools)
    selected_keys = _keys(selected_keys)
    missing = sorted(set(selected_keys) - set(by_key))
    if missing:
        raise HarnessError("native selection keys missing: %s" % (missing,))
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(bm, uv_layer, [by_key[key] for key in selected_keys])
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return _island_state(obj, island_tools)


def _restore(obj, baseline_uv, baseline_selection):
    COMMON.activate_object(obj)
    bm = bmesh.from_edit_mesh(obj.data)
    COMMON.setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("dedicated UV layer disappeared")
    COMMON.restore_uv(bm, uv_layer, baseline_uv)
    COMMON.restore_selection(bm, uv_layer, baseline_selection)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


def _loop_keys(by_key, selected_keys):
    return set().union(
        *(set(COMMON.island_loop_keys(by_key[key])) for key in selected_keys)
    )


def _group_records(result):
    records = []
    for group in result.get("groups", []):
        records.append(
            {
                "master_key": _key(group.get("master_key", ())),
                "member_keys": tuple(_key(key) for key in group.get("member_keys", ())),
            }
        )
    return records


def _accepted_keys(groups):
    return set().union(*(set(group["member_keys"]) for group in groups)) if groups else set()


def _sync_case(obj, island_tools, uv_utils, stack_tools, selected_keys, allow_flipping):
    bm, uv_layer, by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    settings = bpy.context.scene.uv_gpt_settings
    settings.stack_allow_flipping = bool(allow_flipping)
    before_active = COMMON.snapshot_active(obj, bm)
    started = time.perf_counter()
    result = stack_tools.run_align_similar_pro(
        {
            "bpy_context": bpy.context,
            "detail_mappings": True,
            "correspondence_mode": CORRESPONDENCE_MODE,
            "mode": CORRESPONDENCE_MODE,
        }
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    bm, uv_layer, by_key = _island_state(obj, island_tools)
    exact_uv = COMMON.snapshot_uv(bm, uv_layer)
    exact_selection = COMMON.snapshot_selection(bm, uv_layer)
    exact_active = COMMON.snapshot_active(obj, bm)
    if result.get("operator_result") != ["FINISHED"]:
        raise HarnessError("synchronous oracle did not finish: %s" % result)
    if exact_selection != baseline_selection or exact_active != before_active:
        raise HarnessError("synchronous oracle changed selection or active state")
    return {
        "object": obj.name,
        "selected_keys": [list(key) for key in _keys(selected_keys)],
        "allow_flipping": bool(allow_flipping),
        "baseline_uv": baseline_uv,
        "baseline_selection": baseline_selection,
        "baseline_active": before_active,
        "by_key": by_key,
        "sync": result,
        "sync_elapsed_ms": elapsed_ms,
        "sync_result_digest": _result_digest(result),
        "sync_uv_digest": _uv_digest(exact_uv),
        "exact_uv": exact_uv,
        "exact_selection": exact_selection,
        "exact_active": exact_active,
        "groups": _group_records(result),
    }


def _native_run(
    case,
    obj,
    island_tools,
    uv_utils,
    *,
    targets,
    mode,
    expected_targets,
    rejected_targets,
    paste_selection=None,
):
    """Run copy once, then paste to one target or a target set."""

    _restore(obj, case["baseline_uv"], case["baseline_selection"])
    bm, uv_layer, by_key = _select_keys(
        obj, island_tools, uv_utils, [case["groups"][0]["master_key"]]
    )
    copy_before_selection = COMMON.snapshot_selection(bm, uv_layer)
    copy_before_active = COMMON.snapshot_active(obj, bm)
    started = time.perf_counter()
    copy_info = _operator_info("copy", uv_utils)
    copy_elapsed_ms = (time.perf_counter() - started) * 1000.0

    paste_selection = tuple(paste_selection or targets)
    bm, uv_layer, by_key = _select_keys(
        obj, island_tools, uv_utils, paste_selection
    )
    paste_before_selection = COMMON.snapshot_selection(bm, uv_layer)
    paste_before_active = COMMON.snapshot_active(obj, bm)
    started = time.perf_counter()
    paste_info = _operator_info("paste", uv_utils)
    paste_elapsed_ms = (time.perf_counter() - started) * 1000.0

    bm, uv_layer, by_key = _island_state(obj, island_tools)
    native_uv = COMMON.snapshot_uv(bm, uv_layer)
    raw_selection = COMMON.snapshot_selection(bm, uv_layer)
    raw_active = COMMON.snapshot_active(obj, bm)
    target_loop_keys = _loop_keys(by_key, targets)
    expected_loop_keys = _loop_keys(by_key, expected_targets)
    rejected_loop_keys = _loop_keys(by_key, rejected_targets) if rejected_targets else set()
    native_to_exact_delta = _max_delta(case["exact_uv"], native_uv, expected_loop_keys)
    rejected_delta = _max_delta(case["baseline_uv"], native_uv, rejected_loop_keys)
    source_key = case["groups"][0]["master_key"]
    source_delta = _max_delta(
        case["baseline_uv"], native_uv, set(COMMON.island_loop_keys(by_key[source_key]))
    )
    unselected_delta = _max_delta(
        case["baseline_uv"],
        native_uv,
        set(case["baseline_uv"]) - target_loop_keys - set(COMMON.island_loop_keys(by_key[source_key])),
    )

    # The native operators intentionally leave their target selection active.
    # Restore the caller's state as a proposed atomic-wrapper cleanup, and keep
    # both raw and restored evidence so the lifecycle hazard is visible.
    _restore(obj, case["baseline_uv"], case["baseline_selection"])
    bm, uv_layer, _by_key = _island_state(obj, island_tools)
    restored_selection = COMMON.snapshot_selection(bm, uv_layer)
    restored_active = COMMON.snapshot_active(obj, bm)
    operator_finished = _is_finished(copy_info) and _is_finished(paste_info)
    semantic_ok = (
        operator_finished
        and native_to_exact_delta <= DELTA_TOLERANCE
        and rejected_delta <= SELECTION_TOLERANCE
        and source_delta <= SELECTION_TOLERANCE
        and unselected_delta <= SELECTION_TOLERANCE
        and restored_selection == case["baseline_selection"]
        and restored_active == case["baseline_active"]
    )
    return {
        "mode": mode,
        "targets": [list(key) for key in targets],
        "paste_selection": [list(key) for key in paste_selection],
        "expected_targets": [list(key) for key in expected_targets],
        "rejected_targets": [list(key) for key in rejected_targets],
        "copy": copy_info,
        "paste": paste_info,
        "copy_elapsed_ms": copy_elapsed_ms,
        "paste_elapsed_ms": paste_elapsed_ms,
        "copy_before_selection": copy_before_selection == case["baseline_selection"],
        "copy_before_active": copy_before_active == case["baseline_active"],
        "paste_before_selection": paste_before_selection == case["baseline_selection"],
        "paste_before_active": paste_before_active == case["baseline_active"],
        "raw_selection_unchanged": raw_selection == case["baseline_selection"],
        "raw_active_unchanged": raw_active == case["baseline_active"],
        "selection_restored": restored_selection == case["baseline_selection"],
        "active_restored": restored_active == case["baseline_active"],
        "operator_finished": operator_finished,
        "native_uv_digest": _uv_digest(native_uv),
        "native_to_exact_delta": native_to_exact_delta,
        "rejected_target_delta": rejected_delta,
        "source_delta": source_delta,
        "unselected_delta": unselected_delta,
        "semantic_match": semantic_ok,
    }


def _native_case(case, obj, island_tools, uv_utils):
    groups = case["groups"]
    accepted = _accepted_keys(groups)
    selected_keys = _keys(case["selected_keys"])
    accepted_runs = []
    bulk_runs = []
    for group in groups:
        master_key = group["master_key"]
        members = tuple(group["member_keys"])
        # _native_run takes the first group master.  Every fixture case has a
        # single Pro group; fail loudly if a future fixture invalidates that
        # dedicated assumption rather than silently testing the wrong source.
        if any(other["master_key"] != master_key for other in groups):
            raise HarnessError("dedicated case has multiple native masters")
        for member in members:
            accepted_runs.append(
                _native_run(
                    case,
                    obj,
                    island_tools,
                    uv_utils,
                    targets=(member,),
                    mode="individual",
                    expected_targets=(member,),
                    rejected_targets=_sorted_keys(
                        set(selected_keys) - {master_key, member}
                    ),
                )
            )
        bulk_runs.append(
            _native_run(
                case,
                obj,
                island_tools,
                uv_utils,
                targets=members,
                mode="bulk",
                expected_targets=members,
                rejected_targets=_sorted_keys(
                    set(selected_keys) - {master_key} - set(members)
                ),
                paste_selection=(master_key,) + members,
            )
        )

    negative = None
    if case["object"] == "PROExact" and not case["allow_flipping"]:
        master_key = groups[0]["master_key"] if groups else _key(case["selected_keys"][0])
        selected = selected_keys
        same_face_targets = []
        _bm, _uv, by_key = _island_state(obj, island_tools)
        source_faces = len(set(int(loop.face.index) for loop in by_key[master_key]))
        for key in selected:
            if key == master_key:
                continue
            face_count = len(set(int(loop.face.index) for loop in by_key[key]))
            if face_count == source_faces:
                same_face_targets.append(key)
        rejected = _sorted_keys(set(same_face_targets) - accepted)
        if not rejected:
            raise HarnessError("same-face-count negative case was not rejected by sync")
        negative = _native_run(
            case,
            obj,
            island_tools,
            uv_utils,
            targets=tuple(same_face_targets),
            mode="negative_same_face_count_bulk",
            expected_targets=tuple(accepted & set(same_face_targets)),
            rejected_targets=rejected,
            paste_selection=(master_key,) + tuple(same_face_targets),
        )
        # A native bulk paste that changes the exact-rejected target is the
        # concrete false-positive that disqualifies it as a Pro backend.
        negative["false_positive"] = negative["rejected_target_delta"] > SELECTION_TOLERANCE
        negative["semantic_match"] = False
    return {
        "accepted_target_keys": [list(key) for key in sorted(accepted)],
        "individual": accepted_runs,
        "bulk": bulk_runs,
        "negative_same_face_count": negative,
    }


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_safe(value.to_dict())
    return str(value)


def main_work():
    if not FIXTURE_PATH.is_file():
        raise HarnessError("dedicated fixture is missing")
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if fixture_sha_before != EXPECTED_FIXTURE_SHA:
        raise HarnessError("dedicated fixture SHA mismatch: %s" % fixture_sha_before)
    if FIXTURE_SHA_BEFORE_EXTERNAL and fixture_sha_before != FIXTURE_SHA_BEFORE_EXTERNAL:
        raise HarnessError("external fixture SHA mismatch: %s" % fixture_sha_before)

    uv_gpt = COMMON.import_addon()
    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        cases = (
            ("PROExact", ((0,), (1,), (2,), (3,)), False),
            ("PROExact", ((0,), (1,), (2,), (3,)), True),
            ("PROHole", ((0, 1, 2, 3), (4, 5, 6, 7)), False),
            ("PROInterior", ((0, 1, 2, 3), (4, 5, 6, 7)), False),
            (
                "PROSeam",
                ((0, 1, 2, 3, 4, 5, 6, 7, 8), (9, 10, 11, 12, 13, 14, 15, 16, 17)),
                False,
            ),
            ("PRONonIso", ((0,), (1, 2)), False),
        )
        case_results = []
        for object_name, selected_keys, allow_flipping in cases:
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                raise HarnessError("dedicated object missing: %s" % object_name)
            case = _sync_case(
                obj,
                island_tools,
                uv_utils,
                stack_tools,
                selected_keys,
                allow_flipping,
            )
            case["native"] = _native_case(case, obj, island_tools, uv_utils)
            case_results.append(case)

        observed = tuple(int(case["sync"].get("aligned_exact", 0)) for case in case_results)
        if observed != EXPECTED_COUNTS:
            raise HarnessError("six-case synchronous oracle mismatch: %s" % (observed,))
        mismatches = []
        for index, case in enumerate(case_results):
            for run in case["native"]["individual"] + case["native"]["bulk"]:
                if not run["semantic_match"]:
                    mismatches.append({"case": index, "mode": run["mode"], "run": run})
            negative = case["native"].get("negative_same_face_count")
            if negative is not None:
                mismatches.append(
                    {
                        "case": index,
                        "mode": negative["mode"],
                        "reason": "same-face-count rejected target was not preserved by native paste",
                        "run": negative,
                    }
                )
        fixture_sha_after = sha256_file(FIXTURE_PATH)
        if fixture_sha_after != fixture_sha_before:
            raise HarnessError("dedicated fixture SHA changed in process")
        return {
            "status": "rejected" if mismatches else "passed",
            "decision": "reject_native_backend" if mismatches else "native_backend_feasible",
            "packet": PACKET_ID,
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha_before,
            "fixture_sha256_after_in_process": fixture_sha_after,
            "blender_version": list(bpy.app.version),
            "correspondence_mode": CORRESPONDENCE_MODE,
            "oracle_aligned_exact": list(observed),
            "cases": case_results,
            "semantic_mismatch_count": len(mismatches),
            "semantic_mismatches": mismatches,
        }
    finally:
        uv_gpt.unregister()


def main():
    result = {
        "status": "failed",
        "decision": "harness_failure",
        "packet": PACKET_ID,
        "fixture": str(FIXTURE_PATH),
        "worker_started": False,
        "writes": 0,
    }
    exit_code = 0
    try:
        result.update(main_work())
    except Exception as exc:
        exit_code = 1
        result.update(
            {
                "error": "%s: %s" % (type(exc).__name__, str(exc)),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        try:
            result["fixture_sha256_final"] = sha256_file(FIXTURE_PATH) if FIXTURE_PATH.is_file() else None
        except Exception as exc:
            result["fixture_sha256_final_error"] = str(exc)
        try:
            RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULT_PATH.write_text(
                json.dumps(_json_safe(result), indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception:
            traceback.print_exc()
        if result.get("status") == "rejected":
            print(
                "MC4-R1N native paste oracle completed: decision=reject; "
                "oracle=%s; mismatches=%s; result=%s"
                % (result.get("oracle_aligned_exact"), result.get("semantic_mismatch_count"), RESULT_PATH)
            )
        elif result.get("status") == "passed":
            print(
                "MC4-R1N native paste oracle completed: decision=pass; result=%s"
                % RESULT_PATH
            )
        else:
            print("MC4-R1N native paste oracle failed: %s; result=%s" % (result.get("error"), RESULT_PATH))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
