"""PERF-P03 modal-path simulation on the dedicated exact Pro fixture."""

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


PACKET_ID = "PERF-P03-MODAL"
TARGET_UV_NAME = "UVMap.001"
SELECTED_KEYS = ((0,), (1,), (2,), (3,))
EXPECTED_MASTER = (0,)
SELECTION_EPSILON = 1.0e-12


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
    _arg_value(
        "--fixture",
        str(PROJECT_ROOT / "benchmarks" / "pro_02b_dedicated_fixture.blend"),
    )
).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
RESULT_PATH = Path(
    _arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_03_modal.json"),
    )
).resolve()
CORRESPONDENCE_MODE = _arg_value("--correspondence-mode", "EXACT_ONLY").strip().upper()
if CORRESPONDENCE_MODE not in {
    "HYBRID",
    "VERIFIED_NEAREST_ONLY",
    "EXACT_ONLY",
}:
    raise HarnessError("unsupported modal correspondence mode: %s" % CORRESPONDENCE_MODE)


def _load_common_harness():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("pro_modal_common", path)
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


def _restore_baseline(obj, baseline_uv, baseline_selection):
    bm = bmesh.from_edit_mesh(obj.data)
    _setup_bmesh(bm)
    uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer is None:
        raise HarnessError("Dedicated modal UV layer disappeared")
    COMMON.restore_uv(bm, uv_layer, baseline_uv)
    COMMON.restore_selection(bm, uv_layer, baseline_selection)
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return bm, uv_layer


def _prepare_case(obj, island_tools, uv_utils):
    settings = bpy.context.scene.uv_gpt_settings
    settings.duplicate_before_operations = False
    COMMON.activate_object(obj)
    settings.active_uv_map = TARGET_UV_NAME
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    by_key = {COMMON.face_key(island): island for island in islands}
    if not set(SELECTED_KEYS).issubset(by_key):
        raise HarnessError("Dedicated modal exact islands are missing")
    baseline_uv = COMMON.snapshot_uv(bm, uv_layer)
    uv_utils.set_all_uv_selection(bm, uv_layer, False)
    uv_utils.select_islands(
        bm,
        uv_layer,
        [by_key[key] for key in SELECTED_KEYS],
    )
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    baseline_selection = COMMON.snapshot_selection(bm, uv_layer)
    return bm, uv_layer, by_key, baseline_uv, baseline_selection


class _Event:
    def __init__(self, event_type):
        self.type = event_type


def _new_modal_operator(stack_tools, evidence):
    session_evidence = dict(evidence)
    session_evidence["correspondence_mode"] = CORRESPONDENCE_MODE
    session_evidence["mode"] = CORRESPONDENCE_MODE
    session = stack_tools._pro_create_session(
        bpy.context,
        session_evidence,
        modal=True,
        cooperative_yield_every=evidence.get("cooperative_yield_every"),
        correspondence_mode=CORRESPONDENCE_MODE,
        mode=CORRESPONDENCE_MODE,
    )
    # Blender operator RNA types cannot be constructed with a normal Python
    # call in background mode.  A small proxy still drives the registered
    # class's exact modal methods and event handling.
    if CORRESPONDENCE_MODE == "VERIFIED_NEAREST_ONLY":
        operator_class = stack_tools.UVGPT_OT_align_similar_pro_fast
    elif CORRESPONDENCE_MODE == "EXACT_ONLY":
        operator_class = stack_tools.UVGPT_OT_align_similar_pro_exact
    else:
        operator_class = stack_tools._UVGPT_OT_align_similar_pro_mode
    operator = types.SimpleNamespace(
        _session=session,
        _timer=None,
        bl_label=getattr(operator_class, "bl_label", "Align Similar Pro"),
        correspondence_mode=CORRESPONDENCE_MODE,
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
    operator.modal = types.MethodType(
        operator_class.modal,
        operator,
    )
    operator._session = session
    stack_tools._ACTIVE_PRO_OPERATOR = operator
    return operator, session


def _mapping_delta(result, before_uv, after_uv):
    maximum = 0.0
    for group in result.get("groups", []):
        if tuple(group["master_key"]) != EXPECTED_MASTER:
            raise HarnessError("Modal UV-area master mismatch")
        for mapping_pairs in group.get("mapping_pairs", []):
            for pair in mapping_pairs:
                candidate_key = tuple(pair[0])
                master_key = tuple(pair[1])
                expected = before_uv[master_key]
                actual = after_uv[candidate_key]
                maximum = max(
                    maximum,
                    abs(expected[0] - actual[0]),
                    abs(expected[1] - actual[1]),
                )
    return maximum


def run_completion(obj, island_tools, uv_utils, stack_tools):
    bm, uv_layer, by_key, baseline_uv, baseline_selection = _prepare_case(
        obj,
        island_tools,
        uv_utils,
    )
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    evidence = {"detail_mappings": True}
    operator, session = _new_modal_operator(stack_tools, evidence)
    ticks = 0
    tick_wall_ms = []
    while not session.done:
        started = time.perf_counter()
        result = operator.modal(bpy.context, _Event("TIMER"))
        tick_wall_ms.append((time.perf_counter() - started) * 1000.0)
        ticks += 1
        if ticks > 10000:
            raise HarnessError("Modal completion made no progress")
        if result == {"CANCELLED"}:
            raise HarnessError("Modal completion unexpectedly cancelled")
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = COMMON.snapshot_uv(bm, uv_layer)
    after_selection = COMMON.snapshot_selection(bm, uv_layer)
    after_active = COMMON.snapshot_active(obj, bm)
    if session.report.get("aligned_exact", 0) < 2:
        raise HarnessError("Modal exact completion aligned too few islands")
    if before_selection != after_selection or before_selection != baseline_selection:
        raise HarnessError("Modal completion changed selection")
    if before_active != after_active:
        raise HarnessError("Modal completion changed active state")
    mapping_delta = _mapping_delta(session.report, baseline_uv, after_uv)
    if mapping_delta > 1.0e-7:
        raise HarnessError("Modal exact mapping delta is %s" % mapping_delta)
    master_delta = COMMON.max_delta(
        baseline_uv,
        after_uv,
        set(COMMON.island_loop_keys(by_key[EXPECTED_MASTER])),
    )
    if master_delta > SELECTION_EPSILON:
        raise HarnessError("Modal master delta is %s" % master_delta)
    selected_loop_keys = set().union(
        *(set(COMMON.island_loop_keys(by_key[key])) for key in SELECTED_KEYS)
    )
    unselected_delta = COMMON.max_delta(
        baseline_uv,
        after_uv,
        set(baseline_uv) - selected_loop_keys,
    )
    if unselected_delta > SELECTION_EPSILON:
        raise HarnessError("Modal unselected delta is %s" % unselected_delta)
    return {
        "mode": "timer_simulation_completion",
        "correspondence_mode": CORRESPONDENCE_MODE,
        "ticks": ticks,
        "max_tick_wall_ms": max(tick_wall_ms) if tick_wall_ms else 0.0,
        "result": COMMON.clean_json(session.report),
        "mapping_max_delta": mapping_delta,
        "master_delta": master_delta,
        "unselected_delta": unselected_delta,
        "selection_unchanged": before_selection == after_selection,
        "active_unchanged": before_active == after_active,
    }


def run_cancel(obj, island_tools, uv_utils, stack_tools):
    bm, uv_layer, _by_key, baseline_uv, baseline_selection = _prepare_case(
        obj,
        island_tools,
        uv_utils,
    )
    before_uv = COMMON.snapshot_uv(bm, uv_layer)
    before_selection = COMMON.snapshot_selection(bm, uv_layer)
    before_active = COMMON.snapshot_active(obj, bm)
    evidence = {"detail_mappings": True}
    operator, session = _new_modal_operator(stack_tools, evidence)
    timer_result = operator.modal(bpy.context, _Event("TIMER"))
    if session.done:
        raise HarnessError("Cancel case completed before ESC could be delivered")
    cancel_result = operator.modal(bpy.context, _Event("ESC"))
    bm = island_tools.get_active_bmesh(bpy.context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    after_uv = COMMON.snapshot_uv(bm, uv_layer)
    after_selection = COMMON.snapshot_selection(bm, uv_layer)
    after_active = COMMON.snapshot_active(obj, bm)
    uv_delta = COMMON.max_delta(before_uv, after_uv, set(before_uv))
    if uv_delta > SELECTION_EPSILON:
        raise HarnessError("ESC cancellation wrote UVs: %s" % uv_delta)
    if before_selection != after_selection:
        raise HarnessError("ESC cancellation changed selection")
    if before_active != after_active:
        raise HarnessError("ESC cancellation changed active state")
    if session.report.get("exact_loop_writes", 0) != 0:
        raise HarnessError("ESC cancellation applied exact writes")
    if not session.cancelled or session.report.get("cancel_reason") != "user_cancelled":
        raise HarnessError("ESC cancellation reason was not recorded")
    return {
        "mode": "timer_simulation_esc_cancel",
        "correspondence_mode": CORRESPONDENCE_MODE,
        "timer_result": sorted(str(value) for value in timer_result),
        "cancel_result": sorted(str(value) for value in cancel_result),
        "result": COMMON.clean_json(session.report),
        "uv_delta": uv_delta,
        "selection_unchanged": before_selection == after_selection,
        "active_unchanged": before_active == after_active,
    }


def main():
    if not FIXTURE_PATH.is_file():
        raise HarnessError("Dedicated modal fixture missing: %s" % FIXTURE_PATH)
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if FIXTURE_SHA_BEFORE_EXTERNAL and fixture_sha_before != FIXTURE_SHA_BEFORE_EXTERNAL:
        raise HarnessError("Dedicated modal fixture SHA mismatch")
    uv_gpt = COMMON.import_addon()
    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        obj = bpy.data.objects.get("PROExact")
        if obj is None:
            raise HarnessError("Dedicated modal object PROExact is missing")
        completion = run_completion(obj, island_tools, uv_utils, stack_tools)
        cancel = run_cancel(obj, island_tools, uv_utils, stack_tools)
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "package": COMMON.package_metadata(uv_gpt),
            "correspondence_mode": CORRESPONDENCE_MODE,
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha_before,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "completion": completion,
            "cancel": cancel,
            "max_tick_ms": completion["result"].get("max_tick_ms", 0.0),
            "max_correspondence_ms": completion["result"].get(
                "max_correspondence_ms", 0.0
            ),
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(COMMON.clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            "PERF-P03 modal passed: ticks=%s; max_tick_ms=%s; max_corr_ms=%s"
            % (
                completion["ticks"],
                result["max_tick_ms"],
                result["max_correspondence_ms"],
            )
        )
    finally:
        uv_gpt.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("PERF-P03 modal failed: %s" % exc)
        raise SystemExit(1)
