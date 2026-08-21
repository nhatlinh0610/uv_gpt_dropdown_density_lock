"""MC3A mode-aware live failure proof on the disposable dedicated fixture.

The fixture is opened in memory only.  Normal mode runs retain the historical
six-case process comparison, while ``--failure-only`` intentionally skips all
success parity and missing-helper work and exercises only the concrete Pro
mode's modal cancel, context invalidation, owned-worker crash, and unregister
lifecycles.  Results are written to the operating-system TEMP directory by
default; this harness never writes a project benchmark or a .blend file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import types


sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "MC3A-LIVE-PROCESS-COUNT-1"
EXPECTED_FIXTURE_SHA = "EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8"
EXPECTED_COUNTS = (2, 3, 1, 1, 1, 0)
TARGET_UV_NAME = "UVMap.001"
FAILURE_DEBUG_DELAY_MS = 150


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


def _flag(name):
    if "--" not in sys.argv:
        return False
    return name in sys.argv[sys.argv.index("--") + 1 :]


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
        str(Path(__import__("tempfile").gettempdir()) / "uv_gpt_mc3a_result.json"),
    )
).resolve()
MODE = _arg_value("--mode", "HYBRID").upper()
FAILURE_ONLY = _flag("--failure-only")
VALID_MODES = {"HYBRID", "VERIFIED_NEAREST_ONLY", "EXACT_ONLY"}
if MODE not in VALID_MODES:
    raise HarnessError("unsupported MC3A correspondence mode: %s" % MODE)
if FAILURE_ONLY and MODE == "HYBRID":
    raise HarnessError("MC3A failure-only requires a concrete Pro mode")


def _load_common():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("mc3a_common", path)
    if spec is None or spec.loader is None:
        raise HarnessError("could not load existing dedicated harness")
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


def _restore(obj, island_tools, baseline_uv, baseline_selection):
    bm = bmesh.from_edit_mesh(obj.data)
    COMMON.setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("dedicated UV layer disappeared")
    COMMON.restore_uv(bm, uv_layer, baseline_uv)
    COMMON.restore_selection(bm, uv_layer, baseline_selection)
    return island_tools.get_active_bmesh(bpy.context), island_tools.get_active_uv_layer(
        island_tools.get_active_bmesh(bpy.context), obj
    )


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


def _run_request(stack_tools, request):
    started = time.perf_counter()
    result = stack_tools.run_align_similar_pro(request)
    result["harness_wall_ms"] = (time.perf_counter() - started) * 1000.0
    return result


def _process_request():
    return {
        "bpy_context": bpy.context,
        "detail_mappings": True,
        "process_worker_count": 1,
        "process_batch_size": 1,
        "process_blender_binary": str(Path(bpy.app.binary_path).resolve()),
        "process_blender_version": tuple(bpy.app.version),
        "process_fused": True,
        "process_group_first": True,
        "correspondence_mode": MODE,
        "mode": MODE,
    }


def _failure_session(stack_tools, evidence):
    """Create the exact UI-like external group-first route under test."""

    if FAILURE_ONLY and MODE == "HYBRID":
        raise HarnessError("failure-only route requires a concrete Pro mode")
    return stack_tools._pro_create_session(
        bpy.context,
        evidence,
        modal=True,
        process_worker_count=1,
        process_batch_size=1,
        process_blender_binary=str(Path(bpy.app.binary_path).resolve()),
        process_blender_version=tuple(bpy.app.version),
        process_fused=True,
        process_group_first=True,
        correspondence_mode=MODE,
        process_debug_delay_ms=FAILURE_DEBUG_DELAY_MS,
    )


def _assert_failure_report(session, label):
    report = session.report
    if not session.done:
        raise HarnessError("%s session did not terminate" % label)
    if report.get("correspondence_mode") != MODE:
        raise HarnessError(
            "%s correspondence mode mismatch: %s" % (label, report)
        )
    if not report.get("process_fused") or not report.get("process_group_first"):
        raise HarnessError("%s did not use fused group-first process route" % label)
    if report.get("worker_mode") != "external_bundled_python_group_first":
        raise HarnessError(
            "%s worker route mismatch: %s" % (label, report.get("worker_mode"))
        )
    if int(report.get("process_worker_count", 0) or 0) != 1:
        raise HarnessError("%s worker count was not one" % label)
    if int(report.get("process_batch_size", 0) or 0) != 1:
        raise HarnessError("%s batch size was not one" % label)
    if int(report.get("exact_loop_writes", 0) or 0) != 0:
        raise HarnessError("%s exposed exact loop writes" % label)
    if bool(report.get("partial")) or bool(report.get("truncated")):
        raise HarnessError("%s exposed a partial or truncated result" % label)
    if MODE == "VERIFIED_NEAREST_ONLY":
        for field in (
            "process_exact_fallback_calls",
            "process_nearest_fallback_exact_calls",
            "process_exact_primary_calls",
        ):
            if int(report.get(field, 0) or 0) != 0:
                raise HarnessError("FAST %s was non-zero: %s" % (field, report))
    elif MODE == "EXACT_ONLY":
        for field in (
            "process_nearest_attempted",
            "process_nearest_accepted",
            "process_nearest_fallback",
            "process_nearest_fallback_exact_calls",
        ):
            if int(report.get(field, 0) or 0) != 0:
                raise HarnessError("EXACT %s was non-zero: %s" % (field, report))
    return report


def _modal_proxy(stack_tools, session):
    """Bind the modal lifecycle from the selected concrete Pro operator."""

    if MODE == "VERIFIED_NEAREST_ONLY":
        operator_class = stack_tools.UVGPT_OT_align_similar_pro_fast
    elif MODE == "EXACT_ONLY":
        operator_class = stack_tools.UVGPT_OT_align_similar_pro_exact
    elif MODE == "HYBRID":
        operator_class = stack_tools._UVGPT_OT_align_similar_pro_mode
    else:
        raise HarnessError("failure-only route requires a concrete Pro mode")
    operator = types.SimpleNamespace(
        _session=session,
        _timer=None,
        bl_label=getattr(operator_class, "bl_label", "Align Similar Pro"),
        correspondence_mode=(
            MODE
            if MODE == "HYBRID"
            else operator_class.correspondence_mode
        ),
        report=lambda *_args: None,
    )
    operator._cleanup_modal = types.MethodType(
        operator_class._cleanup_modal,
        operator,
    )
    operator._report_result = types.MethodType(
        operator_class._report_result,
        operator,
    )
    operator.modal = types.MethodType(operator_class.modal, operator)
    stack_tools._ACTIVE_PRO_OPERATOR = operator
    return operator


def _drive_modal_to_done(operator, session, label, *, max_seconds=10.0):
    started = time.perf_counter()
    ticks = 0
    terminal_result = None
    while not session.done:
        terminal_result = operator.modal(bpy.context, _Event("TIMER"))
        ticks += 1
        if time.perf_counter() - started > max_seconds:
            raise HarnessError("%s cleanup exceeded %.1f seconds" % (label, max_seconds))
    return {
        "ticks": ticks,
        "cleanup_ms": (time.perf_counter() - started) * 1000.0,
        "terminal_result": sorted(str(item) for item in (terminal_result or ())),
    }


def run_case(obj, island_tools, uv_utils, stack_tools, selected_keys, expected_master, allow_flipping):
    bm, uv_layer, by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    settings = bpy.context.scene.uv_gpt_settings
    settings.stack_allow_flipping = bool(allow_flipping)
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)

    sync = _run_request(
        stack_tools,
        {
            "bpy_context": bpy.context,
            "detail_mappings": True,
            "correspondence_mode": MODE,
            "mode": MODE,
        },
    )
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    sync_uv = COMMON.snapshot_uv(bm, uv_layer)
    sync_selection = COMMON.snapshot_selection(bm, uv_layer)
    sync_active = COMMON.snapshot_active(obj, bm)
    sync_digest = _result_digest(sync)
    sync_uv_digest = _uv_digest(sync_uv)

    _restore(obj, island_tools, baseline_uv, baseline_selection)
    process = _run_request(stack_tools, _process_request())
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    process_uv = COMMON.snapshot_uv(bm, uv_layer)
    process_selection = COMMON.snapshot_selection(bm, uv_layer)
    process_active = COMMON.snapshot_active(obj, bm)
    process_digest = _result_digest(process)
    process_uv_digest = _uv_digest(process_uv)
    if process.get("operator_result") != ["FINISHED"]:
        raise HarnessError("process case did not finish: %s" % process)
    if (
        process.get("aligned_exact", 0) > 0
        and (
            process.get("process_worker_count") != 1
            or not process.get("process_worker_pids")
        )
    ):
        raise HarnessError("process case did not expose one external worker: %s" % process)
    if process.get("process_batch_size") != 1:
        raise HarnessError("process batch size was not locked to one")
    if process.get("aligned_exact", 0) > 0 and process.get("process_python_version") is None:
        print(
            "MC3A worker metadata missing: pids=%s helper=%s executable=%s report=%s"
            % (
                process.get("process_worker_pids"),
                process.get("process_helper_path"),
                process.get("process_python_executable"),
                process,
            )
        )
        raise HarnessError("worker READY did not report Python version")
    if process.get("aligned_exact", 0) > 0:
        thread_caps = process.get("process_thread_caps") or {}
        if not thread_caps or any(str(value) != "1" for value in thread_caps.values()):
            raise HarnessError("worker READY thread caps were not all one: %s" % thread_caps)
    if process.get("aligned_exact") != sync.get("aligned_exact") or process.get("group_count") != sync.get("group_count"):
        raise HarnessError("sync/process count mismatch")
    if process_digest != sync_digest or process_uv_digest != sync_uv_digest:
        print(
            "MC3A parity detail: sync_digest=%s process_digest=%s sync_uv=%s process_uv=%s"
            % (sync_digest, process_digest, sync_uv_digest, process_uv_digest)
        )
        print("MC3A sync groups=%s" % json.dumps(sync.get("groups", []), sort_keys=True))
        print("MC3A process groups=%s" % json.dumps(process.get("groups", []), sort_keys=True))
        print("MC3A sync rejects=%s" % json.dumps(sync.get("topology_rejections", []), sort_keys=True))
        print("MC3A process rejects=%s" % json.dumps(process.get("topology_rejections", []), sort_keys=True))
        raise HarnessError("sync/process result digest mismatch")
    if sync_selection != before_selection or process_selection != before_selection:
        raise HarnessError("sync/process changed selection")
    if sync_active != before_active or process_active != before_active:
        raise HarnessError("sync/process changed active state")
    if _mapping_delta(process, baseline_uv, process_uv) > 1.0e-7:
        raise HarnessError("process mapping delta exceeded tolerance")
    if expected_master is not None:
        for group in process.get("groups", []):
            if tuple(group.get("master_key", ())) != expected_master:
                raise HarnessError("process density master mismatch")
    return {
        "selected_keys": [list(key) for key in selected_keys],
        "allow_flipping": bool(allow_flipping),
        "expected_aligned_exact": int(sync.get("aligned_exact", 0)),
        "sync_aligned_exact": int(sync.get("aligned_exact", 0)),
        "process_aligned_exact": int(process.get("aligned_exact", 0)),
        "sync_group_count": int(sync.get("group_count", 0)),
        "process_group_count": int(process.get("group_count", 0)),
        "sync_result_digest": sync_digest,
        "process_result_digest": process_digest,
        "sync_uv_digest": sync_uv_digest,
        "process_uv_digest": process_uv_digest,
        "mapping_max_delta": _mapping_delta(process, baseline_uv, process_uv),
        "process": process,
        "sync": sync,
    }


class _Event:
    def __init__(self, event_type):
        self.type = event_type


def _owned_worker_snapshot(session):
    """Return the live exact Popen handles owned by this session's pool."""

    pool = getattr(session, "_process_pool", None)
    workers = tuple(getattr(pool, "workers", ()) or ()) if pool is not None else ()
    alive_pids = []
    for worker in workers:
        process = getattr(worker, "process", None)
        pid = getattr(worker, "pid", None)
        if process is None or pid is None or not callable(getattr(process, "poll", None)):
            continue
        try:
            alive = process.poll() is None
        except Exception:
            alive = False
        if alive:
            alive_pids.append(int(pid))
    return pool, workers, tuple(sorted(alive_pids))


def _group_first_rendezvous_snapshot(session):
    """Collect authoritative pending-work state without using legacy inflight."""

    report = getattr(session, "report", {}) or {}
    pipeline = getattr(session, "_process_pipeline", None)
    pool, workers, alive_pids = _owned_worker_snapshot(session)
    progress = None
    progress_error = ""
    if pipeline is not None:
        try:
            progress = pipeline.progress()
        except Exception as exc:
            progress_error = "%s: %s" % (type(exc).__name__, exc)

    shape_submitted = int(getattr(progress, "shape_submitted", 0) or 0)
    shape_completed = int(getattr(progress, "shape_completed", 0) or 0)
    exact_submitted = int(getattr(progress, "exact_submitted", 0) or 0)
    exact_completed = int(getattr(progress, "exact_completed", 0) or 0)
    progress_submissions = shape_submitted + exact_submitted
    progress_completions = shape_completed + exact_completed
    report_submissions = int(report.get("worker_submissions", 0) or 0)
    report_completions = int(report.get("worker_completions", 0) or 0)
    worker_submissions = max(report_submissions, progress_submissions)
    worker_completions = max(report_completions, progress_completions)

    pool_active_workers = int(getattr(pool, "active_workers", 0) or 0) if pool is not None else 0
    pool_queue_depth = int(
        getattr(
            pool,
            "stream_queue_depth",
            getattr(pool, "queue_depth", 0),
        )
        or 0
    ) if pool is not None else 0
    pool_pending = bool(
        pool is not None
        and (
            getattr(pool, "_active", {})
            or getattr(pool, "_ready_batches", ())
            or getattr(pool, "_undispatched", ())
        )
    )
    pipeline_pending = bool(
        pipeline is not None
        and (
            getattr(pipeline, "_task_meta", {})
            or getattr(pipeline, "_completion_buffer", ())
        )
    )
    shape_pending = shape_submitted > shape_completed
    exact_pending = exact_submitted > exact_completed
    submission_gap = worker_submissions > worker_completions
    pending_work = bool(
        shape_pending
        or exact_pending
        or submission_gap
        or pool_pending
        or pipeline_pending
    )
    if exact_pending:
        pending_kind = "exact"
    elif shape_pending:
        pending_kind = "shape"
    elif pipeline is not None and getattr(pipeline, "_task_meta", {}):
        pending_kind = str(
            next(iter(getattr(pipeline, "_task_meta", {}).values()))[0]
        )
    else:
        pending_kind = "unknown"
    pipeline_stage = str(getattr(progress, "stage", "") or "")
    session_stage = str(
        report.get("process_stage", getattr(session, "_process_stage", "")) or ""
    )
    group_first_route = bool(
        getattr(session, "process_group_first_requested", False)
        and getattr(session, "process_fused_requested", False)
        and pipeline is not None
        and type(pipeline).__name__ == "GroupFirstProcessPipeline"
    )
    return {
        "session_terminal": bool(getattr(session, "done", False)),
        "session_state": str(getattr(session, "state", "") or ""),
        "session_stage": session_stage,
        "pipeline_stage": pipeline_stage,
        "pipeline_type": type(pipeline).__name__ if pipeline is not None else "",
        "group_first_route": group_first_route,
        "process_fused": bool(getattr(session, "process_fused_requested", False)),
        "worker_submissions": worker_submissions,
        "worker_completions": worker_completions,
        "report_worker_submissions": report_submissions,
        "report_worker_completions": report_completions,
        "pipeline_shape_submitted": shape_submitted,
        "pipeline_shape_completed": shape_completed,
        "pipeline_exact_submitted": exact_submitted,
        "pipeline_exact_completed": exact_completed,
        "pool_active_workers": pool_active_workers,
        "pool_queue_depth": pool_queue_depth,
        "pool_pending": pool_pending,
        "pipeline_pending": pipeline_pending,
        "submission_gap": submission_gap,
        "pending_work": pending_work,
        "pending_kind": pending_kind,
        "owned_worker_pids": list(alive_pids),
        "active_worker_pids": list(alive_pids),
        "worker_count": len(workers),
        "progress_error": progress_error,
    }


def _reach_group_first_pending(stack_tools, label):
    """Wait at most ten seconds for live GroupFirst work before injection."""

    session = _failure_session(stack_tools, {"detail_mappings": True})
    operator = _modal_proxy(stack_tools, session)
    started = time.perf_counter()
    ticks = 0
    last_snapshot = {}
    while time.perf_counter() - started <= 10.0:
        if session.done:
            break
        last_snapshot = _group_first_rendezvous_snapshot(session)
        if (
            not last_snapshot["session_terminal"]
            and last_snapshot["group_first_route"]
            and last_snapshot["pending_work"]
            and last_snapshot["owned_worker_pids"]
        ):
            last_snapshot.update(
                {
                    "ready": True,
                    "ticks": ticks,
                    "wait_ms": (time.perf_counter() - started) * 1000.0,
                }
            )
            return session, operator, last_snapshot
        operator.modal(bpy.context, _Event("TIMER"))
        ticks += 1
    last_snapshot = _group_first_rendezvous_snapshot(session)
    if session.done:
        raise HarnessError(
            "%s completed before group-first pending-work rendezvous: %s"
            % (label, last_snapshot)
        )
    raise HarnessError(
        "%s group-first pending-work rendezvous timed out after 10 seconds: %s"
        % (label, last_snapshot)
    )


def run_cancel_and_invalidation(obj, island_tools, uv_utils, stack_tools):
    selected_keys = ((0,), (1,), (2,), (3,))
    bm, uv_layer, _by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    session, operator, rendezvous = _reach_group_first_pending(stack_tools, "ESC")
    cancel_started = time.perf_counter()
    cancel_result = operator.modal(bpy.context, _Event("ESC"))
    cancel_response_ms = (time.perf_counter() - cancel_started) * 1000.0
    if cancel_response_ms > 500.0:
        raise HarnessError("ESC response exceeded 500 ms: %s" % cancel_response_ms)
    cancel_cleanup = _drive_modal_to_done(operator, session, "ESC")
    cancel_cleanup_ms = (time.perf_counter() - cancel_started) * 1000.0
    cancel_report = _assert_failure_report(session, "ESC")
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = COMMON.snapshot_uv(bm, uv_layer)
    if COMMON.max_delta(baseline_uv, after_uv, set(baseline_uv)) > 1.0e-12:
        raise HarnessError("ESC process cancellation changed UVs")
    if COMMON.snapshot_selection(bm, uv_layer) != before_selection:
        raise HarnessError("ESC process cancellation changed selection")
    if COMMON.snapshot_active(obj, bm) != before_active:
        raise HarnessError("ESC process cancellation changed active state")
    if not session.cancelled or cancel_report.get("cancel_reason") != "user_cancelled":
        raise HarnessError("ESC process cancellation reason was not user_cancelled")

    _restore(obj, island_tools, baseline_uv, baseline_selection)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    before_uv = COMMON.snapshot_uv(bm, uv_layer)
    invalidation_selection = COMMON.snapshot_selection(bm, uv_layer)
    invalidation_active = COMMON.snapshot_active(obj, bm)
    invalidated, invalidation_operator, invalidation_rendezvous = _reach_group_first_pending(
        stack_tools, "context invalidation"
    )
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    uv_layer_data = bm.faces[0].loops[0][uv_layer].uv
    uv_layer_data.x += 0.125
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    expected_invalidated_uv = COMMON.snapshot_uv(bm, uv_layer)
    invalidation_started = time.perf_counter()
    invalidation_cleanup = _drive_modal_to_done(
        invalidation_operator, invalidated, "context invalidation"
    )
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_invalidated = COMMON.snapshot_uv(bm, uv_layer)
    invalidation_report = _assert_failure_report(invalidated, "context invalidation")
    if (
        COMMON.max_delta(
            expected_invalidated_uv,
            after_invalidated,
            set(expected_invalidated_uv),
        )
        > 1.0e-12
    ):
        raise HarnessError("context invalidation changed the deliberate UV mutation")
    if COMMON.snapshot_selection(bm, uv_layer) != invalidation_selection:
        raise HarnessError("context invalidation changed selection")
    if COMMON.snapshot_active(obj, bm) != invalidation_active:
        raise HarnessError("context invalidation changed active state")
    if invalidation_report.get("cancel_reason") != "context_invalidated":
        raise HarnessError("context invalidation reason was not explicit")
    invalidation_ms = (time.perf_counter() - invalidation_started) * 1000.0
    _restore(obj, island_tools, baseline_uv, baseline_selection)
    return {
        "cancel_result": sorted(str(item) for item in cancel_result),
        "cancel_report": COMMON.clean_json(cancel_report),
        "cancel_rendezvous": COMMON.clean_json(rendezvous),
        "cancel_ticks": rendezvous["ticks"],
        "cancel_rendezvous_wait_ms": rendezvous["wait_ms"],
        "cancel_response_ms": cancel_response_ms,
        "cancel_cleanup_ms": cancel_cleanup_ms,
        "cancel_cleanup": cancel_cleanup,
        "cancel_zero_write": cancel_report.get("exact_loop_writes", 0) == 0,
        "cancel_state_preserved": True,
        "invalidation_report": COMMON.clean_json(invalidation_report),
        "invalidation_rendezvous": COMMON.clean_json(invalidation_rendezvous),
        "invalidation_ticks": invalidation_rendezvous["ticks"],
        "invalidation_rendezvous_wait_ms": invalidation_rendezvous["wait_ms"],
        "invalidation_ms": invalidation_ms,
        "invalidation_cleanup": invalidation_cleanup,
        "invalidation_uv_digest_before": _uv_digest(before_uv),
        "invalidation_uv_digest_expected": _uv_digest(expected_invalidated_uv),
        "invalidation_uv_digest_after": _uv_digest(after_invalidated),
        "invalidation_zero_write": invalidation_report.get("exact_loop_writes", 0) == 0,
        "invalidation_external_mutation_preserved": True,
        "invalidation_state_preserved": True,
    }


def run_crash_cleanup(obj, island_tools, uv_utils, stack_tools):
    """Kill only the exact owned Popen handle twice and require hard failure."""

    selected_keys = ((0,), (1,), (2,), (3,))
    bm, uv_layer, _by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    session, operator, rendezvous = _reach_group_first_pending(stack_tools, "worker crash")

    crash_pids = []
    crash_started = time.perf_counter()
    for failure_index in range(2):
        killed_pid = None
        while time.perf_counter() - crash_started <= 10.0:
            pool = getattr(session, "_process_pool", None)
            workers = tuple(getattr(pool, "workers", ()) or ()) if pool is not None else ()
            for worker in workers:
                process = getattr(worker, "process", None)
                pid = getattr(worker, "pid", None)
                if process is None or pid is None or int(pid) in crash_pids:
                    continue
                if int(pid) not in rendezvous["owned_worker_pids"] and not crash_pids:
                    continue
                if callable(getattr(process, "poll", None)) and process.poll() is None:
                    process.kill()
                    killed_pid = int(pid)
                    crash_pids.append(killed_pid)
                    break
            if killed_pid is not None:
                break
            if session.done:
                break
            operator.modal(bpy.context, _Event("TIMER"))
        if killed_pid is None:
            raise HarnessError("could not kill exact owned worker for failure %d" % failure_index)
        if failure_index == 0:
            restarted = False
            while time.perf_counter() - crash_started <= 10.0:
                if session.done:
                    break
                operator.modal(bpy.context, _Event("TIMER"))
                pool = getattr(session, "_process_pool", None)
                workers = tuple(getattr(pool, "workers", ()) or ()) if pool is not None else ()
                if session.report.get("process_retry_count", 0) >= 1 and any(
                    getattr(worker, "pid", None) not in crash_pids
                    and getattr(getattr(worker, "process", None), "poll", lambda: 1)() is None
                    for worker in workers
                ):
                    restarted = True
                    break
            if not restarted:
                raise HarnessError("worker did not restart for the single retry")

    terminal_result = None
    while not session.done and time.perf_counter() - crash_started <= 10.0:
        terminal_result = operator.modal(bpy.context, _Event("TIMER"))
    if not session.done:
        raise HarnessError("repeated worker crash did not terminate the session")
    crash_ms = (time.perf_counter() - crash_started) * 1000.0
    crash_report = _assert_failure_report(session, "repeated worker crash")
    if not session.cancelled and not session.error:
        raise HarnessError("repeated worker crash did not produce terminal failure")
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = COMMON.snapshot_uv(bm, uv_layer)
    if COMMON.snapshot_selection(bm, uv_layer) != before_selection:
        raise HarnessError("repeated worker crash changed selection")
    if COMMON.snapshot_active(obj, bm) != before_active:
        raise HarnessError("repeated worker crash changed active state")
    if crash_report.get("process_retry_count", 0) != 1:
        raise HarnessError("worker retry count was not exactly one: %s" % crash_report)
    _restore(obj, island_tools, baseline_uv, baseline_selection)
    return {
        "crash_pids": crash_pids,
        "rendezvous": COMMON.clean_json(rendezvous),
        "rendezvous_ticks": rendezvous["ticks"],
        "rendezvous_wait_ms": rendezvous["wait_ms"],
        "crash_ms": crash_ms,
        "terminal_result": sorted(str(item) for item in (terminal_result or ())),
        "retry_count": crash_report.get("process_retry_count", 0),
        "zero_write": crash_report.get("exact_loop_writes", 0) == 0,
        "cancelled": bool(session.cancelled),
        "error": session.error,
        "uv_unchanged": COMMON.max_delta(baseline_uv, after_uv, set(baseline_uv)) <= 1.0e-12,
        "selection_unchanged": True,
        "active_unchanged": True,
        "worker_shutdown": bool(crash_report.get("worker_shutdown")),
        "report": COMMON.clean_json(crash_report),
    }


def run_unregister_cleanup(obj, island_tools, uv_utils, stack_tools, uv_gpt):
    selected_keys = ((0,), (1,), (2,), (3,))
    bm, uv_layer, _by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    before_uv = COMMON.snapshot_uv(bm, uv_layer)
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    session, _operator, rendezvous = _reach_group_first_pending(stack_tools, "unregister")
    unregister_started = time.perf_counter()
    uv_gpt.unregister()
    unregister_ms = (time.perf_counter() - unregister_started) * 1000.0
    second_cleanup_started = time.perf_counter()
    uv_gpt.unregister()
    second_cleanup_ms = (time.perf_counter() - second_cleanup_started) * 1000.0
    if unregister_ms > 10000.0 or second_cleanup_ms > 10000.0:
        raise HarnessError(
            "unregister cleanup exceeded 10 seconds: %.3f/%.3f"
            % (unregister_ms, second_cleanup_ms)
        )
    report = _assert_failure_report(session, "unregister")
    if report.get("cancel_reason") != "unregister":
        raise HarnessError("unregister did not cancel the active Pro session")
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = COMMON.snapshot_uv(bm, uv_layer)
    if COMMON.max_delta(before_uv, after_uv, set(before_uv)) > 1.0e-12:
        raise HarnessError("unregister cleanup changed UVs")
    if COMMON.snapshot_selection(bm, uv_layer) != before_selection:
        raise HarnessError("unregister cleanup changed selection")
    if COMMON.snapshot_active(obj, bm) != before_active:
        raise HarnessError("unregister cleanup changed active state")
    return {
        "rendezvous": COMMON.clean_json(rendezvous),
        "ticks_before_unregister": rendezvous["ticks"],
        "rendezvous_wait_ms": rendezvous["wait_ms"],
        "cancel_reason": report.get("cancel_reason"),
        "unregister_ms": unregister_ms,
        "second_cleanup_ms": second_cleanup_ms,
        "second_cleanup_safe": True,
        "zero_write": report.get("exact_loop_writes", 0) == 0,
        "uv_unchanged": True,
        "selection_unchanged": True,
        "active_unchanged": True,
        "worker_shutdown": report.get("worker_shutdown", False),
        "worker_pids": report.get("process_worker_pids", []),
        "report": COMMON.clean_json(report),
    }


def main():
    if not FIXTURE_PATH.is_file():
        raise HarnessError("dedicated fixture is missing")
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if fixture_sha_before != EXPECTED_FIXTURE_SHA:
        raise HarnessError("dedicated fixture SHA mismatch")
    if FIXTURE_SHA_BEFORE_EXTERNAL and fixture_sha_before != FIXTURE_SHA_BEFORE_EXTERNAL:
        raise HarnessError("external fixture SHA preflight mismatch")
    uv_gpt = COMMON.import_addon()
    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils
        from uv_gpt.pro_process_runtime import resolve_bundled_python

        expected_python = resolve_bundled_python(
            blender_binary=str(Path(bpy.app.binary_path).resolve()),
            blender_version=tuple(bpy.app.version),
        )
        if FAILURE_ONLY:
            target = bpy.data.objects.get("PROExact")
            if target is None:
                raise HarnessError("dedicated object missing: PROExact")
            failure_results = run_cancel_and_invalidation(
                target, island_tools, uv_utils, stack_tools
            )
            crash_results = run_crash_cleanup(
                target, island_tools, uv_utils, stack_tools
            )
            if not crash_results["zero_write"] or crash_results["retry_count"] != 1:
                raise HarnessError("crash cleanup guard failed: %s" % crash_results)
            unregister_result = run_unregister_cleanup(
                target, island_tools, uv_utils, stack_tools, uv_gpt
            )
            result = {
                "status": "passed",
                "packet": PACKET_ID,
                "fixture": str(FIXTURE_PATH),
                "fixture_sha256_before": fixture_sha_before,
                "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
                "bundled_python": str(expected_python),
                "blender_version": list(bpy.app.version),
                "correspondence_mode": MODE,
                "failure_only": True,
                "worker_mode": "external_bundled_python_group_first",
                "synchronous_fast_invoked": False,
                "success_cases_skipped": True,
                "cases": [],
                "oracle_aligned_exact": [],
                "failure_guards": failure_results,
                "crash_cleanup": crash_results,
                "missing_helper": None,
                "unregister_cleanup": unregister_result,
            }
        else:
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
                    raise HarnessError("dedicated object missing: %s" % object_name)
                case_results.append(
                    run_case(
                        obj,
                        island_tools,
                        uv_utils,
                        stack_tools,
                        selected_keys,
                        expected_master,
                        allow_flipping,
                    )
                )
            observed = tuple(item["process_aligned_exact"] for item in case_results)
            if observed != EXPECTED_COUNTS:
                raise HarnessError("six-case oracle mismatch: %s" % (observed,))
            failure_results = run_cancel_and_invalidation(
                bpy.data.objects["PROExact"], island_tools, uv_utils, stack_tools
            )
            crash_results = run_crash_cleanup(
                bpy.data.objects["PROExact"], island_tools, uv_utils, stack_tools
            )
            if not crash_results["zero_write"] or crash_results["retry_count"] != 1:
                raise HarnessError("crash cleanup guard failed: %s" % crash_results)
            missing = _run_request(
                stack_tools,
                {
                    **_process_request(),
                    "process_python_executable": str(
                        Path(PROJECT_ROOT) / "missing-mc3a-python.exe"
                    ),
                },
            )
            if missing.get("operator_result") != ["CANCELLED"] or not missing.get("error"):
                raise HarnessError("missing-helper failure was not explicit: %s" % missing)
            unregister_result = run_unregister_cleanup(
                bpy.data.objects["PROExact"], island_tools, uv_utils, stack_tools, uv_gpt
            )
            result = {
                "status": "passed",
                "packet": PACKET_ID,
                "fixture": str(FIXTURE_PATH),
                "fixture_sha256_before": fixture_sha_before,
                "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
                "bundled_python": str(expected_python),
                "blender_version": list(bpy.app.version),
                "correspondence_mode": MODE,
                "failure_only": False,
                "cases": case_results,
                "oracle_aligned_exact": list(observed),
                "failure_guards": failure_results,
                "crash_cleanup": crash_results,
                "missing_helper": missing,
                "unregister_cleanup": unregister_result,
            }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(COMMON.clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            "MC3A live process proof passed: mode=%s; failure_only=%s; cases=%s; "
            "worker_mode=%s; python=%s; result=%s"
            % (
                MODE,
                FAILURE_ONLY,
                result.get("oracle_aligned_exact", []),
                result.get("worker_mode", ""),
                expected_python,
                RESULT_PATH,
            )
        )
    finally:
        uv_gpt.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("MC3A live process proof failed: %s" % exc)
        raise SystemExit(1)
