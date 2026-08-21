"""MATCH-04 smoke test for the extracted release ZIP.

The add-on is imported only from ``--package-root``.  Workspace test helpers
are used for deterministic fixture setup and measurement, but the operator and
all add-on modules come from the extracted package copy.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback


sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "MATCH-04/package-smoke"
SCHEMA_VERSION = "match-04-package-smoke-v1"
DEFAULT_FIXTURE = Path(r"C:\Users\linhp\Downloads\cc.blend")
EXPECTED_FIXTURE_SHA = (
    "840EA32C822784201EFAB30B9441A98621E6FBD87DC9BDD431B7EB90A2FF93CD"
)
EXPECTED_VERSION = (1, 2, 6)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
TARGET_FACE_KEY = (602, 603, 604, 605)
EXPECTED_CANDIDATE_FACE_KEY = (9448, 9484, 9967, 17967)
QUALITY_TOLERANCE = 1.0e-4
SELECTION_EPSILON = 1.0e-12
WARMUP_RUNS = 1
MEASURED_RUNS = 3


class SmokeError(RuntimeError):
    """Raised when the extracted package does not satisfy the smoke contract."""


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


PROJECT_ROOT = Path(_arg_value("--project-root", Path.cwd())).resolve()
FIXTURE_PATH = Path(_arg_value("--fixture", DEFAULT_FIXTURE)).resolve()
PACKAGE_ROOT = Path(_arg_value("--package-root", "")).resolve()
RESULT_PATH = Path(
    _arg_value("--result", PROJECT_ROOT / "benchmarks" / "match_04_package_smoke.json")
).resolve()
EXPECTED_SHA_BEFORE = _arg_value("--fixture-sha-before", "").upper()


def _clean_json(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_json(item) for item in value]
    if hasattr(value, "x") and hasattr(value, "y"):
        return [float(value.x), float(value.y)]
    if hasattr(value, "__dict__"):
        return _clean_json(vars(value))
    return str(value)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _numpy_version(module):
    optional_numpy = getattr(module, "_numpy", None)
    return getattr(optional_numpy, "__version__", None)


def _percentile(values, percentile):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise SmokeError("Cannot calculate a percentile from an empty sample")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * float(percentile) / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _load_support():
    support_path = PROJECT_ROOT / "tests" / "blender" / "match_02_fixture.py"
    spec = importlib.util.spec_from_file_location("match_02_package_smoke_support", support_path)
    if spec is None or spec.loader is None:
        raise SmokeError(f"Cannot load support harness: {support_path}")
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    return support


def _package_files():
    if not PACKAGE_ROOT.is_dir() or PACKAGE_ROOT.name != "uv_gpt":
        raise SmokeError(f"Invalid extracted package root: {PACKAGE_ROOT}")
    files = sorted(PACKAGE_ROOT.glob("*.py"))
    unexpected = [
        path
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".pyc", ".pyo"}
    ]
    if unexpected:
        raise SmokeError(f"Package contains cache files: {unexpected}")
    if len(files) != 14:
        raise SmokeError(f"Expected 14 package Python files, found {len(files)}")
    return files


def _import_extracted_addon():
    package_files = _package_files()
    package_parent = str(PACKAGE_ROOT.parent)
    sys.path = [
        item
        for item in sys.path
        if str(Path(item or ".").resolve())
        not in {str(PROJECT_ROOT), str(PROJECT_ROOT / "tests")}
    ]
    sys.path.insert(0, package_parent)
    for name in list(sys.modules):
        if name == "uv_gpt" or name.startswith("uv_gpt."):
            del sys.modules[name]
    importlib.invalidate_caches()
    addon = importlib.import_module("uv_gpt")
    imported_path = Path(addon.__file__).resolve()
    if imported_path != (PACKAGE_ROOT / "__init__.py").resolve():
        raise SmokeError(f"Imported add-on from wrong path: {imported_path}")
    if tuple(addon.bl_info.get("version", ())) != EXPECTED_VERSION:
        raise SmokeError(f"Packaged version mismatch: {addon.bl_info.get('version')}")
    return addon, package_files


def _islands_by_key(support, islands):
    return {support.face_key(island): island for island in islands}


def _run_one(support, obj, island_tools, uv_utils, stack_tools, addon, baseline_uv, baseline_selection, run_index, measured):
    bm, uv_layer = support.restore_state(obj, uv_utils, baseline_uv, baseline_selection)
    islands_before = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    selected_before = island_tools.get_selected_uv_islands(bm, uv_layer)
    if [support.face_key(item) for item in selected_before] != [TARGET_FACE_KEY]:
        raise SmokeError(f"Target selection lost before run {run_index}")
    before_by_key = _islands_by_key(support, islands_before)
    before_uv = support.snapshot_uv(bm, uv_layer)
    before_selection = support.snapshot_selection(bm, uv_layer)
    before_summaries = {
        key: support.boundary_summary(island, uv_layer, stack_tools)
        for key, island in before_by_key.items()
        if key in (TARGET_FACE_KEY, EXPECTED_CANDIDATE_FACE_KEY)
    }

    started = time.perf_counter()
    operator_result = list(bpy.ops.uv_gpt.align_to_selected())
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if operator_result != ["FINISHED"]:
        raise SmokeError(f"Unexpected operator result on run {run_index}: {operator_result}")

    bm_after = bmesh.from_edit_mesh(obj.data)
    support.setup_bmesh(bm_after)
    uv_layer_after = bm_after.loops.layers.uv.get(TARGET_UV_NAME)
    if uv_layer_after is None:
        raise SmokeError("Packaged operator removed the active UV layer")
    islands_after = island_tools.get_uv_islands(bm_after, uv_layer_after, selected_only=False)
    selected_after = island_tools.get_selected_uv_islands(bm_after, uv_layer_after)
    after_by_key = _islands_by_key(support, islands_after)
    after_uv = support.snapshot_uv(bm_after, uv_layer_after)
    after_selection = support.snapshot_selection(bm_after, uv_layer_after)
    selected_loop_keys = [
        key for island in selected_before for key in support.island_loop_keys(island)
    ]
    selected_delta = support.max_uv_delta(before_uv, after_uv, selected_loop_keys)
    changed_candidates = []
    incompatible_candidates = []
    for key, island_before in sorted(before_by_key.items()):
        if key == TARGET_FACE_KEY:
            continue
        island_after = after_by_key.get(key)
        delta = (
            support.island_delta(before_uv, after_uv, island_after)
            if island_after is not None
            else float("inf")
        )
        changed = delta > SELECTION_EPSILON
        record = {
            "target_key": list(key),
            "classification": (
                "expected_compatible"
                if key == EXPECTED_CANDIDATE_FACE_KEY
                else "incompatible_candidate"
            ),
            "max_uv_delta": delta,
            "changed": changed,
        }
        if changed:
            changed_candidates.append(record)
        if key != EXPECTED_CANDIDATE_FACE_KEY:
            incompatible_candidates.append(record)

    after_summaries = {
        key: support.boundary_summary(island, uv_layer_after, stack_tools)
        for key, island in after_by_key.items()
        if key in (TARGET_FACE_KEY, EXPECTED_CANDIDATE_FACE_KEY)
    }
    quality = support.boundary_rms(
        before_summaries[TARGET_FACE_KEY],
        before_summaries[EXPECTED_CANDIDATE_FACE_KEY],
    )
    quality_after = support.boundary_rms(
        after_summaries[TARGET_FACE_KEY],
        after_summaries[EXPECTED_CANDIDATE_FACE_KEY],
    )
    diagnostics = addon.similarity_matcher.get_diagnostics()
    return {
        "run_index": run_index,
        "measured": measured,
        "elapsed_ms": elapsed_ms,
        "operator_result": operator_result,
        "selected_uv_unchanged": selected_delta <= SELECTION_EPSILON,
        "selected_uv_max_delta": selected_delta,
        "selection_unchanged": support.selected_snapshot_unchanged(
            before_selection, after_selection
        ),
        "changed_candidates": changed_candidates,
        "incompatible_candidates": incompatible_candidates,
        "quality_before": quality,
        "quality_after": quality_after,
        "diagnostics": _clean_json(diagnostics),
        "visual_geometry": {
            "target_before": copy.deepcopy(before_summaries[TARGET_FACE_KEY]),
            "candidate_before": copy.deepcopy(before_summaries[EXPECTED_CANDIDATE_FACE_KEY]),
            "target_after": copy.deepcopy(after_summaries[TARGET_FACE_KEY]),
            "candidate_after": copy.deepcopy(after_summaries[EXPECTED_CANDIDATE_FACE_KEY]),
        },
    }


def run_smoke():
    if not FIXTURE_PATH.is_file():
        raise SmokeError(f"Fixture missing: {FIXTURE_PATH}")
    if EXPECTED_SHA_BEFORE and _sha256(FIXTURE_PATH) != EXPECTED_SHA_BEFORE:
        raise SmokeError("Fixture SHA mismatch before packaged smoke")
    support = _load_support()
    addon, package_files = _import_extracted_addon()
    island_tools = addon.island_tools
    uv_utils = addon.uv_utils
    stack_tools = addon.stack_tools
    obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
    if obj is None or obj.type != "MESH":
        raise SmokeError(f"Target mesh object missing: {TARGET_OBJECT_NAME}")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bm, uv_layer = support.open_case(obj, TARGET_UV_NAME, island_tools, uv_utils)
    baseline_uv = support.snapshot_uv(bm, uv_layer)
    baseline_selection = support.snapshot_selection(bm, uv_layer)
    registered = False
    unregister_clean = False
    runs = []
    try:
        addon.register()
        registered = True
        if not hasattr(bpy.ops.uv_gpt, "align_to_selected"):
            raise SmokeError("Packaged operator id was not registered")
        for run_index in range(WARMUP_RUNS + MEASURED_RUNS):
            measured = run_index >= WARMUP_RUNS
            try:
                result = _run_one(
                    support,
                    obj,
                    island_tools,
                    uv_utils,
                    stack_tools,
                    addon,
                    baseline_uv,
                    baseline_selection,
                    run_index,
                    measured,
                )
                runs.append(result)
            finally:
                support.restore_state(obj, uv_utils, baseline_uv, baseline_selection)
    finally:
        if registered:
            addon.unregister()
            unregister_clean = (
                not hasattr(bpy.types.Scene, "uv_gpt_settings")
                and not hasattr(bpy.types, "UVGPT_OT_align_to_selected")
            )

    if not unregister_clean:
        raise SmokeError("Packaged add-on did not unregister cleanly")

    measured_runs = [item for item in runs if item["measured"]]
    if len(measured_runs) != MEASURED_RUNS:
        raise SmokeError(f"Expected {MEASURED_RUNS} measured runs")
    for item in measured_runs:
        if not item["selected_uv_unchanged"] or not item["selection_unchanged"]:
            raise SmokeError(f"Run {item['run_index']} changed target or selection")
        if any(record["changed"] for record in item["incompatible_candidates"]):
            raise SmokeError(f"Run {item['run_index']} changed an incompatible island")
        if len(item["changed_candidates"]) != 1:
            raise SmokeError(f"Run {item['run_index']} changed !=1 candidate")
        if item["changed_candidates"][0]["target_key"] != list(EXPECTED_CANDIDATE_FACE_KEY):
            raise SmokeError(f"Run {item['run_index']} changed the wrong candidate")
        if not item["quality_after"].get("within_tolerance"):
            raise SmokeError(f"Run {item['run_index']} failed RMS quality")

    final_sha = _sha256(FIXTURE_PATH)
    values = sorted(float(item["elapsed_ms"]) for item in measured_runs)
    middle = len(values) // 2
    median = (values[middle - 1] + values[middle]) * 0.5 if len(values) % 2 == 0 else values[middle]
    return {
        "packet": PACKET_ID,
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "fixture": str(FIXTURE_PATH),
        "fixture_opened_path_exact": os.path.normcase(os.path.abspath(bpy.data.filepath))
        == os.path.normcase(str(FIXTURE_PATH)),
        "fixture_sha256_before": EXPECTED_SHA_BEFORE or final_sha,
        "fixture_sha256_after": final_sha,
        "fixture_sha256_unchanged": final_sha == (EXPECTED_SHA_BEFORE or final_sha),
        "package_root": str(PACKAGE_ROOT),
        "package_files": [path.name for path in package_files],
        "package_file_sha256": {
            path.name: _sha256(path) for path in package_files
        },
        "addon_version": ".".join(str(part) for part in EXPECTED_VERSION),
        "operator_id": "uv_gpt.align_to_selected",
        "registered_from_extracted_package": True,
        "register_unregister_clean": unregister_clean,
        "case": {
            "object": TARGET_OBJECT_NAME,
            "uv_map": TARGET_UV_NAME,
            "target_face_key": list(TARGET_FACE_KEY),
            "expected_candidate_face_key": list(EXPECTED_CANDIDATE_FACE_KEY),
            "selection_source": "deterministic_in_memory",
        },
        "benchmark": {
            "warmup_count": WARMUP_RUNS,
            "measured_run_count": MEASURED_RUNS,
            "elapsed_ms": {
                "min": min(values),
                "median": median,
                "p95": _percentile(values, 95.0),
            },
        },
        "correctness": {
            "all_selected_uv_unchanged": all(item["selected_uv_unchanged"] for item in measured_runs),
            "all_selection_unchanged": all(item["selection_unchanged"] for item in measured_runs),
            "incompatible_candidate_change_count": sum(
                sum(record["changed"] for record in item["incompatible_candidates"])
                for item in measured_runs
            ),
            "changed_candidate_counts": [len(item["changed_candidates"]) for item in measured_runs],
            "max_normalized_boundary_rms": max(
                item["quality_after"]["normalized_rms"] for item in measured_runs
            ),
            "threshold": QUALITY_TOLERANCE,
        },
        "runs": runs,
        "load_context": {
            "factory_startup": True,
            "disable_autoexec": True,
            "persistent_install": False,
            "workspace_source_imported": False,
            "save_called": False,
        },
        "runtime": {
            "blender_version": bpy.app.version_string,
            "python_version": sys.version,
            "logical_cpu_count": os.cpu_count(),
            "numpy_version": _numpy_version(addon.similarity_matcher),
        },
    }


def main():
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run_smoke()
    except Exception as exc:
        result = {
            "packet": PACKET_ID,
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "fixture_sha256_observed": _sha256(FIXTURE_PATH)
            if FIXTURE_PATH.is_file()
            else None,
        }
        RESULT_PATH.write_text(json.dumps(_clean_json(result), indent=2, sort_keys=True), encoding="utf-8")
        print(f"MATCH-04 package smoke status=failed: {result['error']}")
        print(f"MATCH-04 package smoke result: {RESULT_PATH}")
        return 1
    RESULT_PATH.write_text(json.dumps(_clean_json(result), indent=2, sort_keys=True), encoding="utf-8")
    print("MATCH-04 package smoke status=completed")
    print(f"MATCH-04 package smoke result: {RESULT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
