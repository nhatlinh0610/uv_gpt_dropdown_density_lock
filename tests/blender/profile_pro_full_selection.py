"""PERF-P02 full-selection Blender 5.0 harness for Align Similar Pro."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time


sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "PERF-P02-FULL"
EXPECTED_FIXTURE_SHA = (
    "49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 577
MAX_SYNC_MS = 30_000.0
COOPERATIVE_YIELD_EVERY = None
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
    if index + 1 >= len(args):
        return default
    return args[index + 1]


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
        str(PROJECT_ROOT / "benchmarks" / "pro_02_full_selection.json"),
    )
).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()


def _load_common_harness():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("pro_full_common", path)
    if spec is None or spec.loader is None:
        raise HarnessError("Unable to load focused Pro harness: %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COMMON = _load_common_harness()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def stable_signature(result):
    """Hash only deterministic outcome fields, excluding elapsed timings."""

    value = {
        "operator_result": result.get("operator_result", []),
        "aligned_exact": result.get("aligned_exact", 0),
        "group_count": result.get("group_count", 0),
        "skipped_shape": result.get("skipped_shape", 0),
        "skipped_topology_unproven": result.get(
            "skipped_topology_unproven", 0
        ),
        "skipped_invalid_density": result.get("skipped_invalid_density", 0),
        "skipped_ownership": result.get("skipped_ownership", 0),
        "truncated": result.get("truncated", False),
        "partial": result.get("partial", False),
        "truncation_reasons": result.get("truncation_reasons", []),
        "candidate_pairs_planned": result.get("candidate_pairs_planned", 0),
        "candidate_pairs_processed": result.get("candidate_pairs_processed", 0),
        "planner_record_count": result.get("planner_record_count", 0),
        "planner_diagnostics": result.get("planner_diagnostics", {}),
        "group_summaries": result.get("group_summaries", []),
        "graphs_built": result.get("graphs_built", 0),
        "graph_cache_peak": result.get("graph_cache_peak", 0),
    }
    payload = json.dumps(
        COMMON.clean_json(value),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def activate_full_selection(obj, island_tools, uv_utils):
    settings = bpy.context.scene.uv_gpt_settings
    settings.duplicate_before_operations = False
    settings.active_uv_map = TARGET_UV_NAME
    COMMON.activate_object(obj)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    if len(islands) != EXPECTED_ISLAND_COUNT:
        raise HarnessError("Unexpected island count: %s" % len(islands))
    baseline_uv = COMMON.snapshot_uv(bm, uv_layer)
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(bm, uv_layer, islands)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    selected = island_tools.get_selected_uv_islands(bm, uv_layer)
    if len(selected) != EXPECTED_ISLAND_COUNT:
        raise HarnessError("Full selection mismatch: %s" % len(selected))
    return (
        bm,
        uv_layer,
        baseline_uv,
        COMMON.snapshot_selection(bm, uv_layer),
        COMMON.snapshot_active(obj, bm),
    )


def restore_baseline(bm, uv_layer, baseline_uv, baseline_selection):
    COMMON.restore_uv(bm, uv_layer, baseline_uv)
    COMMON.restore_selection(bm, uv_layer, baseline_selection)
    bm = bmesh.from_edit_mesh(bpy.context.edit_object.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    return bm, bm.loops.layers.uv.get(TARGET_UV_NAME)


def run_once(
    obj,
    island_tools,
    stack_tools,
    baseline_uv,
    baseline_selection,
    baseline_active,
    run_kind,
    run_index,
):
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    bm, uv_layer = restore_baseline(
        bm,
        uv_layer,
        baseline_uv,
        baseline_selection,
    )
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    started = time.perf_counter()
    result = stack_tools.run_align_similar_pro(
        {
            "bpy_context": bpy.context,
            "cooperative_yield_every": COOPERATIVE_YIELD_EVERY,
            "correspondence_mode": CORRESPONDENCE_MODE,
            "mode": CORRESPONDENCE_MODE,
        }
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_selection = COMMON.snapshot_selection(bm, uv_layer)
    after_active = COMMON.snapshot_active(obj, bm)
    if result.get("operator_result") != ["FINISHED"]:
        raise HarnessError("Full Pro operator did not finish: %s" % result)
    if before_selection != after_selection:
        raise HarnessError("Full Pro changed UV/mesh selection")
    if before_active != after_active or before_active != baseline_active:
        raise HarnessError("Full Pro changed active state")
    return {
        "run_kind": run_kind,
        "run_index": run_index,
        "elapsed_ms": elapsed_ms,
        "under_sync_budget": elapsed_ms <= MAX_SYNC_MS,
        "stable_signature": stable_signature(result),
        "result": COMMON.clean_json(result),
        "selection_unchanged": before_selection == after_selection,
        "active_unchanged": before_active == after_active,
    }


def main():
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if fixture_sha_before != EXPECTED_FIXTURE_SHA:
        raise HarnessError("Fixture SHA mismatch: %s" % fixture_sha_before)
    if FIXTURE_SHA_BEFORE_EXTERNAL and fixture_sha_before != FIXTURE_SHA_BEFORE_EXTERNAL:
        raise HarnessError("External fixture SHA mismatch: %s" % fixture_sha_before)

    uv_gpt = COMMON.import_addon()
    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH":
            raise HarnessError("Target object missing: %s" % TARGET_OBJECT_NAME)
        bm, uv_layer, baseline_uv, baseline_selection, baseline_active = (
            activate_full_selection(obj, island_tools, uv_utils)
        )
        runs = []
        first = run_once(
            obj,
            island_tools,
            stack_tools,
            baseline_uv,
            baseline_selection,
            baseline_active,
            "warmup",
            0,
        )
        runs.append(first)
        status = "passed" if first["under_sync_budget"] else "diagnostic_over_budget"
        if status == "passed":
            for run_index in range(3):
                measured = run_once(
                    obj,
                    island_tools,
                    stack_tools,
                    baseline_uv,
                    baseline_selection,
                    baseline_active,
                    "measured",
                    run_index,
                )
                runs.append(measured)
                if not measured["under_sync_budget"]:
                    status = "diagnostic_over_budget"
                    break
        measured = [run for run in runs if run["run_kind"] == "measured"]
        signatures = [run["stable_signature"] for run in runs]
        deterministic = len(set(signatures)) == 1
        result = {
            "status": status,
            "packet": PACKET_ID,
            "package": COMMON.package_metadata(uv_gpt),
            "correspondence_mode": CORRESPONDENCE_MODE,
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha_before,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "object": TARGET_OBJECT_NAME,
            "uv_map": TARGET_UV_NAME,
            "island_count": EXPECTED_ISLAND_COUNT,
            "warmup_runs": 1,
            "measured_runs": len(measured),
            "runs": runs,
            "deterministic_signature": deterministic,
            "stable_signatures": signatures,
            "measured_elapsed_ms": [run["elapsed_ms"] for run in measured],
            "aligned_exact": [
                run["result"].get("aligned_exact", 0) for run in measured
            ],
            "group_counts": [
                run["result"].get("group_count", 0) for run in measured
            ],
            "truncated_runs": [
                run["result"].get("truncated", False) for run in runs
            ],
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(COMMON.clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            "PERF-P02 full selection %s: runs=%s; measured=%s; elapsed=%s"
            % (status, len(runs), len(measured), result["measured_elapsed_ms"])
        )
    finally:
        uv_gpt.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("PERF-P02 full selection failed: %s" % exc)
        raise SystemExit(1)
