from __future__ import annotations

from dataclasses import dataclass
import time
import types
import unittest
from unittest import mock

from uv_gpt import pro_process_adapter as adapter
from uv_gpt import pro_process_payload as payload
from uv_gpt import pro_process_pipeline as pipeline_module
from uv_gpt import pro_process_pool as pool_module
from uv_gpt import pro_process_worker as worker_module

import test_pro_process_snapshot_guard as snapshot_fixture


# The direct-file worker intentionally imports the sibling payload module by
# name.  Use the same module instance for direct worker proofs; wire/runtime
# behavior is unchanged and the bundled process test decodes the same bytes.
_WORKER_PAYLOAD_GLOBALS = worker_module.GraphBuildTask.__init__.__globals__
DIRECT_PAYLOAD = types.SimpleNamespace(
    SnapshotIdentity=_WORKER_PAYLOAD_GLOBALS["SnapshotIdentity"],
    GraphBuildItem=_WORKER_PAYLOAD_GLOBALS["GraphBuildItem"],
    GraphBuildTask=worker_module.GraphBuildTask,
    GraphBuildResult=worker_module.GraphBuildResult,
    stable_digest=_WORKER_PAYLOAD_GLOBALS["stable_digest"],
)


ROOT = snapshot_fixture.ROOT
BLENDER_PATH = (
    ROOT / ".test_runtime" / "blender-5.0.0" /
    "blender-5.0.0-windows-x64" / "blender.exe"
)
PYTHON_PATH = (
    ROOT / ".test_runtime" / "blender-5.0.0" /
    "blender-5.0.0-windows-x64" / "5.0" / "python" / "bin" / "python.exe"
)
WORKER_PATH = ROOT / "uv_gpt" / "pro_process_worker.py"


def _capture_and_material():
    capture_builder = snapshot_fixture._build(
        *snapshot_fixture._fixture(1), budget=16
    )
    capture = capture_builder.result
    material = snapshot_fixture.ADAPTER.graph_material_wire_for_island(
        capture, (0,)
    )
    return capture, material


def _graph_task(*, nonce="graph-test", generation=4, batch_id="graph-1", material=None):
    capture, default_material = _capture_and_material()
    del capture
    if material is None:
        material = default_material
    identity = DIRECT_PAYLOAD.SnapshotIdentity(
        nonce, generation, "graph-test-snapshot"
    )
    item = DIRECT_PAYLOAD.GraphBuildItem(
        island_key=(0,),
        material_digest=DIRECT_PAYLOAD.stable_digest(material),
        material=material,
    )
    return DIRECT_PAYLOAD.GraphBuildTask(identity, batch_id, (item,))


def _package_capture(source_capture):
    context_wire = snapshot_fixture.ADAPTER.graph_context_wire_for_capture(
        source_capture
    )
    return adapter.SnapshotCapture(
        identity=payload.SnapshotIdentity(
            source_capture.identity.session_nonce,
            source_capture.identity.generation,
            source_capture.identity.snapshot_digest,
        ),
        canonical=("resident-exact-test",),
        material=adapter.graph_context_material_from_wire(context_wire),
    )


def _resident_task(capture, *, batch_id="resident-exact-1", ordinals=(0, 1)):
    context = adapter.make_graph_context_payload(capture)
    island_keys = tuple(
        tuple(int(value) for value in face_keys)
        for face_keys, _loop_keys in capture.material.island_face_keys
    )
    pairs = []
    for ordinal, island_key in zip(ordinals, island_keys):
        loop_keys = adapter.graph_loop_keys_for_island(capture, island_key)
        pairs.append(
            (
                ordinal,
                island_key,
                island_key,
                loop_keys,
                loop_keys,
                snapshot_fixture._options(),
            )
        )
    task = adapter.make_resident_exact_batch(
        context.identity,
        context.context_digest,
        pairs,
        batch_id=batch_id,
    )
    return context, task


class GraphSchemaTests(unittest.TestCase):
    def test_resident_exact_wire_is_tiny_and_has_no_graph_payload(self):
        capture = _package_capture(
            snapshot_fixture._build(*snapshot_fixture._fixture(2), budget=16).result
        )
        context, task = _resident_task(capture)
        restored = payload.ResidentExactBatchTask.from_wire(task.to_wire())
        self.assertEqual(restored.payload_digest(), task.payload_digest())
        self.assertEqual(restored.context_digest, context.context_digest)
        self.assertNotIn("graphs", restored.to_wire())
        self.assertLess(restored.estimate_frame().frame_bytes, 10000)

    def test_context_digest_reuses_snapshot_identity_without_material_rehash(self):
        capture_builder = snapshot_fixture._build(
            *snapshot_fixture._fixture(1), budget=16
        )
        source_capture = capture_builder.result
        context_wire = snapshot_fixture.ADAPTER.graph_context_wire_for_capture(
            source_capture
        )
        capture = adapter.SnapshotCapture(
            identity=payload.SnapshotIdentity(
                source_capture.identity.session_nonce,
                source_capture.identity.generation,
                source_capture.identity.snapshot_digest,
            ),
            canonical=("identity-context-test",),
            material=adapter.graph_context_material_from_wire(context_wire),
        )
        with mock.patch.object(
            payload,
            "graph_context_material_digest",
            side_effect=AssertionError("main context construction rehashed material"),
        ):
            context = adapter.make_graph_context_payload(capture)
        self.assertEqual(
            context.context_digest,
            payload.graph_context_identity_digest(context.identity),
        )
        changed = payload.SnapshotIdentity(
            context.identity.session_nonce,
            context.identity.generation + 1,
            context.identity.snapshot_digest,
        )
        self.assertNotEqual(
            context.context_digest,
            payload.graph_context_identity_digest(changed),
        )

    def test_graph_task_result_wire_digest_and_complete_only_cache(self):
        task = _graph_task()
        wire = task.to_wire()
        restored = DIRECT_PAYLOAD.GraphBuildTask.from_wire(wire)
        self.assertEqual(task.payload_digest(), restored.payload_digest())
        state = worker_module._WorkerState(
            session_nonce=task.identity.session_nonce,
            generation=task.identity.generation,
            ready=True,
        )
        result = worker_module._compute_graph_batch(state, task)
        result.validate_against(task)
        result_wire = DIRECT_PAYLOAD.GraphBuildResult.from_wire(result.to_wire())
        result_wire.validate_against(task)
        self.assertTrue(result_wire.graph_results[0].accepted)
        self.assertEqual(result_wire.result_digest(), result.result_digest())

        second = worker_module._compute_graph_batch(
            state, DIRECT_PAYLOAD.GraphBuildTask(task.identity, "graph-2", task.graph_items)
        )
        self.assertEqual(second.cache_hits, 1)

    def test_partial_face_is_a_legacy_rejection_not_an_accepted_graph(self):
        _capture, material = _capture_and_material()
        value = list(material)
        loops = tuple(material[5])
        value[7] = (((0,), (loops[0][0],)),)
        partial = tuple(value)
        task = _graph_task(material=partial, batch_id="partial-face")
        state = worker_module._WorkerState(
            session_nonce=task.identity.session_nonce,
            generation=task.identity.generation,
            ready=True,
        )
        result = worker_module._compute_graph_batch(state, task)
        self.assertFalse(result.graph_results[0].accepted)
        self.assertEqual(result.graph_results[0].reason, "invalid_record_partial_face")

    def test_graph_task_is_order_stable_and_frame_is_bounded(self):
        task = _graph_task()
        reversed_item = tuple(reversed(task.graph_items))
        reordered = DIRECT_PAYLOAD.GraphBuildTask(
            task.identity, "graph-reordered", reversed_item
        )
        self.assertEqual(task.graph_items, reordered.graph_items)
        self.assertGreater(task.estimate_frame().frame_bytes, 0)


class _ShapeResult:
    def __init__(self, ordinal):
        self.pair_ordinal = ordinal
        self.accepted = True

    def to_wire(self):
        return (self.pair_ordinal, True)


class _ExactResult:
    def __init__(self, ordinal):
        self.pair_ordinal = ordinal
        self.accepted = True

    def to_wire(self):
        return (self.pair_ordinal, True, "exact")


@dataclass(frozen=True)
class _Task:
    batch_id: str
    pair_ordinals: tuple[int, ...]
    operation_kind: str

    @property
    def pair_tasks(self):
        return self.pair_ordinals

    @property
    def item_count(self):
        return 1

    def validate(self):
        if self.operation_kind != "graph" and not self.pair_ordinals:
            raise ValueError("empty pair task")

    def estimate_frame(self):
        return type("Estimate", (), {"frame_bytes": 128})()


@dataclass(frozen=True)
class _Completion:
    task: _Task
    result: object
    worker_index: int
    batch_id: str
    sequence: int = 1


class _MixedPool:
    worker_count = 1

    def __init__(self):
        self.pending = []
        self.closed = False
        self.terminal = False
        self.submitted = []

    @property
    def is_terminal(self):
        return self.terminal

    @property
    def stream_capacity(self):
        return max(0, 2 - len(self.pending))

    @property
    def stream_queue_depth(self):
        return len(self.pending)

    @property
    def queue_depth(self):
        return len(self.pending)

    @property
    def worker_pids(self):
        return (9001,)

    @property
    def worker_task_distribution(self):
        return ((0, len(self.submitted)),)

    @property
    def startup_timings_ms(self):
        return (0.1,)

    def begin_stream(self):
        return None

    def stream_submit(self, task):
        self.pending.append(task)
        self.submitted.append(task)
        return (task.batch_id,)

    def stream_finish(self):
        self.closed = True
        if not self.pending:
            self.terminal = True

    def poll_stream(self, _timeout=0.0):
        if not self.pending:
            if self.closed:
                self.terminal = True
            return ()
        task = self.pending.pop(0)
        if task.operation_kind == "shape":
            result = type("Result", (), {
                "pair_results": (_ShapeResult(task.pair_ordinals[0]),)
            })()
        elif task.operation_kind == "graph":
            result = type("GraphResult", (), {"pair_results": ()})()
        else:
            result = type("Result", (), {
                "pair_results": (_ExactResult(task.pair_ordinals[0]),)
            })()
        return (_Completion(task, result, 0, task.batch_id),)

    def progress(self):
        return pool_module.PoolProgress(
            pairs_total=0,
            pairs_done=0,
            exact_count=0,
            batches_total=len(self.submitted),
            batches_done=0,
            active_workers=bool(self.pending),
            retry_count=0,
            elapsed_ms=0.0,
        )

    def cancel(self, timeout=1.0):
        del timeout
        self.pending.clear()
        self.terminal = True

    def invalidate_generation(self, generation):
        del generation
        self.cancel()

    def close(self):
        self.cancel()


class _BusyGraphPool(_MixedPool):
    def __init__(self):
        super().__init__()
        self.fail_graph_once = True

    def stream_submit(self, task):
        if task.operation_kind == "graph" and self.fail_graph_once:
            self.fail_graph_once = False
            raise pool_module.PoolStreamBusyError("synthetic graph admission busy")
        return super().stream_submit(task)


class MixedFrontierTests(unittest.TestCase):
    def test_resident_exact_completion_merges_canonical_ordinal_immediately(self):
        capture = _package_capture(
            snapshot_fixture._build(*snapshot_fixture._fixture(2), budget=16).result
        )
        context, task_by_ordinal = {}, {}
        for ordinal in (0, 1):
            context_payload = adapter.make_graph_context_payload(capture)
            island_key = tuple(
                tuple(int(value) for value in face_keys)
                for face_keys, _loop_keys in capture.material.island_face_keys
            )[ordinal]
            loop_keys = adapter.graph_loop_keys_for_island(capture, island_key)
            task_by_ordinal[ordinal] = adapter.make_resident_exact_batch(
                context_payload.identity,
                context_payload.context_digest,
                ((ordinal, island_key, island_key, loop_keys, loop_keys, snapshot_fixture._options()),),
                batch_id="resident-pipeline-%d" % ordinal,
            )

        class _ResidentPool(_MixedPool):
            @property
            def stream_capacity(self):
                return max(0, 1 - len(self.pending))

            def poll_stream(self, _timeout=0.0):
                if not self.pending:
                    if self.closed:
                        self.terminal = True
                    return ()
                task = self.pending.pop(0)
                if task.operation_kind == "shape":
                    result = type("Result", (), {
                        "pair_results": (_ShapeResult(task.pair_ordinals[0]),)
                    })()
                else:
                    resident = task_by_ordinal[task.pair_ordinals[0]]
                    pair = resident.pair_tasks[0]
                    loop_key = pair.member_loop_keys[0]
                    pair_result = payload.PairResult(
                        pair_ordinal=pair.pair_ordinal,
                        master_key=pair.master_key,
                        member_key=pair.member_key,
                        master_graph_digest="master-%d" % pair.pair_ordinal,
                        member_graph_digest="member-%d" % pair.pair_ordinal,
                        accepted=True,
                        loop_mapping=((loop_key, pair.master_loop_keys[0]),),
                        score=0.0,
                        residual=0.0,
                        transform=payload.TransformData(
                            0.0, 1.0, False, (0.0, 0.0), (0.0, 0.0)
                        ),
                        complete=True,
                    )
                    result = payload.ResidentExactBatchResult(
                        identity=resident.identity,
                        context_digest=resident.context_digest,
                        batch_id=resident.batch_id,
                        payload_digest=resident.payload_digest(),
                        pair_results=(pair_result,),
                        graph_cache_builds=2,
                        graph_cache_hits=0,
                    )
                return (_Completion(task, result, 0, task.batch_id),)

        pool = _ResidentPool()
        frontier = pipeline_module.FrontierProcessPipeline(
            pool,
            (0, 1),
            domain_for_ordinal=lambda ordinal: "member-%d" % ordinal,
            shape_builder=lambda values: _Task(
                "resident-shape-%d" % tuple(values)[0], tuple(values), "shape"
            ),
            exact_builder=lambda shape_result: task_by_ordinal[shape_result.pair_ordinal],
            batch_size=1,
            merge_limit=1,
        )
        frontier.start()
        for _ in range(30):
            frontier.advance()
            if frontier.is_terminal:
                break
        result = frontier.final_result()
        self.assertTrue(result.complete)
        self.assertEqual(tuple(item.pair_ordinal for item in result.outcomes), (0, 1))
        self.assertEqual(result.progress.merged_pairs, 2)
        self.assertEqual(result.progress.resident_exact_batches_submitted, 2)
        self.assertEqual(result.progress.resident_exact_batches_completed, 2)
        self.assertTrue(result.progress.exact_started_before_shape_terminal)

    def test_resident_graph_two_completions_restore_canonical_merge(self):
        class _WidePool(_MixedPool):
            def __init__(self):
                super().__init__()
                self.graph_submitted_count = 0
                self.release_later = False

            @property
            def stream_capacity(self):
                if self.release_later:
                    return max(0, 8 - len(self.pending))
                if any(task.operation_kind == "shape" for task in self.pending):
                    return 0
                if self.graph_submitted_count >= 2:
                    return 0
                if not self.pending:
                    return 2
                return max(0, 2 - len(self.pending))

            def stream_submit(self, task):
                result = super().stream_submit(task)
                if task.operation_kind == "graph":
                    self.graph_submitted_count += 1
                return result

        pool = _WidePool()
        graph_done = []
        graph_ready = {"value": False}

        def shape_builder(values):
            ordinal = tuple(values)[0]
            return _Task("shape-%d" % ordinal, tuple(values), "shape")

        def exact_builder(shape_result):
            if shape_result.pair_ordinal == 0 and not graph_ready["value"]:
                return (
                    _Task("graph-0-master", (), "graph"),
                    _Task("graph-0-member", (), "graph"),
                )
            return _Task(
                "exact-%d" % shape_result.pair_ordinal,
                (shape_result.pair_ordinal,),
                "exact",
            )

        def graph_callback(_task, _result):
            graph_done.append(True)
            graph_ready["value"] = len(graph_done) >= 2
            if graph_ready["value"]:
                pool.release_later = True

        frontier = pipeline_module.FrontierProcessPipeline(
            pool,
            (0, 1),
            domain_for_ordinal=lambda ordinal: "member-%d" % ordinal,
            shape_builder=shape_builder,
            exact_builder=exact_builder,
            graph_result_callback=graph_callback,
            batch_size=1,
            merge_limit=1,
        )
        frontier.start()
        for _ in range(40):
            frontier.advance()
            if frontier.is_terminal:
                break
        result = frontier.final_result()
        self.assertTrue(result.complete)
        self.assertEqual(tuple(item.pair_ordinal for item in result.outcomes), (0, 1))
        self.assertEqual(result.progress.merged_pairs, 2)
        self.assertEqual(result.progress.graph_tasks_completed, 2)
        self.assertEqual(result.progress.exact_completed, 2)
        self.assertTrue(result.progress.exact_started_before_shape_terminal)
        self.assertEqual(len(graph_done), 2)
        self.assertEqual(result.progress.graph_waiter_registrations, 1)

    def test_graph_waiter_epoch_deduplicates_no_event_retries(self):
        class _NoEventPool(_MixedPool):
            @property
            def stream_capacity(self):
                return max(0, 8 - len(self.pending))

        pool = _NoEventPool()
        exact_builder_calls = []

        def exact_builder(_shape_result):
            exact_builder_calls.append(True)
            return None

        frontier = pipeline_module.FrontierProcessPipeline(
            pool,
            (0,),
            domain_for_ordinal=lambda _ordinal: "member",
            shape_builder=lambda values: _Task("shape-wait", tuple(values), "shape"),
            exact_builder=exact_builder,
            merge_limit=1,
        )
        frontier.start()
        frontier.advance()
        for _ in range(5):
            frontier.advance()
        progress = frontier.progress()
        self.assertEqual(len(exact_builder_calls), 1)
        self.assertGreaterEqual(progress.graph_waiter_dedup, 5)
        self.assertGreaterEqual(progress.no_progress_loops, 5)

    def test_graph_callback_then_exact_keeps_canonical_visibility(self):
        pool = _MixedPool()
        graph_done = []
        admitted = []
        phase = {"graph": True}

        def shape_builder(values):
            return _Task("shape-1", tuple(values), "shape")

        def exact_builder(_shape_result):
            if phase["graph"]:
                return _Task("graph-1", (), "graph")
            return _Task("exact-1", (0,), "exact")

        def graph_callback(_task, _result):
            graph_done.append(True)
            phase["graph"] = False

        def admitted_callback(task):
            admitted.append(task.batch_id)

        frontier = pipeline_module.FrontierProcessPipeline(
            pool,
            (0,),
            domain_for_ordinal=lambda _ordinal: "member",
            shape_builder=shape_builder,
            exact_builder=exact_builder,
            graph_result_callback=graph_callback,
            graph_task_admitted_callback=admitted_callback,
            merge_limit=1,
        )
        frontier.start()
        for _ in range(20):
            frontier.advance()
            if frontier.is_terminal:
                break
        result = frontier.final_result()
        self.assertTrue(result.complete)
        self.assertEqual(graph_done, [True])
        self.assertEqual(admitted, ["graph-1"])
        self.assertEqual(tuple(item.pair_ordinal for item in result.outcomes), (0,))

    def test_graph_admission_busy_does_not_leave_stale_pending_state(self):
        pool = _BusyGraphPool()
        admitted = []
        graph_phase = {"pending": True}

        def exact_builder(_shape_result):
            if graph_phase["pending"]:
                return _Task("graph-retry", (), "graph")
            return _Task("exact-after-graph", (0,), "exact")

        def on_admitted(_task):
            admitted.append(True)
            graph_phase["pending"] = False

        frontier = pipeline_module.FrontierProcessPipeline(
            pool,
            (0,),
            domain_for_ordinal=lambda _ordinal: "member",
            shape_builder=lambda values: _Task("shape-busy", tuple(values), "shape"),
            exact_builder=exact_builder,
            graph_task_admitted_callback=on_admitted,
            merge_limit=1,
        )
        frontier.start()
        frontier.advance()
        # The same bounded tick may retry the builder after the first busy
        # admission; the callback must still fire exactly once, after the
        # successful submission.
        self.assertEqual(admitted, [True])
        self.assertFalse(frontier.is_terminal)
        for _ in range(20):
            frontier.advance()
            if frontier.is_terminal:
                break
        self.assertTrue(frontier.final_result().complete)
        self.assertEqual(admitted, [True])


class RealBundledGraphTests(unittest.TestCase):
    def test_bundled_resident_exact_reuses_graph_cache_and_preserves_mapping(self):
        capture = _package_capture(
            snapshot_fixture._build(*snapshot_fixture._fixture(2), budget=16).result
        )
        context, task = _resident_task(capture, batch_id="resident-exact-1")
        pool = pool_module.PersistentWorkerPool(
            1,
            worker_script=WORKER_PATH,
            blender_binary=BLENDER_PATH,
            blender_version="5.0",
            session_nonce=context.identity.session_nonce,
            generation=context.identity.generation,
            handshake_timeout=8.0,
            io_timeout=8.0,
            use_cache=False,
        )
        owned = ()
        try:
            pool.start()
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and not pool.graph_context_ready:
                pool.load_graph_context(context)
                time.sleep(0.005)
            self.assertTrue(pool.graph_context_ready)
            pool.begin_stream()
            pool.stream_submit(task)
            completions = ()
            while time.monotonic() < deadline and not completions:
                completions = pool.poll_stream(0.05)
            self.assertEqual(len(completions), 1)
            first = completions[0].result
            first.validate_against(task)
            self.assertEqual(len(first.pair_results), 2)
            self.assertTrue(all(item.accepted for item in first.pair_results))
            self.assertEqual(first.graph_cache_builds, 2)
            self.assertEqual(first.graph_cache_hits, 0)
            cached_task = adapter.make_resident_exact_batch(
                context.identity,
                context.context_digest,
                tuple(
                    (
                        pair.pair_ordinal,
                        pair.master_key,
                        pair.member_key,
                        pair.master_loop_keys,
                        pair.member_loop_keys,
                        pair.options,
                    )
                    for pair in task.pair_tasks
                ),
                batch_id="resident-exact-cache",
            )
            pool.stream_submit(cached_task)
            cached_completions = ()
            while time.monotonic() < deadline and not cached_completions:
                cached_completions = pool.poll_stream(0.05)
            self.assertEqual(len(cached_completions), 1)
            cached = cached_completions[0].result
            cached.validate_against(cached_task)
            self.assertGreaterEqual(cached.graph_cache_hits, 2)
            self.assertEqual(
                tuple(item.loop_mapping for item in first.pair_results),
                tuple(item.loop_mapping for item in cached.pair_results),
            )
            self.assertLess(task.estimate_frame().frame_bytes, 10000)
            owned = pool.worker_pids
        finally:
            pool.cancel(timeout=1.0)
        self.assertTrue(all(pid not in pool.worker_pids for pid in owned))

    def test_resident_context_load_tiny_graph_and_restart_reload(self):
        capture_builder = snapshot_fixture._build(
            *snapshot_fixture._fixture(1), budget=16
        )
        source_capture = capture_builder.result
        # The snapshot fixture is intentionally loaded as a direct module for
        # Blender-free tests.  Rehydrate its primitive wire into the package
        # adapter class identity used by the real pool boundary.
        source_context_wire = snapshot_fixture.ADAPTER.graph_context_wire_for_capture(
            source_capture
        )
        capture = adapter.SnapshotCapture(
            identity=payload.SnapshotIdentity(
                source_capture.identity.session_nonce,
                source_capture.identity.generation,
                source_capture.identity.snapshot_digest,
            ),
            canonical=("resident-context-test",),
            material=adapter.graph_context_material_from_wire(source_context_wire),
        )
        context = adapter.make_graph_context_payload(capture)
        restored_context = payload.GraphContextPayload.from_wire(context.to_wire())
        self.assertEqual(restored_context.context_digest, context.context_digest)
        task = adapter.make_graph_build_context_task(
            context.identity,
            context.context_digest,
            ((0,),),
            batch_id="resident-graph-1",
        )
        pool = pool_module.PersistentWorkerPool(
            1,
            worker_script=WORKER_PATH,
            blender_binary=BLENDER_PATH,
            blender_version="5.0",
            session_nonce=context.identity.session_nonce,
            generation=context.identity.generation,
            handshake_timeout=8.0,
            io_timeout=8.0,
            use_cache=False,
        )
        owned = ()
        try:
            pool.start()
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and not pool.graph_context_ready:
                pool.load_graph_context(context)
                time.sleep(0.005)
            self.assertTrue(pool.graph_context_ready)
            self.assertEqual(pool.context_load_submitted, 1)
            self.assertEqual(pool.context_load_acked, 1)
            pool.begin_stream()
            pool.stream_submit(task)
            completions = ()
            while time.monotonic() < deadline and not completions:
                completions = pool.poll_stream(0.05)
            self.assertEqual(len(completions), 1)
            first = completions[0].result
            first.validate_against(task)
            self.assertTrue(first.graph_results[0].accepted)
            cached_task = adapter.make_graph_build_context_task(
                context.identity,
                context.context_digest,
                ((0,),),
                batch_id="resident-graph-cache",
            )
            pool.stream_submit(cached_task)
            cached_completions = ()
            while time.monotonic() < deadline and not cached_completions:
                cached_completions = pool.poll_stream(0.05)
            self.assertEqual(len(cached_completions), 1)
            cached = cached_completions[0].result
            cached.validate_against(cached_task)
            self.assertGreaterEqual(cached.cache_hits, 1)
            owned = pool.worker_pids

            # A restarted slot must lose its resident context and reload it
            # before accepting the same tiny island request.
            slot = pool._slots[0]
            old_pid = slot.worker.pid
            pool._restart_slot(slot)
            self.assertNotEqual(slot.worker.pid, old_pid)
            self.assertFalse(pool.graph_context_ready)
            reload_deadline = time.monotonic() + 15.0
            while time.monotonic() < reload_deadline and not pool.graph_context_ready:
                pool.load_graph_context(context)
                time.sleep(0.005)
            self.assertTrue(pool.graph_context_ready)
            task2 = adapter.make_graph_build_context_task(
                context.identity,
                context.context_digest,
                ((0,),),
                batch_id="resident-graph-2",
            )
            pool.stream_submit(task2)
            completions = ()
            while time.monotonic() < reload_deadline and not completions:
                completions = pool.poll_stream(0.05)
            self.assertEqual(len(completions), 1)
            second = completions[0].result
            second.validate_against(task2)
            self.assertTrue(second.graph_results[0].accepted)
            self.assertGreaterEqual(pool.context_load_submitted, 2)
            self.assertGreaterEqual(pool.context_load_acked, 2)
        finally:
            pool.cancel(timeout=1.0)
        self.assertTrue(all(pid not in pool.worker_pids for pid in owned))

    def test_bundled_worker_graph_task_roundtrip_and_pid_cleanup(self):
        self.assertTrue(BLENDER_PATH.is_file())
        self.assertTrue(PYTHON_PATH.is_file())
        task = _graph_task(nonce="bundled-graph", generation=2)
        pool = pool_module.PersistentWorkerPool(
            1,
            worker_script=WORKER_PATH,
            blender_binary=BLENDER_PATH,
            blender_version="5.0",
            session_nonce=task.identity.session_nonce,
            generation=task.identity.generation,
            handshake_timeout=8.0,
            io_timeout=8.0,
            use_cache=False,
        )
        owned = ()
        try:
            pool.begin_stream()
            pool.stream_submit(task)
            owned = pool.worker_pids
            self.assertEqual(len(owned), 1)
            deadline = time.monotonic() + 15.0
            completions = ()
            while time.monotonic() < deadline and not completions:
                completions = pool.poll_stream(0.05)
            self.assertEqual(len(completions), 1)
            result = completions[0].result
            result.validate_against(task)
            self.assertTrue(result.graph_results[0].accepted)
            self.assertEqual(result.graph_results[0].graph.graph_key, "island-" + DIRECT_PAYLOAD.stable_digest(("graph-key", (0,)))[:32])
        finally:
            pool.cancel(timeout=1.0)
        self.assertTrue(all(pid not in pool.worker_pids for pid in owned))


if __name__ == "__main__":
    unittest.main()
