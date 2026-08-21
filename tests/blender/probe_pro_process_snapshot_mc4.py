"""MC4-C4 snapshot-only probe for the portable Blender 5.0 BMesh adapter.

The probe deliberately calls only the resumable session preparation seam.  It
never enters records/planning, starts a worker, applies staged UVs, or saves
the fixture.  All failure detail is written to the caller-provided TEMP JSON.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback


sys.dont_write_bytecode = True

import bpy


EXPECTED_FIXTURE_SHA = (
    "49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
EXPECTED_ISLAND_COUNT = 577
SNAPSHOT_OPERATION_BUDGET = 96
SNAPSHOT_SLICE_MS = 12.0
PROBE_CORRESPONDENCE_MODE = "EXACT_ONLY"


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
        str(Path(__import__("tempfile").gettempdir()) / "uvgpt_mc4c3_snapshot.json"),
    )
).resolve()


def _load_common():
    path = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
    spec = importlib.util.spec_from_file_location("mc4c3_common", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load shared Blender harness: %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COMMON = _load_common()


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _clean(value):
    return COMMON.clean_json(value)


def _bounded_text(value, limit=8192):
    text = "" if value is None else str(value)
    return text[:limit]


def _atomic_write_json(path, value):
    """Write probe evidence atomically even when Blender exits nonzero later."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        ".%s.%s.%s.tmp" % (path.name, os.getpid(), time.monotonic_ns())
    )
    payload = json.dumps(_clean(value), indent=2, sort_keys=True)
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(str(temporary), str(path))
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _safe_sha256(path):
    try:
        return _sha256_file(path)
    except Exception as exc:
        return "unavailable:%s" % _bounded_text(exc, 192)


def _run_snapshot_only():
    started = time.perf_counter()
    fixture_before = _safe_sha256(FIXTURE_PATH)
    fixture_after = None
    uv_gpt = None
    registered = False
    session = None
    primary_error = None
    primary_traceback = None
    cleanup_errors = []
    islands = ()
    steps = 0
    snapshot_completed = False
    last_builder = None
    try:
        if fixture_before != EXPECTED_FIXTURE_SHA:
            raise RuntimeError("fixture SHA mismatch: %s" % fixture_before)
        if FIXTURE_SHA_BEFORE_EXTERNAL and fixture_before != FIXTURE_SHA_BEFORE_EXTERNAL:
            raise RuntimeError("external fixture SHA mismatch: %s" % fixture_before)

        uv_gpt = COMMON.import_addon()
        uv_gpt.register()
        registered = True
        import uv_gpt.island_tools as island_tools
        import uv_gpt.stack_tools as stack_tools
        import uv_gpt.uv_utils as uv_utils

        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH":
            raise RuntimeError("benchmark object missing: %s" % TARGET_OBJECT_NAME)

        _bm, _uv_layer, _by_key, _baseline_uv, _selection = COMMON.prepare_case(
            obj, island_tools, uv_utils
        )
        islands = island_tools.get_uv_islands(
            island_tools.get_active_bmesh(bpy.context),
            island_tools.get_active_uv_layer(
                island_tools.get_active_bmesh(bpy.context), obj
            ),
            selected_only=False,
        )
        if len(islands) != EXPECTED_ISLAND_COUNT:
            raise RuntimeError(
                "expected %d islands, got %d" % (EXPECTED_ISLAND_COUNT, len(islands))
            )

        session = stack_tools._pro_create_session(
            bpy.context,
            {
                "detail_mappings": True,
                "correspondence_mode": PROBE_CORRESPONDENCE_MODE,
                "mode": PROBE_CORRESPONDENCE_MODE,
            },
            modal=False,
            process_worker_count=1,
            process_batch_size=64,
            process_blender_binary=str(Path(bpy.app.binary_path).resolve()),
            process_blender_version=tuple(bpy.app.version),
            time_budget_ms=180000.0,
            correspondence_mode=PROBE_CORRESPONDENCE_MODE,
            mode=PROBE_CORRESPONDENCE_MODE,
        )
        while session._process_identity is None and steps < 200000:
            tick_started = time.perf_counter()
            session._tick_started = tick_started
            session._tick_deadline = tick_started + SNAPSHOT_SLICE_MS / 1000.0
            try:
                session._prepare()
            except Exception as exc:
                primary_error = exc
                primary_traceback = traceback.format_exc(limit=32)
                break
            finally:
                session._tick_started = None
                session._tick_deadline = None
            steps += 1
            if session._process_identity is not None:
                snapshot_completed = True
                break

        last_builder = getattr(session, "_process_snapshot_builder", None)
        diagnostics = getattr(last_builder, "failure_diagnostics", None)
        if primary_error is None and not snapshot_completed:
            primary_error = RuntimeError("snapshot probe bound exhausted")
    except BaseException as exc:
        if primary_error is None:
            primary_error = exc
            primary_traceback = traceback.format_exc(limit=32)
    finally:
        if session is not None and not session.done:
            try:
                session.cancel("mc4c3_snapshot_probe_complete")
            except Exception as exc:
                cleanup_errors.append(
                    {
                        "operation": "session.cancel",
                        "exception_type": type(exc).__name__,
                        "message": _bounded_text(exc, 192),
                        "traceback": _bounded_text(traceback.format_exc(limit=16)),
                    }
                )
        if registered and uv_gpt is not None:
            try:
                uv_gpt.unregister()
            except Exception as exc:
                cleanup_errors.append(
                    {
                        "operation": "uv_gpt.unregister",
                        "exception_type": type(exc).__name__,
                        "message": _bounded_text(exc, 192),
                        "traceback": _bounded_text(traceback.format_exc(limit=16)),
                    }
                )
        fixture_after = _safe_sha256(FIXTURE_PATH)

    builder = last_builder or getattr(session, "_process_snapshot_builder", None)
    diagnostics = getattr(builder, "failure_diagnostics", None)
    snapshot_capture = getattr(session, "_process_snapshot_capture", None)
    worker_pids = list(getattr(session, "_process_started_pids", ())) if session else []
    worker_started = bool(getattr(session, "_process_pool", None)) if session else False
    current_primitive = getattr(builder, "current_primitive", None)
    last_observed = getattr(builder, "_last_observed", None)
    record = {
        "status": "snapshot_completed" if snapshot_completed else "snapshot_failed",
        "correspondence_mode": PROBE_CORRESPONDENCE_MODE,
        "mode": PROBE_CORRESPONDENCE_MODE,
        "fixture": str(FIXTURE_PATH),
        "object": TARGET_OBJECT_NAME,
        "uv_map": TARGET_UV_NAME,
        "island_count": len(islands),
        "steps": int(steps),
        "ticks": int(steps),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "snapshot_completed": bool(snapshot_completed),
        "snapshot_digest": getattr(
            getattr(snapshot_capture, "identity", None), "snapshot_digest", None
        ),
        "snapshot_report": _clean(getattr(session, "report", {})) if session else {},
        "builder_phase": getattr(builder, "_phase", None),
        "last_phase": getattr(builder, "_phase", None),
        "last_primitive": _clean(current_primitive),
        "last_observed": _clean(last_observed),
        "builder_slices": getattr(builder, "slices", 0),
        "builder_operations": getattr(builder, "primitive_operations", 0),
        "builder_elapsed_ms": getattr(builder, "elapsed_ms", 0.0),
        "builder_max_slice_ms": getattr(builder, "max_slice_ms", 0.0),
        "builder_phase_transitions": _clean(
            getattr(builder, "phase_transitions", ())
        ),
        "diagnostics": _clean(diagnostics),
        "error": None if primary_error is None else _bounded_text(primary_error, 192),
        "traceback": _bounded_text(primary_traceback),
        "cleanup_errors": _clean(cleanup_errors),
        "worker_pids": worker_pids,
        "worker_started": worker_started,
        "writes": 0,
        "snapshot_only_worker_violation": bool(worker_started or worker_pids),
        "fixture_sha256_before_in_process": fixture_before,
        "fixture_sha256_after_in_process": fixture_after,
    }
    record["error"] = record["error"] or (
        "cleanup_error: %s" % cleanup_errors[0]["message"]
        if cleanup_errors
        else None
    )
    return record


def main():
    fixture_before = _safe_sha256(FIXTURE_PATH)
    record = None
    primary_error = None
    primary_traceback = None
    try:
        record = _run_snapshot_only()
    except BaseException as exc:
        primary_error = exc
        primary_traceback = traceback.format_exc(limit=32)
        record = {
            "status": "snapshot_failed",
            "snapshot_completed": False,
            "correspondence_mode": PROBE_CORRESPONDENCE_MODE,
            "mode": PROBE_CORRESPONDENCE_MODE,
            "error": _bounded_text(exc, 192),
            "traceback": _bounded_text(primary_traceback),
            "ticks": 0,
            "elapsed_ms": 0.0,
            "builder_slices": 0,
            "builder_operations": 0,
            "last_phase": None,
            "last_primitive": None,
            "last_observed": None,
            "diagnostics": None,
            "worker_started": False,
            "worker_pids": [],
            "writes": 0,
            "fixture_sha256_before_in_process": fixture_before,
            "fixture_sha256_after_in_process": _safe_sha256(FIXTURE_PATH),
        }
    finally:
        output = {
            "status": "diagnostic",
            "packet": "MC4-C4-SNAPSHOT-PROBE",
            "correspondence_mode": PROBE_CORRESPONDENCE_MODE,
            "mode": PROBE_CORRESPONDENCE_MODE,
            "fixture": str(FIXTURE_PATH),
            "fixture_sha256_before": fixture_before,
            "fixture_sha256_after_in_process": _safe_sha256(FIXTURE_PATH),
            "run": _clean(record or {}),
            "error": None if primary_error is None else _bounded_text(primary_error, 192),
            "traceback": _bounded_text(primary_traceback),
        }
        _atomic_write_json(RESULT_PATH, output)
        print(
            "MC4-C4 snapshot evidence written: %s; status=%s; error=%s"
            % (
                RESULT_PATH,
                output["run"].get("status"),
                output["run"].get("error"),
            )
        )


if __name__ == "__main__":
    main()
