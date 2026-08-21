"""PERF-P03 full-selection modal-path simulation at the locked Pro budget."""

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


PACKET_ID = "PERF-P03-FULL-MODAL"
EXPECTED_SHA = (
    "49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 577
COOPERATIVE_YIELD_EVERY = None


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
        str(PROJECT_ROOT / "benchmarks" / "pro_03_full_modal_1024.json"),
    )
).resolve()


def _load_modal_harness():
    path = PROJECT_ROOT / "tests" / "blender" / "profile_pro_modal.py"
    spec = importlib.util.spec_from_file_location("pro_full_modal_common", path)
    if spec is None or spec.loader is None:
        raise HarnessError("Unable to load modal harness: %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODAL = _load_modal_harness()
COMMON = MODAL.COMMON


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main():
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if fixture_sha_before != EXPECTED_SHA:
        raise HarnessError("Full modal fixture SHA mismatch")
    if (
        FIXTURE_SHA_BEFORE_EXTERNAL
        and fixture_sha_before != FIXTURE_SHA_BEFORE_EXTERNAL
    ):
        raise HarnessError("Full modal external fixture SHA mismatch")
    uv_gpt = COMMON.import_addon()
    uv_gpt.register()
    try:
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH":
            raise HarnessError("Full modal target object missing")
        settings = bpy.context.scene.uv_gpt_settings
        settings.duplicate_before_operations = False
        COMMON.activate_object(obj)
        settings.active_uv_map = TARGET_UV_NAME
        bm = island_tools.get_active_bmesh(bpy.context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        if len(islands) != EXPECTED_ISLAND_COUNT:
            raise HarnessError("Full modal island count mismatch")
        baseline_uv = COMMON.snapshot_uv(bm, uv_layer)
        uv_utils.set_all_uv_selection(bm, uv_layer, False)
        uv_utils.select_islands(bm, uv_layer, islands)
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        bm = island_tools.get_active_bmesh(bpy.context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        before_selection = COMMON.snapshot_selection(bm, uv_layer)
        before_active = COMMON.snapshot_active(obj, bm)
        evidence = {
            "detail_mappings": False,
            "cooperative_yield_every": COOPERATIVE_YIELD_EVERY,
        }
        operator, session = MODAL._new_modal_operator(stack_tools, evidence)
        ticks = 0
        outer_tick_ms = []
        while not session.done:
            started = time.perf_counter()
            result = operator.modal(bpy.context, MODAL._Event("TIMER"))
            outer_tick_ms.append((time.perf_counter() - started) * 1000.0)
            ticks += 1
            if ticks > 10000:
                raise HarnessError("Full modal session made no progress")
            if result == {"CANCELLED"}:
                raise HarnessError("Full modal session unexpectedly cancelled")
        bm = island_tools.get_active_bmesh(bpy.context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        after_selection = COMMON.snapshot_selection(bm, uv_layer)
        after_active = COMMON.snapshot_active(obj, bm)
        if before_selection != after_selection:
            raise HarnessError("Full modal session changed selection")
        if before_active != after_active:
            raise HarnessError("Full modal session changed active state")
        result = {
            "status": "passed",
            "packet": PACKET_ID,
            "package": COMMON.package_metadata(uv_gpt),
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha_before,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "island_count": len(islands),
            "timer_ticks": ticks,
            "outer_max_tick_ms": max(outer_tick_ms) if outer_tick_ms else 0.0,
            "session_report": COMMON.clean_json(session.report),
            "selection_unchanged": before_selection == after_selection,
            "active_unchanged": before_active == after_active,
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(COMMON.clean_json(result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            "PERF-P03 full modal passed: ticks=%s; max_tick_ms=%s; max_corr_ms=%s"
            % (
                ticks,
                result["session_report"].get("max_tick_ms", 0.0),
                result["session_report"].get("max_correspondence_ms", 0.0),
            )
        )
    finally:
        uv_gpt.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("PERF-P03 full modal failed: %s" % exc)
        raise SystemExit(1)
