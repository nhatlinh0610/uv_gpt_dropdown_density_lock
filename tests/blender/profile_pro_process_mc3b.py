"""MC3B live shape+exact pipeline proof on the disposable fixture.

This profile deliberately uses the dedicated fixture only.  It compares the
same six cases with the synchronous oracle, exercises process counts 1/2/4,
and keeps all result files in the operating-system TEMP directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import types
import traceback

sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "MC3B-LIVE-SHAPE-EXACT-PIPELINE"
EXPECTED_FIXTURE_SHA = "EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8"
EXPECTED_COUNTS = (2, 3, 1, 1, 1, 0)
TARGET_UV_NAME = "UVMap.001"
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
FIXTURE_SHA_BEFORE = _arg_value("--fixture-sha-before", "").upper()
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(Path(__import__("tempfile").gettempdir()) / "uv_gpt_mc3b_result.json"),
    )
).resolve()


def _load_common():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("mc3b_common", path)
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
    result = stack_tools.run_align_similar_pro(
        {
            **request,
            "correspondence_mode": CORRESPONDENCE_MODE,
            "mode": CORRESPONDENCE_MODE,
        }
    )
    result["harness_wall_ms"] = (time.perf_counter() - started) * 1000.0
    return result


def _process_request(count, batch_size, *, delay=0):
    return {
        "bpy_context": bpy.context,
        "detail_mappings": True,
        "process_worker_count": int(count),
        "process_batch_size": int(batch_size),
        "process_test_override": True,
        "process_debug_delay_ms": int(delay),
        "correspondence_mode": CORRESPONDENCE_MODE,
        "mode": CORRESPONDENCE_MODE,
        "process_blender_binary": str(Path(bpy.app.binary_path).resolve()),
        "process_blender_version": tuple(bpy.app.version),
    }


def _case(obj, island_tools, uv_utils, stack_tools, selected_keys, expected_master, allow_flipping):
    bm, uv_layer, _by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    settings = bpy.context.scene.uv_gpt_settings
    settings.stack_allow_flipping = bool(allow_flipping)
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)

    sync = _run_request(stack_tools, {"bpy_context": bpy.context, "detail_mappings": True})
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    sync_digest = _result_digest(sync)
    sync_uv_digest = _uv_digest(COMMON.snapshot_uv(bm, uv_layer))

    process_runs = {}
    for count in (1, 2, 4):
        _restore(obj, baseline_uv, baseline_selection)
        process = _run_request(stack_tools, _process_request(count, 2))
        bm = island_tools.get_active_bmesh(bpy.context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        process_uv = COMMON.snapshot_uv(bm, uv_layer)
        process_digest = _result_digest(process)
        process_uv_digest = _uv_digest(process_uv)
        if process.get("operator_result") != ["FINISHED"]:
            raise HarnessError("MC3B process count %d failed: %s" % (count, process))
        if not process.get("process_pipeline"):
            raise HarnessError("count %d did not use the MC3B pipeline" % count)
        if process_digest != sync_digest or process_uv_digest != sync_uv_digest:
            raise HarnessError(
                "count %d sync/process digest mismatch: %s %s %s %s; "
                "sync_groups=%r process_groups=%r sync_rejections=%r "
                "process_rejections=%r process_graph=%r"
                % (
                    count,
                    sync_digest,
                    process_digest,
                    sync_uv_digest,
                    process_uv_digest,
                    sync.get("groups", []),
                    process.get("groups", []),
                    sync.get("topology_rejections", []),
                    process.get("topology_rejections", []),
                    {
                        "operator_result": process.get("operator_result"),
                        "error": process.get("error"),
                        "stage": process.get("process_stage"),
                        "shape": [process.get("process_shape_pairs_submitted"), process.get("process_shape_pairs_completed"), process.get("process_shape_accepted"), process.get("process_shape_rejected")],
                        "exact": [process.get("process_exact_pairs_submitted"), process.get("process_exact_pairs_completed"), process.get("process_exact_accepted")],
                        "merged": process.get("process_merged_pairs"),
                        "pipeline": process.get("process_pipeline"),
                        "submitted": process.get("process_graph_worker_submitted"),
                        "completed": process.get("process_graph_worker_completed"),
                        "main_ops": process.get("process_graph_main_operations"),
                        "last": process.get("process_last_progress_kind"),
                    },
                )
            )
        if process.get("process_shape_pairs_submitted") != process.get("process_shape_pairs_completed"):
            raise HarnessError("count %d shape coverage incomplete" % count)
        if process.get("process_exact_pairs_submitted") != process.get("process_exact_pairs_completed"):
            raise HarnessError("count %d exact coverage incomplete" % count)
        if process.get("max_tick_ms", 0.0) > 250.0:
            print("MC3B tick detail count=%d max=%s stage=%s timings=%s" % (count, process.get("max_tick_ms"), process.get("max_tick_stage"), process.get("timings", {})))
            raise HarnessError("count %d modal tick exceeded 250ms: %s" % (count, process.get("max_tick_ms")))
        if COMMON.snapshot_selection(bm, uv_layer) != before_selection:
            raise HarnessError("count %d changed selection" % count)
        if COMMON.snapshot_active(obj, bm) != before_active:
            raise HarnessError("count %d changed active state" % count)
        if _mapping_delta(process, baseline_uv, process_uv) > 1.0e-7:
            raise HarnessError("count %d mapping delta exceeded tolerance" % count)
        if expected_master is not None:
            for group in process.get("groups", []):
                if tuple(group.get("master_key", ())) != expected_master:
                    raise HarnessError("count %d UV-area master mismatch" % count)
        process_runs[str(count)] = {
            "result_digest": process_digest,
            "uv_digest": process_uv_digest,
            "mapping_delta": _mapping_delta(process, baseline_uv, process_uv),
            "process": process,
        }
    return {
        "selected_keys": [list(key) for key in selected_keys],
        "allow_flipping": bool(allow_flipping),
        "sync_aligned_exact": int(sync.get("aligned_exact", 0)),
        "sync_group_count": int(sync.get("group_count", 0)),
        "sync_result_digest": sync_digest,
        "sync_uv_digest": sync_uv_digest,
        "process_runs": process_runs,
    }


def _reach_pipeline(stack_tools, obj, *, count=2, batch_size=1, delay=100):
    session = stack_tools._pro_create_session(
        bpy.context,
        {"detail_mappings": True},
        modal=True,
        process_worker_count=count,
        process_batch_size=batch_size,
        process_test_override=True,
        process_debug_delay_ms=delay,
        process_blender_binary=str(Path(bpy.app.binary_path).resolve()),
        process_blender_version=tuple(bpy.app.version),
        correspondence_mode=CORRESPONDENCE_MODE,
        mode=CORRESPONDENCE_MODE,
    )
    for _ in range(2000):
        session.step(active_budget_ms=12.0, max_correspondence=1)
        pool = getattr(session, "_process_pool", None)
        pipeline = getattr(session, "_process_pipeline", None)
        if pipeline is not None and pool is not None and pool.active_workers:
            return session
        if session.done:
            break
    raise HarnessError("MC3B pipeline did not reach an in-flight worker")


def _failure_guards(obj, island_tools, uv_utils, stack_tools):
    selected_keys = ((0,), (1,), (2,), (3,))
    bm, uv_layer, _by_key, baseline_uv, baseline_selection = COMMON.prepare_dedicated_case(
        obj, island_tools, uv_utils, selected_keys
    )
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)

    cancel_session = _reach_pipeline(stack_tools, obj, count=2, batch_size=1, delay=150)
    cancel_started = time.perf_counter()
    cancel_session.cancel("user_cancelled")
    cancel_ms = (time.perf_counter() - cancel_started) * 1000.0
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    if cancel_session.report.get("exact_loop_writes", 0) != 0:
        raise HarnessError("MC3B cancel exposed UV writes")
    if COMMON.snapshot_selection(bm, uv_layer) != before_selection or COMMON.snapshot_active(obj, bm) != before_active:
        raise HarnessError("MC3B cancel changed selection/active state")

    _restore(obj, baseline_uv, baseline_selection)
    invalidated = _reach_pipeline(stack_tools, obj, count=2, batch_size=1, delay=150)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    bm.faces[0].loops[0][uv_layer].uv.x += 0.125
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    invalidation_started = time.perf_counter()
    for _ in range(1000):
        invalidated.step(active_budget_ms=12.0, max_correspondence=1)
        if invalidated.done:
            break
    if not invalidated.done or invalidated.report.get("exact_loop_writes", 0) != 0:
        raise HarnessError("MC3B invalidation was not bounded zero-write")
    invalidation_ms = (time.perf_counter() - invalidation_started) * 1000.0
    _restore(obj, baseline_uv, baseline_selection)

    missing = _run_request(
        stack_tools,
        {
            **_process_request(2, 1),
            "process_python_executable": str(PROJECT_ROOT / "missing-mc3b-python.exe"),
        },
    )
    if missing.get("operator_result") != ["CANCELLED"] or not missing.get("error"):
        raise HarnessError("MC3B missing-helper failure was not explicit")
    _restore(obj, baseline_uv, baseline_selection)

    crash_session = _reach_pipeline(stack_tools, obj, count=2, batch_size=1, delay=150)
    pool = crash_session._process_pool
    worker = next(worker for worker in pool.workers if worker.process.poll() is None)
    first_pid = int(worker.pid)
    worker.process.kill()
    for _ in range(1000):
        crash_session.step(active_budget_ms=12.0, max_correspondence=1)
        if crash_session.report.get("process_retry_count", 0) >= 1:
            break
        if crash_session.done:
            break
    if crash_session.done or crash_session.report.get("process_retry_count") != 1:
        raise HarnessError("MC3B single crash did not reach one retry")
    replacement = next(
        worker for worker in crash_session._process_pool.workers
        if worker.process.poll() is None and int(worker.pid) != first_pid
    )
    second_pid = int(replacement.pid)
    replacement.process.kill()
    for _ in range(1000):
        crash_session.step(active_budget_ms=12.0, max_correspondence=1)
        if crash_session.done:
            break
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    if not crash_session.done or crash_session.report.get("exact_loop_writes", 0) != 0:
        raise HarnessError("MC3B repeated crash was not zero-write")
    _restore(obj, baseline_uv, baseline_selection)
    return {
        "cancel_ms": cancel_ms,
        "invalidation_ms": invalidation_ms,
        "cancel_zero_write": cancel_session.report.get("exact_loop_writes", 0) == 0,
        "invalidation_zero_write": invalidated.report.get("exact_loop_writes", 0) == 0,
        "missing_helper_error": missing.get("error"),
        "crash_pids": [first_pid, second_pid],
        "crash_retry_count": crash_session.report.get("process_retry_count", 0),
        "crash_zero_write": crash_session.report.get("exact_loop_writes", 0) == 0,
        "crash_cancelled": bool(crash_session.cancelled),
    }


def _smoke_counts(bpy_path, runtime, shape_module, payload_module, pool_module, similarity):
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    segments = tuple((points[index], points[(index + 1) % 4]) for index in range(4))
    descriptor = similarity.build_descriptor(
        segments, face_key=((0,),), topology={"face_count": 1, "edge_count": 4, "vertex_count": 4}
    )
    values = {}
    for count in (6, 8):
        nonce = "mc3b-smoke-%d" % count
        identity = payload_module.SnapshotIdentity(nonce, 0, "smoke")
        task = shape_module.make_shape_batch(
            identity,
            ((0, (0,), (1,), descriptor, descriptor),),
            shape_module.ShapeOptions(tolerance=0.1),
            batch_id="smoke",
        )
        pool = pool_module.PersistentWorkerPool(
            count,
            blender_binary=bpy_path,
            blender_version=tuple(bpy.app.version),
            session_nonce=nonce,
            use_cache=False,
        )
        try:
            result = pool.run((task,), timeout=30.0)
            if not result.complete:
                raise HarnessError("MC3B %d-worker smoke failed: %s" % (count, result.failure))
            values[str(count)] = {
                "pids": list(pool.worker_pids),
                "startup_ms": list(pool.startup_timings_ms),
            }
        finally:
            pool.close()
    return values


def main():
    if not FIXTURE_PATH.is_file() or sha256_file(FIXTURE_PATH) != EXPECTED_FIXTURE_SHA:
        raise HarnessError("dedicated fixture preflight SHA mismatch")
    if FIXTURE_SHA_BEFORE and FIXTURE_SHA_BEFORE != EXPECTED_FIXTURE_SHA:
        raise HarnessError("external fixture preflight SHA mismatch")
    common = COMMON
    uv_gpt = common.import_addon()
    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils
        from uv_gpt import pro_process_payload, pro_process_pool, pro_process_runtime, pro_process_shape

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
        for object_name, selected_keys, expected_master, allow_flipping in cases:
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                raise HarnessError("dedicated object missing: %s" % object_name)
            case_results.append(_case(obj, island_tools, uv_utils, stack_tools, selected_keys, expected_master, allow_flipping))
        observed = tuple(item["process_runs"]["1"]["process"].get("aligned_exact", 0) for item in case_results)
        if observed != EXPECTED_COUNTS:
            raise HarnessError("MC3B six-case oracle mismatch: %s" % (observed,))

        stress_obj = bpy.data.objects["PROSeam"]
        _restore(
            stress_obj,
            COMMON.snapshot_uv(island_tools.get_active_bmesh(bpy.context), island_tools.get_active_uv_layer(island_tools.get_active_bmesh(bpy.context), stress_obj)),
            COMMON.snapshot_selection(island_tools.get_active_bmesh(bpy.context), island_tools.get_active_uv_layer(island_tools.get_active_bmesh(bpy.context), stress_obj)),
        )
        smoke = _smoke_counts(str(Path(bpy.app.binary_path).resolve()), pro_process_runtime, pro_process_shape, pro_process_payload, pro_process_pool, __import__("uv_gpt.similarity_matcher", fromlist=["similarity_matcher"]))
        failure_guards = _failure_guards(bpy.data.objects["PROExact"], island_tools, uv_utils, stack_tools)

        # The fixed MC3A registration guard is exercised once more after all
        # process activity, including an explicit second unregister.
        unregister_started = time.perf_counter()
        uv_gpt.unregister()
        unregister_ms = (time.perf_counter() - unregister_started) * 1000.0
        second_started = time.perf_counter()
        uv_gpt.unregister()
        second_ms = (time.perf_counter() - second_started) * 1000.0
        uv_gpt.register()
        uv_gpt.unregister()

        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": sha256_file(FIXTURE_PATH),
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "bundled_python": str(expected_python),
            "blender_version": list(bpy.app.version),
            "correspondence_mode": CORRESPONDENCE_MODE,
            "oracle_aligned_exact": list(observed),
            "cases": case_results,
            "smoke_6_8": smoke,
            "failure_guards": failure_guards,
            "unregister": {
                "first_ms": unregister_ms,
                "second_ms": second_ms,
                "second_safe": True,
            },
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(common.clean_json(result), indent=2, sort_keys=True), encoding="utf-8")
        print("MC3B live process proof passed: oracle=%s; python=%s; result=%s" % (observed, expected_python, RESULT_PATH))
    finally:
        try:
            uv_gpt.unregister()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        print("MC3B live process proof failed: %s" % exc)
        raise SystemExit(1)
