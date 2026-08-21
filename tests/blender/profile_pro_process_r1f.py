"""Focused MC4-R1F live fused oracle on the disposable six-case fixture.

This harness is deliberately separate from MC3B: it opts into the fused
worker path explicitly and compares the same in-memory case against the
existing synchronous Pro result before restoring the case baseline.
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


PACKET_ID = "MC4-R1F-LIVE-FUSED"
EXPECTED_FIXTURE_SHA = "EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8"
EXPECTED_COUNTS = (2, 3, 1, 1, 1, 0)
TARGET_UV_NAME = "UVMap.001"


class HarnessError(RuntimeError):
    pass


_LAST_RUN_DIAGNOSTICS = {}


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
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(Path(__import__("tempfile").gettempdir()) / "uv_gpt_r1f_result.json"),
    )
).resolve()
FIXTURE_SHA_BEFORE = _arg_value("--fixture-sha-before", "").upper()
WORKER_COUNT = int(_arg_value("--worker-count", "4"))
BATCH_SIZE = int(_arg_value("--batch-size", "32"))
CORRESPONDENCE_MODE = _arg_value("--mode", "HYBRID").strip().upper()
if CORRESPONDENCE_MODE not in {
    "HYBRID",
    "VERIFIED_NEAREST_ONLY",
    "EXACT_ONLY",
}:
    raise HarnessError("invalid correspondence mode: %s" % CORRESPONDENCE_MODE)


def _load_common():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("r1f_common", path)
    if spec is None or spec.loader is None:
        raise HarnessError("could not load dedicated harness helpers")
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


def _uv_digest(values):
    payload = repr(tuple(sorted((tuple(key), tuple(value)) for key, value in values.items())))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()


def _result_digest(result):
    # Rejection detail is diagnostic evidence, not an ownership/mapping
    # semantic.  The fused path intentionally stores compact ordinal/reason
    # negatives while the synchronous path may retain verbose keys.  Exclude
    # that representation detail so the A/B oracle compares the result that
    # can affect UV/group semantics.
    value = {
        "aligned_exact": result.get("aligned_exact", 0),
        "group_count": result.get("group_count", 0),
        "groups": result.get("groups", []),
        "skipped_topology_unproven": result.get("skipped_topology_unproven", 0),
    }
    return hashlib.sha256(
        json.dumps(COMMON.clean_json(value), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest().upper()


def _mapping_delta(result, before_uv, after_uv):
    maximum = 0.0
    for group in result.get("groups", []):
        for mapping_pairs in group.get("mapping_pairs", []):
            for candidate_key, master_key in mapping_pairs:
                candidate_key = tuple(candidate_key)
                master_key = tuple(master_key)
                expected = before_uv[master_key]
                actual = after_uv[candidate_key]
                maximum = max(
                    maximum,
                    abs(float(expected[0]) - float(actual[0])),
                    abs(float(expected[1]) - float(actual[1])),
                )
    return maximum


def _compact_run_diagnostics(result, selected_keys=()):
    fields = (
        "operator_result",
        "error",
        "session_state",
        "aligned_exact",
        "group_count",
        "candidate_pairs_planned",
        "candidate_pairs_processed",
        "process_graph_context_load_submitted",
        "process_graph_context_load_acked",
        "process_graph_context_ready",
        "process_fused_context_ready",
        "process_fused_context_digest",
        "process_fused_descriptor_count",
        "process_fused_batches_submitted",
        "process_fused_batches_completed",
        "process_fused_pairs_submitted",
        "process_fused_pairs_completed",
        "process_shape_pairs_submitted",
        "process_shape_pairs_completed",
        "process_exact_pairs_submitted",
        "process_exact_pairs_completed",
        "process_group_first",
        "process_group_first_stage",
        "grouping_comparisons_planned",
        "grouping_comparisons_completed",
        "shape_groups",
        "shape_singletons",
        "group_membership_digest",
        "uv_area_masters",
        "uv_area_master_areas",
        "uv_area_by_key",
        "direct_exact_jobs_planned",
        "direct_exact_jobs_completed",
        "direct_exact_jobs_failed",
        "exact_job_bound",
        "process_nearest_attempted",
        "process_nearest_accepted",
        "process_nearest_fallback",
        "process_nearest_seeded_jobs_planned",
        "process_nearest_seedless_jobs_planned",
        "process_nearest_fallback_exact_calls",
        "process_nearest_missing_seed_fallbacks",
        "process_fused_lower_bound_checked",
        "process_fused_lower_bound_rejected",
        "process_fused_lower_bound_skipped",
        "process_thread_caps",
        "process_stage",
        "max_tick_ms",
        "max_tick_stage",
        "tick_p95_ms",
        "tick_p99_ms",
        "worker_mode",
        "process_worker_pids",
    )
    diagnostics = {name: result.get(name) for name in fields}
    diagnostics["selected_keys"] = [list(key) for key in selected_keys]
    return diagnostics


def _request(*, fused=False):
    return {
        "bpy_context": bpy.context,
        "detail_mappings": True,
        "process_worker_count": WORKER_COUNT,
        "process_batch_size": BATCH_SIZE,
        "process_test_override": True,
        "process_fused": bool(fused),
        "process_group_first": bool(fused),
        "correspondence_mode": CORRESPONDENCE_MODE,
        "mode": CORRESPONDENCE_MODE,
        "process_blender_binary": str(Path(bpy.app.binary_path).resolve()),
        "process_blender_version": tuple(bpy.app.version),
        "time_budget_ms": 180000.0,
    }


def _independent_fast_oracle(expected_master, expected_aligned_exact, baseline_uv):
    """Use the dedicated fixture oracle; never invoke synchronous Fast."""

    return {
        "operator_result": ["ORACLE_ONLY"],
        "aligned_exact": int(expected_aligned_exact),
        "group_count": 1 if expected_master is not None else 0,
        "correspondence_mode": CORRESPONDENCE_MODE,
        "mode": CORRESPONDENCE_MODE,
        "oracle_source": "dedicated_fixture_uv_area_master",
        "expected_master": list(expected_master) if expected_master is not None else None,
        "baseline_uv_digest": _uv_digest(baseline_uv),
    }


def _run_case(
    obj,
    island_tools,
    uv_utils,
    stack_tools,
    selected_keys,
    expected_master,
    expected_aligned_exact,
    allow_flipping,
):
    global _LAST_RUN_DIAGNOSTICS
    bm, uv_layer, by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    # BMesh loop wrappers may be invalidated by the operator lifecycle.  Keep
    # the stable primitive loop-key tuples needed for post-run safety checks
    # before either oracle call can rebuild/update the active BMesh.
    loop_keys_by_key = {
        tuple(key): tuple(COMMON.island_loop_keys(island))
        for key, island in by_key.items()
    }
    # Keep the authoritative option identical for the explicit fused request.
    # Fast deliberately has no synchronous oracle: its expected count/master
    # comes from the dedicated fixture oracle below.
    bpy.context.scene.uv_gpt_settings.stack_allow_flipping = bool(allow_flipping)
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)

    if CORRESPONDENCE_MODE == "VERIFIED_NEAREST_ONLY":
        sync = _independent_fast_oracle(
            expected_master,
            expected_aligned_exact,
            baseline_uv,
        )
        _LAST_RUN_DIAGNOSTICS = {
            "correspondence_mode": CORRESPONDENCE_MODE,
            "oracle_source": sync["oracle_source"],
            "expected_master": sync["expected_master"],
            "expected_aligned_exact": int(expected_aligned_exact),
        }
        sync_uv = dict(baseline_uv)
        sync_digest = None
        sync_uv_digest = _uv_digest(sync_uv)
    else:
        sync = stack_tools.run_align_similar_pro(
            {
                "bpy_context": bpy.context,
                "detail_mappings": True,
                "correspondence_mode": CORRESPONDENCE_MODE,
                "mode": CORRESPONDENCE_MODE,
            }
        )
        _LAST_RUN_DIAGNOSTICS = _compact_run_diagnostics(sync, selected_keys)
        bm = island_tools.get_active_bmesh(bpy.context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        sync_uv = COMMON.snapshot_uv(bm, uv_layer)
        sync_digest = _result_digest(sync)
        sync_uv_digest = _uv_digest(sync_uv)
    _restore(obj, baseline_uv, baseline_selection)

    started = time.perf_counter()
    fused = stack_tools.run_align_similar_pro(_request(fused=True))
    _LAST_RUN_DIAGNOSTICS = _compact_run_diagnostics(fused, selected_keys)
    fused_wall_ms = (time.perf_counter() - started) * 1000.0
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    fused_uv = COMMON.snapshot_uv(bm, uv_layer)
    fused_digest = _result_digest(fused)
    fused_uv_digest = _uv_digest(fused_uv)

    if fused.get("operator_result") != ["FINISHED"]:
        raise HarnessError("fused case failed: %s" % fused)
    expected_group_count = 1 if expected_master is not None else 0
    if int(fused.get("aligned_exact", 0) or 0) != int(expected_aligned_exact):
        raise HarnessError(
            "fused independent count mismatch: got=%s expected=%s"
            % (fused.get("aligned_exact", 0), expected_aligned_exact)
        )
    if int(fused.get("group_count", 0) or 0) != expected_group_count:
        raise HarnessError(
            "fused independent group count mismatch: got=%s expected=%s"
            % (fused.get("group_count", 0), expected_group_count)
        )
    if not fused.get("process_fused") or fused.get("worker_mode") not in (
        "external_bundled_python_fused",
        "external_bundled_python_pipeline",
        "external_bundled_python_group_first",
    ):
        raise HarnessError("request did not use the explicit fused mode: %s" % fused)
    if not fused.get("process_group_first"):
        raise HarnessError("request did not use the explicit group-first mode")
    worker_pids = tuple(int(pid) for pid in (fused.get("process_worker_pids") or ()))
    if len(worker_pids) != WORKER_COUNT or len(set(worker_pids)) != WORKER_COUNT:
        raise HarnessError(
            "group-first worker PID coverage mismatch: expected=%s actual=%s"
            % (WORKER_COUNT, worker_pids)
        )
    thread_caps = fused.get("process_thread_caps") or {}
    if not thread_caps or any(str(value) != "1" for value in thread_caps.values()):
        raise HarnessError("worker thread caps are not all 1: %s" % thread_caps)
    positive_case = expected_master is not None and int(expected_aligned_exact) > 0
    if positive_case:
        if not fused.get("process_fused_context_ready"):
            raise HarnessError("fused context was not ACKed")
        if not fused.get("process_fused_batches_submitted") or not fused.get(
            "process_fused_batches_completed"
        ):
            raise HarnessError("fused worker did not complete a fused batch")
        if fused.get("process_graph_waiter_registrations", 0) != 0 or fused.get(
            "process_graph_worker_submitted", 0
        ) != 0:
            raise HarnessError("legacy graph waiter activity leaked into fused mode")
    if fused.get("process_shape_pairs_submitted") != fused.get(
        "process_shape_pairs_completed"
    ):
        raise HarnessError("fused shape coverage is incomplete")
    if fused.get("process_exact_pairs_submitted") != fused.get(
        "process_exact_pairs_completed"
    ):
        raise HarnessError("fused exact coverage is incomplete")
    direct_jobs = int(fused.get("direct_exact_jobs_planned", 0) or 0)
    direct_failed = int(fused.get("direct_exact_jobs_failed", 0) or 0)
    if direct_failed < 0 or direct_failed > direct_jobs:
        raise HarnessError(
            "group-first direct exact failure accounting is invalid: "
            "failed=%s planned=%s" % (direct_failed, direct_jobs)
        )
    exact_job_bound = int(fused.get("exact_job_bound", 0) or 0)
    if direct_jobs > exact_job_bound:
        raise HarnessError(
            "group-first direct exact bound exceeded: planned=%s bound=%s"
            % (direct_jobs, exact_job_bound)
        )
    direct_completed = int(fused.get("direct_exact_jobs_completed", 0) or 0)
    if direct_completed != direct_jobs:
        raise HarnessError(
            "group-first direct exact coverage is incomplete: planned=%s completed=%s"
            % (direct_jobs, direct_completed)
        )
    seeded_jobs = int(
        fused.get("process_nearest_seeded_jobs_planned", 0) or 0
    )
    seedless_jobs = int(
        fused.get("process_nearest_seedless_jobs_planned", 0) or 0
    )
    if seeded_jobs + seedless_jobs != direct_jobs:
        raise HarnessError(
            "nearest seed accounting mismatch: seeded=%s seedless=%s direct=%s"
            % (seeded_jobs, seedless_jobs, direct_jobs)
        )
    nearest_attempted = int(fused.get("process_nearest_attempted", 0) or 0)
    nearest_accepted = int(fused.get("process_nearest_accepted", 0) or 0)
    nearest_fallback = int(fused.get("process_nearest_fallback", 0) or 0)
    fallback_exact_calls = int(
        fused.get("process_nearest_fallback_exact_calls", 0) or 0
    )
    exact_primary_calls = int(
        fused.get("process_exact_primary_calls", 0) or 0
    )
    graph_rejected = int(
        fused.get("process_graph_rejected_before_nearest", 0) or 0
    )
    if CORRESPONDENCE_MODE == "EXACT_ONLY":
        if any((nearest_attempted, nearest_accepted, nearest_fallback, fallback_exact_calls)):
            raise HarnessError("EXACT_ONLY unexpectedly used verified-nearest/fallback")
        if graph_rejected + exact_primary_calls != direct_jobs:
            raise HarnessError("EXACT_ONLY primary-call accounting mismatch")
    elif CORRESPONDENCE_MODE == "VERIFIED_NEAREST_ONLY":
        if nearest_attempted != seeded_jobs:
            raise HarnessError(
                "nearest attempt denominator mismatch: attempted=%s seeded=%s"
                % (nearest_attempted, seeded_jobs)
            )
        if nearest_accepted + nearest_fallback != nearest_attempted:
            raise HarnessError("nearest acceptance/fallback accounting mismatch")
        if fallback_exact_calls != 0:
            raise HarnessError(
                "VERIFIED_NEAREST_ONLY exact fallback calls were non-zero: %s"
                % fallback_exact_calls
            )
        if exact_primary_calls != 0:
            raise HarnessError("VERIFIED_NEAREST_ONLY used exact-primary calls")
    elif CORRESPONDENCE_MODE == "HYBRID":
        if nearest_attempted != seeded_jobs:
            raise HarnessError(
                "HYBRID nearest attempt denominator mismatch: attempted=%s seeded=%s"
                % (nearest_attempted, seeded_jobs)
            )
        if nearest_accepted + nearest_fallback != nearest_attempted:
            raise HarnessError("HYBRID nearest acceptance/fallback accounting mismatch")
        expected_fallbacks = direct_jobs - nearest_accepted
        if fallback_exact_calls != expected_fallbacks:
            raise HarnessError(
                "HYBRID exact fallback call accounting mismatch: calls=%s expected=%s"
                % (fallback_exact_calls, expected_fallbacks)
            )
        if exact_primary_calls != 0:
            raise HarnessError("HYBRID unexpectedly used exact-primary calls")
    else:
        raise HarnessError("unsupported correspondence mode accounting: %s" % CORRESPONDENCE_MODE)
    if int(fused.get("process_fused_lower_bound_checked", 0) or 0) != 0 or int(
        fused.get("process_fused_lower_bound_rejected", 0) or 0
    ) != 0:
        raise HarnessError("unsafe R2D lower-bound rejection was reachable: %s" % fused)
    if int(fused.get("process_shape_pairs_submitted", 0) or 0) != int(
        fused.get("process_shape_pairs_completed", 0) or 0
    ):
        raise HarnessError("group-first shape phase is not terminal before exact")
    if fused.get("process_merged_pairs", 0) <= 0 and expected_master is not None:
        raise HarnessError("fused canonical merge produced no accepted pair")
    max_tick_ms = float(fused.get("max_tick_ms", 0.0) or 0.0)
    max_tick_stage = str(fused.get("max_tick_stage", "") or "")
    if max_tick_ms > 250.0:
        if max_tick_stage == "process_startup":
            fused.setdefault("warnings", []).append(
                "startup_tick_over_250ms:%0.3f" % max_tick_ms
            )
        else:
            raise HarnessError(
                "fused modal tick exceeded 250ms outside startup: %s at %s"
                % (max_tick_ms, max_tick_stage)
            )
    if fused.get("process_fused_frame_bytes", 0) > 16 * 1024 * 1024:
        raise HarnessError("fused task frame exceeded 16 MiB")
    # FAST has no synchronous result to compare.  EXACT/HYBRID comparisons are
    # diagnostic only; the fused result is gated independently by the explicit
    # count, UV-area master, complete mapping and zero-delta checks below.
    sync_fused_digest_equal = (
        None
        if CORRESPONDENCE_MODE == "VERIFIED_NEAREST_ONLY"
        else fused_digest == sync_digest and fused_uv_digest == sync_uv_digest
    )
    if COMMON.snapshot_selection(bm, uv_layer) != before_selection:
        raise HarnessError("fused changed selection")
    if COMMON.snapshot_active(obj, bm) != before_active:
        raise HarnessError("fused changed active state")
    selected_loop_keys = set().union(
        *(set(loop_keys_by_key[key]) for key in selected_keys)
    )
    unselected_delta = COMMON.max_delta(
        baseline_uv,
        fused_uv,
        set(baseline_uv) - selected_loop_keys,
    )
    if unselected_delta > COMMON.SELECTION_EPSILON:
        raise HarnessError("fused changed unselected UVs: %s" % unselected_delta)
    uv_area_masters = {
        tuple(key) for key in (fused.get("uv_area_masters") or ())
    }
    area_master_loop_keys = set().union(
        *(set(loop_keys_by_key[key]) for key in uv_area_masters)
    ) if uv_area_masters else set()
    master_delta = 0.0
    if expected_master is not None:
        master_delta = COMMON.max_delta(
            baseline_uv,
            fused_uv,
            area_master_loop_keys,
        )
        if master_delta > COMMON.SELECTION_EPSILON:
            raise HarnessError("fused changed UV-area master UVs: %s" % master_delta)
    elif COMMON.max_delta(baseline_uv, fused_uv, set(baseline_uv)) > COMMON.SELECTION_EPSILON:
        raise HarnessError("fused rejection wrote UVs")
    mapping_delta = _mapping_delta(fused, baseline_uv, fused_uv)
    if mapping_delta > 1.0e-7:
        raise HarnessError("fused mapping delta exceeded tolerance: %s" % mapping_delta)
    if expected_master is not None:
        if not uv_area_masters:
            raise HarnessError("group-first UV-area master report is empty")
        area_by_key = {}
        for item in fused.get("uv_area_by_key") or ():
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            key, area = tuple(item[0]), item[1]
            if isinstance(area, (int, float)) and math.isfinite(float(area)):
                area_by_key[key] = max(0.0, float(area))
        for group in fused.get("groups", []):
            group_master = tuple(group.get("master_key", ()))
            if group_master != tuple(expected_master):
                raise HarnessError(
                    "fused independent UV-area master mismatch: got=%s expected=%s"
                    % (group_master, expected_master)
                )
            group_keys = [group_master] + [
                tuple(key) for key in group.get("member_keys", ())
            ]
            valid = [
                (area_by_key[key], key)
                for key in group_keys
                if key in area_by_key
            ]
            if not valid:
                raise HarnessError("group has no finite UV-area records")
            expected_area_master = sorted(
                valid, key=lambda item: (-item[0], item[1])
            )[0][1]
            if group_master != expected_area_master:
                raise HarnessError(
                    "fused UV-area master mismatch: got=%s expected=%s"
                    % (group_master, expected_area_master)
                )
        mapped_member_keys = {
            tuple(key)
            for group in fused.get("groups", [])
            for key in group.get("member_keys", ())
        }
        untouched_selected_keys = (
            {tuple(key) for key in selected_keys}
            - mapped_member_keys
            - uv_area_masters
        )
        untouched_selected_loop_keys = (
            set().union(
                *(
                    set(loop_keys_by_key[key])
                    for key in untouched_selected_keys
                )
            )
            if untouched_selected_keys
            else set()
        )
        failed_member_delta = COMMON.max_delta(
            baseline_uv,
            fused_uv,
            untouched_selected_loop_keys,
        )
        if failed_member_delta > COMMON.SELECTION_EPSILON:
            raise HarnessError(
                "fused changed a member whose direct exact job failed: %s"
                % failed_member_delta
            )
    else:
        if int(expected_aligned_exact) != 0:
            raise HarnessError("negative case has a non-zero independent oracle count")
        if int(fused.get("aligned_exact", 0)) != 0 or int(
            fused.get("group_count", 0)
        ) != 0:
            raise HarnessError("negative case produced fused ownership/group output")
        if int(fused.get("process_merged_pairs", 0)) != 0:
            raise HarnessError("negative case produced a canonical merged pair")

    record = {
        "selected_keys": [list(key) for key in selected_keys],
        "allow_flipping": bool(allow_flipping),
        "correspondence_mode": CORRESPONDENCE_MODE,
        "sync_oracle": (
            "dedicated_fixture_uv_area_master"
            if CORRESPONDENCE_MODE == "VERIFIED_NEAREST_ONLY"
            else "explicit_synchronous"
        ),
        "sync_aligned_exact": int(expected_aligned_exact),
        "sync_group_count": int(sync.get("group_count", 0)),
        "sync_result_digest": sync_digest,
        "sync_uv_digest": sync_uv_digest,
        "fused_result_digest": fused_digest,
        "fused_uv_digest": fused_uv_digest,
        "sync_fused_digest_equal": sync_fused_digest_equal,
        "mapping_delta": mapping_delta,
        "master_delta": master_delta,
        "unselected_delta": unselected_delta,
        "direct_exact_jobs_failed": direct_failed,
        "nearest_seeded_jobs_planned": seeded_jobs,
        "nearest_seedless_jobs_planned": seedless_jobs,
        "nearest_attempted": nearest_attempted,
        "nearest_accepted": nearest_accepted,
        "nearest_fallback": nearest_fallback,
        "nearest_fallback_exact_calls": fallback_exact_calls,
        "nearest_missing_seed_fallbacks": int(
            fused.get("process_nearest_missing_seed_fallbacks", 0) or 0
        ),
        "fused_wall_ms": fused_wall_ms,
        "fused": COMMON.clean_json(fused),
    }
    _restore(obj, baseline_uv, baseline_selection)
    return record


def main():
    fixture_before = sha256_file(FIXTURE_PATH)
    if fixture_before != EXPECTED_FIXTURE_SHA:
        raise HarnessError("dedicated fixture SHA mismatch: %s" % fixture_before)
    if WORKER_COUNT != 4 or BATCH_SIZE != 32:
        raise HarnessError(
            "R2E2 dedicated gate requires worker_count=4,batch_size=32; got %s/%s"
            % (WORKER_COUNT, BATCH_SIZE)
        )
    if CORRESPONDENCE_MODE not in {
        "HYBRID",
        "VERIFIED_NEAREST_ONLY",
        "EXACT_ONLY",
    }:
        raise HarnessError("invalid correspondence mode: %s" % CORRESPONDENCE_MODE)
    if FIXTURE_SHA_BEFORE and fixture_before != FIXTURE_SHA_BEFORE:
        raise HarnessError("external fixture SHA mismatch: %s" % fixture_before)

    uv_gpt = None
    registered = False
    result = None
    primary_error = None
    try:
        uv_gpt = COMMON.import_addon()
        uv_gpt.register()
        registered = True
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils
        from uv_gpt import pro_process_runtime

        expected_python = pro_process_runtime.resolve_bundled_python(
            blender_binary=str(Path(bpy.app.binary_path).resolve()),
            blender_version=tuple(bpy.app.version),
        )
        cases = (
            ("PROExact", ((0,), (1,), (2,), (3,)), (0,), False),
            ("PROExact", ((0,), (1,), (2,), (3,)), (0,), True),
            ("PROHole", ((0, 1, 2, 3), (4, 5, 6, 7)), (0, 1, 2, 3), False),
            ("PROInterior", ((0, 1, 2, 3), (4, 5, 6, 7)), (0, 1, 2, 3), False),
            ("PROSeam", ((0, 1, 2, 3, 4, 5, 6, 7, 8), (9, 10, 11, 12, 13, 14, 15, 16, 17)), (0, 1, 2, 3, 4, 5, 6, 7, 8), False),
            ("PRONonIso", ((0,), (1, 2)), None, False),
        )
        case_results = []
        for case_index, (
            object_name,
            selected_keys,
            expected_master,
            allow_flipping,
        ) in enumerate(cases):
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                raise HarnessError("dedicated object missing: %s" % object_name)
            case_results.append(
                _run_case(
                    obj,
                    island_tools,
                    uv_utils,
                    stack_tools,
                    selected_keys,
                    expected_master,
                    EXPECTED_COUNTS[case_index],
                    allow_flipping,
                )
            )
        observed = tuple(item["fused"]["aligned_exact"] for item in case_results)
        if observed != EXPECTED_COUNTS:
            raise HarnessError("R1F six-case oracle mismatch: %s" % (observed,))
        uv_gpt.unregister()
        second_started = time.perf_counter()
        uv_gpt.unregister()
        second_unregister_ms = (time.perf_counter() - second_started) * 1000.0
        uv_gpt.register()
        uv_gpt.unregister()
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "correspondence_mode": CORRESPONDENCE_MODE,
            "fixture_sha256_before": fixture_before,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "bundled_python": str(expected_python),
            "blender_version": list(bpy.app.version),
            "oracle_aligned_exact": list(observed),
            "cases": case_results,
            "unregister": {"second_safe": True, "second_ms": second_unregister_ms},
        }
    except Exception as exc:
        primary_error = exc
    finally:
        if registered and uv_gpt is not None:
            try:
                uv_gpt.unregister()
            except Exception as exc:
                if primary_error is None:
                    primary_error = exc
        fixture_after = sha256_file(FIXTURE_PATH)
        if result is None:
            result = {
                "status": "failed",
                "packet": PACKET_ID,
                "fixture_sha256_before": fixture_before,
                "fixture_sha256_after_in_process": fixture_after,
                "error": str(primary_error or "R1F harness did not produce a result"),
                "traceback": traceback.format_exc() if primary_error else "",
                "diagnostics": dict(_LAST_RUN_DIAGNOSTICS),
                "cases": [],
            }
        elif primary_error is not None:
            result["status"] = "failed"
            result["error"] = str(primary_error)
            result["diagnostics"] = dict(_LAST_RUN_DIAGNOSTICS)
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(COMMON.clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    if result.get("status") != "passed":
        print(
            "R1F live fused proof failed: %s; diagnostics=%s"
            % (
                result.get("error"),
                json.dumps(
                    result.get("diagnostics", {}),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        return 1
    if result.get("fixture_sha256_after_in_process") != EXPECTED_FIXTURE_SHA:
        print("R1F fixture SHA changed")
        return 1
    print(
        "R1F live fused proof passed: oracle=%s; python=%s; result=%s"
        % (result["oracle_aligned_exact"], result["bundled_python"], RESULT_PATH)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
