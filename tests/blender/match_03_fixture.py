"""MATCH-03 exact-fixture correctness and scheduler-evidence harness.

The harness reuses the read-only geometry/snapshot helpers from the MATCH-02
fixture harness, then calls a narrow adapter supplied by the primary after the
C14 scheduler contract is finalized.  It intentionally refuses to guess a
production scheduler API: without ``--scheduler-module`` it only reports that
the contract is pending and does not execute a source-dependent benchmark.

Required adapter result contract (harness-side, not a production API)::

    {
        "operator_result": ["FINISHED"],
        "operator_error": None,
        "scheduler_result": <SchedulerResult with .decision and .diagnostics>,
        "scheduler_decision": <optional explicit copy of SchedulerResult.decision>,
        "phase_reconciliation": {"reconciled": True, ...},
        "apply_records": [{"target_key": [face indices], ...}],
        "diagnostics": <optional mapping; matcher diagnostics are also read>
    }

The adapter receives one mapping containing the in-memory Blender case and may
return any additional JSON-safe evidence.  No fixture save, persistent add-on
install, source edit, or user Blender process control occurs here.
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
from collections.abc import Mapping


sys.dont_write_bytecode = True

import bmesh
import bpy


PACKET_ID = "MATCH-03/C15"
SCHEMA_VERSION = "match-03-exact-fixture-v1"
DEFAULT_FIXTURE = Path(r"C:\Users\linhp\Downloads\cc.blend")
EXPECTED_FIXTURE_SHA = (
    "840EA32C822784201EFAB30B9441A98621E6FBD87DC9BDD431B7EB90A2FF93CD"
)
TARGET_OBJECT_NAME = "Bottom.001"
TARGET_UV_NAME = "UVMap.001"
TARGET_FACE_KEY = (602, 603, 604, 605)
EXPECTED_CANDIDATE_FACE_KEY = (9448, 9484, 9967, 17967)
QUALITY_TOLERANCE = 1.0e-4
SELECTION_EPSILON = 1.0e-12
MEASURED_RUNS = 10
WARMUP_RUNS = 1
CONTRACT_VERSION = "C14-pending-harness-adapter-v1"


class HarnessError(RuntimeError):
    """Raised when fixture, contract, or correctness evidence is invalid."""


class ContractPending(HarnessError):
    """Raised when the primary has not supplied the C14 scheduler adapter."""


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
DEFAULT_PROJECT_ROOT = SCRIPT_PATH.parents[2]
PROJECT_ROOT = Path(_arg_value("--project-root", str(DEFAULT_PROJECT_ROOT))).resolve()
FIXTURE_PATH = Path(_arg_value("--fixture", str(DEFAULT_FIXTURE))).resolve()
FIXTURE_SHA_BEFORE_EXTERNAL = _arg_value("--fixture-sha-before", "").upper()
SCHEDULER_MODULE = _arg_value("--scheduler-module", "").strip()
SCHEDULER_ENTRYPOINT = _arg_value("--scheduler-entrypoint", "run_match_03").strip()
BENCHMARK_ROOT = PROJECT_ROOT / "benchmarks"
RESULT_PATH = BENCHMARK_ROOT / "match_03_fixture.json"


def _clean_json(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "x") and hasattr(value, "y"):
        return [float(value.x), float(value.y)]
    if isinstance(value, Mapping):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_json(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _clean_json(value.to_dict())
    if hasattr(value, "__dict__"):
        return _clean_json(vars(value))
    return str(value)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_and_artifact_hashes():
    result = {}
    for path in sorted((PROJECT_ROOT / "uv_gpt").glob("*.py")):
        result[path.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(path)
    artifact = PROJECT_ROOT / "uv_gpt_v1.2.5.zip"
    if artifact.is_file():
        result[artifact.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(artifact)
    return result


def harness_hashes():
    result = {}
    for path in (
        PROJECT_ROOT / "tests" / "blender" / "match_02_fixture.py",
        PROJECT_ROOT / "tests" / "blender" / "match_03_fixture.py",
        PROJECT_ROOT / "tests" / "blender" / "run_match_03.ps1",
    ):
        if path.is_file():
            result[path.relative_to(PROJECT_ROOT).as_posix()] = sha256_file(path)
    return result


def cache_scan():
    paths = []
    for suffix in ("*.pyc", "*.pyo"):
        paths.extend(
            path
            for path in (PROJECT_ROOT / "uv_gpt").rglob(suffix)
            if path.is_file()
        )
    return [path.relative_to(PROJECT_ROOT).as_posix() for path in sorted(paths)]


def _load_match_02_support():
    support_path = PROJECT_ROOT / "tests" / "blender" / "match_02_fixture.py"
    if not support_path.is_file():
        raise HarnessError(f"MATCH-02 support harness missing: {support_path}")
    spec = importlib.util.spec_from_file_location("match_02_fixture_support", support_path)
    if spec is None or spec.loader is None:
        raise HarnessError(f"Cannot load MATCH-02 support harness: {support_path}")
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    return support


def _load_scheduler_adapter():
    if not SCHEDULER_MODULE:
        raise ContractPending(
            "C14 scheduler contract pending; rerun with --scheduler-module and "
            "--scheduler-entrypoint after primary handoff"
        )
    if not SCHEDULER_ENTRYPOINT:
        raise ContractPending("--scheduler-entrypoint must be non-empty")
    module = importlib.import_module(SCHEDULER_MODULE)
    adapter = getattr(module, SCHEDULER_ENTRYPOINT, None)
    if not callable(adapter):
        raise HarnessError(
            f"Scheduler adapter is not callable: {SCHEDULER_MODULE}.{SCHEDULER_ENTRYPOINT}"
        )
    return adapter


def _load_scheduler_public_api():
    """Load and verify the C14 pure-numeric public contract."""

    module = importlib.import_module("uv_gpt.match_scheduler")
    required = (
        "SchedulerPolicy",
        "SchedulerRequest",
        "SchedulerDecision",
        "choose_backend",
        "NumericTask",
        "schedule_numeric_batch",
        "run_numeric_batch",
        "run_process_benchmark",
        "run_process_prototype",
        "SchedulerResult",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise HarnessError(
            "C14 scheduler public API missing required symbols: " + ", ".join(missing)
        )
    if module.run_process_benchmark is not module.run_process_prototype:
        raise HarnessError("C14 process prototype aliases diverged")
    return module


def _scheduler_result_evidence(value):
    decision = getattr(value, "decision", None)
    diagnostics = getattr(value, "diagnostics", None)
    if isinstance(value, Mapping):
        decision = value.get("decision", decision)
        diagnostics = value.get("diagnostics", diagnostics)
    if decision is None or diagnostics is None:
        raise HarnessError(
            "scheduler_result must expose both .decision and .diagnostics"
        )
    return decision, diagnostics


def _invoke_scheduler(adapter, request):
    result = adapter(request)
    if not isinstance(result, Mapping):
        raise HarnessError(
            "Scheduler adapter must return a mapping; "
            f"got {type(result).__name__}"
        )
    return dict(result)


def _require_adapter_result(raw):
    missing = [
        key
        for key in (
            "operator_result",
            "scheduler_result",
            "phase_reconciliation",
            "apply_records",
        )
        if key not in raw
    ]
    if missing:
        raise HarnessError(
            "Scheduler adapter result is missing required harness evidence: "
            + ", ".join(missing)
        )
    decision, diagnostics = _scheduler_result_evidence(raw["scheduler_result"])
    raw.setdefault("scheduler_decision", decision)
    raw.setdefault("scheduler_diagnostics", diagnostics)
    if not raw["scheduler_decision"]:
        raise HarnessError("scheduler_result.decision must be non-empty")
    if not isinstance(raw["phase_reconciliation"], Mapping):
        raise HarnessError("phase_reconciliation must be a mapping")
    if raw["phase_reconciliation"].get("reconciled") is not True:
        raise HarnessError("phase_reconciliation.reconciled must be true")
    if not isinstance(raw["apply_records"], (list, tuple)):
        raise HarnessError("apply_records must be a list or tuple")


def _applied_keys(raw, support):
    keys = set()
    for record in raw.get("apply_records", ()):
        if not isinstance(record, Mapping):
            continue
        key = record.get("target_key", record.get("candidate_key"))
        if key is None:
            continue
        try:
            keys.add(tuple(int(item) for item in key))
        except (TypeError, ValueError):
            raise HarnessError(f"Invalid apply record target key: {key!r}")
    return keys


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    ratio = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * ratio


def timing_summary(runs):
    measured = [item for item in runs if item["measured"]]
    values = [item["elapsed_ms"] for item in measured]
    ordered = sorted(float(value) for value in values)
    median = None
    if ordered:
        middle = len(ordered) // 2
        median = ordered[middle]
        if len(ordered) % 2 == 0:
            median = (ordered[middle - 1] + ordered[middle]) * 0.5
    return {
        "warmup_count": sum(not item["measured"] for item in runs),
        "measured_run_count": len(measured),
        "elapsed_ms": {
            "min": min(values) if values else None,
            "median": median,
            "p95": _percentile(values, 0.95),
        },
    }


def _run_once(
    support,
    adapter,
    scheduler_api,
    obj,
    island_tools,
    stack_tools,
    uv_utils,
    diagnostics_reset,
    diagnostics_read,
    baseline_uv,
    baseline_selection,
    run_index,
    measured,
):
    support.restore_state(obj, uv_utils, baseline_uv, baseline_selection)
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        support.setup_bmesh(bm)
        uv_layer = bm.loops.layers.uv.get(TARGET_UV_NAME)
        if uv_layer is None:
            raise HarnessError(f"UV map disappeared before run: {TARGET_UV_NAME}")
        islands_before = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        selected_before = island_tools.get_selected_uv_islands(bm, uv_layer)
        if [support.face_key(item) for item in selected_before] != [TARGET_FACE_KEY]:
            raise HarnessError(
                "Baseline restore lost deterministic target selection: "
                f"{[support.face_key(item) for item in selected_before]}"
            )
        before_by_key = {support.face_key(item): item for item in islands_before}
        before_uv = support.snapshot_uv(bm, uv_layer)
        before_selection = support.snapshot_selection(bm, uv_layer)
        before_summaries = {
            key: support.boundary_summary(island, uv_layer, stack_tools)
            for key, island in before_by_key.items()
            if key in (TARGET_FACE_KEY, EXPECTED_CANDIDATE_FACE_KEY)
        }
        diagnostics_reset()
        diagnostics_read()
        request = {
            "contract_version": CONTRACT_VERSION,
            "packet": PACKET_ID,
            "object": obj,
            "object_name": TARGET_OBJECT_NAME,
            "uv_map_name": TARGET_UV_NAME,
            "target_face_key": TARGET_FACE_KEY,
            "expected_candidate_face_key": EXPECTED_CANDIDATE_FACE_KEY,
            "bmesh": bm,
            "uv_layer": uv_layer,
            "all_islands": tuple(islands_before),
            "selected_islands": tuple(selected_before),
            "island_tools": island_tools,
            "stack_tools": stack_tools,
            "uv_utils": uv_utils,
            "bpy_context": bpy.context,
            "scheduler_api": {
                "SchedulerPolicy": scheduler_api.SchedulerPolicy,
                "SchedulerRequest": scheduler_api.SchedulerRequest,
                "SchedulerDecision": scheduler_api.SchedulerDecision,
                "choose_backend": scheduler_api.choose_backend,
                "NumericTask": scheduler_api.NumericTask,
                "schedule_numeric_batch": scheduler_api.schedule_numeric_batch,
                "run_numeric_batch": scheduler_api.run_numeric_batch,
                "run_process_benchmark": scheduler_api.run_process_benchmark,
                "run_process_prototype": scheduler_api.run_process_prototype,
            },
            "run": {"index": run_index, "measured": measured},
        }
        adapter_error = None
        raw = {}
        started = time.perf_counter()
        try:
            raw = _invoke_scheduler(adapter, request)
            _require_adapter_result(raw)
        except Exception as exc:
            adapter_error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started

        bm_after = bmesh.from_edit_mesh(obj.data)
        support.setup_bmesh(bm_after)
        uv_layer_after = bm_after.loops.layers.uv.get(TARGET_UV_NAME)
        if uv_layer_after is None:
            raise HarnessError(f"UV map disappeared after run: {TARGET_UV_NAME}")
        islands_after = island_tools.get_uv_islands(
            bm_after, uv_layer_after, selected_only=False
        )
        selected_after = island_tools.get_selected_uv_islands(bm_after, uv_layer_after)
        after_by_key = {support.face_key(item): item for item in islands_after}
        after_uv = support.snapshot_uv(bm_after, uv_layer_after)
        after_selection = support.snapshot_selection(bm_after, uv_layer_after)

        selected_keys = [
            key
            for island in selected_before
            for key in support.island_loop_keys(island)
        ]
        selected_delta = support.max_uv_delta(before_uv, after_uv, selected_keys)
        selected_unchanged = selected_delta <= SELECTION_EPSILON
        selection_unchanged = support.selected_snapshot_unchanged(
            before_selection, after_selection
        )
        applied_keys = _applied_keys(raw, support) if not adapter_error else set()
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
            classification = (
                "expected_compatible"
                if key == EXPECTED_CANDIDATE_FACE_KEY
                else "incompatible_candidate"
            )
            record = {
                "target_key": list(key),
                "classification": classification,
                "max_uv_delta": delta,
                "changed": changed,
                "observed_apply_call": key in applied_keys,
                "present_after": island_after is not None,
            }
            if changed:
                changed_candidates.append(record)
            if classification == "incompatible_candidate":
                incompatible_candidates.append(record)

        after_summaries = {
            key: support.boundary_summary(island, uv_layer_after, stack_tools)
            for key, island in after_by_key.items()
            if key in (TARGET_FACE_KEY, EXPECTED_CANDIDATE_FACE_KEY)
        }
        quality = {}
        if TARGET_FACE_KEY in before_summaries and EXPECTED_CANDIDATE_FACE_KEY in before_summaries:
            before_quality = support.boundary_rms(
                before_summaries[TARGET_FACE_KEY],
                before_summaries[EXPECTED_CANDIDATE_FACE_KEY],
            )
            after_quality = None
            if (
                TARGET_FACE_KEY in after_summaries
                and EXPECTED_CANDIDATE_FACE_KEY in after_summaries
            ):
                after_quality = support.boundary_rms(
                    after_summaries[TARGET_FACE_KEY],
                    after_summaries[EXPECTED_CANDIDATE_FACE_KEY],
                )
            quality = {
                "target_key": list(TARGET_FACE_KEY),
                "candidate_key": list(EXPECTED_CANDIDATE_FACE_KEY),
                "before": before_quality,
                "after": after_quality,
            }
        diagnostics = diagnostics_read()
        result = {
            "run_index": run_index,
            "measured": measured,
            "elapsed_seconds": elapsed,
            "elapsed_ms": elapsed * 1000.0,
            "operator_result": _clean_json(raw.get("operator_result")),
            "operator_error": raw.get("operator_error") or adapter_error,
            "diagnostics": _clean_json(diagnostics),
            "scheduler_decision": _clean_json(raw.get("scheduler_decision")),
            "scheduler_diagnostics": _clean_json(raw.get("scheduler_diagnostics")),
            "phase_reconciliation": _clean_json(raw.get("phase_reconciliation")),
            "adapter_evidence": _clean_json(raw),
            "case_size": support._case_size(bm_after, islands_after, selected_after),
            "correctness": {
                "selected_uv_unchanged": selected_unchanged,
                "selected_uv_max_delta": selected_delta,
                "selection_snapshot_unchanged": selection_unchanged,
                "changed_candidates": changed_candidates,
                "incompatible_candidates": incompatible_candidates,
                "incompatible_candidates_unchanged": all(
                    not item["changed"] for item in incompatible_candidates
                ),
                "applied_candidate_keys": [list(key) for key in sorted(applied_keys)],
                "unobserved_changed_candidates": [
                    item for item in changed_candidates if not item["observed_apply_call"]
                ],
                "quality": quality,
            },
            "visual_geometry": {
                "target_before": copy.deepcopy(before_summaries.get(TARGET_FACE_KEY)),
                "candidate_before": copy.deepcopy(
                    before_summaries.get(EXPECTED_CANDIDATE_FACE_KEY)
                ),
                "target_after": copy.deepcopy(after_summaries.get(TARGET_FACE_KEY)),
                "candidate_after": copy.deepcopy(
                    after_summaries.get(EXPECTED_CANDIDATE_FACE_KEY)
                ),
            },
        }
        return result
    finally:
        # Never leave the opened fixture with in-memory UV/selection changes.
        support.restore_state(obj, uv_utils, baseline_uv, baseline_selection)


def _validate_runs(runs, source_before, source_after, harness_before, harness_after, fixture_sha_before, fixture_sha_after):
    measured = [item for item in runs if item["measured"]]
    if len(runs) != WARMUP_RUNS + MEASURED_RUNS:
        raise HarnessError(
            f"Expected {WARMUP_RUNS} warmup + {MEASURED_RUNS} measured runs, got {len(runs)}"
        )
    for item in measured:
        if item["operator_error"]:
            raise HarnessError(f"Run {item['run_index']} adapter/operator error: {item['operator_error']}")
        operator_result = item["operator_result"]
        if isinstance(operator_result, str):
            finished = operator_result.upper() == "FINISHED"
        else:
            finished = "FINISHED" in [str(value).upper() for value in (operator_result or [])]
        if not finished:
            raise HarnessError(f"Run {item['run_index']} did not finish: {operator_result!r}")
        correctness = item["correctness"]
        if not correctness["selected_uv_unchanged"]:
            raise HarnessError(f"Run {item['run_index']} changed selected target UV")
        if not correctness["selection_snapshot_unchanged"]:
            raise HarnessError(f"Run {item['run_index']} changed UV/mesh selection state")
        if not correctness["incompatible_candidates_unchanged"]:
            raise HarnessError(f"Run {item['run_index']} changed an incompatible candidate")
        if correctness["unobserved_changed_candidates"]:
            raise HarnessError(
                f"Run {item['run_index']} changed candidates without apply evidence: "
                f"{correctness['unobserved_changed_candidates']}"
            )
        quality_after = correctness["quality"].get("after")
        if not quality_after or not quality_after.get("within_tolerance"):
            raise HarnessError(
                f"Run {item['run_index']} failed normalized boundary RMS: {quality_after}"
            )
        phase = item["phase_reconciliation"]
        if not isinstance(phase, Mapping) or phase.get("reconciled") is not True:
            raise HarnessError(f"Run {item['run_index']} lacks reconciled phase evidence")
        boundary_overhead = phase.get("operator_boundary_overhead_ms")
        if boundary_overhead is None or float(boundary_overhead) < 0.0:
            raise HarnessError(
                f"Run {item['run_index']} lacks classified operator-boundary overhead"
            )
        if float(phase.get("unattributed_overhead_ms", 0.0)) > 1.0e-3:
            raise HarnessError(
                f"Run {item['run_index']} has unexplained phase overhead: {phase}"
            )
        decision = item["scheduler_decision"]
        if not isinstance(decision, Mapping) or not decision:
            raise HarnessError(f"Run {item['run_index']} lacks scheduler decision evidence")
        full_fits = item["diagnostics"].get("full_fits")
        if full_fits is not None and int(full_fits) < 1:
            raise HarnessError(f"Run {item['run_index']} reported no full fit")
    if fixture_sha_before != EXPECTED_FIXTURE_SHA or fixture_sha_after != fixture_sha_before:
        raise HarnessError(
            f"Fixture SHA changed or mismatched: before={fixture_sha_before}, after={fixture_sha_after}"
        )
    if source_before != source_after:
        raise HarnessError("Source/artifact hashes changed during MATCH-03 fixture run")
    if harness_before != harness_after:
        raise HarnessError("Harness hashes changed during MATCH-03 fixture run")
    cache_files = cache_scan()
    if cache_files:
        raise HarnessError(f"Project add-on cache/pyc remains: {cache_files}")


def run_harness():
    if not FIXTURE_PATH.is_file():
        raise HarnessError(f"Exact fixture missing: {FIXTURE_PATH}")
    fixture_sha_before = sha256_file(FIXTURE_PATH)
    if FIXTURE_SHA_BEFORE_EXTERNAL and FIXTURE_SHA_BEFORE_EXTERNAL != fixture_sha_before:
        raise HarnessError(
            "Fixture SHA changed between PowerShell preflight and Blender process: "
            f"external={FIXTURE_SHA_BEFORE_EXTERNAL}, in_process={fixture_sha_before}"
        )
    if fixture_sha_before != EXPECTED_FIXTURE_SHA:
        raise HarnessError(
            f"Unexpected exact fixture SHA: {fixture_sha_before}; expected {EXPECTED_FIXTURE_SHA}"
        )
    source_before = source_and_artifact_hashes()
    harness_before = harness_hashes()
    if "uv_gpt/stack_tools.py" not in source_before:
        raise HarnessError("Project source package missing uv_gpt/stack_tools.py")

    support = _load_match_02_support()
    adapter = _load_scheduler_adapter()
    addon = None
    registered = False
    runs = []
    try:
        addon = importlib.import_module("uv_gpt")
        similarity_matcher = importlib.import_module("uv_gpt.similarity_matcher")
        island_tools = importlib.import_module("uv_gpt.island_tools")
        stack_tools = importlib.import_module("uv_gpt.stack_tools")
        uv_utils = importlib.import_module("uv_gpt.uv_utils")
        scheduler_api = _load_scheduler_public_api()
        diagnostics_reset, diagnostics_read = support.resolve_diagnostics_adapter(
            similarity_matcher
        )
        addon.register()
        registered = True
        obj = bpy.data.objects.get(TARGET_OBJECT_NAME)
        if obj is None or obj.type != "MESH":
            raise HarnessError(f"Target mesh missing: {TARGET_OBJECT_NAME}")
        bm, uv_layer = support.open_case(
            obj, TARGET_UV_NAME, island_tools, uv_utils
        )
        baseline_uv = support.snapshot_uv(bm, uv_layer)
        baseline_selection = support.snapshot_selection(bm, uv_layer)
        baseline_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
        baseline_selected = island_tools.get_selected_uv_islands(bm, uv_layer)
        baseline_size = support._case_size(bm, baseline_islands, baseline_selected)
        if baseline_size["uv_islands"] != 577 or baseline_size["unselected_candidates"] != 576:
            raise HarnessError(f"Unexpected exact fixture case size: {baseline_size}")
        runs.append(
            _run_once(
                support,
                adapter,
                scheduler_api,
                obj,
                island_tools,
                stack_tools,
                uv_utils,
                diagnostics_reset,
                diagnostics_read,
                baseline_uv,
                baseline_selection,
                run_index=0,
                measured=False,
            )
        )
        for run_index in range(1, MEASURED_RUNS + 1):
            runs.append(
                _run_once(
                    support,
                    adapter,
                    scheduler_api,
                    obj,
                    island_tools,
                    stack_tools,
                    uv_utils,
                    diagnostics_reset,
                    diagnostics_read,
                    baseline_uv,
                    baseline_selection,
                    run_index=run_index,
                    measured=True,
                )
            )
        support.restore_state(obj, uv_utils, baseline_uv, baseline_selection)
        source_after = source_and_artifact_hashes()
        harness_after = harness_hashes()
        fixture_sha_after = sha256_file(FIXTURE_PATH)
        _validate_runs(
            runs,
            source_before,
            source_after,
            harness_before,
            harness_after,
            fixture_sha_before,
            fixture_sha_after,
        )
        result = {
            "packet": PACKET_ID,
            "status": "completed",
            "schema_version": SCHEMA_VERSION,
            "script": str(SCRIPT_PATH),
            "project_root": str(PROJECT_ROOT),
            "fixture": str(FIXTURE_PATH),
            "commands": {
                "background": (
                    "blender.exe --factory-startup --disable-autoexec --background "
                    f"{FIXTURE_PATH} --python {SCRIPT_PATH} -- --project-root {PROJECT_ROOT} "
                    f"--fixture {FIXTURE_PATH} --fixture-sha-before {fixture_sha_before} "
                    f"--scheduler-module {SCHEDULER_MODULE} "
                    f"--scheduler-entrypoint {SCHEDULER_ENTRYPOINT}"
                ),
                "fixture_sha_preflight": FIXTURE_SHA_BEFORE_EXTERNAL or None,
                "orphan_process_check": "run_match_03.ps1 checks only the exact portable Blender path; no process termination",
            },
            "fixture_sha256_before_external": FIXTURE_SHA_BEFORE_EXTERNAL or None,
            "fixture_sha256_before_in_process": fixture_sha_before,
            "fixture_sha256_after": fixture_sha_after,
            "fixture_sha256_unchanged": fixture_sha_after == fixture_sha_before,
            "runtime": {
                "blender_version": bpy.app.version_string,
                "blender_version_tuple": list(bpy.app.version),
                "blender_binary": bpy.app.binary_path,
                "python_version": sys.version,
                "logical_cpu_count": os.cpu_count(),
                "numpy": _numpy_info(),
                "process_id": os.getpid(),
            },
            "fixture_opened_path": bpy.data.filepath,
            "fixture_opened_path_exact": os.path.normcase(os.path.abspath(bpy.data.filepath))
            == os.path.normcase(str(FIXTURE_PATH)),
            "load_context": {
                "factory_startup": True,
                "disable_autoexec": True,
                "persistent_addon_install": False,
                "save_called": False,
                "in_memory_deterministic_target": {
                    "object": TARGET_OBJECT_NAME,
                    "uv_map": TARGET_UV_NAME,
                    "target_face_key": list(TARGET_FACE_KEY),
                },
            },
            "scheduler_adapter": {
                "contract_version": CONTRACT_VERSION,
                "module": SCHEDULER_MODULE,
                "entrypoint": SCHEDULER_ENTRYPOINT,
                "required_result_keys": [
                    "operator_result",
                    "scheduler_result",
                    "phase_reconciliation",
                    "apply_records",
                ],
                "source_dependent_run_allowed_only_after_c14_handoff": True,
            },
            "scheduler_public_api": {
                "module": "uv_gpt.match_scheduler",
                "symbols": [
                    "SchedulerPolicy",
                    "SchedulerRequest",
                    "SchedulerDecision",
                    "choose_backend",
                    "NumericTask",
                    "schedule_numeric_batch",
                    "run_numeric_batch",
                    "run_process_benchmark",
                    "run_process_prototype",
                    "SchedulerResult.decision",
                    "SchedulerResult.diagnostics",
                ],
            },
            "execution_case": {
                "object": TARGET_OBJECT_NAME,
                "uv_map": TARGET_UV_NAME,
                "selection_source": "deterministic_in_memory",
                "selected_target_keys": [list(TARGET_FACE_KEY)],
                "expected_compatible_candidate_key": list(EXPECTED_CANDIDATE_FACE_KEY),
                "case_size": baseline_size,
            },
            "benchmark": {
                "warmup": runs[0],
                "measured_runs": [item for item in runs if item["measured"]],
                "summary": timing_summary(runs),
                "measurement_policy": "one warmup plus ten measured runs; median is the standard midpoint average for even samples; every run restores UV and selection baseline",
            },
            "correctness_summary": {
                "all_measured_selected_uv_unchanged": all(
                    item["correctness"]["selected_uv_unchanged"]
                    for item in runs
                    if item["measured"]
                ),
                "all_measured_selection_unchanged": all(
                    item["correctness"]["selection_snapshot_unchanged"]
                    for item in runs
                    if item["measured"]
                ),
                "all_measured_incompatible_candidates_unchanged": all(
                    item["correctness"]["incompatible_candidates_unchanged"]
                    for item in runs
                    if item["measured"]
                ),
                "changed_candidate_counts": [
                    len(item["correctness"]["changed_candidates"])
                    for item in runs
                    if item["measured"]
                ],
                "applied_candidate_counts": [
                    len(item["correctness"]["applied_candidate_keys"])
                    for item in runs
                    if item["measured"]
                ],
                "normalized_boundary_rms_threshold": QUALITY_TOLERANCE,
                "max_applied_normalized_boundary_rms": max(
                    item["correctness"]["quality"]["after"]["normalized_rms"]
                    for item in runs
                    if item["measured"]
                ),
                "incompatible_candidate_change_count": sum(
                    sum(item["changed"] for item in run["correctness"]["incompatible_candidates"])
                    for run in runs
                    if run["measured"]
                ),
            },
            "source_artifact_hashes_before": source_before,
            "source_artifact_hashes_after": source_after,
            "source_artifact_hashes_unchanged": source_before == source_after,
            "harness_hashes_before": harness_before,
            "harness_hashes_after": harness_after,
            "harness_hashes_unchanged": harness_before == harness_after,
            "uv_gpt_cache_files": cache_scan(),
            "result_path": str(RESULT_PATH),
        }
        return result
    finally:
        if registered and addon is not None:
            addon.unregister()


def _numpy_info():
    try:
        numpy = importlib.import_module("numpy")
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "version": getattr(numpy, "__version__", None)}


def _write_result(value):
    BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(_clean_json(value), indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )


def main():
    BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
    if not SCHEDULER_MODULE:
        pending = {
            "packet": PACKET_ID,
            "status": "contract_pending",
            "schema_version": SCHEMA_VERSION,
            "script": str(SCRIPT_PATH),
            "project_root": str(PROJECT_ROOT),
            "fixture": str(FIXTURE_PATH),
            "scheduler_adapter": {
                "contract_version": CONTRACT_VERSION,
                "required_result_keys": [
                    "operator_result",
                    "scheduler_result",
                    "phase_reconciliation",
                    "apply_records",
                ],
                "message": "Primary must hand off finalized C14 scheduler API before source-dependent Blender run",
            },
            "scheduler_public_api": {
                "module": "uv_gpt.match_scheduler",
                "symbols": [
                    "SchedulerPolicy",
                    "SchedulerRequest",
                    "SchedulerDecision",
                    "choose_backend",
                    "NumericTask",
                    "schedule_numeric_batch",
                    "run_numeric_batch",
                    "run_process_benchmark",
                    "run_process_prototype",
                    "SchedulerResult.decision",
                    "SchedulerResult.diagnostics",
                ],
            },
            "result_path": str(RESULT_PATH),
        }
        _write_result(pending)
        print("MATCH-03 fixture status=contract_pending")
        print(f"MATCH-03 fixture result: {RESULT_PATH}")
        return 2
    try:
        result = run_harness()
    except Exception as exc:
        failure = {
            "packet": PACKET_ID,
            "status": "failed",
            "schema_version": SCHEMA_VERSION,
            "script": str(SCRIPT_PATH),
            "project_root": str(PROJECT_ROOT),
            "fixture": str(FIXTURE_PATH),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "fixture_sha256_observed": sha256_file(FIXTURE_PATH)
            if FIXTURE_PATH.is_file()
            else None,
            "source_artifact_hashes": source_and_artifact_hashes(),
            "harness_hashes": harness_hashes(),
            "uv_gpt_cache_files": cache_scan(),
            "result_path": str(RESULT_PATH),
        }
        _write_result(failure)
        print(f"MATCH-03 fixture status=failed: {failure['error']}")
        print(f"MATCH-03 fixture result: {RESULT_PATH}")
        return 1
    _write_result(result)
    print("MATCH-03 fixture status=completed")
    print(f"MATCH-03 fixture result: {RESULT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
