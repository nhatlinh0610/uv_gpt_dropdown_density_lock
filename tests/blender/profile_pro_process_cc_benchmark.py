"""Bounded T2R4L benchmark profile for the current authoritative cc.blend.

The existing MC4 profile owns the product-session and UV oracle mechanics.  This
thin benchmark wrapper supplies the newly discovered target metadata, forces the
same fused/group-first route for both concrete modes, and records additional
test-only tick/mapping contract evidence.  It never saves the blend.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


sys.dont_write_bytecode = True

EXPECTED_FIXTURE_SHA = (
    "3398B55425512AC0FCADCDFC8535B21847F209ADE0C25EDCA757B635D3DF145B"
)
EXPECTED_FIXTURE_LENGTH = 3638416
TARGET_OBJECT_NAME = "Bottom.003"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 484
TARGET_RULE = "visible_mesh_max_polygons_then_name; active_render_uv_layer"
SELECTION_RULE = "select_all_target_uv_islands_after_explicit_object_activation"
WORKER_MODE = "external_bundled_python_group_first"
TICK_LIMIT_MS = 250.0


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
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(Path(__import__("tempfile").gettempdir()) / "uv_gpt_t2r4l_cc.json"),
    )
).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
MODE = _arg_value("--mode", "HYBRID").strip().upper()
WORKER_COUNT = int(_arg_value("--worker-count", "1"))
RUN_ID = _arg_value("--run-id", "t2r4l")
TIME_BUDGET_MS = float(_arg_value("--time-budget-ms", "300000"))
BATCH_SIZE = int(_arg_value("--batch-size", "32"))

if MODE not in {"VERIFIED_NEAREST_ONLY", "EXACT_ONLY"}:
    raise RuntimeError("T2R4L requires VERIFIED_NEAREST_ONLY or EXACT_ONLY")
if WORKER_COUNT not in {1, 4}:
    raise RuntimeError("T2R4L worker count must be 1 or 4")


def _load_mc4_profile():
    path = PROJECT_ROOT / "tests" / "blender" / "profile_pro_process_mc4.py"
    spec = importlib.util.spec_from_file_location("t2r4l_mc4_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load the maintained MC4 profile: %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Keep the pure benchmark-contract helpers importable by Blender-independent
# unit tests.  MC4 imports bpy, so load it only for the actual Blender run.
BASE = None
TICK_TRACE = []
LOOP_KEYS_BY_KEY = {}


def _install_runtime_instrumentation():
    original_step = None

    def instrumented_import_addon():
        nonlocal original_step
        addon = BASE.COMMON.import_addon_original()
        import uv_gpt.stack_tools as stack_tools

        if original_step is None:
            original_step = stack_tools._ProAlignSession.step

            def instrumented_step(session, *args, **kwargs):
                info = original_step(session, *args, **kwargs)
                stage = str(
                    getattr(session, "_tick_stage", None)
                    or (info or {}).get("state", "")
                )
                tick_ms = float((info or {}).get("tick_ms", 0.0) or 0.0)
                TICK_TRACE.append(
                    {
                        "tick_ms": tick_ms,
                        "stage": stage,
                        "state": str((info or {}).get("state", "")),
                    }
                )
                return info

            stack_tools._ProAlignSession.step = instrumented_step
        return addon

    BASE.COMMON.import_addon_original = BASE.COMMON.import_addon
    BASE.COMMON.import_addon = instrumented_import_addon

    original_prepare = BASE._prepare_full

    def instrumented_prepare(obj, island_tools, uv_utils):
        BASE.COMMON.activate_object(obj)
        value = original_prepare(obj, island_tools, uv_utils)
        _bm, _uv_layer, islands, _by_key, _baseline_uv, _selection, _active = value
        LOOP_KEYS_BY_KEY.clear()
        for island in islands:
            key = tuple(BASE.COMMON.face_key(island))
            LOOP_KEYS_BY_KEY[key] = tuple(BASE.COMMON.island_loop_keys(island))
        return value

    BASE._prepare_full = instrumented_prepare


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _flatten_mapping_pairs(group):
    """Return member-scoped mapping buckets without flattening their scope."""

    raw = group.get("mapping_pairs", ()) or ()

    def is_loop_key(value):
        return isinstance(value, (list, tuple)) and all(
            not isinstance(item, (list, tuple, dict)) for item in value
        )

    def is_pair(value):
        return (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and is_loop_key(value[0])
            and is_loop_key(value[1])
        )

    if isinstance(raw, (list, tuple)) and raw and all(is_pair(item) for item in raw):
        return [list(raw)]
    return [
        list(bucket)
        for bucket in raw
        if isinstance(bucket, (list, tuple))
        and all(is_pair(item) for item in bucket)
    ]


def _mapping_contract_from_summary(summary):
    """Audit a persisted runner summary without inventing raw loop evidence.

    The runner keeps per-group pair counts but not the nested raw mappings.
    Equal actual/expected counts therefore clear the historical duplicate-
    master false positive, while the result explicitly records that detailed
    per-member uniqueness was not retained in this summary artifact.
    """

    issues = []
    group_rows = []
    for row in summary.get("groups", ()) or ():
        expected = _as_int(row.get("expected_mapping_pair_count"))
        actual = _as_int(row.get("mapping_pair_count"))
        row_issues = []
        if expected and actual != expected:
            row_issues.append(
                "incomplete bijection group %r: pairs=%d expected=%d"
                % (tuple(row.get("master_key", ())), actual, expected)
            )
            issues.extend(row_issues)
        group_rows.append(
            {
                "master_key": row.get("master_key", []),
                "member_count": _as_int(row.get("member_count")),
                "mapping_pair_count": actual,
                "expected_mapping_pair_count": expected,
                "member_scoped": True,
                "raw_mapping_detail_available": False,
                "bijective": not row_issues,
            }
        )
    return {
        "passed": not issues,
        "issues": issues,
        "groups": group_rows,
        "raw_mapping_detail_available": False,
        "summary_only": True,
    }


def _mapping_contract(result):
    issues = []
    group_rows = []
    for group in result.get("groups", ()) or ():
        master_key = tuple(group.get("master_key", ()))
        member_keys = [tuple(key) for key in group.get("member_keys", ()) or ()]
        buckets = _flatten_mapping_pairs(group)
        expected_count = sum(len(LOOP_KEYS_BY_KEY.get(key, ())) for key in member_keys)
        member_rows = []
        all_candidates = set()
        if len(buckets) != len(member_keys):
            issues.append(
                "mapping member coverage mismatch for group %r: mappings=%d members=%d"
                % (master_key, len(buckets), len(member_keys))
            )
        for index, member_key in enumerate(member_keys):
            bucket = buckets[index] if index < len(buckets) else []
            pairs = [(tuple(pair[0]), tuple(pair[1])) for pair in bucket]
            candidates = [pair[0] for pair in pairs]
            sources = [pair[1] for pair in pairs]
            expected_member = tuple(LOOP_KEYS_BY_KEY.get(member_key, ()))
            expected_master = tuple(LOOP_KEYS_BY_KEY.get(master_key, ()))
            member_issues = []
            if len(candidates) != len(set(candidates)):
                member_issues.append("duplicate candidate loop")
            if len(sources) != len(set(sources)):
                member_issues.append("duplicate master loop")
            if expected_member and set(candidates) != set(expected_member):
                member_issues.append("candidate loop coverage mismatch")
            if expected_master and set(sources) != set(expected_master):
                member_issues.append("master loop coverage mismatch")
            if expected_member and len(pairs) != len(expected_member):
                member_issues.append(
                    "member pair count=%d expected=%d"
                    % (len(pairs), len(expected_member))
                )
            for candidate in candidates:
                if candidate in all_candidates:
                    member_issues.append("duplicate candidate loop across members")
                all_candidates.add(candidate)
            if member_issues:
                issues.extend(
                    "%s for member %r in group %r"
                    % (reason, member_key, master_key)
                    for reason in sorted(set(member_issues))
                )
            member_rows.append(
                {
                    "member_key": list(member_key),
                    "mapping_pair_count": len(pairs),
                    "expected_mapping_pair_count": len(expected_member),
                    "bijective": not member_issues,
                }
            )
        actual_count = sum(len(bucket) for bucket in buckets)
        if expected_count and actual_count != expected_count:
            issues.append(
                "incomplete bijection group %r: pairs=%d expected=%d"
                % (master_key, actual_count, expected_count)
            )
        group_rows.append(
            {
                "master_key": list(master_key),
                "member_count": len(member_keys),
                "mapping_pair_count": actual_count,
                "expected_mapping_pair_count": expected_count,
                "member_rows": member_rows,
                "bijective": not any(
                    row["bijective"] is False for row in member_rows
                ),
            }
        )
    return {
        "passed": not issues,
        "issues": issues,
        "groups": group_rows,
        "raw_mapping_detail_available": True,
        "summary_only": False,
    }


def _area_contract(result):
    issues = []
    area_by_key = {}
    for item in result.get("uv_area_by_key", ()) or ():
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                area_by_key[tuple(item[0])] = float(item[1])
            except (TypeError, ValueError):
                continue
    reported_master_keys = [
        tuple(key) for key in result.get("uv_area_masters", ()) or ()
    ]
    reported_masters = set(reported_master_keys)
    reported_areas = result.get("uv_area_master_areas", ()) or ()
    if reported_areas and len(reported_areas) != len(reported_master_keys):
        issues.append(
            "uv_area_master_areas count=%d does not match uv_area_masters count=%d"
            % (len(reported_areas), len(reported_master_keys))
        )
    reported_area_by_key = {}
    for key, value in zip(reported_master_keys, reported_areas):
        try:
            reported_area_by_key[key] = float(value)
        except (TypeError, ValueError):
            issues.append("invalid reported UV-area master value: %r" % (key,))
    rows = []
    for group in result.get("groups", ()) or ():
        master = tuple(group.get("master_key", ()))
        members = [tuple(key) for key in group.get("member_keys", ()) or ()]
        master_area = area_by_key.get(master)
        if master not in reported_masters:
            issues.append("group master missing from uv_area_masters: %r" % (master,))
        if master_area is None:
            issues.append("group master missing UV area: %r" % (master,))
        reported_area = reported_area_by_key.get(master)
        if (
            reported_area is not None
            and master_area is not None
            and abs(reported_area - master_area) > 1.0e-9
        ):
            issues.append("reported UV-area mismatch for group master: %r" % (master,))
        larger_members = []
        if master_area is not None:
            for member in members:
                member_area = area_by_key.get(member)
                if member_area is not None and member_area > master_area + 1.0e-9:
                    larger_members.append(list(member))
        if larger_members:
            issues.append(
                "UV-area master is not largest for %r: %r" % (master, larger_members)
            )
        rows.append(
            {
                "master_key": list(master),
                "master_area": master_area,
                "member_count": len(members),
                "larger_members": larger_members,
            }
        )
    return {
        "passed": not issues,
        "issues": issues,
        "reported_master_count": len(reported_masters),
        "rows": rows,
    }


def _mode_contract(result, requested_mode=None):
    counters = {
        name: _as_int(result.get(name))
        for name in (
            "process_nearest_attempted",
            "process_nearest_accepted",
            "process_nearest_fallback",
            "process_nearest_seeded_jobs_planned",
            "process_nearest_seedless_jobs_planned",
            "process_nearest_fallback_exact_calls",
            "process_nearest_seed_missing",
            "process_nearest_fast_miss",
            "process_exact_fallback_calls",
            "process_exact_primary_calls",
            "process_exact_pairs_submitted",
            "process_exact_pairs_completed",
            "process_exact_accepted",
            "direct_exact_jobs_completed",
            "direct_exact_jobs_planned",
            "process_graph_rejected_before_nearest",
        )
    }
    issues = []
    expected_mode = str(requested_mode or MODE).strip().upper()
    actual_mode = str(result.get("correspondence_mode", result.get("mode", "")))
    if actual_mode != expected_mode:
        issues.append(
            "worker correspondence mode=%s expected=%s"
            % (actual_mode, expected_mode)
        )
    if str(result.get("mode", "")) != expected_mode:
        issues.append("worker mode field did not preserve requested mode")
    if str(result.get("worker_mode", "")) != WORKER_MODE:
        issues.append("worker_mode=%s expected=%s" % (result.get("worker_mode"), WORKER_MODE))
    if not bool(result.get("process_fused")) or not bool(result.get("process_group_first")):
        issues.append("fused/group-first route was not active")
    if not bool(result.get("process_nearest_accounting_valid")):
        issues.append("process nearest/exact accounting was not valid")

    thread_caps = result.get("process_thread_caps") or {}
    thread_caps_ok = bool(thread_caps) and all(str(value) == "1" for value in thread_caps.values())
    if not thread_caps_ok:
        issues.append("thread caps were not all 1: %r" % (thread_caps,))

    direct_completed = counters["direct_exact_jobs_completed"]
    graph_rejected = counters["process_graph_rejected_before_nearest"]
    exact_primary = counters["process_exact_primary_calls"]
    if expected_mode == "VERIFIED_NEAREST_ONLY":
        for field in (
            "process_nearest_fallback_exact_calls",
            "process_exact_fallback_calls",
            "process_exact_primary_calls",
        ):
            if counters[field] != 0:
                issues.append("FAST %s=%d, expected zero" % (field, counters[field]))
        if (
            counters["process_graph_rejected_before_nearest"]
            + counters["process_nearest_seed_missing"]
            + counters["process_nearest_attempted"]
            != direct_completed
        ):
            issues.append(
                "FAST graph/seed/nearest accounting mismatch: graph=%d seed_missing=%d attempted=%d completed=%d"
                % (
                    counters["process_graph_rejected_before_nearest"],
                    counters["process_nearest_seed_missing"],
                    counters["process_nearest_attempted"],
                    direct_completed,
                )
            )
        if counters["process_nearest_attempted"] != (
            counters["process_nearest_accepted"] + counters["process_nearest_fast_miss"]
        ):
            issues.append("FAST nearest accepted/miss accounting mismatch")
        solver_summary = {
            "exact_primary_calls": 0,
            "exact_fallback_calls": 0,
            "nearest_attempted": counters["process_nearest_attempted"],
            "nearest_verified": counters["process_nearest_accepted"],
            "nearest_unverified_or_missed": counters["process_nearest_fast_miss"],
        }
    elif expected_mode == "EXACT_ONLY":
        for field in (
            "process_nearest_attempted",
            "process_nearest_accepted",
            "process_nearest_fallback",
            "process_nearest_fallback_exact_calls",
            "process_nearest_seed_missing",
            "process_nearest_fast_miss",
            "process_exact_fallback_calls",
        ):
            if counters[field] != 0:
                issues.append("EXACT %s=%d, expected zero" % (field, counters[field]))
        if graph_rejected + exact_primary != direct_completed:
            issues.append(
                "EXACT primary accounting mismatch: graph_rejected=%d primary=%d completed=%d"
                % (graph_rejected, exact_primary, direct_completed)
            )
        solver_summary = {
            "exact_primary_calls": exact_primary,
            "exact_fallback_calls": 0,
            "nearest_attempted": 0,
            "nearest_verified": 0,
            "nearest_unverified_or_missed": 0,
        }
    else:
        issues.append("unsupported benchmark mode=%s" % expected_mode)
        solver_summary = {
            "exact_primary_calls": exact_primary,
            "exact_fallback_calls": counters["process_exact_fallback_calls"],
            "nearest_attempted": counters["process_nearest_attempted"],
            "nearest_verified": counters["process_nearest_accepted"],
            "nearest_unverified_or_missed": counters["process_nearest_fast_miss"],
        }
    pair_phase_counters = {
        "direct_resident_group_pairs_submitted": counters["process_exact_pairs_submitted"],
        "direct_resident_group_pairs_completed": counters["process_exact_pairs_completed"],
        "direct_resident_group_pairs_accepted": counters["process_exact_accepted"],
    }
    return {
        "passed": not issues,
        "issues": issues,
        "actual_mode": actual_mode,
        "worker_mode": result.get("worker_mode"),
        "thread_caps": thread_caps,
        "counters": counters,
        "pair_phase_counters": pair_phase_counters,
        "pair_phase_counter_semantics": (
            "Raw process_exact_pairs_* fields are retained for compatibility; "
            "they count direct/resident group pair phase coverage, not "
            "CorrespondenceSearch construction or calls."
        ),
        "solver_summary": solver_summary,
    }


def _tick_summary(result):
    all_ticks = [float(item.get("tick_ms", 0.0)) for item in TICK_TRACE]
    startup_ticks = [
        float(item.get("tick_ms", 0.0))
        for item in TICK_TRACE
        if str(item.get("stage", "")) == "process_startup"
    ]
    max_tick = max(all_ticks or [0.0])
    startup_max = max(startup_ticks or [0.0])
    report_max = _as_float(result.get("max_tick_ms"))
    if report_max > max_tick:
        max_tick = report_max
    if not startup_ticks and str(result.get("max_tick_stage", "")) == "process_startup":
        startup_max = report_max
    return {
        "tick_count": len(all_ticks),
        "max_tick_ms": max_tick,
        "max_startup_tick_ms": startup_max,
        "startup_tick_count": len(startup_ticks),
        "max_tick_stage": result.get("max_tick_stage"),
        "process_startup_ms": _as_float(result.get("process_startup_ms")),
        "tick_limit_ms": TICK_LIMIT_MS,
        "within_limit": max_tick <= TICK_LIMIT_MS,
        "stage_max_ms": {
            stage: max(
                float(item.get("tick_ms", 0.0))
                for item in TICK_TRACE
                if str(item.get("stage", "")) == stage
            )
            for stage in sorted({str(item.get("stage", "")) for item in TICK_TRACE})
        },
    }


def _audit_persisted_summary(payload):
    """Re-evaluate a persisted runner summary without opening Blender."""

    if not isinstance(payload, dict):
        raise RuntimeError("persisted benchmark summary is not an object")
    contract = payload.get("contract")
    if not isinstance(contract, dict):
        raise RuntimeError("persisted benchmark summary has no contract")
    mode_data = contract.get("mode") or {}
    counters = dict(mode_data.get("counters") or {})
    requested_mode = str(payload.get("mode", mode_data.get("actual_mode", "")))
    direct_completed = _as_int(counters.get("direct_exact_jobs_completed"))
    graph_rejected = _as_int(counters.get("process_graph_rejected_before_nearest"))
    seed_missing = _as_int(counters.get("process_nearest_seed_missing"))
    nearest_attempted = _as_int(counters.get("process_nearest_attempted"))
    nearest_accepted = _as_int(counters.get("process_nearest_accepted"))
    fast_miss = _as_int(counters.get("process_nearest_fast_miss"))
    exact_fallback = _as_int(counters.get("process_exact_fallback_calls"))
    exact_primary = _as_int(counters.get("process_exact_primary_calls"))
    accounting_valid = (
        graph_rejected + seed_missing + nearest_attempted == direct_completed
        and nearest_attempted == nearest_accepted + fast_miss
        and exact_fallback == 0
        and exact_primary == 0
    )
    mode_result = {
        "correspondence_mode": mode_data.get("actual_mode", requested_mode),
        "mode": requested_mode,
        "worker_mode": mode_data.get("worker_mode", payload.get("worker_mode")),
        "process_fused": True,
        "process_group_first": True,
        "process_nearest_accounting_valid": accounting_valid,
        "process_thread_caps": payload.get("process_thread_caps") or {"summary": "1"},
        **counters,
    }
    mapping = _mapping_contract_from_summary(contract.get("mapping") or {})
    area_data = contract.get("uv_area") or {}
    area_issues = list(area_data.get("issues") or [])
    area_rows = list(area_data.get("rows") or [])
    if any(row.get("larger_members") for row in area_rows):
        area_issues.append("persisted UV-area oracle found a larger member")
    area = {
        "passed": not area_issues and bool(area_data.get("passed", False)),
        "issues": area_issues,
        "reported_master_count": _as_int(area_data.get("reported_master_count")),
        "rows": area_rows,
        "summary_only": True,
    }
    tick = dict(payload.get("tick_metrics") or {})
    tick["within_limit"] = _as_float(tick.get("max_tick_ms")) <= TICK_LIMIT_MS
    mode = _mode_contract(mode_result, requested_mode=requested_mode)
    return {
        "mode": mode,
        "mapping": mapping,
        "uv_area": area,
        "tick": tick,
        "raw_phase_counters": {
            key: counters.get(key, 0)
            for key in (
                "process_exact_pairs_submitted",
                "process_exact_pairs_completed",
                "process_exact_accepted",
            )
        },
    }


def _augment_and_validate():
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    run = payload.get("run") if isinstance(payload, dict) else None
    if not isinstance(run, dict):
        raise RuntimeError("benchmark result has no run record")
    result = run.get("result")
    if not isinstance(result, dict):
        result = run.get("session_report")
    if not isinstance(result, dict):
        raise RuntimeError("benchmark result has no session report")

    mapping = _mapping_contract(result)
    area = _area_contract(result)
    mode_contract = _mode_contract(result)
    ticks = _tick_summary(result)
    base_checks = {
        "full_completion": bool(run.get("full_completion")),
        "selection_unchanged": bool(run.get("selection_unchanged")),
        "active_unchanged": bool(run.get("active_unchanged")),
        "mapping_max_delta_ok": _as_float(run.get("mapping_max_delta")) <= 1.0e-7,
        "master_uv_delta_ok": _as_float(run.get("master_uv_delta")) <= 1.0e-10,
        "unselected_uv_delta_ok": _as_float(run.get("unselected_uv_delta")) <= 1.0e-10,
        "fixture_sha_before_ok": run.get("fixture_sha256_before_in_process") == EXPECTED_FIXTURE_SHA,
        "fixture_sha_after_ok": run.get("fixture_sha256_after_in_process") == EXPECTED_FIXTURE_SHA,
        "target_ok": run.get("object") == TARGET_OBJECT_NAME and run.get("uv_map") == TARGET_UV_NAME,
        "island_count_ok": _as_int(run.get("island_count")) == EXPECTED_ISLAND_COUNT,
    }
    checks = {
        **base_checks,
        "mode_contract": mode_contract["passed"],
        "mapping_bijection": mapping["passed"],
        "uv_area_master": area["passed"],
        "tick_limit": bool(ticks["within_limit"]),
    }
    issues = [name for name, passed in checks.items() if not passed]
    issues.extend(mode_contract["issues"])
    issues.extend(mapping["issues"])
    issues.extend(area["issues"])
    if issues:
        payload["status"] = "failed"
        payload["error"] = "T2R4L contract failure: " + "; ".join(issues)
        run["status"] = "failed"
        run["error"] = payload["error"]

    run["benchmark_target_rule"] = TARGET_RULE
    run["benchmark_selection_rule"] = SELECTION_RULE
    run["benchmark_fixture_length"] = EXPECTED_FIXTURE_LENGTH
    run["benchmark_contract"] = {
        "passed": not issues,
        "checks": checks,
        "issues": issues,
        "mode": mode_contract,
        "mapping": mapping,
        "uv_area": area,
    }
    run["tick_metrics"] = ticks
    run["tick_trace_stage_counts"] = {
        stage: sum(1 for item in TICK_TRACE if str(item.get("stage", "")) == stage)
        for stage in sorted({str(item.get("stage", "")) for item in TICK_TRACE})
    }
    payload["fixture_length"] = EXPECTED_FIXTURE_LENGTH
    payload["fixture_sha256_expected"] = EXPECTED_FIXTURE_SHA
    RESULT_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    print(
        "T2R4L profile: status=%s mode=%s workers=%d target=%s/%s islands=%d "
        "max_tick=%.3f startup_max=%.3f direct_pairs=%d exact_primary=%d nearest=%d"
        % (
            payload.get("status"),
            MODE,
            WORKER_COUNT,
            TARGET_OBJECT_NAME,
            TARGET_UV_NAME,
            _as_int(run.get("island_count")),
            ticks["max_tick_ms"],
            ticks["max_startup_tick_ms"],
            mode_contract["pair_phase_counters"][
                "direct_resident_group_pairs_completed"
            ],
            mode_contract["counters"]["process_exact_primary_calls"],
            mode_contract["counters"]["process_nearest_attempted"],
        )
    )
    if issues:
        raise RuntimeError(payload["error"])


def main():
    global BASE
    BASE = _load_mc4_profile()
    BASE.PACKET_ID = "T2R4L-CC-BENCHMARK"
    BASE.EXPECTED_FIXTURE_SHA = EXPECTED_FIXTURE_SHA
    BASE.TARGET_OBJECT_NAME = TARGET_OBJECT_NAME
    BASE.TARGET_UV_NAME = TARGET_UV_NAME
    BASE.EXPECTED_ISLAND_COUNT = EXPECTED_ISLAND_COUNT
    BASE.FIXTURE_PATH = FIXTURE_PATH
    BASE.FIXTURE_SHA_BEFORE_EXTERNAL = FIXTURE_SHA_BEFORE_EXTERNAL
    BASE.RESULT_PATH = RESULT_PATH
    BASE.WORKER_COUNT = WORKER_COUNT
    BASE.BATCH_SIZE = BATCH_SIZE
    BASE.RUN_ID = RUN_ID
    BASE.RUN_CLASS = "bounded_cc_real_fixture"
    BASE.SUPERSEDES_RUN_ID = ""
    BASE.TIME_BUDGET_MS = TIME_BUDGET_MS
    BASE.SCENARIO = "complete"
    BASE.PROCESS_FUSED = True
    BASE.PROCESS_GROUP_FIRST = True
    BASE.CORRESPONDENCE_MODE = MODE
    _install_runtime_instrumentation()
    BASE.main()
    _augment_and_validate()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("T2R4L profile failed: %s" % exc)
        raise SystemExit(1)
