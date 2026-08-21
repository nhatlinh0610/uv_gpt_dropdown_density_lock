"""MATCH-03 synthetic backend benchmark.

This runner is deliberately independent of the Blender fixture/operator path.
It benchmarks the numeric fitting kernel through the C14 pure-numeric scheduler
API over a deterministic in-memory corpus so that Python, NumPy, threads, and a
ProcessPool prototype can be compared before the exact fixture integration is
confirmed.  No Blender data or Blender UI state is imported.

The output is a JSON evidence file under ``benchmarks/match_03_*``.  Every
backend records acceptance/rejection and a fitted transform for every candidate;
the first measured run is reconciled against the Python single-process
baseline.  Parallel backends are timing prototypes only: they return immutable
numeric results to the parent and do not touch Blender state.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
import pickle
import random
import sys
import time
import traceback


sys.dont_write_bytecode = True

PACKET_ID = "MATCH-03/C15"
SCHEMA_VERSION = "match-03-synthetic-v1"
DEFAULT_SEED = 20260818
DEFAULT_CANDIDATE_SIZES = (8, 32, 96)
DEFAULT_PAYLOAD_SIZES = (32, 96, 256)
DEFAULT_WORKER_COUNTS = (1, 2, 4)
DEFAULT_WARMUPS = 1
DEFAULT_REPEATS = 10
DEFAULT_TOLERANCE = 1.0e-4
SCORE_PARITY_TOLERANCE = 1.0e-9
TRANSFORM_PARITY_TOLERANCE = 1.0e-8


class HarnessError(RuntimeError):
    """Raised when reproducibility or backend parity evidence is invalid."""


def _parse_int_list(value: str, name: str, minimum: int = 1) -> tuple[int, ...]:
    result = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            parsed = int(item)
        except ValueError as exc:
            raise HarnessError(f"{name} contains a non-integer value: {item!r}") from exc
        if parsed < minimum:
            raise HarnessError(f"{name} values must be >= {minimum}: {parsed}")
        result.append(parsed)
    if not result:
        raise HarnessError(f"{name} must contain at least one integer")
    return tuple(dict.fromkeys(result))


def _clean_json(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if is_dataclass(value):
        return _clean_json(asdict(value))
    if isinstance(value, dict):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_json(item) for item in value]
    return str(value)


def _percentile(values, percentile: float):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    ratio = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * ratio


def _digest(value) -> str:
    payload = json.dumps(
        _clean_json(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def _point_distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _perimeter(points) -> float:
    return sum(
        _point_distance(points[index], points[(index + 1) % len(points)])
        for index in range(len(points))
    )


def _reference_shape(point_count: int, seed: int):
    """Create an asymmetric, closed loop with stable geometry."""

    rng = random.Random(seed)
    phase = rng.random() * math.pi * 2.0
    points = []
    for index in range(point_count):
        theta = phase + 2.0 * math.pi * index / point_count
        radius = 1.0 + 0.17 * math.sin(3.0 * theta) + 0.08 * math.cos(5.0 * theta)
        x = radius * math.cos(theta)
        y = 0.73 * radius * math.sin(theta) + 0.05 * math.cos(2.0 * theta)
        points.append((float(x), float(y)))
    return tuple(points)


def _transform_points(points, angle: float, scale: float, offset, sx: float = 1.0, sy: float = 1.0):
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return tuple(
        (
            float(offset[0] + scale * (sx * point[0] * cosine - sy * point[1] * sine)),
            float(offset[1] + scale * (sx * point[0] * sine + sy * point[1] * cosine)),
        )
        for point in points
    )


def _make_case(candidate_count: int, payload_size: int, seed: int):
    reference = _reference_shape(payload_size, seed + payload_size * 31)
    candidates = []
    for index in range(candidate_count):
        angle = 0.13 + (index % 11) * 0.037
        scale = 0.72 + (index % 7) * 0.09
        offset = (2.5 + index * 0.017, -1.5 + index * 0.011)
        if index % 3 == 0:
            points = _transform_points(reference, angle, scale, offset)
            kind = "compatible_transform"
            expected_accept = True
        elif index % 3 == 1:
            # Anisotropic distortion cannot be repaired by a uniform similarity
            # transform and is intentionally well outside the acceptance gate.
            points = _transform_points(
                reference,
                angle,
                scale,
                offset,
                sx=1.22 + (index % 5) * 0.02,
                sy=0.71,
            )
            kind = "incompatible_anisotropic"
            expected_accept = False
        else:
            # A radial deformation keeps the payload topology but changes its
            # boundary shape, exercising a numeric reject rather than a size
            # or worker-count special case.
            transformed = _transform_points(reference, angle, scale, offset)
            points = tuple(
                (
                    point[0] + 0.19 * math.sin(4.0 * 2.0 * math.pi * item / payload_size),
                    point[1] + 0.13 * math.cos(3.0 * 2.0 * math.pi * item / payload_size),
                )
                for item, point in enumerate(transformed)
            )
            kind = "incompatible_radial_warp"
            expected_accept = False
        candidates.append(
            {
                "candidate_id": f"candidate-{index:04d}",
                "points": tuple(points),
                "kind": kind,
                "expected_accept": expected_accept,
            }
        )
    return {
        "case_id": f"candidates-{candidate_count:04d}_payload-{payload_size:04d}",
        "candidate_count": candidate_count,
        "payload_size": payload_size,
        "reference": reference,
        "candidates": tuple(candidates),
    }


def _fit_python(reference, candidate):
    reference_center = (
        sum(point[0] for point in reference) / len(reference),
        sum(point[1] for point in reference) / len(reference),
    )
    candidate_center = (
        sum(point[0] for point in candidate) / len(candidate),
        sum(point[1] for point in candidate) / len(candidate),
    )
    reference_centered = [
        (point[0] - reference_center[0], point[1] - reference_center[1])
        for point in reference
    ]
    candidate_centered = [
        (point[0] - candidate_center[0], point[1] - candidate_center[1])
        for point in candidate
    ]
    a = sum(
        ref[0] * cand[0] + ref[1] * cand[1]
        for ref, cand in zip(reference_centered, candidate_centered)
    )
    b = sum(
        ref[1] * cand[0] - ref[0] * cand[1]
        for ref, cand in zip(reference_centered, candidate_centered)
    )
    denominator = sum(
        point[0] * point[0] + point[1] * point[1] for point in candidate_centered
    )
    angle = math.atan2(b, a) if abs(a) > 1.0e-12 or abs(b) > 1.0e-12 else 0.0
    scale = max(0.0, math.hypot(a, b) / denominator) if denominator > 1.0e-12 else 1.0
    cosine = math.cos(angle)
    sine = math.sin(angle)
    squared_error = 0.0
    for reference_point, candidate_point in zip(reference_centered, candidate_centered):
        transformed = (
            scale * (candidate_point[0] * cosine - candidate_point[1] * sine),
            scale * (candidate_point[0] * sine + candidate_point[1] * cosine),
        )
        squared_error += (
            (reference_point[0] - transformed[0]) ** 2
            + (reference_point[1] - transformed[1]) ** 2
        )
    rms = math.sqrt(squared_error / max(len(reference), 1))
    return angle, scale, rms, reference_center, candidate_center


def _fit_numpy(reference, candidate):
    import numpy

    reference_array = numpy.asarray(reference, dtype=float)
    candidate_array = numpy.asarray(candidate, dtype=float)
    reference_center_array = reference_array.mean(axis=0)
    candidate_center_array = candidate_array.mean(axis=0)
    reference_centered = reference_array - reference_center_array
    candidate_centered = candidate_array - candidate_center_array
    a = float(
        numpy.sum(
            reference_centered[:, 0] * candidate_centered[:, 0]
            + reference_centered[:, 1] * candidate_centered[:, 1]
        )
    )
    b = float(
        numpy.sum(
            reference_centered[:, 1] * candidate_centered[:, 0]
            - reference_centered[:, 0] * candidate_centered[:, 1]
        )
    )
    denominator = float(numpy.sum(candidate_centered * candidate_centered))
    angle = math.atan2(b, a) if abs(a) > 1.0e-12 or abs(b) > 1.0e-12 else 0.0
    scale = max(0.0, math.hypot(a, b) / denominator) if denominator > 1.0e-12 else 1.0
    cosine = math.cos(angle)
    sine = math.sin(angle)
    transformed = scale * numpy.column_stack(
        (
            candidate_centered[:, 0] * cosine - candidate_centered[:, 1] * sine,
            candidate_centered[:, 0] * sine + candidate_centered[:, 1] * cosine,
        )
    )
    squared_error = float(numpy.sum((reference_centered - transformed) ** 2))
    rms = math.sqrt(squared_error / max(len(reference), 1))
    return (
        angle,
        scale,
        rms,
        (float(reference_center_array[0]), float(reference_center_array[1])),
        (float(candidate_center_array[0]), float(candidate_center_array[1])),
    )


def _score_candidate(reference, candidate, tolerance: float, kernel: str):
    if kernel == "numpy":
        angle, scale, rms, reference_center, candidate_center = _fit_numpy(
            reference, candidate["points"]
        )
    elif kernel == "python":
        angle, scale, rms, reference_center, candidate_center = _fit_python(
            reference, candidate["points"]
        )
    else:
        raise HarnessError(f"Unknown synthetic kernel: {kernel}")
    normalizer = max(_perimeter(reference), 1.0e-12)
    score = rms / normalizer
    accepted = bool(score <= tolerance)
    return {
        "candidate_id": candidate["candidate_id"],
        "kind": candidate["kind"],
        "expected_accept": bool(candidate["expected_accept"]),
        "accepted": accepted,
        "reason": "accepted" if accepted else "score_above_tolerance",
        "score": float(score),
        "transform": {
            "angle": float(angle),
            "scale": float(scale),
            "rms": float(rms),
            "reference_center": [float(reference_center[0]), float(reference_center[1])],
            "candidate_center": [float(candidate_center[0]), float(candidate_center[1])],
        },
    }


def _candidate_payload(candidate):
    return (
        candidate["candidate_id"],
        candidate["kind"],
        bool(candidate["expected_accept"]),
        tuple(tuple(float(value) for value in point) for point in candidate["points"]),
    )


def _task_payload(case, candidate, tolerance: float):
    return (case["reference"], _candidate_payload(candidate), float(tolerance))


def _make_tasks(case, tolerance: float, scheduler):
    return tuple(
        scheduler.NumericTask(
            index,
            _task_payload(case, candidate, tolerance),
            key=candidate["candidate_id"],
        )
        for index, candidate in enumerate(case["candidates"])
    )


def _encode_worker_result(result):
    transform = result["transform"]
    return (
        result["candidate_id"],
        result["kind"],
        bool(result["expected_accept"]),
        bool(result["accepted"]),
        result["reason"],
        float(result["score"]),
        float(transform["angle"]),
        float(transform["scale"]),
        float(transform["rms"]),
        tuple(float(value) for value in transform["reference_center"]),
        tuple(float(value) for value in transform["candidate_center"]),
        int(os.getpid()),
    )


def _decode_worker_result(value):
    (
        candidate_id,
        kind,
        expected_accept,
        accepted,
        reason,
        score,
        angle,
        scale,
        rms,
        reference_center,
        candidate_center,
        worker_pid,
    ) = value
    return {
        "candidate_id": candidate_id,
        "kind": kind,
        "expected_accept": bool(expected_accept),
        "accepted": bool(accepted),
        "reason": reason,
        "score": float(score),
        "transform": {
            "angle": float(angle),
            "scale": float(scale),
            "rms": float(rms),
            "reference_center": [float(item) for item in reference_center],
            "candidate_center": [float(item) for item in candidate_center],
        },
        "worker_pid": int(worker_pid),
    }


def _worker_from_payload(payload, kernel: str):
    reference, candidate_payload, tolerance = payload
    candidate_id, kind, expected_accept, points = candidate_payload
    candidate = {
        "candidate_id": candidate_id,
        "kind": kind,
        "expected_accept": expected_accept,
        "points": points,
    }
    return _encode_worker_result(
        _score_candidate(reference, candidate, float(tolerance), kernel)
    )


def _numeric_worker_python(payload):
    """Pickle-safe pure-Python worker for ``match_scheduler``."""

    return _worker_from_payload(payload, "python")


def _numeric_worker_numpy(payload):
    """Pickle-safe NumPy worker for single/thread/process prototypes."""

    return _worker_from_payload(payload, "numpy")


def _payload_size(case, scheduler) -> dict:
    reference_bytes = len(pickle.dumps(case["reference"], protocol=4))
    candidate_payloads = [
        len(pickle.dumps(_candidate_payload(candidate), protocol=4))
        for candidate in case["candidates"]
    ]
    task_payloads = _make_tasks(case, DEFAULT_TOLERANCE, scheduler)
    return {
        "reference_bytes_pickle": reference_bytes,
        "candidate_bytes_pickle_total": sum(candidate_payloads),
        "candidate_bytes_pickle_min": min(candidate_payloads),
        "candidate_bytes_pickle_max": max(candidate_payloads),
        "task_bytes_pickle_total": sum(
            len(pickle.dumps(task.payload, protocol=4)) for task in task_payloads
        ),
    }


def _numeric_outcome_view(outcomes):
    """Remove process identity metadata before checking numeric repeatability."""

    return [
        {key: value for key, value in item.items() if key != "worker_pid"}
        for item in outcomes
    ]


def _run_summary(outcomes, elapsed_ms: float, measured: bool, scheduler_result, requested_decision):
    return {
        "measured": measured,
        "elapsed_ms": float(elapsed_ms),
        "accepted_count": sum(bool(item["accepted"]) for item in outcomes),
        "rejected_count": sum(not bool(item["accepted"]) for item in outcomes),
        "worker_pids": sorted({int(item["worker_pid"]) for item in outcomes}),
        "outcome_digest": _digest(_numeric_outcome_view(outcomes)),
        "outcomes": outcomes,
        "scheduler_decision": _clean_json(scheduler_result.decision),
        "scheduler_diagnostics": _clean_json(scheduler_result.diagnostics),
        "requested_decision": _clean_json(requested_decision),
    }


def _policy_and_request(case, backend: str, kernel: str, worker_count: int, scheduler):
    if backend == "numpy_threads":
        policy = scheduler.SchedulerPolicy(
            backend="thread",
            thread_min_batch_size=1,
            process_min_batch_size=1,
            max_workers=worker_count,
            allow_numpy_threads=True,
        )
        request_backend = "thread"
        pure_python = False
        numpy_enabled = True
        payload_serializable = False
    elif backend == "process_pool":
        policy = scheduler.SchedulerPolicy(
            backend="process",
            thread_min_batch_size=1,
            process_min_batch_size=1,
            max_workers=worker_count,
            allow_process_benchmark=True,
        )
        request_backend = "process"
        # The process prototype deliberately keeps scheduler's NumPy flag
        # conservative; the worker itself is the NumPy kernel under test.
        pure_python = True
        numpy_enabled = False
        payload_serializable = True
    elif backend == "numpy_single":
        policy = scheduler.SchedulerPolicy(backend="single", max_workers=1)
        request_backend = "single"
        pure_python = False
        numpy_enabled = True
        payload_serializable = False
    else:
        policy = scheduler.SchedulerPolicy(backend="single", max_workers=1)
        request_backend = "single"
        pure_python = True
        numpy_enabled = False
        payload_serializable = False
    request = scheduler.SchedulerRequest(
        backend=request_backend,
        batch_size=case["candidate_count"],
        full_fit_count=case["candidate_count"],
        pure_python=pure_python,
        numpy_enabled=numpy_enabled,
        independent=True,
        payload_serializable=payload_serializable,
    )
    requested_decision = scheduler.choose_backend(request, policy=policy)
    return policy, requested_decision, request_backend, pure_python, numpy_enabled


def _run_batch(case, tolerance: float, backend: str, kernel: str, worker_count: int, scheduler):
    tasks = _make_tasks(case, tolerance, scheduler)
    policy, requested_decision, request_backend, pure_python, numpy_enabled = _policy_and_request(
        case, backend, kernel, worker_count, scheduler
    )
    if backend == "process_pool":
        scheduler_result = scheduler.run_process_benchmark(
            tasks,
            _numeric_worker_numpy,
            policy=policy,
            full_fit_count=case["candidate_count"],
            validate_results=True,
            raise_on_error=True,
            force_process=True,
        )
    else:
        worker = _numeric_worker_numpy if kernel == "numpy" else _numeric_worker_python
        schedule = (
            scheduler.schedule_numeric_batch
            if backend == "python_single"
            else scheduler.run_numeric_batch
        )
        scheduler_result = schedule(
            tasks,
            worker,
            policy=policy,
            backend=request_backend,
            full_fit_count=case["candidate_count"],
            pure_python=pure_python,
            numpy_enabled=numpy_enabled,
            independent=True,
            validate_results=True,
            raise_on_error=True,
        )
    if scheduler_result.decision != requested_decision:
        raise HarnessError(
            f"{case['case_id']}/{backend}: scheduler decision mismatch: "
            f"requested={requested_decision!r}, actual={scheduler_result.decision!r}"
        )
    outcomes = []
    for entry in scheduler_result.results:
        if entry.status != "completed":
            raise HarnessError(
                f"{case['case_id']}/{backend}: task {entry.index} status={entry.status}"
            )
        outcomes.append(_decode_worker_result(entry.value))
    if [item["candidate_id"] for item in outcomes] != [
        candidate["candidate_id"] for candidate in case["candidates"]
    ]:
        raise HarnessError(f"{case['case_id']}/{backend}: scheduler ordering was not preserved")
    return outcomes, scheduler_result, requested_decision


def _backend_result(case, backend: str, kernel: str, worker_count: int, warmups: int, repeats: int, tolerance: float, numpy_available: bool, scheduler):
    if kernel == "numpy" and not numpy_available:
        return {
            "backend": backend,
            "kernel": kernel,
            "worker_count": worker_count,
            "status": "unavailable",
            "reason": "NumPy is not importable in this Python runtime",
            "warmup_count": warmups,
            "measured_count": repeats,
            "parity": {"status": "unavailable"},
        }

    warmup_runs = []
    measured_runs = []
    for _index in range(warmups):
        started = time.perf_counter_ns()
        outcomes, scheduler_result, requested_decision = _run_batch(
            case, tolerance, backend, kernel, worker_count, scheduler
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        warmup_runs.append(
            _run_summary(
                outcomes,
                elapsed_ms,
                measured=False,
                scheduler_result=scheduler_result,
                requested_decision=requested_decision,
            )
        )
    for _index in range(repeats):
        started = time.perf_counter_ns()
        outcomes, scheduler_result, requested_decision = _run_batch(
            case, tolerance, backend, kernel, worker_count, scheduler
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        measured_runs.append(
            _run_summary(
                outcomes,
                elapsed_ms,
                measured=True,
                scheduler_result=scheduler_result,
                requested_decision=requested_decision,
            )
        )

    if len(measured_runs) != repeats:
        raise HarnessError(
            f"{case['case_id']}/{backend}: expected {repeats} measured runs, "
            f"got {len(measured_runs)}"
        )
    digests = [item["outcome_digest"] for item in measured_runs]
    return {
        "backend": backend,
        "kernel": kernel,
        "worker_count": worker_count,
        "status": "completed",
        "warmup_count": warmups,
        "measured_count": repeats,
        "warmup": warmup_runs,
        "measured": measured_runs,
        "outcomes": measured_runs[0]["outcomes"],
        "scheduler_decision": measured_runs[0]["scheduler_decision"],
        "scheduler_diagnostics": measured_runs[0]["scheduler_diagnostics"],
        "scheduler_process_alias": {
            "run_process_benchmark_is_run_process_prototype": scheduler.run_process_benchmark
            is scheduler.run_process_prototype
        },
        "deterministic_across_measured_runs": len(set(digests)) == 1,
        "timing_ms": {
            "min": min(item["elapsed_ms"] for item in measured_runs),
            "median": sorted(item["elapsed_ms"] for item in measured_runs)[len(measured_runs) // 2],
            "p95": _percentile([item["elapsed_ms"] for item in measured_runs], 0.95),
        },
    }


def _transform_delta(lhs, rhs) -> float:
    if lhs is None or rhs is None:
        return float("inf") if lhs != rhs else 0.0
    values = []
    for key in ("angle", "scale", "rms"):
        values.append(abs(float(lhs[key]) - float(rhs[key])))
    for key in ("reference_center", "candidate_center"):
        values.extend(
            abs(float(left) - float(right))
            for left, right in zip(lhs[key], rhs[key])
        )
    return max(values, default=0.0)


def _parity_against(canonical, candidate):
    if canonical["candidate_id"] != candidate["candidate_id"]:
        raise HarnessError(
            "Synthetic result order changed: "
            f"{canonical['candidate_id']} != {candidate['candidate_id']}"
        )
    score_delta = abs(float(canonical["score"]) - float(candidate["score"]))
    transform_delta = _transform_delta(canonical.get("transform"), candidate.get("transform"))
    accepted_equal = bool(canonical["accepted"]) == bool(candidate["accepted"])
    reason_equal = canonical["reason"] == candidate["reason"]
    return {
        "candidate_id": candidate["candidate_id"],
        "accepted_equal": accepted_equal,
        "reason_equal": reason_equal,
        "score_abs_delta": score_delta,
        "transform_max_abs_delta": transform_delta,
        "ok": bool(
            accepted_equal
            and reason_equal
            and score_delta <= SCORE_PARITY_TOLERANCE
            and transform_delta <= TRANSFORM_PARITY_TOLERANCE
        ),
    }


def _reconcile_backend(case, result, canonical_outcomes):
    if result["status"] != "completed":
        return {
            "status": result["status"],
            "all_candidates_parity": False,
            "candidate_checks": [],
        }
    outcomes = result["outcomes"]
    if len(outcomes) != len(canonical_outcomes):
        raise HarnessError(
            f"{case['case_id']}/{result['backend']}: outcome count mismatch "
            f"{len(outcomes)} != {len(canonical_outcomes)}"
        )
    candidate_checks = [
        _parity_against(canonical, candidate)
        for canonical, candidate in zip(canonical_outcomes, outcomes)
    ]
    expected_checks = [
        {
            "candidate_id": item["candidate_id"],
            "expected_accept": bool(item["expected_accept"]),
            "observed_accept": bool(item["accepted"]),
            "ok": bool(item["expected_accept"]) == bool(item["accepted"]),
        }
        for item in outcomes
    ]
    result["acceptance_reconciliation"] = {
        "candidate_checks": expected_checks,
        "all_expected_outcomes": all(item["ok"] for item in expected_checks),
    }
    result["parity"] = {
        "status": "completed",
        "reference_backend": "python_single",
        "candidate_checks": candidate_checks,
        "all_candidates_parity": all(item["ok"] for item in candidate_checks),
        "max_score_abs_delta": max(
            (item["score_abs_delta"] for item in candidate_checks),
            default=0.0,
        ),
        "max_transform_abs_delta": max(
            (item["transform_max_abs_delta"] for item in candidate_checks),
            default=0.0,
        ),
    }
    if not result["acceptance_reconciliation"]["all_expected_outcomes"]:
        raise HarnessError(
            f"{case['case_id']}/{result['backend']}: acceptance/rejection mismatch"
        )
    if not result["parity"]["all_candidates_parity"]:
        raise HarnessError(f"{case['case_id']}/{result['backend']}: transform parity mismatch")


def _backend_specs(worker_counts):
    specs = [("python_single", "python", 1), ("numpy_single", "numpy", 1)]
    specs.extend(("numpy_threads", "numpy", count) for count in worker_counts)
    specs.extend(("process_pool", "numpy", count) for count in worker_counts)
    return tuple(specs)


def _numpy_info():
    try:
        import numpy
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "version": numpy.__version__}


def _load_scheduler(project_root):
    scheduler_path = project_root / "uv_gpt" / "match_scheduler.py"
    if not scheduler_path.is_file():
        raise HarnessError(f"C14 scheduler module missing: {scheduler_path}")
    # Load the pure-numeric module directly so a host Python without Blender
    # does not execute uv_gpt/__init__.py just to run the synthetic benchmark.
    spec = importlib.util.spec_from_file_location(
        "match_scheduler_c14_synthetic", scheduler_path
    )
    if spec is None or spec.loader is None:
        raise HarnessError(f"Cannot load C14 scheduler module: {scheduler_path}")
    scheduler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scheduler)
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
    missing = [name for name in required if not hasattr(scheduler, name)]
    if missing:
        raise HarnessError(
            "C14 scheduler public API missing synthetic benchmark symbols: "
            + ", ".join(missing)
        )
    if scheduler.run_process_benchmark is not scheduler.run_process_prototype:
        raise HarnessError("C14 process prototype aliases diverged")
    return scheduler


def _run_case(case, worker_counts, warmups, repeats, tolerance, numpy_available, scheduler):
    backend_results = []
    for backend, kernel, worker_count in _backend_specs(worker_counts):
        result = _backend_result(
            case,
            backend,
            kernel,
            worker_count,
            warmups,
            repeats,
            tolerance,
            numpy_available,
            scheduler,
        )
        backend_results.append(result)

    python_result = next(
        (item for item in backend_results if item["backend"] == "python_single"),
        None,
    )
    if python_result is None or python_result["status"] != "completed":
        raise HarnessError(f"{case['case_id']}: Python single baseline did not complete")
    canonical_outcomes = python_result["outcomes"]
    for result in backend_results:
        _reconcile_backend(case, result, canonical_outcomes)

    payload = _payload_size(case, scheduler)
    return {
        "case_id": case["case_id"],
        "candidate_count": case["candidate_count"],
        "payload_size_points": case["payload_size"],
        "payload_size_bytes": payload,
        "backend_results": backend_results,
    }


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output", default="")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--candidate-sizes",
        default=",".join(str(item) for item in DEFAULT_CANDIDATE_SIZES),
    )
    parser.add_argument(
        "--payload-sizes",
        default=",".join(str(item) for item in DEFAULT_PAYLOAD_SIZES),
    )
    parser.add_argument(
        "--worker-counts",
        default=",".join(str(item) for item in DEFAULT_WORKER_COUNTS),
    )
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    return parser


def run_benchmark(args):
    if args.warmups < 1:
        raise HarnessError(f"warmups must be >= 1: {args.warmups}")
    if args.repeats < 1:
        raise HarnessError(f"repeats must be >= 1: {args.repeats}")
    if args.tolerance <= 0.0:
        raise HarnessError(f"tolerance must be > 0: {args.tolerance}")
    candidate_sizes = _parse_int_list(args.candidate_sizes, "candidate-sizes", minimum=1)
    payload_sizes = _parse_int_list(args.payload_sizes, "payload-sizes", minimum=3)
    worker_counts = _parse_int_list(args.worker_counts, "worker-counts", minimum=1)
    project_root = Path(args.project_root).resolve()
    output_path = (
        Path(args.output).resolve()
        if args.output
        else project_root / "benchmarks" / "match_03_synthetic.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scheduler = _load_scheduler(project_root)
    numpy_info = _numpy_info()
    cases = []
    for candidate_count in candidate_sizes:
        for payload_size in payload_sizes:
            case = _make_case(candidate_count, payload_size, args.seed)
            cases.append(
                _run_case(
                    case,
                    worker_counts,
                    args.warmups,
                    args.repeats,
                    args.tolerance,
                    numpy_info["available"],
                    scheduler,
                )
            )
    completed_backends = sum(
        item["status"] == "completed"
        for case in cases
        for item in case["backend_results"]
    )
    unavailable_backends = sum(
        item["status"] == "unavailable"
        for case in cases
        for item in case["backend_results"]
    )
    result = {
        "packet": PACKET_ID,
        "status": "completed" if unavailable_backends == 0 else "completed_with_unavailable_backends",
        "schema_version": SCHEMA_VERSION,
        "project_root": str(project_root),
        "script": str(Path(__file__).resolve()),
        "command": [sys.executable, *sys.argv],
        "reproducibility": {
            "seed": args.seed,
            "candidate_sizes": list(candidate_sizes),
            "payload_sizes_points": list(payload_sizes),
            "worker_counts": list(worker_counts),
            "warmups": args.warmups,
            "measured_repeats": args.repeats,
            "tolerance": args.tolerance,
        },
        "runtime": {
            "python_version": sys.version,
            "python_executable": sys.executable,
            "logical_cpu_count": os.cpu_count(),
            "parent_pid": os.getpid(),
            "numpy": numpy_info,
        },
        "scheduler_api": {
            "module": str(Path(scheduler.__file__).resolve()),
            "public_symbols": [
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
            "process_alias_same_object": scheduler.run_process_benchmark
            is scheduler.run_process_prototype,
        },
        "backend_matrix": [
            "python_single",
            "numpy_single",
            "numpy_threads",
            "process_pool",
        ],
        "validation": {
            "cases": len(cases),
            "completed_backend_case_runs": completed_backends,
            "unavailable_backend_case_runs": unavailable_backends,
            "all_completed_backends_expected_acceptance": True,
            "all_completed_backends_transform_parity": True,
        },
        "cases": cases,
        "result_path": str(output_path),
    }
    output_path.write_text(
        json.dumps(_clean_json(result), indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return result, output_path


def main() -> int:
    args = _parser().parse_args()
    try:
        result, output_path = run_benchmark(args)
    except Exception as exc:
        project_root = Path(args.project_root).resolve()
        output_path = (
            Path(args.output).resolve()
            if args.output
            else project_root / "benchmarks" / "match_03_synthetic.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        failure = {
            "packet": PACKET_ID,
            "status": "failed",
            "schema_version": SCHEMA_VERSION,
            "script": str(Path(__file__).resolve()),
            "command": [sys.executable, *sys.argv],
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "result_path": str(output_path),
        }
        output_path.write_text(
            json.dumps(_clean_json(failure), indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        print(f"MATCH-03 synthetic status=failed: {failure['error']}")
        print(f"MATCH-03 synthetic result: {output_path}")
        return 1
    print(f"MATCH-03 synthetic status={result['status']}")
    print(f"MATCH-03 synthetic cases={result['validation']['cases']}")
    print(f"MATCH-03 synthetic result: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
