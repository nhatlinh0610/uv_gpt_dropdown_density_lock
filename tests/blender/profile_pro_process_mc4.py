"""One fresh-process MC4 benchmark run on the authoritative cc.blend fixture.

The PowerShell runner owns process sampling and orchestration.  This profile
only opens the fixture in Blender 5.0 memory, runs one explicitly requested
MC3B configuration, validates the UV/state oracle, and emits one JSON record
under the operating-system TEMP directory.  It never saves the blend.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
import traceback


sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "MC4-REAL-CC-BENCHMARK"
EXPECTED_FIXTURE_SHA = (
    "49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 577
SEED = "mc4-canonical-planner-order-v1"
SELECTION_EPSILON = 1.0e-10


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
PROJECT_ROOT = Path(
    _arg_value("--project-root", str(SCRIPT_PATH.parents[2]))
).resolve()
FIXTURE_PATH = Path(
    _arg_value("--fixture", r"C:\Users\linhp\Downloads\cc.blend")
).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(Path(__import__("tempfile").gettempdir()) / "uv_gpt_mc4_result.json"),
    )
).resolve()
WORKER_COUNT = int(_arg_value("--worker-count", "1"))
BATCH_SIZE = int(_arg_value("--batch-size", "64"))
RUN_CLASS = str(_arg_value("--run-class", "measured"))
RUN_ID = str(_arg_value("--run-id", "mc4-run"))
SUPERSEDES_RUN_ID = str(_arg_value("--supersedes-run-id", ""))
TIME_BUDGET_MS = float(_arg_value("--time-budget-ms", "180000"))
SCENARIO = str(_arg_value("--scenario", "complete"))
DIAGNOSTIC_MAX_MS = float(_arg_value("--diagnostic-max-ms", "45000"))
PROCESS_FUSED = bool(int(_arg_value("--process-fused", "0")))
PROCESS_GROUP_FIRST = bool(int(_arg_value("--process-group-first", "0")))
CORRESPONDENCE_MODE = "EXACT_ONLY"


def _load_common():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("mc4_common", path)
    if spec is None or spec.loader is None:
        raise HarnessError("unable to load shared Blender harness: %s" % path)
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


NONFINITE_TOKEN_KEY = "__float__"


def _path_for_key(path, key):
    return '%s[%s]' % (path, json.dumps(str(key), ensure_ascii=True, separators=(",", ":")))


def _path_for_index(path, index):
    return "%s[%d]" % (path, index)


def _nonfinite_token(value):
    if math.isnan(value):
        return "nan"
    if value > 0.0:
        return "pos_inf"
    return "neg_inf"


def _canonical(value, path="$", nonfinite_paths=None):
    if nonfinite_paths is None:
        nonfinite_paths = []
    if isinstance(value, dict):
        return {
            str(key): _canonical(item, _path_for_key(path, key), nonfinite_paths)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [
            _canonical(item, _path_for_index(path, index), nonfinite_paths)
            for index, item in enumerate(value)
        ]
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        token = _nonfinite_token(value)
        nonfinite_paths.append(
            {"path": path, "type": "float", "token": token}
        )
        return {NONFINITE_TOKEN_KEY: token}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (set, frozenset)):
        ordered = sorted(value, key=lambda item: repr(item))
        return _canonical(ordered, path, nonfinite_paths)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return _canonical(value.to_dict(), path, nonfinite_paths)
    if hasattr(value, "__dict__"):
        return _canonical(vars(value), path, nonfinite_paths)
    return str(value)


def _unique_nonfinite_paths(paths):
    unique = {}
    for item in paths:
        if not isinstance(item, dict):
            continue
        normalized = {
            "path": str(item.get("path", "$")),
            "type": str(item.get("type", "float")),
            "token": str(item.get("token", "")),
        }
        key = (normalized["path"], normalized["type"], normalized["token"])
        unique[key] = normalized
    return [unique[key] for key in sorted(unique)]


def _digest(value, path="$", nonfinite_paths=None):
    payload = json.dumps(
        _canonical(value, path=path, nonfinite_paths=nonfinite_paths),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def _source_hashes():
    names = (
        "uv_gpt/__init__.py",
        "uv_gpt/stack_tools.py",
        "uv_gpt/pro_process_protocol.py",
        "uv_gpt/pro_process_payload.py",
        "uv_gpt/pro_process_worker.py",
        "uv_gpt/pro_process_runtime.py",
        "uv_gpt/pro_process_pool.py",
        "uv_gpt/pro_process_adapter.py",
        "uv_gpt/pro_process_shape.py",
        "uv_gpt/pro_process_pipeline.py",
        "uv_gpt/pro_group_first.py",
        "uv_gpt/pro_verified_nearest.py",
    )
    hashes = {}
    for name in names:
        path = PROJECT_ROOT / name
        if path.is_file():
            hashes[name] = sha256_file(path)
    return hashes


def _uv_digest(snapshot):
    return _digest(
        [(list(key), [float(value[0]), float(value[1])]) for key, value in sorted(snapshot.items())]
    )


def _result_digest(result, nonfinite_paths=None):
    return _digest(
        {
            "aligned_exact": result.get("aligned_exact", 0),
            "group_count": result.get("group_count", 0),
            "groups": result.get("groups", []),
            "topology_rejections": result.get("topology_rejections", []),
        },
        path="$.result_digest_input",
        nonfinite_paths=nonfinite_paths,
    )


def _mapping_digest(result):
    mappings = []
    for group in result.get("groups", []):
        master_key = list(group.get("master_key", []))
        for mapping_pairs in group.get("mapping_pairs", []):
            for candidate_key, source_key in mapping_pairs:
                mappings.append(
                    {
                        "candidate": list(candidate_key),
                        "source": list(source_key),
                        "group_master": master_key,
                    }
                )
    mappings.sort(key=lambda item: (item["candidate"], item["source"], item["group_master"]))
    return _digest(mappings)


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


def _mapping_delta(result, before, after):
    maximum = 0.0
    for group in result.get("groups", []):
        for mapping_pairs in group.get("mapping_pairs", []):
            for candidate_key, source_key in mapping_pairs:
                candidate_key = tuple(candidate_key)
                source_key = tuple(source_key)
                if source_key not in before or candidate_key not in after:
                    raise HarnessError("mapping references an unknown UV loop")
                maximum = max(
                    maximum,
                    abs(float(before[source_key][0]) - float(after[candidate_key][0])),
                    abs(float(before[source_key][1]) - float(after[candidate_key][1])),
                )
    return maximum


def _process_request():
    return {
        "bpy_context": bpy.context,
        "detail_mappings": True,
        "process_worker_count": WORKER_COUNT,
        "process_batch_size": BATCH_SIZE,
        "process_fused": PROCESS_FUSED,
        "process_group_first": PROCESS_GROUP_FIRST,
        "correspondence_mode": CORRESPONDENCE_MODE,
        "mode": CORRESPONDENCE_MODE,
        "process_blender_binary": str(Path(bpy.app.binary_path).resolve()),
        "process_blender_version": tuple(bpy.app.version),
        "time_budget_ms": TIME_BUDGET_MS,
    }


def _prepare_full(obj, island_tools, uv_utils):
    settings = bpy.context.scene.uv_gpt_settings
    settings.duplicate_before_operations = False
    settings.active_uv_map = TARGET_UV_NAME
    COMMON.activate_object(obj)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    if len(islands) != EXPECTED_ISLAND_COUNT:
        raise HarnessError("expected %d islands, got %d" % (EXPECTED_ISLAND_COUNT, len(islands)))
    by_key = {COMMON.face_key(island): island for island in islands}
    baseline_uv = COMMON.snapshot_uv(bm, uv_layer)
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(bm, uv_layer, islands)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    return (
        bm,
        uv_layer,
        islands,
        by_key,
        baseline_uv,
        COMMON.snapshot_selection(bm, uv_layer),
        COMMON.snapshot_active(obj, bm),
    )


def _run_complete(uv_gpt, island_tools, uv_utils, stack_tools, obj):
    started = time.perf_counter()
    harness_wall_ms = (time.perf_counter() - started) * 1000.0
    result = None
    bm = None
    uv_layer = None
    islands = []
    by_key = {}
    baseline_uv = {}
    before_selection = None
    before_active = None
    after_uv = None
    after_selection = None
    after_active = None
    fixture_sha_before = None
    fixture_sha_after = None
    errors = []
    nonfinite_paths = []

    def capture_error(phase, exc):
        errors.append(
            {
                "phase": phase,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )

    try:
        fixture_sha_before = sha256_file(FIXTURE_PATH)
        (
            bm,
            uv_layer,
            islands,
            by_key,
            baseline_uv,
            before_selection,
            before_active,
        ) = _prepare_full(obj, island_tools, uv_utils)
        result = stack_tools.run_align_similar_pro(_process_request())
    except Exception as exc:
        capture_error("operation", exc)
    harness_wall_ms = (time.perf_counter() - started) * 1000.0

    if result is not None:
        try:
            bm = island_tools.get_active_bmesh(bpy.context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            after_uv = COMMON.snapshot_uv(bm, uv_layer)
            after_selection = COMMON.snapshot_selection(bm, uv_layer)
            after_active = COMMON.snapshot_active(obj, bm)
        except Exception as exc:
            capture_error("post_operation_snapshot", exc)
    try:
        fixture_sha_after = sha256_file(FIXTURE_PATH)
    except Exception as exc:
        capture_error("fixture_hash_after", exc)

    canonical_result = {}
    if result is not None:
        try:
            canonical_result = _canonical(result, path="$.result", nonfinite_paths=nonfinite_paths)
        except Exception as exc:
            capture_error("result_canonicalization", exc)
            canonical_result = {"__error__": str(exc)}
    else:
        canonical_result = {"__error__": "operation did not return a session report"}

    def safe_digest(label, callback):
        try:
            return callback()
        except Exception as exc:
            capture_error(label, exc)
            return None

    master_loop_keys = set()
    if isinstance(result, dict):
        for group in result.get("groups", []):
            master_key = tuple(group.get("master_key", ()))
            island = by_key.get(master_key)
            if island is not None:
                master_loop_keys.update(COMMON.island_loop_keys(island))

    mapping_delta = None
    if isinstance(result, dict) and baseline_uv and after_uv is not None:
        mapping_delta = safe_digest(
            "mapping_validation",
            lambda: _mapping_delta(result, baseline_uv, after_uv),
        )
    result_digest = None
    group_digest = None
    mapping_digest = None
    uv_digest = None
    baseline_uv_digest = None
    if isinstance(result, dict):
        result_digest = safe_digest(
            "result_digest", lambda: _result_digest(result, nonfinite_paths)
        )
        group_digest = safe_digest(
            "group_digest",
            lambda: _digest(
                result.get("groups", []),
                path="$.groups",
                nonfinite_paths=nonfinite_paths,
            ),
        )
        mapping_digest = safe_digest(
            "mapping_digest", lambda: _mapping_digest(result)
        )
    if after_uv is not None:
        uv_digest = safe_digest("uv_digest", lambda: _uv_digest(after_uv))
    if baseline_uv:
        baseline_uv_digest = safe_digest(
            "baseline_uv_digest", lambda: _uv_digest(baseline_uv)
        )

    master_uv_delta = None
    unselected_uv_delta = None
    uv_changed_delta = None
    if baseline_uv and after_uv is not None:
        master_uv_delta = safe_digest(
            "master_uv_delta",
            lambda: _max_delta(baseline_uv, after_uv, master_loop_keys),
        )
        selected_loop_keys = set(baseline_uv)
        unselected_loop_keys = set(baseline_uv) - selected_loop_keys
        unselected_uv_delta = safe_digest(
            "unselected_uv_delta",
            lambda: _max_delta(baseline_uv, after_uv, unselected_loop_keys),
        )
        uv_changed_delta = safe_digest(
            "uv_changed_delta",
            lambda: _max_delta(baseline_uv, after_uv, set(baseline_uv)),
        )

    source_hashes = safe_digest("source_hashes", _source_hashes) or {}
    source_digest = safe_digest("source_digest", lambda: _digest(source_hashes))
    schema_version = None
    try:
        schema_version = getattr(
            importlib.import_module("uv_gpt.pro_process_payload"),
            "SCHEMA_VERSION",
            None,
        )
    except Exception as exc:
        capture_error("schema_version", exc)

    full_completion = bool(
        isinstance(result, dict)
        and result.get("operator_result") == ["FINISHED"]
        and not result.get("cancelled")
        and not result.get("error")
        and not result.get("partial")
        and not result.get("truncated")
    )
    validation_errors = []
    if result is not None:
        if fixture_sha_before != EXPECTED_FIXTURE_SHA:
            validation_errors.append("fixture SHA changed before operation")
        if fixture_sha_after != EXPECTED_FIXTURE_SHA:
            validation_errors.append("fixture SHA changed in Blender process")
        if before_selection is not None and after_selection is not None and before_selection != after_selection:
            validation_errors.append("selection/active state changed")
        if before_active is not None and after_active is not None and before_active != after_active:
            validation_errors.append("active state changed")
        if master_uv_delta is not None and master_uv_delta > SELECTION_EPSILON:
            validation_errors.append("master UV changed: %s" % master_uv_delta)
        if unselected_uv_delta is not None and unselected_uv_delta > SELECTION_EPSILON:
            validation_errors.append("unselected UV changed: %s" % unselected_uv_delta)
        if not full_completion:
            validation_errors.append("operation did not reach full completion")
    errors.extend(
        {
            "phase": "oracle_validation",
            "type": "HarnessError",
            "message": message,
        }
        for message in validation_errors
    )

    record = {
        "status": "passed" if result is not None and not errors else "failed",
        "run_id": RUN_ID,
        "run_class": RUN_CLASS,
        "supersedes_run_id": SUPERSEDES_RUN_ID or None,
        "scenario": SCENARIO,
        "correspondence_mode": CORRESPONDENCE_MODE,
        "seed": SEED,
        "worker_count": WORKER_COUNT,
        "batch_size": BATCH_SIZE,
        "time_budget_ms": TIME_BUDGET_MS,
        "fixture": str(FIXTURE_PATH),
        "fixture_sha256_before_in_process": fixture_sha_before,
        "fixture_sha256_after_in_process": fixture_sha_after,
        "object": TARGET_OBJECT_NAME,
        "uv_map": TARGET_UV_NAME,
        "island_count": len(islands),
        "source_hashes": source_hashes,
        "source_digest": source_digest,
        "schema_version": schema_version,
        "harness_wall_ms": harness_wall_ms,
        "result": canonical_result,
        "session_report": canonical_result,
        "result_digest": result_digest,
        "group_digest": group_digest,
        "mapping_digest": mapping_digest,
        "uv_digest": uv_digest,
        "baseline_uv_digest": baseline_uv_digest,
        "master_uv_delta": master_uv_delta,
        "unselected_uv_delta": unselected_uv_delta,
        "mapping_max_delta": mapping_delta,
        "uv_changed_delta": uv_changed_delta,
        "selection_unchanged": before_selection == after_selection,
        "active_unchanged": before_active == after_active,
        "full_completion": full_completion,
        "nonfinite_paths": _unique_nonfinite_paths(nonfinite_paths),
        "validation_errors": validation_errors,
        "errors": errors,
        "traceback": "\n\n".join(item.get("traceback", "") for item in errors if item.get("traceback")),
    }
    if isinstance(result, dict):
        record["oracles"] = {
            "planner_record_count": result.get("planner_record_count", 0),
            "candidate_pairs_planned": result.get("candidate_pairs_planned", 0),
            "candidate_pairs_processed": result.get("candidate_pairs_processed", 0),
            "aligned_exact": result.get("aligned_exact", 0),
            "group_count": result.get("group_count", 0),
        }
    else:
        record["oracles"] = {}
    if errors:
        record["error"] = "; ".join(item["message"] for item in errors if item.get("message"))
    elif isinstance(result, dict) and result.get("error"):
        record["error"] = str(result.get("error"))

    if not full_completion and isinstance(result, dict):
        failure_summary = {
            key: result.get(key)
            for key in (
                "operator_result", "elapsed_ms", "error", "session_state",
                "candidate_pairs_planned", "candidate_pairs_processed",
                "aligned_exact", "group_count", "truncated", "partial",
                "process_stage", "process_shape_prefiltered",
                "process_shape_pairs_submitted", "process_shape_pairs_completed",
                "process_shape_accepted", "process_shape_rejected",
                "process_exact_pairs_submitted", "process_exact_pairs_completed",
                "process_resident_exact_batches_submitted",
                "process_resident_exact_batches_completed",
                "process_resident_graph_cache_builds", "process_resident_graph_cache_hits",
                "process_resident_graph_compute_ms", "process_resident_exact_compute_ms",
                "process_resident_exact_frame_bytes",
                "process_pruned_pairs", "process_exact_started_before_shape_terminal",
                "process_last_progress_kind",
                "process_poll_calls", "process_no_progress_loops",
                "process_event_epoch", "process_graph_event_epoch",
                "process_graph_waiter_registrations", "process_graph_waiter_dedup",
                "process_worker_pids", "process_stage_distributions",
                "process_frame_bytes", "process_snapshot_checks",
                "process_snapshot_forced_checks", "process_poll_ms",
                "process_startup_ms", "process_retry_count", "worker_errors",
                "max_tick_ms", "tick_p95_ms", "tick_p99_ms",
            )
        }
        record["failure"] = _canonical(
            failure_summary, path="$.failure", nonfinite_paths=nonfinite_paths
        )
    return record
    master_loop_keys = set()
    for group in result.get("groups", []):
        master_key = tuple(group.get("master_key", ()))
        island = by_key.get(master_key)
        if island is not None:
            master_loop_keys.update(COMMON.island_loop_keys(island))
    unselected_loop_keys = set(baseline_uv) - selected_loop_keys
    mapping_delta = _mapping_delta(result, baseline_uv, after_uv)
    result_digest = _result_digest(result)
    record = {
        "status": "passed",
        "run_id": RUN_ID,
        "run_class": RUN_CLASS,
        "scenario": SCENARIO,
        "correspondence_mode": CORRESPONDENCE_MODE,
        "seed": SEED,
        "worker_count": WORKER_COUNT,
        "batch_size": BATCH_SIZE,
        "time_budget_ms": TIME_BUDGET_MS,
        "fixture": str(FIXTURE_PATH),
        "fixture_sha256_before_in_process": sha256_file(FIXTURE_PATH),
        "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
        "object": TARGET_OBJECT_NAME,
        "uv_map": TARGET_UV_NAME,
        "island_count": len(islands),
        "source_hashes": _source_hashes(),
        "source_digest": _digest(_source_hashes()),
        "schema_version": getattr(importlib.import_module("uv_gpt.pro_process_payload"), "SCHEMA_VERSION", None),
        "harness_wall_ms": harness_wall_ms,
        "result": COMMON.clean_json(result),
        "result_digest": result_digest,
        "group_digest": _digest(result.get("groups", [])),
        "mapping_digest": _mapping_digest(result),
        "uv_digest": _uv_digest(after_uv),
        "baseline_uv_digest": _uv_digest(baseline_uv),
        "master_uv_delta": _max_delta(baseline_uv, after_uv, master_loop_keys),
        "unselected_uv_delta": _max_delta(baseline_uv, after_uv, unselected_loop_keys),
        "mapping_max_delta": mapping_delta,
        "uv_changed_delta": _max_delta(baseline_uv, after_uv, set(baseline_uv)),
        "selection_unchanged": before_selection == after_selection,
        "active_unchanged": before_active == after_active,
        "full_completion": bool(
            result.get("operator_result") == ["FINISHED"]
            and not result.get("cancelled")
            and not result.get("error")
            and not result.get("partial")
            and not result.get("truncated")
        ),
        "oracles": {
            "planner_record_count": result.get("planner_record_count", 0),
            "candidate_pairs_planned": result.get("candidate_pairs_planned", 0),
            "candidate_pairs_processed": result.get("candidate_pairs_processed", 0),
            "aligned_exact": result.get("aligned_exact", 0),
            "group_count": result.get("group_count", 0),
        },
    }
    if record["fixture_sha256_before_in_process"] != EXPECTED_FIXTURE_SHA:
        raise HarnessError("fixture SHA changed before operation")
    if record["fixture_sha256_after_in_process"] != EXPECTED_FIXTURE_SHA:
        raise HarnessError("fixture SHA changed in Blender process")
    if not record["selection_unchanged"] or not record["active_unchanged"]:
        raise HarnessError("selection/active state changed")
    if record["master_uv_delta"] > SELECTION_EPSILON:
        raise HarnessError("master UV changed: %s" % record["master_uv_delta"])
    if record["unselected_uv_delta"] > SELECTION_EPSILON:
        raise HarnessError("unselected UV changed: %s" % record["unselected_uv_delta"])
    if not record["full_completion"]:
        failure_summary = {
            key: result.get(key)
            for key in (
                "operator_result", "elapsed_ms", "error", "session_state",
                "candidate_pairs_planned", "candidate_pairs_processed",
                "aligned_exact", "group_count", "truncated", "partial",
                "process_stage", "process_shape_prefiltered",
                "process_shape_pairs_submitted", "process_shape_pairs_completed",
                "process_shape_accepted", "process_shape_rejected",
                "process_exact_pairs_submitted", "process_exact_pairs_completed",
                "process_resident_exact_batches_submitted",
                "process_resident_exact_batches_completed",
                "process_resident_graph_cache_builds", "process_resident_graph_cache_hits",
                "process_resident_graph_compute_ms", "process_resident_exact_compute_ms",
                "process_resident_exact_frame_bytes",
                "process_pruned_pairs", "process_exact_started_before_shape_terminal",
                "process_last_progress_kind",
                "process_poll_calls", "process_no_progress_loops",
                "process_event_epoch", "process_graph_event_epoch",
                "process_graph_waiter_registrations", "process_graph_waiter_dedup",
                "process_worker_pids", "process_stage_distributions",
                "process_frame_bytes", "process_snapshot_checks",
                "process_snapshot_forced_checks", "process_poll_ms",
                "process_startup_ms", "process_retry_count", "worker_errors",
                "max_tick_ms", "tick_p95_ms", "tick_p99_ms",
            )
        }
        record["status"] = "failed"
        record["failure"] = COMMON.clean_json(failure_summary)
    return record


def _run_diagnostic(island_tools, uv_utils, stack_tools, obj):
    """Bounded one-shot diagnosis for a pre-handshake full-fixture stall."""

    (
        _bm,
        _uv_layer,
        islands,
        _by_key,
        _baseline_uv,
        _before_selection,
        _before_active,
    ) = _prepare_full(obj, island_tools, uv_utils)
    session = stack_tools._pro_create_session(
        bpy.context,
        {"detail_mappings": True},
        modal=False,
        process_worker_count=WORKER_COUNT,
        process_batch_size=BATCH_SIZE,
        process_blender_binary=str(Path(bpy.app.binary_path).resolve()),
        process_blender_version=tuple(bpy.app.version),
        process_fused=PROCESS_FUSED,
        process_group_first=PROCESS_GROUP_FIRST,
        correspondence_mode=CORRESPONDENCE_MODE,
        mode=CORRESPONDENCE_MODE,
        time_budget_ms=TIME_BUDGET_MS,
    )
    started = time.perf_counter()
    steps = 0
    last_marker = None
    while not session.done and (time.perf_counter() - started) * 1000.0 < DIAGNOSTIC_MAX_MS:
        session.step(active_budget_ms=12.0, max_correspondence=1)
        steps += 1
        marker = (
            session.state,
            len(getattr(session, "_planner_records", ())),
            len(getattr(session, "_process_pair_contexts", {})),
            len(getattr(session, "_process_shape_batches", ())),
            tuple(getattr(session, "_process_started_pids", ())),
        )
        if marker != last_marker and (steps <= 5 or steps % 100 == 0):
            print(
                "MC4 diagnostic step=%d state=%s records=%d contexts=%d shape_batches=%d pids=%s report=%s"
                % (
                    steps,
                    session.state,
                    marker[1],
                    marker[2],
                    marker[3],
                    marker[4],
                    {
                        "planned": session.report.get("candidate_pairs_planned", 0),
                        "processed": session.report.get("candidate_pairs_processed", 0),
                        "stage": session.report.get("process_stage"),
                        "enum_ops": session.report.get("enum_primitive_ops", 0),
                        "record_ms": session.report.get("planner_record_build_ms", 0.0),
                    },
                )
            )
            last_marker = marker
    if not session.done:
        session.cancel("mc4_diagnostic_bound")
    report = COMMON.clean_json(session.report)
    return {
        "status": "diagnostic",
        "run_id": RUN_ID,
        "run_class": RUN_CLASS,
        "scenario": SCENARIO,
        "correspondence_mode": CORRESPONDENCE_MODE,
        "worker_count": WORKER_COUNT,
        "batch_size": BATCH_SIZE,
        "time_budget_ms": TIME_BUDGET_MS,
        "diagnostic_max_ms": DIAGNOSTIC_MAX_MS,
        "steps": steps,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "island_count": len(islands),
        "session_state": session.state,
        "session_report": report,
        "process_worker_pids": list(getattr(session, "_process_started_pids", ())),
        "full_completion": False,
    }


def main():
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if fixture_sha_before != EXPECTED_FIXTURE_SHA:
        raise HarnessError("fixture SHA mismatch: %s" % fixture_sha_before)
    if FIXTURE_SHA_BEFORE_EXTERNAL and fixture_sha_before != FIXTURE_SHA_BEFORE_EXTERNAL:
        raise HarnessError("external fixture SHA mismatch: %s" % fixture_sha_before)
    if WORKER_COUNT < 1 or WORKER_COUNT > 8:
        raise HarnessError("worker_count must be in 1..8")
    allowed_batches = (16, 32, 64, 96) if PROCESS_FUSED else (32, 64, 96)
    if BATCH_SIZE not in allowed_batches:
        raise HarnessError(
            "batch_size must be one of %s for process_fused=%s"
            % (allowed_batches, PROCESS_FUSED)
        )
    uv_gpt = None
    registered = False
    record = None
    primary_error = None
    try:
        uv_gpt = COMMON.import_addon()
        # Mark before calling register so a partial registration still follows
        # the package's idempotent cleanup path in finally.
        registered = True
        uv_gpt.register()
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH":
            raise HarnessError("benchmark object missing: %s" % TARGET_OBJECT_NAME)
        if SCENARIO == "diagnostic":
            record = _run_diagnostic(island_tools, uv_utils, stack_tools, obj)
        elif SCENARIO == "complete":
            record = _run_complete(uv_gpt, island_tools, uv_utils, stack_tools, obj)
        else:
            raise HarnessError("unsupported MC4 profile scenario: %s" % SCENARIO)
    except Exception as exc:
        primary_error = exc
    finally:
        if registered and uv_gpt is not None:
            try:
                uv_gpt.unregister()
            except Exception as exc:
                if primary_error is None:
                    primary_error = exc

    fixture_sha_after = sha256_file(FIXTURE_PATH)
    if record is None:
        record = {
            "status": "failed",
            "run_id": RUN_ID,
            "run_class": RUN_CLASS,
            "scenario": SCENARIO,
            "correspondence_mode": CORRESPONDENCE_MODE,
        "worker_count": WORKER_COUNT,
        "batch_size": BATCH_SIZE,
        "process_fused": PROCESS_FUSED,
        "process_group_first": PROCESS_GROUP_FIRST,
        "time_budget_ms": TIME_BUDGET_MS,
            "error": str(primary_error or "MC4 harness did not produce a run record"),
            "full_completion": False,
            "nonfinite_paths": [],
        }
    if primary_error is not None:
        record["status"] = "failed"
        record["error"] = str(primary_error)
        record["full_completion"] = False
        record.setdefault("errors", []).append(
            {
                "phase": "package_cleanup",
                "type": type(primary_error).__name__,
                "message": str(primary_error),
                "traceback": "",
            }
        )
    output_paths = []
    canonical_record = _canonical(record, path="$.run", nonfinite_paths=output_paths)
    output_nonfinite_paths = _unique_nonfinite_paths(
        list(record.get("nonfinite_paths", [])) + output_paths
    )
    if isinstance(canonical_record, dict):
        canonical_record["nonfinite_paths"] = output_nonfinite_paths
    output_status = "passed"
    if not (
        record.get("status") == "diagnostic"
        or (record.get("status") == "passed" and record.get("full_completion") is True)
    ):
        output_status = "failed"
    output = {
        "status": output_status,
        "packet": PACKET_ID,
        "run": canonical_record,
        "nonfinite_paths": output_nonfinite_paths,
        "error": record.get("error") if output_status == "failed" else None,
        "fixture_sha256_before": fixture_sha_before,
        "fixture_sha256_after_in_process": fixture_sha_after,
    }
    RESULT_PATH.write_text(
        json.dumps(output, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    if fixture_sha_after != EXPECTED_FIXTURE_SHA:
        raise HarnessError("fixture SHA changed before result emission")
    if output_status != "passed":
        print(
            "MC4 run failed: worker=%d batch=%d stage=%s error=%s"
            % (
                WORKER_COUNT,
                BATCH_SIZE,
                record.get("result", record).get("process_stage", record.get("session_report", {}).get("process_stage", "unknown"))
                if isinstance(record.get("result", record), dict)
                else "unknown",
                record.get("error", record.get("failure", "incomplete operation")),
            )
        )
        if primary_error is not None:
            raise primary_error
        raise HarnessError("run was not a complete successful operation")
    print(
        "MC4 run passed: worker=%d batch=%d wall_ms=%.3f pairs=%s exact=%s groups=%s max_tick=%.3f p95=%.3f"
        % (
            WORKER_COUNT,
            BATCH_SIZE,
            record.get("harness_wall_ms", record.get("elapsed_ms", 0.0)),
            record.get("oracles", {}).get("candidate_pairs_planned", 0),
            record.get("oracles", {}).get("aligned_exact", 0),
            record.get("oracles", {}).get("group_count", 0),
            record.get("result", {}).get("max_tick_ms", record.get("session_report", {}).get("max_tick_ms", 0.0)),
            record.get("result", {}).get("tick_p95_ms", record.get("session_report", {}).get("tick_p95_ms", 0.0)),
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("MC4 run failed: %s" % exc)
        raise SystemExit(1)
