"""PERF-P03 empirical Pro correspondence search-budget profile."""

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


PACKET_ID = "PERF-P03-SEARCH-BUDGET"
BUDGETS = (512, 1024, 2048, 4096)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"


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
MODE = _arg_value("--mode", "cc")
CORRESPONDENCE_MODE = "EXACT_ONLY"
FIXTURE_PATH = Path(
    _arg_value("--fixture", r"C:\Users\linhp\Downloads\cc.blend")
).resolve()
EXPECTED_SHA = _arg_value("--expected-sha", "").upper()
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(PROJECT_ROOT / ("benchmarks" if MODE == "cc" else "benchmarks") /
            ("pro_03_search_budget_cc.json" if MODE == "cc" else "pro_03_search_budget_dedicated.json")),
    )
).resolve()


def _load_common_harness():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("pro_search_common", path)
    if spec is None or spec.loader is None:
        raise HarnessError("Unable to load common Pro harness: %s" % path)
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


def _setup_bmesh(bm):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()


def _restore(obj, baseline_uv, baseline_selection):
    bm = bmesh.from_edit_mesh(obj.data)
    _setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("Profile UV layer disappeared")
    COMMON.restore_uv(bm, uv_layer, baseline_uv)
    COMMON.restore_selection(bm, uv_layer, baseline_selection)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return bm, uv_layer


def _select(obj, island_tools, uv_utils, by_key, keys, baseline_uv, baseline_selection):
    bm, uv_layer = _restore(obj, baseline_uv, baseline_selection)
    live_by_key = {
        COMMON.face_key(island): island
        for island in island_tools.get_uv_islands(
            bm,
            uv_layer,
            selected_only=False,
        )
    }
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(bm, uv_layer, [live_by_key[key] for key in keys])
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    return bm, uv_layer


def _run(stack_tools, budget):
    started = time.perf_counter()
    result = stack_tools.run_align_similar_pro(
        {
            "bpy_context": bpy.context,
            "correspondence_max_search": budget,
            "correspondence_mode": CORRESPONDENCE_MODE,
            "mode": CORRESPONDENCE_MODE,
        }
    )
    result = COMMON.clean_json(result)
    result["harness_elapsed_ms"] = (time.perf_counter() - started) * 1000.0
    return result


def _prepare_cc(obj, island_tools, uv_utils):
    settings = bpy.context.scene.uv_gpt_settings
    settings.duplicate_before_operations = False
    COMMON.activate_object(obj)
    settings.active_uv_map = TARGET_UV_NAME
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    if len(islands) != 577:
        raise HarnessError("Expected 577 current-fixture islands, got %s" % len(islands))
    by_key = {COMMON.face_key(island): island for island in islands}
    pair = (COMMON.MASTER_CANDIDATE_A, COMMON.MASTER_CANDIDATE_B)
    if pair[0] not in by_key or pair[1] not in by_key:
        raise HarnessError("Locked current-fixture pair is missing")
    baseline_uv = COMMON.snapshot_uv(bm, uv_layer)
    baseline_selection = COMMON.snapshot_selection(bm, uv_layer)
    baseline_active = COMMON.snapshot_active(obj, bm)
    return by_key, baseline_uv, baseline_selection, baseline_active, pair


def _run_cc_pair(obj, island_tools, uv_utils, stack_tools, state, budget):
    by_key, baseline_uv, baseline_selection, baseline_active, pair = state
    bm, uv_layer = _select(
        obj,
        island_tools,
        uv_utils,
        by_key,
        pair,
        baseline_uv,
        baseline_selection,
    )
    selected_before = COMMON.snapshot_selection(bm, uv_layer)
    before_uv = COMMON.snapshot_uv(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    result = _run(stack_tools, budget)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = COMMON.snapshot_uv(bm, uv_layer)
    after_selection = COMMON.snapshot_selection(bm, uv_layer)
    after_active = COMMON.snapshot_active(obj, bm)
    live_by_key = {
        COMMON.face_key(island): island
        for island in island_tools.get_uv_islands(
            bm,
            uv_layer,
            selected_only=False,
        )
    }
    if result["operator_result"] != ["FINISHED"]:
        raise HarnessError("Current focused budget run did not finish: %s" % result)
    if selected_before != after_selection or before_active != after_active:
        raise HarnessError("Current focused budget run changed context state")
    master_keys = set(COMMON.island_loop_keys(live_by_key[pair[1]]))
    selected_keys = set().union(
        *(set(COMMON.island_loop_keys(live_by_key[key])) for key in pair)
    )
    return {
        "budget": budget,
        "case": "current_locked_pair",
        "result": result,
        "master_delta": COMMON.max_delta(before_uv, after_uv, master_keys),
        "unselected_delta": COMMON.max_delta(
            before_uv,
            after_uv,
            set(before_uv) - selected_keys,
        ),
        "selection_unchanged": selected_before == after_selection,
        "active_unchanged": before_active == after_active,
        "baseline_active_unchanged": after_active == baseline_active,
    }


def _run_cc_sample(obj, island_tools, uv_utils, stack_tools, state, budget):
    by_key, baseline_uv, baseline_selection, baseline_active, pair = state
    ordered = sorted(by_key)
    sample = list(pair) + [key for key in ordered if key not in pair][:30]
    bm, uv_layer = _select(
        obj,
        island_tools,
        uv_utils,
        by_key,
        tuple(sample),
        baseline_uv,
        baseline_selection,
    )
    selected_before = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    result = _run(stack_tools, budget)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_selection = COMMON.snapshot_selection(bm, uv_layer)
    after_active = COMMON.snapshot_active(obj, bm)
    if result["operator_result"] != ["FINISHED"]:
        raise HarnessError("Current full-fixture sample did not finish: %s" % result)
    if selected_before != after_selection or before_active != after_active:
        raise HarnessError("Current sample changed context state")
    return {
        "budget": budget,
        "case": "current_fixture_sample",
        "selected_sample_count": len(sample),
        "result": result,
        "selection_unchanged": selected_before == after_selection,
        "active_unchanged": before_active == after_active,
        "baseline_active_unchanged": after_active == baseline_active,
    }


DEDICATED_CASES = (
    ("PROExact", ((0,), (1,), (2,), (3,)), False),
    ("PROExact", ((0,), (1,), (2,), (3,)), True),
    ("PROHole", ((0, 1, 2, 3), (4, 5, 6, 7)), False),
    ("PROInterior", ((0, 1, 2, 3), (4, 5, 6, 7)), False),
    ("PROSeam", ((0, 1, 2, 3, 4, 5, 6, 7, 8), (9, 10, 11, 12, 13, 14, 15, 16, 17)), False),
    ("PRONonIso", ((0,), (1, 2)), False),
)


def _run_dedicated_case(obj, island_tools, uv_utils, stack_tools, keys, flipping, budget):
    bm, uv_layer, by_key, baseline_uv, baseline_selection = (
        COMMON.prepare_dedicated_case(obj, island_tools, uv_utils, keys)
    )
    before_active = COMMON.snapshot_active(obj, bm)
    settings = bpy.context.scene.uv_gpt_settings
    settings.stack_allow_flipping = bool(flipping)
    result = _run(stack_tools, budget)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_selection = COMMON.snapshot_selection(bm, uv_layer)
    after_active = COMMON.snapshot_active(obj, bm)
    if result["operator_result"] != ["FINISHED"]:
        raise HarnessError("Dedicated budget run did not finish: %s" % result)
    if after_selection != baseline_selection or after_active != before_active:
        raise HarnessError("Dedicated budget run changed context state")
    _restore(obj, baseline_uv, baseline_selection)
    return {
        "budget": budget,
        "object": obj.name,
        "selected_keys": [list(key) for key in keys],
        "allow_flipping": bool(flipping),
        "result": result,
        "selection_unchanged": after_selection == baseline_selection,
        "active_unchanged": after_active == before_active,
    }


def main_cc(uv_gpt, island_tools, uv_utils, stack_tools):
    obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
    if obj is None or obj.type != "MESH":
        raise HarnessError("Current target object missing")
    state = _prepare_cc(obj, island_tools, uv_utils)
    rows = []
    for budget in BUDGETS:
        rows.append(_run_cc_pair(obj, island_tools, uv_utils, stack_tools, state, budget))
        rows.append(_run_cc_sample(obj, island_tools, uv_utils, stack_tools, state, budget))
    by_key, baseline_uv, baseline_selection, _active, _pair = state
    _restore(obj, baseline_uv, baseline_selection)
    return rows


def main_dedicated(uv_gpt, island_tools, uv_utils, stack_tools):
    rows = []
    for budget in BUDGETS:
        for object_name, keys, flipping in DEDICATED_CASES:
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                raise HarnessError("Dedicated object missing: %s" % object_name)
            rows.append(
                _run_dedicated_case(
                    obj,
                    island_tools,
                    uv_utils,
                    stack_tools,
                    keys,
                    flipping,
                    budget,
                )
            )
    return rows


def main():
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if EXPECTED_SHA and fixture_sha_before != EXPECTED_SHA:
        raise HarnessError("Search profile fixture SHA mismatch")
    uv_gpt = COMMON.import_addon()
    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        if MODE == "cc":
            rows = main_cc(uv_gpt, island_tools, uv_utils, stack_tools)
        elif MODE == "dedicated":
            rows = main_dedicated(uv_gpt, island_tools, uv_utils, stack_tools)
        else:
            raise HarnessError("Unknown search profile mode: %s" % MODE)
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "mode": MODE,
            "correspondence_mode": CORRESPONDENCE_MODE,
            "budgets": list(BUDGETS),
            "package": COMMON.package_metadata(uv_gpt),
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha_before,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "runs": rows,
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(COMMON.clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print("PERF-P03 search profile passed: mode=%s; rows=%s" % (MODE, len(rows)))
    finally:
        uv_gpt.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("PERF-P03 search profile failed: %s" % exc)
        raise SystemExit(1)
