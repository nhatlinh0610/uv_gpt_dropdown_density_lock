"""PERF-P07 bounded full-577 modal evidence harness.

The session is allowed to finish normally.  If a bounded wall-clock/tick
limit is reached, it cancels atomically and still writes a partial result so
the runner cannot hide the failure behind a missing JSON file.
"""

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


PACKET_ID = "PERF-P07-FULL-MODAL"
EXPECTED_SHA = (
    "49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 577
MAX_SESSION_SECONDS = 80.0
MAX_SESSION_TICKS = 30000


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
        str(PROJECT_ROOT / "benchmarks" / "pro_07_full_modal.json"),
    )
).resolve()


def _load_modal_harness():
    path = PROJECT_ROOT / "tests" / "blender" / "profile_pro_modal.py"
    spec = importlib.util.spec_from_file_location("pro_full_modal_p07_common", path)
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
    status = "passed"
    bounded_reason = None
    ticks = 0
    outer_tick_ms = []
    session = None
    before_selection = None
    before_active = None
    after_selection = None
    after_active = None
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
        evidence = {"detail_mappings": False}
        operator, session = MODAL._new_modal_operator(stack_tools, evidence)
        started = time.perf_counter()
        while not session.done:
            tick_started = time.perf_counter()
            result = operator.modal(bpy.context, MODAL._Event("TIMER"))
            outer_tick_ms.append((time.perf_counter() - tick_started) * 1000.0)
            ticks += 1
            if result == {"CANCELLED"} and not session.cancelled:
                raise HarnessError("Full modal session unexpectedly cancelled")
            if ticks >= MAX_SESSION_TICKS:
                status = "bounded_partial"
                bounded_reason = "tick_guard"
                session.cancel("p07_tick_guard")
                break
            if time.perf_counter() - started >= MAX_SESSION_SECONDS:
                status = "bounded_partial"
                bounded_reason = "wall_clock_guard"
                session.cancel("p07_wall_clock_guard")
                break

        bm = island_tools.get_active_bmesh(bpy.context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        after_selection = COMMON.snapshot_selection(bm, uv_layer)
        after_active = COMMON.snapshot_active(obj, bm)
        selection_unchanged = before_selection == after_selection
        active_unchanged = before_active == after_active
        if status == "bounded_partial":
            uv_delta = COMMON.max_delta(baseline_uv, COMMON.snapshot_uv(bm, uv_layer), set(baseline_uv))
            if uv_delta > 1.0e-12:
                raise HarnessError("Bounded partial session wrote UVs: %s" % uv_delta)
        else:
            uv_delta = None
        result_payload = {
            "status": status,
            "packet": PACKET_ID,
            "bounded_reason": bounded_reason,
            "package": COMMON.package_metadata(uv_gpt),
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_sha_before,
            "fixture_sha256_after_in_process": sha256_file(FIXTURE_PATH),
            "island_count": len(islands),
            "timer_ticks": ticks,
            "outer_max_tick_ms": max(outer_tick_ms) if outer_tick_ms else 0.0,
            "session_report": COMMON.clean_json(session.report if session else {}),
            "selection_unchanged": selection_unchanged,
            "active_unchanged": active_unchanged,
            "bounded_uv_delta": uv_delta,
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(COMMON.clean_json(result_payload), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            "PERF-P07 full-modal %s: ticks=%s; enum_ops=%s; records=%s; pairs=%s; max_tick_ms=%s"
            % (
                status,
                ticks,
                result_payload["session_report"].get("enum_primitive_ops", 0),
                result_payload["session_report"].get("planner_record_count", 0),
                result_payload["session_report"].get("candidate_pairs_processed", 0),
                result_payload["session_report"].get("max_tick_ms", 0.0),
            )
        )
    finally:
        uv_gpt.unregister()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("PERF-P07 full-modal failed: %s" % exc)
        raise SystemExit(1)
