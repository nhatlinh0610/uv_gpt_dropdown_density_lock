"""Pure contract tests for the explicit verified-nearest/exact-only backend modes.

The mode is part of the immutable pair payload.  These tests deliberately stop
at payload validation, the worker's pure correspondence seam, and the complete
result cache; they never create Blender data, start a helper process, or write
UVs.

The production seam is intentionally resolved with a small amount of API
diagnostic support.  This packet is contract-first while the backend writer is
landing the mode record, so a missing/renamed symbol fails with an actionable
``API mismatch`` assertion rather than silently exercising the legacy
nearest-then-exact fallback.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
import inspect
import sys
import unittest
from unittest import mock

from uv_gpt import pro_process_payload as payload
from uv_gpt import pro_process_worker as worker
from uv_gpt import topology_correspondence as topology


# The direct-file worker intentionally imports ``pro_process_payload`` without
# the package prefix.  Use that same module identity for worker tasks so the
# pure worker's strict SnapshotIdentity checks remain meaningful.
WORKER_PAYLOAD = worker._payload


MODE_NAMES = ("VERIFIED_NEAREST_ONLY", "EXACT_ONLY")


def _resolve_mode(name: str):
    """Resolve the public mode constant while reporting an API mismatch."""

    modules = [payload, worker]
    try:
        modules.insert(0, importlib.import_module("uv_gpt.pro_two_modes_backend"))
    except ModuleNotFoundError:
        pass
    candidates = (name, f"CORRESPONDENCE_MODE_{name}", f"MODE_{name}")
    for module in modules:
        for candidate in candidates:
            if hasattr(module, candidate):
                return getattr(module, candidate)
        for enum_name in (
            "CorrespondenceMode",
            "BackendMode",
            "TwoModeBackendMode",
        ):
            enum = getattr(module, enum_name, None)
            if enum is not None and hasattr(enum, name):
                return getattr(enum, name)
    raise AssertionError(
        "API mismatch: expected immutable mode constant %s (or "
        "CorrespondenceMode.%s) in pro_process_payload/worker/backend"
        % (name, name)
    )


def _mode_options(mode, *, payload_module=payload):
    """Build exact options, including mode when options own that field."""

    options_cls = getattr(payload_module, "ExactOptions", None)
    if options_cls is None:
        raise AssertionError("API mismatch: payload.ExactOptions is missing")
    params = inspect.signature(options_cls).parameters
    mode_name = next(
        (name for name in ("mode", "correspondence_mode", "backend_mode") if name in params),
        None,
    )
    kwargs = dict(
        allow_flipping=False,
        match_scale=True,
        tolerance=1.0e-8,
        max_search=100000,
        cooperative_yield_every=0,
    )
    if mode_name is not None:
        kwargs[mode_name] = mode
    return options_cls(
        **kwargs,
    )


def _option_mode(options):
    for name in ("mode", "correspondence_mode", "backend_mode"):
        if hasattr(options, name):
            return getattr(options, name)
    return None


def _pair_mode(pair):
    for name in ("mode", "correspondence_mode", "backend_mode"):
        if hasattr(pair, name):
            return getattr(pair, name)
    mode = _option_mode(pair.options)
    if mode is not None:
        return mode
    raise AssertionError(
        "API mismatch: PairTask/ExactOptions does not expose its validated mode"
    )


def _polygon_graph(points, *, face_key=0):
    count = len(points)
    loop_keys = tuple((face_key, index) for index in range(count))
    loops = tuple(
        topology.LoopRecord(
            key=loop_keys[index],
            face_key=face_key,
            edge_key=index,
            vertex_key=index,
            next_key=loop_keys[(index + 1) % count],
            prev_key=loop_keys[(index - 1) % count],
            uv=tuple(points[index]),
            boundary=True,
        )
        for index in range(count)
    )
    edges = tuple(
        topology.EdgeRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            face_keys=(face_key,),
            boundary=True,
        )
        for index in range(count)
    )
    vertices = tuple(
        topology.VertexRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            boundary=True,
        )
        for index in range(count)
    )
    return topology.make_graph(
        faces=(topology.FaceRecord(face_key, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(topology.BoundaryComponentRecord("outer", loop_keys, "outer"),),
    )


def _pair(identity, master, member, ordinal, mode, *, payload_module=payload):
    master_data = payload_module.GraphData.from_topology(master, f"master-{ordinal}")
    member_data = payload_module.GraphData.from_topology(member, f"member-{ordinal}")
    options = _mode_options(mode, payload_module=payload_module)
    pair_params = inspect.signature(payload_module.PairTask).parameters
    kwargs = {
        "pair_ordinal": ordinal,
        "master_key": "master",
        "member_key": f"member-{ordinal}",
        "master_graph": payload_module.GraphRef(master_data.graph_key, master_data.content_digest),
        "member_graph": payload_module.GraphRef(member_data.graph_key, member_data.content_digest),
        "options": options,
    }
    for name in ("mode", "correspondence_mode", "backend_mode"):
        if name in pair_params:
            kwargs[name] = mode
            break
    return payload_module.PairTask(**kwargs), (master_data, member_data)


def _identity(label="two-mode-session", *, payload_module=payload):
    return payload_module.SnapshotIdentity(label, 7, "snapshot-digest")


def _triangle_result(*, accepted, reason=""):
    mapping = (((0, 0), (0, 0)), ((0, 1), (0, 1)), ((0, 2), (0, 2)))
    transform = topology.SimilarityTransform2D(
        angle=0.0,
        scale=1.0,
        reflected=False,
        source_center=(0.0, 0.0),
        target_center=(0.0, 0.0),
    )
    return topology.CorrespondenceResult(
        accepted=bool(accepted),
        loop_mapping=mapping if accepted else (),
        score=0.0 if accepted else float("inf"),
        residual=0.0 if accepted else float("inf"),
        reason=reason,
        transform=transform if accepted else None,
    )


def _diagnostics(result):
    return dict(getattr(result, "diagnostics", ()))


def _counter(metrics, *names, default=None):
    for name in names:
        if name in metrics:
            return metrics[name]
    return default


class TwoModePayloadContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fast_mode = _resolve_mode("VERIFIED_NEAREST_ONLY")
        cls.exact_mode = _resolve_mode("EXACT_ONLY")

    def setUp(self):
        self.identity = _identity()
        self.master = _polygon_graph(((0.0, 0.0), (2.0, 0.0), (0.0, 1.0)))
        self.member = _polygon_graph(((0.0, 0.0), (2.0, 0.0), (0.0, 1.0)))

    def test_modes_are_immutable_validated_and_wire_round_trip(self):
        fast_options = _mode_options(self.fast_mode)
        exact_options = _mode_options(self.exact_mode)
        pair, _ = _pair(self.identity, self.master, self.member, 0, self.fast_mode)
        self.assertEqual(_pair_mode(pair), self.fast_mode)
        if _option_mode(fast_options) is not None:
            self.assertEqual(_option_mode(fast_options), self.fast_mode)
            self.assertEqual(_option_mode(exact_options), self.exact_mode)
            self.assertNotEqual(fast_options.to_wire(), exact_options.to_wire())
        decoded = type(fast_options).from_wire(fast_options.to_wire())
        self.assertEqual(decoded, fast_options)
        immutable_owner = fast_options if _option_mode(fast_options) is not None else pair
        immutable_field = next(
            name
            for name in ("mode", "correspondence_mode", "backend_mode")
            if hasattr(immutable_owner, name)
        )
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            setattr(immutable_owner, immutable_field, self.exact_mode)
        with self.assertRaises((ValueError, payload.PayloadValidationError, TypeError)):
            _mode_options("not-a-correspondence-mode")
            _pair(
                self.identity,
                self.master,
                self.member,
                1,
                "not-a-correspondence-mode",
            )

    def test_mode_is_separate_in_pair_wire_payload_digest_and_cache_key(self):
        fast_pair, fast_graphs = _pair(
            self.identity, self.master, self.member, 0, self.fast_mode
        )
        exact_pair, exact_graphs = _pair(
            self.identity, self.master, self.member, 0, self.exact_mode
        )
        self.assertNotEqual(fast_pair.to_wire(), exact_pair.to_wire())
        fast_task = payload.BatchTask(
            self.identity, "mode-separated", (fast_pair,), fast_graphs
        )
        exact_task = payload.BatchTask(
            self.identity, "mode-separated", (exact_pair,), exact_graphs
        )
        self.assertNotEqual(fast_task.payload_digest(), exact_task.payload_digest())
        self.assertNotEqual(fast_task.cache_key(), exact_task.cache_key())
        fast_round_trip = payload.BatchTask.from_wire(fast_task.to_wire())
        exact_round_trip = payload.BatchTask.from_wire(exact_task.to_wire())
        self.assertEqual(_pair_mode(fast_round_trip.pair_tasks[0]), self.fast_mode)
        self.assertEqual(_pair_mode(exact_round_trip.pair_tasks[0]), self.exact_mode)

        exact_result = topology.find_correspondence(
            self.master, self.member, tolerance=1.0e-8
        )
        fast_pair_result = payload.PairResult.from_correspondence(fast_pair, exact_result)
        exact_pair_result = payload.PairResult.from_correspondence(exact_pair, exact_result)
        fast_batch_result = payload.BatchResult(
            self.identity,
            fast_task.batch_id,
            fast_task.payload_digest(),
            (fast_pair_result,),
        )
        exact_batch_result = payload.BatchResult(
            self.identity,
            exact_task.batch_id,
            exact_task.payload_digest(),
            (exact_pair_result,),
        )
        self.assertNotEqual(fast_pair_result.to_wire(), exact_pair_result.to_wire())
        self.assertEqual(
            payload.PairResult.from_wire(fast_pair_result.to_wire()).correspondence_mode,
            self.fast_mode,
        )
        self.assertEqual(
            payload.PairResult.from_wire(exact_pair_result.to_wire()).correspondence_mode,
            self.exact_mode,
        )
        self.assertNotEqual(fast_batch_result.result_digest(), exact_batch_result.result_digest())
        cache = payload.CompleteResultCache()
        cache.put(fast_task, fast_batch_result)
        cache.put(exact_task, exact_batch_result)
        self.assertEqual(len(cache), 2)
        self.assertIs(cache.get(fast_task.cache_key()), fast_batch_result)
        self.assertIs(cache.get(exact_task.cache_key()), exact_batch_result)

        context_digest = payload.graph_context_identity_digest(self.identity)
        master_loop_keys = tuple(item.key for item in self.master.loops)
        member_loop_keys = tuple(item.key for item in self.member.loops)
        resident_fast = payload.ResidentExactPair(
            pair_ordinal=0,
            master_key="master",
            member_key="member-0",
            master_loop_keys=master_loop_keys,
            member_loop_keys=member_loop_keys,
            options=_mode_options(self.fast_mode),
            correspondence_mode=self.fast_mode,
        )
        resident_exact = payload.ResidentExactPair(
            pair_ordinal=0,
            master_key="master",
            member_key="member-0",
            master_loop_keys=master_loop_keys,
            member_loop_keys=member_loop_keys,
            options=_mode_options(self.exact_mode),
            correspondence_mode=self.exact_mode,
        )
        resident_fast_task = payload.ResidentExactBatchTask(
            self.identity,
            context_digest,
            "resident-mode-separated",
            (resident_fast,),
        )
        resident_exact_task = payload.ResidentExactBatchTask(
            self.identity,
            context_digest,
            "resident-mode-separated",
            (resident_exact,),
        )
        self.assertNotEqual(resident_fast_task.to_wire(), resident_exact_task.to_wire())
        self.assertNotEqual(resident_fast_task.payload_digest(), resident_exact_task.payload_digest())
        self.assertNotEqual(resident_fast_task.cache_key(), resident_exact_task.cache_key())
        decoded_resident = payload.ResidentExactBatchTask.from_wire(resident_fast_task.to_wire())
        self.assertEqual(decoded_resident.pair_tasks[0].correspondence_mode, self.fast_mode)

    def test_batch_and_result_ordering_is_canonical_across_wire_and_digest(self):
        pairs = []
        graphs = []
        for ordinal in (3, 1, 2):
            pair, values = _pair(
                self.identity, self.master, self.member, ordinal, self.fast_mode
            )
            pairs.append(pair)
            graphs.extend(values)
        task = payload.BatchTask(self.identity, "ordered", tuple(reversed(pairs)), tuple(reversed(graphs)))
        self.assertEqual(task.pair_ordinals, (1, 2, 3))
        decoded = payload.BatchTask.from_wire(task.to_wire())
        self.assertEqual(decoded.pair_ordinals, (1, 2, 3))
        self.assertEqual(decoded.payload_digest(), task.payload_digest())
        exact = topology.find_correspondence(self.master, self.member, tolerance=1.0e-8)
        results = tuple(
            payload.PairResult.from_correspondence(pair, exact)
            for pair in reversed(task.pair_tasks)
        )
        batch = payload.BatchResult(
            self.identity,
            task.batch_id,
            task.payload_digest(),
            results,
        )
        self.assertEqual(tuple(item.pair_ordinal for item in batch.pair_results), (1, 2, 3))


class TwoModeWorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fast_mode = _resolve_mode("VERIFIED_NEAREST_ONLY")
        cls.exact_mode = _resolve_mode("EXACT_ONLY")

    def setUp(self):
        self.identity = _identity("two-mode-worker", payload_module=WORKER_PAYLOAD)
        self.master = _polygon_graph(((0.0, 0.0), (2.0, 0.0), (0.0, 1.0)))
        self.member = _polygon_graph(((0.0, 0.0), (2.0, 0.0), (0.0, 1.0)))

    def _task(self, mode):
        pair, graphs = _pair(
            self.identity,
            self.master,
            self.member,
            0,
            mode,
            payload_module=WORKER_PAYLOAD,
        )
        return WORKER_PAYLOAD.BatchTask(
            self.identity, f"worker-{mode}", (pair,), graphs
        )

    def _state(self):
        return worker._WorkerState(
            session_nonce=self.identity.session_nonce,
            generation=self.identity.generation,
            ready=True,
        )

    def test_fast_accepted_is_primary_and_never_constructs_or_calls_exact(self):
        task = self._task(self.fast_mode)
        accepted = _triangle_result(accepted=True)
        with mock.patch.object(
            worker._verified_nearest,
            "find_verified_nearest",
            return_value=accepted,
        ) as nearest, mock.patch.object(
            worker._topology,
            "CorrespondenceSearch",
            side_effect=AssertionError("FAST accepted path constructed CorrespondenceSearch"),
        ), mock.patch.object(
            worker._topology,
            "find_correspondence",
            side_effect=AssertionError("FAST accepted path invoked exact solver"),
        ):
            result = worker._compute_exact_batch(self._state(), task)
        nearest.assert_called_once()
        pair_result = result.pair_results[0]
        self.assertTrue(pair_result.accepted)
        self.assertTrue(pair_result.loop_mapping)
        metrics = _diagnostics(pair_result)
        self.assertEqual(metrics.get("nearest_attempted"), 1)
        self.assertEqual(metrics.get("nearest_accepted"), 1)
        self.assertEqual(metrics.get("nearest_fallback"), 0)
        self.assertEqual(metrics.get("nearest_fast_miss"), 0)
        self.assertEqual(metrics.get("exact_fallback_calls"), 0)
        self.assertEqual(_counter(metrics, "exact_primary_calls", "exact_primary", default=0), 0)

    def test_fast_unverified_is_a_stable_skip_without_exact_or_pair_write(self):
        task = self._task(self.fast_mode)
        # ``fast_unverified:fallback_required`` is the compact, stable wire
        # reason for a verified-nearest miss when FAST is selected without an
        # exact fallback.
        unverified = _triangle_result(
            accepted=False,
            reason="fallback_required",
        )
        with mock.patch.object(
            worker._verified_nearest,
            "find_verified_nearest",
            return_value=unverified,
        ) as nearest, mock.patch.object(
            worker._topology,
            "CorrespondenceSearch",
            side_effect=AssertionError("FAST unverified path constructed CorrespondenceSearch"),
        ), mock.patch.object(
            worker._topology,
            "find_correspondence",
            side_effect=AssertionError("FAST unverified path invoked exact solver"),
        ):
            result = worker._compute_exact_batch(self._state(), task)
        nearest.assert_called_once()
        pair_result = result.pair_results[0]
        self.assertFalse(pair_result.accepted)
        self.assertEqual(pair_result.loop_mapping, ())
        self.assertEqual(pair_result.reason, "fast_unverified:fallback_required")
        metrics = _diagnostics(pair_result)
        self.assertEqual(metrics.get("nearest_attempted"), 1)
        self.assertEqual(metrics.get("nearest_accepted"), 0)
        self.assertEqual(metrics.get("nearest_fallback"), 1)
        self.assertEqual(metrics.get("nearest_fast_miss"), 1)
        self.assertEqual(metrics.get("exact_fallback_calls"), 0)
        self.assertEqual(_counter(metrics, "exact_primary_calls", "exact_primary", default=0), 0)
        result.validate_against(task)

    def test_exact_bypasses_verified_nearest_and_invokes_exact_solver_once(self):
        task = self._task(self.exact_mode)
        exact = _triangle_result(accepted=True)
        with mock.patch.object(
            worker._verified_nearest,
            "find_verified_nearest",
            side_effect=AssertionError("EXACT_ONLY path invoked verified-nearest"),
        ), mock.patch.object(
            worker._topology,
            "find_correspondence",
            return_value=exact,
        ) as exact_solver:
            result = worker._compute_exact_batch(self._state(), task)
        exact_solver.assert_called_once()
        pair_result = result.pair_results[0]
        self.assertTrue(pair_result.accepted)
        metrics = _diagnostics(pair_result)
        self.assertEqual(metrics.get("nearest_attempted", 0), 0)
        self.assertEqual(metrics.get("nearest_accepted", 0), 0)
        self.assertEqual(metrics.get("nearest_fallback", 0), 0)
        self.assertEqual(metrics.get("nearest_fast_miss", 0), 0)
        self.assertEqual(metrics.get("nearest_seed_missing", 0), 0)
        self.assertEqual(metrics.get("exact_fallback_calls", 0), 0)
        self.assertEqual(_counter(metrics, "exact_primary_calls", "exact_primary"), 1)
        result.validate_against(task)


if __name__ == "__main__":
    unittest.main()
