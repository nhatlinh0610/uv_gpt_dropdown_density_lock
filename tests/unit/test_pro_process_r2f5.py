"""MC4-R2F5 contract-first pure/fake oracles.

This packet deliberately stays on immutable topology/payload values and
small lifecycle doubles.  It never starts Blender, an external helper, a
fixture, or a package/runner process.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import math
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def _load_stack_tools_for_pure_helpers():
    """Load stack_tools behind the same tiny Blender boundary as pure tests."""

    module_name = "uv_gpt.stack_tools"
    if module_name in sys.modules:
        return sys.modules[module_name]

    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Operator=type("Operator", (), {}))
    bmesh = types.ModuleType("bmesh")
    mathutils = types.ModuleType("mathutils")

    class Vector(tuple):
        def __new__(cls, value):
            return tuple.__new__(cls, value)

    mathutils.Vector = Vector
    package = types.ModuleType("uv_gpt")
    package.__path__ = [str(ROOT / "uv_gpt")]

    sys.modules.setdefault("bpy", bpy)
    sys.modules.setdefault("bmesh", bmesh)
    sys.modules.setdefault("mathutils", mathutils)
    sys.modules.setdefault("uv_gpt", package)

    for name in ("island_tools", "uv_utils"):
        sys.modules.setdefault(
            f"uv_gpt.{name}", types.ModuleType(f"uv_gpt.{name}")
        )

    for name in ("match_scheduler", "similarity_matcher"):
        full_name = f"uv_gpt.{name}"
        if full_name in sys.modules:
            continue
        path = ROOT / "uv_gpt" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(full_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)

    path = ROOT / "uv_gpt" / "stack_tools.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


STACK_TOOLS = _load_stack_tools_for_pure_helpers()

from uv_gpt import pro_process_payload as PAYLOAD
from uv_gpt import pro_process_pipeline as PIPELINE
from uv_gpt import pro_process_pool as POOL
from uv_gpt import pro_process_worker as WORKER
from uv_gpt import pro_verified_nearest as NEAREST
from uv_gpt import topology_correspondence as TOPOLOGY


def _polygon_graph(points, *, face_key=0):
    points = tuple(tuple(float(value) for value in point) for point in points)
    loop_keys = tuple((face_key, index) for index in range(len(points)))
    loops = tuple(
        TOPOLOGY.LoopRecord(
            key=loop_keys[index],
            face_key=face_key,
            edge_key=index,
            vertex_key=index,
            next_key=loop_keys[(index + 1) % len(points)],
            prev_key=loop_keys[(index - 1) % len(points)],
            uv=points[index],
            boundary=True,
        )
        for index in range(len(points))
    )
    edges = tuple(
        TOPOLOGY.EdgeRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            face_keys=(face_key,),
            boundary=True,
        )
        for index in range(len(points))
    )
    vertices = tuple(
        TOPOLOGY.VertexRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            boundary=True,
        )
        for index in range(len(points))
    )
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(
            TOPOLOGY.BoundaryComponentRecord(
                ("outer", face_key), loop_keys, "outer"
            ),
        ),
    )


def _permuted_graph(graph):
    """Return the same immutable graph with every input record sequence reversed."""

    return TOPOLOGY.make_graph(
        faces=tuple(reversed(graph.faces)),
        edges=tuple(reversed(graph.edges)),
        vertices=tuple(reversed(graph.vertices)),
        loops=tuple(reversed(graph.loops)),
        boundaries=tuple(reversed(graph.boundaries)),
    )


def _identity_transform():
    return TOPOLOGY.SimilarityTransform2D(
        angle=0.0,
        scale=1.0,
        reflected=False,
        source_center=(0.0, 0.0),
        target_center=(0.0, 0.0),
    )


def _pair_task(ordinal=0, options=None):
    options = options or PAYLOAD.ExactOptions(
        allow_flipping=False,
        match_scale=True,
        tolerance=1.0e-6,
        max_search=1024,
    )
    return PAYLOAD.PairTask(
        pair_ordinal=ordinal,
        master_key=(0,),
        member_key=(1,),
        master_graph=PAYLOAD.GraphRef("master", "master-digest"),
        member_graph=PAYLOAD.GraphRef("member", "member-digest"),
        options=options,
    )


def _mapping_signature(result):
    transform = getattr(result, "transform", None)
    transform_wire = None
    if transform is not None:
        transform_wire = (
            round(float(transform.angle), 12),
            round(float(transform.scale), 12),
            bool(transform.reflected),
            tuple(transform.source_center),
            tuple(transform.target_center),
        )
    return (
        bool(result.accepted),
        tuple(sorted(tuple(item) for item in result.loop_mapping)),
        bool(result.reflected),
        bool(result.reversed),
        int(result.cyclic_shift),
        str(result.reason),
        transform_wire,
    )


def _assert_full_verified(testcase, result, master, candidate):
    testcase.assertTrue(result.accepted, result)
    mapping = tuple(result.loop_mapping)
    candidate_keys = {loop.key for loop in candidate.loops}
    master_keys = {loop.key for loop in master.loops}
    testcase.assertEqual({item[0] for item in mapping}, candidate_keys)
    testcase.assertEqual({item[1] for item in mapping}, master_keys)
    testcase.assertEqual(len(mapping), len(candidate_keys))
    testcase.assertEqual(len({item[0] for item in mapping}), len(mapping))
    testcase.assertEqual(len({item[1] for item in mapping}), len(mapping))
    testcase.assertIsNotNone(result.transform)
    diagnostics = result.nearest_diagnostics
    testcase.assertTrue(diagnostics.refinement_stable)
    testcase.assertTrue(diagnostics.topology_verified)
    testcase.assertTrue(diagnostics.geometry_verified)
    testcase.assertGreaterEqual(diagnostics.complete_mappings, 1)


class _Progress:
    """Attribute-tolerant progress value for the session seam."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def __getattr__(self, name):
        if (
            name.endswith("_pids")
            or name.endswith("_states")
            or name.startswith("frame_bytes_")
            or "distribution" in name
        ):
            return ()
        if name.endswith("_reasons") or name.endswith("_batches"):
            return ()
        if name.startswith("process_") and name.endswith("_ms"):
            return 0.0
        if name in {"failure", "retry_failure_reason", "last_progress_kind"}:
            return ""
        if name in {"complete", "cancelled", "failed"}:
            return False
        return 0


class _DonePipeline:
    def __init__(self, progress, *, canonical_ordinals=(0,)):
        self.stage = "shape_wait"
        self._progress = progress
        self.canonical_ordinals = tuple(canonical_ordinals)
        self.has_consumable_result = True
        self.cancel_calls = []

    @property
    def is_terminal(self):
        return self.stage in {"done", "cancelled", "failed"}

    def advance(self, **_kwargs):
        self.stage = "done"
        return self._progress

    def progress(self):
        return self._progress

    def final_result(self):
        return types.SimpleNamespace(
            complete=True,
            result_digest="r2f5-success-digest",
            failure="",
        )

    def cancel(self, *, nonblocking=False, **_kwargs):
        self.cancel_calls.append(bool(nonblocking))
        self.stage = "cancelled"
        return None


class _SliceGuard:
    def __init__(self, *, slices=3, invalid=False):
        self.required_slices = int(slices)
        self.invalid = bool(invalid)
        self.validation_complete = False
        self.validation_slices = 0
        self.validation_operations = 0
        self.validation_elapsed_ms = 0.0
        self.max_validation_slice_ms = 0.0
        self.invalid_reason = ""
        self.requested = False
        self._cheap = True

    def request_validation(self):
        self.requested = True

    def cheap_check(self):
        return self._cheap

    def advance_validation(self, **_kwargs):
        self.validation_slices += 1
        self.validation_operations += 1
        if self.invalid:
            self._cheap = False
            self.invalid_reason = "snapshot_digest_changed"
            return "invalid"
        if self.validation_slices < self.required_slices:
            return "pending"
        self.validation_complete = True
        return "valid"


def _session(**kwargs):
    defaults = {
        "process_worker_count": 2,
        "process_batch_size": 1,
        "process_test_override": True,
        "process_fused": True,
        "modal": True,
    }
    defaults.update(kwargs)
    return STACK_TOOLS._ProAlignSession(
        None,
        None,
        None,
        None,
        selected_islands=[],
        all_islands=[],
        evidence={},
        **defaults,
    )


class _SlowOwnedWorker:
    def __init__(self, *, start_delay=1.5, close_delay=1.5, shutdown_rounds=2):
        self.start_delay = float(start_delay)
        self.close_delay = float(close_delay)
        self.shutdown_rounds = int(shutdown_rounds)
        self.is_ready = False
        self._alive = True
        self.process = object()
        self.pid = None
        self.close_calls = 0
        self.start_calls = 0
        self.shutdown_calls = 0
        self.force_calls = 0

    @property
    def is_alive(self):
        return self._alive

    def start(self):
        self.start_calls += 1
        time.sleep(self.start_delay)
        if self._alive:
            self.is_ready = True

    def close(self, **_kwargs):
        self.close_calls += 1
        time.sleep(self.close_delay)
        self._alive = False
        self.is_ready = False
        self.process = None

    def begin_shutdown(self):
        return "wait"

    def advance_shutdown(self):
        self.shutdown_calls += 1
        if self.shutdown_calls >= self.shutdown_rounds:
            self._alive = False
            self.is_ready = False
            self.process = None
            return "complete"
        return "wait"

    def force_close_nonblocking(self):
        self.force_calls += 1
        self._alive = False
        self.is_ready = False
        self.process = None
        return "complete"


class R2F5VerifiedNearestTests(unittest.TestCase):
    def test_stable_symmetric_equal_distance_mapping_is_fully_verified_and_permutation_stable(self):
        square = ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0))
        # The square intentionally contains repeated equal edge/diagonal
        # distances; the identity seed still permits a complete proof.
        self.assertEqual(
            math.dist(square[0], square[1]),
            math.dist(square[1], square[2]),
        )
        master = _polygon_graph(square)
        candidate = _polygon_graph(square)
        permuted_master = _permuted_graph(master)
        permuted_candidate = _permuted_graph(candidate)
        first = NEAREST.find_verified_nearest(
            master,
            candidate,
            seed_transform=_identity_transform(),
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-6,
            nearest_max_nodes=4096,
        )
        second = NEAREST.find_verified_nearest(
            permuted_master,
            permuted_candidate,
            seed_transform=_identity_transform(),
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-6,
            nearest_max_nodes=4096,
        )
        _assert_full_verified(self, first, master, candidate)
        _assert_full_verified(self, second, permuted_master, permuted_candidate)
        self.assertEqual(_mapping_signature(first), _mapping_signature(second))

    def test_invalid_stable_tie_invokes_exact_solver_once(self):
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        master = _polygon_graph(square)
        candidate = _polygon_graph(square)
        seed = TOPOLOGY.SimilarityTransform2D(
            angle=math.pi / 4.0,
            scale=1.0,
            reflected=False,
            source_center=(0.5, 0.5),
            target_center=(0.5, 0.5),
        )
        options = PAYLOAD.ExactOptions(
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-6,
            max_search=1024,
        )
        original = WORKER._topology.find_correspondence
        calls = []

        def counted(*args, **kwargs):
            calls.append((args, dict(kwargs)))
            return original(*args, **kwargs)

        with mock.patch.object(
            WORKER._topology, "find_correspondence", side_effect=counted
        ):
            exact, nearest = WORKER._verified_exact_correspondence(
                master,
                candidate,
                options,
                seed_transform=seed,
            )
        self.assertFalse(nearest.accepted)
        self.assertEqual(nearest.loop_mapping, ())
        self.assertGreaterEqual(nearest.nearest_diagnostics.tie_count, 1)
        self.assertTrue(nearest.nearest_diagnostics.refinement_stable)
        self.assertTrue(exact.accepted, exact)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["max_search"], options.max_search)

    def test_458_like_accounting_excludes_25_graph_rejections_before_nearest(self):
        progress = _Progress(
            stage="done",
            grouping_comparisons_completed=458,
            grouping_comparisons_planned=458,
            direct_exact_jobs_completed=458,
            direct_exact_jobs_planned=458,
            exact_completed=458,
            exact_total=458,
            merged_pairs=458,
            complete=True,
            nearest_attempted=433,
            nearest_accepted=17,
            nearest_fallback=416,
            nearest_fallback_reasons=((NEAREST.fallback_reason_code("symmetric_nearest_tie"), 416),),
        )
        session = _session()
        session._process_pipeline = _DonePipeline(
            progress, canonical_ordinals=tuple(range(458))
        )
        session._process_graph_rejections = {"invalid_record_partial_face": 25}
        session._process_identity = None
        session._tick_deadline = time.perf_counter() + 0.05

        session._advance_process_pipeline()

        self.assertEqual(session.report["candidate_pairs_processed"], 458)
        self.assertEqual(session.report["direct_exact_jobs_completed"], 458)
        self.assertEqual(session.report["process_graph_rejections"], {
            "invalid_record_partial_face": 25
        })
        self.assertEqual(session.report["process_nearest_attempted"], 433)
        self.assertEqual(session.report["process_nearest_accepted"], 17)
        self.assertEqual(session.report["process_nearest_fallback"], 416)
        # 25 graph rejects never reached nearest.  Only the 416 seeded fast
        # misses invoke the exact solver, and none is a missing-seed fallback.
        self.assertEqual(session.report["process_nearest_fallback_exact_calls"], 416)
        self.assertEqual(session.report["process_nearest_missing_seed_fallbacks"], 0)
        self.assertEqual(session.state, "process_shutdown")

    def test_explicit_true_missing_seed_is_not_counted_as_nearest_attempt(self):
        graph = _polygon_graph(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)))
        options = PAYLOAD.ExactOptions(max_search=1024)
        calls = []
        original = WORKER._topology.find_correspondence

        def counted(*args, **kwargs):
            calls.append((args, dict(kwargs)))
            return original(*args, **kwargs)

        with mock.patch.object(
            WORKER._topology, "find_correspondence", side_effect=counted
        ):
            exact, nearest = WORKER._verified_exact_correspondence(
                graph, graph, options, seed_transform=None
            )
        pair_result = PAYLOAD.PairResult.from_correspondence(
            _pair_task(options=options), exact, nearest_result=nearest
        )
        metrics = dict(pair_result.diagnostics)
        self.assertFalse(nearest.nearest_diagnostics.seed_supplied)
        self.assertEqual(nearest.fallback_reason, "missing_transform")
        self.assertEqual(metrics["nearest_attempted"], 0)
        self.assertEqual(metrics["nearest_accepted"], 0)
        self.assertEqual(metrics["nearest_fallback"], 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(pair_result.accepted)


class R2F5LifecycleTests(unittest.TestCase):
    def test_multislice_snapshot_validation_reaches_success_done_chain(self):
        progress = _Progress(
            stage="done",
            pairs_total=1,
            merged_pairs=1,
            complete=True,
        )
        session = _session()
        session._process_pipeline = _DonePipeline(progress)
        session._process_snapshot_guard = _SliceGuard(slices=3)
        guard = session._process_snapshot_guard
        session._process_identity = None
        session._process_pool = None
        session.state = "process_pipeline"
        states = []

        for _ in range(12):
            states.append(session.state)
            if session.state == "process_pipeline":
                session._advance_process_pipeline()
            elif session.state == "process_shutdown":
                session._advance_process_shutdown()
            elif session.state == "finish":
                session._finalize(apply=True)
            else:
                break
            if session.done:
                states.append(session.state)
                break

        compressed = []
        for state in states:
            if not compressed or compressed[-1] != state:
                compressed.append(state)
        self.assertEqual(
            compressed,
            ["process_pipeline", "process_shutdown", "finish", "done"],
        )
        self.assertGreater(guard.validation_slices, 1)
        self.assertTrue(guard.validation_complete)
        self.assertEqual(session.report["process_validation_status"], "valid")
        self.assertEqual(session.report["process_validation_slices"], 3)
        self.assertEqual(session.report["session_state"], "done")
        self.assertFalse(session.cancelled)
        self.assertIsNone(session.error)

    def test_snapshot_invalidation_discards_staged_writes_without_apply(self):
        progress = _Progress(
            stage="done",
            pairs_total=1,
            merged_pairs=1,
            complete=True,
        )
        session = _session()
        session._process_pipeline = _DonePipeline(progress)
        session._process_snapshot_guard = _SliceGuard(slices=1, invalid=True)
        session._process_identity = None
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.report["aligned_exact"] = 1
        session.state = "process_pipeline"

        with mock.patch.object(
            STACK_TOOLS,
            "_pro_apply_staged_writes",
            side_effect=AssertionError("invalid snapshot must not apply"),
        ):
            session._advance_process_pipeline()
            self.assertIn(session.state, {"process_cancel", "cancelled"})
            if session.state == "process_cancel":
                session._advance_process_cancel()

        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.report["cancel_reason"], "context_invalidated")
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertEqual(session._staged_writes, [])

    def test_slow_close_and_replacement_keep_advances_bounded_and_retain_batch(self):
        pool = POOL.PersistentWorkerPool(1)
        old = _SlowOwnedWorker(start_delay=0.0, close_delay=1.5)
        replacement = _SlowOwnedWorker(start_delay=1.5, close_delay=1.5)
        slot = POOL._Slot(index=0, worker=old)
        pool._slots = [slot]
        pool._make_worker = lambda: replacement
        task = types.SimpleNamespace(
            batch_id="r2f5-slow-batch",
            pair_ordinals=(50,),
            operation_kind="shape",
        )
        active = POOL._ActiveBatch(task, object(), time.perf_counter())

        started = time.perf_counter()
        pool._schedule_restart(
            slot,
            task,
            POOL.WorkerCrashedError("synthetic slow replacement"),
            is_context=False,
        )
        schedule_ms = (time.perf_counter() - started) * 1000.0
        self.assertLessEqual(schedule_ms, 250.0)

        advances = []
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline and pool.restart_pending:
            tick = time.perf_counter()
            pool._advance_pending_restarts(deadline=time.perf_counter() + 0.01)
            advances.append((time.perf_counter() - tick) * 1000.0)
            if not pool.restart_pending:
                break
            time.sleep(0.02)
        self.assertEqual(pool.restart_pending, 0)
        self.assertLessEqual(max(advances or [0.0]), 250.0)
        self.assertIs(pool._ready_batches[0].task, task)
        self.assertEqual(pool._ready_batches[0].task.pair_ordinals, (50,))
        self.assertEqual(pool.retry_batches, (("r2f5-slow-batch", 1),))

        pool._ready_batches.clear()
        pool.begin_shutdown(grace_timeout=1.0)
        shutdown_advances = []
        while not pool.shutdown_complete:
            tick = time.perf_counter()
            pool.advance_shutdown(deadline=time.perf_counter() + 0.01)
            shutdown_advances.append((time.perf_counter() - tick) * 1000.0)
        self.assertLessEqual(max(shutdown_advances or [0.0]), 250.0)
        self.assertEqual(pool._slots, [])
        self.assertTrue(pool.shutdown_complete)
        self.assertGreaterEqual(replacement.shutdown_calls, 1)
        pool.close()

    def test_repeated_failure_exposes_no_staged_result(self):
        session = _session()
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.report["aligned_exact"] = 1

        with mock.patch.object(
            STACK_TOOLS,
            "_pro_apply_staged_writes",
            side_effect=AssertionError("failure path must not apply"),
        ):
            session._fail(RuntimeError("repeated worker failure"))

        self.assertTrue(session.done)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.report["error"], "repeated worker failure")
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertEqual(session.report["aligned_exact"], 0)
        self.assertEqual(session._staged_writes, [])


if __name__ == "__main__":
    unittest.main()
